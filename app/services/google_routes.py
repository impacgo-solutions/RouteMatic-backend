"""Google Routes API client for real road distances and route geometry.

Three calls are offered:

* ``compute_route_matrix``: distance/duration for every origin/destination
  pair in ONE request (used to plan assignments and pickup order);
* ``compute_route_legs``: road geometry for a whole multi-stop vehicle route in
  ONE request (one call per vehicle instead of one per leg);
* ``compute_route``: a single origin/destination route (kept for callers).

Failures raise :class:`GoogleRoutesError` with a machine-readable ``kind``
(``rate_limited``, ``permission_or_billing``, ``timeout``, ...). Nothing here
ever substitutes straight-line estimates: that is the caller's labelled choice.
The API key is never logged.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import httpx

from app.config import get_settings

log = logging.getLogger("routematic.routes")

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
FIELD_MASK = (
    "routes.distanceMeters,"
    "routes.duration,"
    "routes.polyline.encodedPolyline"
)
LEGS_FIELD_MASK = (
    "routes.legs.distanceMeters,"
    "routes.legs.duration,"
    "routes.legs.staticDuration,"
    "routes.legs.polyline.encodedPolyline"
)
MATRIX_FIELD_MASK = (
    "originIndex,destinationIndex,status,condition,"
    "distanceMeters,duration,staticDuration"
)
# Google's per-request element limit for TRAFFIC_AWARE route matrices.
MATRIX_MAX_ELEMENTS = 625
TIMEOUT_S = 20.0
RETRY_STATUSES = {429, 500, 502, 503, 504}
CACHE_TTL_S = 300.0  # traffic changes: cache only briefly


class GoogleRoutesError(RuntimeError):
    """Safe error for failed Google Routes API requests."""

    def __init__(self, message: str, kind: str = "error", status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


_KIND_BY_STATUS = {
    400: "invalid_request",
    401: "permission_or_billing",
    403: "permission_or_billing",
    404: "invalid_request",
    429: "rate_limited",
}


class _TTLCache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._data: dict = {}
        self._lock = threading.Lock()
        self.hits = 0

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if item and time.monotonic() - item[0] < self.ttl:
                self.hits += 1
                return item[1]
            self._data.pop(key, None)
            return None

    def put(self, key, value):
        with self._lock:
            self._data[key] = (time.monotonic(), value)

    def clear(self):
        with self._lock:
            self._data.clear()
            self.hits = 0


_cache = _TTLCache(CACHE_TTL_S)


def clear_cache() -> None:
    _cache.clear()


def _api_key() -> str:
    api_key = get_settings().google_maps_api_key.strip()
    if not api_key:
        raise GoogleRoutesError(
            "GOOGLE_MAPS_API_KEY is not configured in backend/.env",
            kind="not_configured",
        )
    return api_key


def _key(point: dict) -> tuple[float, float]:
    return (round(float(point["lat"]), 6), round(float(point["lng"]), 6))


def _waypoint(point: dict) -> dict:
    return {
        "location": {
            "latLng": {"latitude": point["lat"], "longitude": point["lng"]}
        }
    }


def _seconds(value) -> float:
    if not isinstance(value, str) or not value.endswith("s"):
        raise ValueError("bad duration")
    seconds = float(value[:-1])
    if seconds < 0:
        raise ValueError("negative duration")
    return seconds


def _post(url: str, body: dict, field_mask: str):
    """POST with one retry on 429/5xx; returns parsed JSON or raises."""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": field_mask,
    }
    for attempt in (1, 2):
        try:
            with httpx.Client(timeout=TIMEOUT_S) as client:
                response = client.post(url, json=body, headers=headers)
            if response.status_code in RETRY_STATUSES and attempt == 1:
                retry_after = response.headers.get("Retry-After", "")
                wait = min(float(retry_after), 2.0) if retry_after.isdigit() else 0.5
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            # Never log request headers or the API key.
            log.error("Google Routes API returned HTTP %s", status)
            raise GoogleRoutesError(
                f"Google Routes API returned HTTP {status}",
                kind=_KIND_BY_STATUS.get(status, "upstream_error"),
                status=status,
            ) from None
        except httpx.TimeoutException:
            if attempt == 1:
                continue
            log.error("Google Routes API request timed out")
            raise GoogleRoutesError(
                f"Google Routes API did not answer within {TIMEOUT_S:.0f} s",
                kind="timeout",
            ) from None
        except (httpx.HTTPError, ValueError) as exc:
            log.error("Google Routes API request failed: %s", type(exc).__name__)
            raise GoogleRoutesError(
                "Could not reach or parse the Google Routes API response",
                kind="unreachable_or_invalid",
            ) from None
    raise GoogleRoutesError("Google Routes API request failed", kind="upstream_error")


def compute_route(origin: dict, destination: dict) -> dict:
    """Return road distance, traffic-aware duration and encoded route polyline."""
    # Identical coordinates require no Google API request.
    if _key(origin) == _key(destination):
        return {
            "distance_km": 0.0,
            "duration_min": 0.0,
            "encoded_polyline": "",
            "source": "same_location",
        }
    cache_key = ("route", _key(origin), _key(destination))
    cached = _cache.get(cache_key)
    if cached is not None:
        return dict(cached)

    data = _post(
        ROUTES_URL,
        {
            "origin": _waypoint(origin),
            "destination": _waypoint(destination),
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",
            "polylineQuality": "HIGH_QUALITY",
            "polylineEncoding": "ENCODED_POLYLINE",
            "units": "METRIC",
        },
        FIELD_MASK,
    )
    routes = (data.get("routes") or []) if isinstance(data, dict) else []
    if not routes:
        raise GoogleRoutesError("Google Routes API returned no route", kind="no_route")
    route = routes[0]
    polyline = (route.get("polyline") or {}).get("encodedPolyline")
    try:
        duration_seconds = _seconds(route.get("duration"))
        distance_m = float(route.get("distanceMeters"))
        if distance_m < 0 or not polyline:
            raise ValueError("bad route")
    except (TypeError, ValueError):
        raise GoogleRoutesError(
            "Google Routes API response is missing route data",
            kind="invalid_response",
        ) from None
    result = {
        "distance_km": distance_m / 1000.0,
        "duration_min": duration_seconds / 60.0,
        "encoded_polyline": polyline,
        "source": "google_routes_api",
    }
    _cache.put(cache_key, result)
    return dict(result)


def compute_route_matrix(points: list[dict]) -> dict[tuple[int, int], dict]:
    """Road distance/duration between every pair of ``points`` (deduplicated).

    Returns ``{(i, j): {"distance_km", "duration_min", "static_duration_min"}}``
    for i != j; a pair Google cannot route is simply absent. Rows are chunked
    so each request stays within Google's element limit.
    """
    n = len(points)
    if n < 2:
        return {}
    cache_key = ("matrix", tuple(_key(p) for p in points))
    cached = _cache.get(cache_key)
    if cached is not None:
        return dict(cached)

    rows_per_request = max(1, MATRIX_MAX_ELEMENTS // n)
    result: dict[tuple[int, int], dict] = {}
    destinations = [{"waypoint": _waypoint(p)} for p in points]
    for start in range(0, n, rows_per_request):
        origins = points[start:start + rows_per_request]
        data = _post(
            MATRIX_URL,
            {
                "origins": [{"waypoint": _waypoint(p)} for p in origins],
                "destinations": destinations,
                "travelMode": "DRIVE",
                "routingPreference": "TRAFFIC_AWARE",
                "units": "METRIC",
            },
            MATRIX_FIELD_MASK,
        )
        if isinstance(data, str):  # tolerate a raw JSON string body
            data = json.loads(data)
        if not isinstance(data, list):
            raise GoogleRoutesError(
                "Google Route Matrix response is not a list", kind="invalid_response"
            )
        for element in data:
            if not isinstance(element, dict):
                continue
            i = element.get("originIndex", 0) + start
            j = element.get("destinationIndex", 0)
            if i == j or element.get("condition") != "ROUTE_EXISTS":
                continue
            if (element.get("status") or {}).get("code", 0) not in (0, None):
                continue
            try:
                result[(i, j)] = {
                    "distance_km": float(element.get("distanceMeters", 0)) / 1000.0,
                    "duration_min": _seconds(element.get("duration")) / 60.0,
                    "static_duration_min": _seconds(
                        element.get("staticDuration", element.get("duration"))
                    ) / 60.0,
                }
            except (TypeError, ValueError):
                raise GoogleRoutesError(
                    "Google Route Matrix returned invalid distance or duration",
                    kind="invalid_response",
                ) from None
    _cache.put(cache_key, result)
    return dict(result)


def compute_route_legs(waypoints: list[dict]) -> list[dict]:
    """Road legs (distance, duration, polyline) along ``waypoints`` in order,
    in ONE request. Consecutive identical points produce zero-length legs."""
    if len(waypoints) < 2:
        return []
    # Google rejects zero-length hops; route the distinct points and re-expand.
    distinct = [waypoints[0]]
    for p in waypoints[1:]:
        if _key(p) != _key(distinct[-1]):
            distinct.append(p)
    zero = {"distance_km": 0.0, "duration_min": 0.0, "encoded_polyline": ""}
    if len(distinct) == 1:
        return [dict(zero) for _ in waypoints[1:]]

    cache_key = ("legs", tuple(_key(p) for p in distinct))
    google_legs = _cache.get(cache_key)
    if google_legs is None:
        body = {
            "origin": _waypoint(distinct[0]),
            "destination": _waypoint(distinct[-1]),
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",
            "polylineQuality": "HIGH_QUALITY",
            "polylineEncoding": "ENCODED_POLYLINE",
            "units": "METRIC",
        }
        if len(distinct) > 2:
            body["intermediates"] = [_waypoint(p) for p in distinct[1:-1]]
        data = _post(ROUTES_URL, body, LEGS_FIELD_MASK)
        routes = (data.get("routes") or []) if isinstance(data, dict) else []
        legs = (routes[0].get("legs") if routes else None) or []
        if len(legs) != len(distinct) - 1:
            raise GoogleRoutesError(
                "Google Routes API returned an unexpected number of legs",
                kind="invalid_response",
            )
        google_legs = []
        try:
            for leg in legs:
                google_legs.append({
                    "distance_km": float(leg.get("distanceMeters", 0)) / 1000.0,
                    "duration_min": _seconds(leg.get("duration")) / 60.0,
                    "encoded_polyline": (leg.get("polyline") or {}).get("encodedPolyline", ""),
                })
        except (TypeError, ValueError):
            raise GoogleRoutesError(
                "Google Routes API returned invalid leg data", kind="invalid_response"
            ) from None
        _cache.put(cache_key, google_legs)

    out, g = [], 0
    for prev, cur in zip(waypoints, waypoints[1:]):
        if _key(prev) == _key(cur):
            out.append(dict(zero))
        else:
            out.append(dict(google_legs[g]))
            g += 1
    return out
