
from __future__ import annotations

import copy
import json

import httpx
import pytest

from app.routers import route_simulation as router
from app.services import google_routes, simulation_engine
from app.services.google_routes import GoogleRoutesError
from app.services.simulation_engine import distance_km, simulate_routes

OFFICE = {"lat": 17.7394, "lng": 83.3110}
# The real client functions, captured before the autouse fake replaces them.
REAL_MATRIX = google_routes.compute_route_matrix
REAL_LEGS = google_routes.compute_route_legs
REAL_ROUTE = google_routes.compute_route


def _emp(eid, lat, lng, deadline=None, **extra):
    return {"id": eid, "pickup": {"lat": lat, "lng": lng}, "dropoff": dict(OFFICE),
            "arrival_deadline_min": deadline, **extra}


def _veh(vid, lat, lng, capacity, **extra):
    return {"id": vid, "depot": {"lat": lat, "lng": lng}, "capacity": capacity,
            "speed_kmph": 25, **extra}


@pytest.fixture(autouse=True)
def fake_google(monkeypatch):
    """Deterministic stand-in for Google: 30 km/h along straight line x 1.3.
    Counts calls so tests can check batching."""
    google_routes.clear_cache()
    calls = {"matrix": 0, "legs": 0}

    def matrix(points):
        calls["matrix"] += 1
        out = {}
        for i, a in enumerate(points):
            for j, b in enumerate(points):
                if i != j:
                    km = distance_km(a, b) * 1.3
                    out[(i, j)] = {"distance_km": km, "duration_min": km / 30 * 60,
                                   "static_duration_min": km / 35 * 60}
        return out

    def legs(waypoints):
        calls["legs"] += 1
        return [{"distance_km": 0.0, "duration_min": 0.0, "encoded_polyline": f"poly{i}"}
                for i in range(len(waypoints) - 1)]

    monkeypatch.setattr(google_routes, "compute_route_matrix", matrix)
    monkeypatch.setattr(google_routes, "compute_route_legs", legs)
    return calls


def _assigned(result):
    return [eid for r in result["routes"] for eid in r["employee_ids"]]


# ---------------------------------------------------------------- 1-3 assignment
def test_assigns_everyone_within_capacity_without_duplicates():
    emps = [_emp(f"E{i}", 17.70 + i * 0.005, 83.28 + i * 0.003, deadline=540) for i in range(7)]
    vehs = [_veh("V1", 17.73, 83.30, 3), _veh("V2", 17.72, 83.29, 4)]
    result = simulate_routes(emps, vehs)

    assigned = _assigned(result)
    assert sorted(assigned) == sorted(e["id"] for e in emps)
    assert len(assigned) == len(set(assigned)), "an employee was assigned twice"
    for route in result["routes"]:
        assert route["assigned_count"] <= route["capacity"]
        assert route["assigned_count"] == len(route["employee_ids"])
    assert result["summary"]["employees_assigned"] == 7
    assert result["summary"]["employees_unassigned"] == 0


def test_capacity_overflow_is_reported_not_overbooked():
    emps = [_emp(f"E{i}", 17.70 + i * 0.002, 83.30, deadline=540) for i in range(5)]
    result = simulate_routes(emps, [_veh("V1", 17.73, 83.30, 3)])
    assert result["routes"][0]["assigned_count"] == 3
    assert len(result["unassigned"]) == 2
    assert all("No free passenger seat" in u["reason"] for u in result["unassigned"])


def test_balances_instead_of_chaining_everyone_into_one_vehicle():
    # Two clusters, each with its own nearby vehicle; one vehicle COULD take all.
    north = [_emp(f"N{i}", 17.82 + i * 0.002, 83.35, deadline=540) for i in range(3)]
    south = [_emp(f"S{i}", 17.69 + i * 0.002, 83.21, deadline=540) for i in range(3)]
    vehs = [_veh("VN", 17.83, 83.36, 6), _veh("VS", 17.68, 83.20, 6)]
    result = simulate_routes(north + south, vehs, max_ride_min=60)
    by_vehicle = {r["vehicle_id"]: set(r["employee_ids"]) for r in result["routes"]}
    assert by_vehicle["VN"] == {e["id"] for e in north}
    assert by_vehicle["VS"] == {e["id"] for e in south}


# ---------------------------------------------------------------- 4-5 windows & reasons
def test_arrival_deadlines_are_met_and_clock_times_reported():
    emps = [_emp("E1", 17.70, 83.25, deadline=540), _emp("E2", 17.71, 83.26, deadline=545)]
    result = simulate_routes(emps, [_veh("V1", 17.69, 83.24, 4)])
    route = result["routes"][0]
    for stop in route["stops"]:
        if stop["type"] == "dropoff":
            h, m = map(int, stop["eta_clock"].split(":"))
            deadline = next(e["arrival_deadline_min"] for e in emps if e["id"] == stop["employee_id"])
            assert h * 60 + m <= deadline
    assert route["departure_clock"] and route["arrival_clock"]


def test_vehicle_availability_blocks_impossible_deadline_with_reason():
    emps = [_emp("E1", 17.60, 83.10, deadline=540)]  # ~30 km away
    vehs = [_veh("V1", 17.60, 83.10, 4, available_from_min=535)]  # free only at 08:55
    result = simulate_routes(emps, vehs)
    assert result["summary"]["employees_assigned"] == 0
    assert "not available early enough" in result["unassigned"][0]["reason"]
    assert "V1" in result["unassigned"][0]["reason"]


def test_pickup_window_is_respected():
    emps = [_emp("E1", 17.70, 83.25, deadline=600, earliest_pickup_min=560)]
    result = simulate_routes(emps, [_veh("V1", 17.70, 83.25, 4)])
    pickup = result["routes"][0]["stops"][0]
    h, m = map(int, pickup["eta_clock"].split(":"))
    assert h * 60 + m >= 560


def test_unavailable_vehicle_is_excluded_with_reason():
    emps = [_emp("E1", 17.70, 83.25, deadline=540)]
    vehs = [_veh("V1", 17.70, 83.25, 4, available=False, unavailable_reason="In maintenance"),
            _veh("V2", 17.71, 83.26, 4)]
    result = simulate_routes(emps, vehs)
    assert result["vehicles_excluded"] == [{"vehicle_id": "V1", "reason": "In maintenance"}]
    assert _assigned(result) == ["E1"]


def test_ride_time_cap_leaves_far_employee_unassigned_with_reason():
    emps = [_emp("FAR", 18.30, 83.90, deadline=540)]  # ~90 km
    result = simulate_routes(emps, [_veh("V1", 18.30, 83.90, 4)], max_ride_min=60)
    assert result["unassigned"][0]["employee_id"] == "FAR"
    assert "maximum ride time" in result["unassigned"][0]["reason"]


# ---------------------------------------------------------------- 6 consistency
def test_route_totals_and_eta_are_internally_consistent():
    emps = [_emp(f"E{i}", 17.70 + i * 0.01, 83.25 + i * 0.01, deadline=550) for i in range(4)]
    result = simulate_routes(emps, [_veh("V1", 17.69, 83.24, 6)])
    route = result["routes"][0]
    stops = route["stops"]
    assert [s["sequence"] for s in stops] == list(range(1, len(stops) + 1))
    assert all(s["type"] == "pickup" for s in stops[:4]) and all(s["type"] == "dropoff" for s in stops[4:])
    assert sum(s["leg_distance_km"] for s in stops) == pytest.approx(route["distance_km"], abs=0.05)
    etas = [s["eta_min"] for s in stops]
    assert etas == sorted(etas)
    assert abs(route["duration_min"] - stops[-1]["eta_min"]) <= 1
    assert result["summary"]["distance_km"] == pytest.approx(sum(r["distance_km"] for r in result["routes"]), abs=0.05)
    assert result["optimization"]["proven_optimal"] is False


def test_planning_uses_one_matrix_call_and_one_geometry_call_per_used_vehicle(fake_google):
    emps = [_emp(f"E{i}", 17.70 + i * 0.01, 83.25, deadline=550) for i in range(4)]
    vehs = [_veh("V1", 17.69, 83.24, 6), _veh("V2", 17.90, 83.50, 6)]
    result = simulate_routes(emps, vehs)
    used = result["summary"]["vehicles_used"]
    assert fake_google == {"matrix": 1, "legs": used}
    assert result["routing"]["api_calls"] == 1 + used
    assert all(s["encoded_polyline"].startswith("poly") for r in result["routes"] for s in r["stops"])


# ---------------------------------------------------------------- 9 fallback
def test_google_failure_raises_unless_estimate_allowed(monkeypatch):
    def fail(points):
        raise GoogleRoutesError("Google Routes API returned HTTP 429", kind="rate_limited", status=429)

    monkeypatch.setattr(google_routes, "compute_route_matrix", fail)
    emps = [_emp("E1", 17.70, 83.25, deadline=540)]
    vehs = [_veh("V1", 17.69, 83.24, 4)]
    with pytest.raises(GoogleRoutesError) as err:
        simulate_routes(emps, vehs)
    assert err.value.kind == "rate_limited"

    result = simulate_routes(emps, vehs, allow_estimate=True)
    assert result["routing"]["estimate"] is True
    assert result["routing"]["status"] == "fallback_estimate"
    assert result["routing"]["error"]["status"] == 429
    assert "ESTIMATE" in result["distance_source"]
    for stop in result["routes"][0]["stops"]:
        assert stop["distance_source"] == "estimate_straight_line"
        assert stop["encoded_polyline"] == "", "no fake road geometry for estimates"


# ---------------------------------------------------------------- 10 bad input
def test_empty_or_missing_inputs_are_rejected():
    with pytest.raises(ValueError):
        simulate_routes([], [_veh("V1", 17.7, 83.3, 4)])
    with pytest.raises(ValueError):
        simulate_routes([_emp("E1", 17.7, 83.3)], [])


def test_invalid_coordinates_become_unassigned_with_reason():
    emps = [_emp("OK", 17.70, 83.25, deadline=540),
            {"id": "NOGPS", "pickup": None, "dropoff": dict(OFFICE)},
            _emp("BAD", 95.0, 83.25, deadline=540)]
    result = simulate_routes(emps, [_veh("V1", 17.69, 83.24, 4)])
    reasons = {u["employee_id"]: u["reason"] for u in result["unassigned"]}
    assert set(reasons) == {"NOGPS", "BAD"}
    assert all("coordinates" in r for r in reasons.values())
    assert _assigned(result) == ["OK"]
    assert result["summary"]["employees_total"] == 3


# ---------------------------------------------------------------- 11-12
def test_inputs_are_not_mutated_and_results_are_deterministic():
    emps = [_emp(f"E{i}", 17.70 + i * 0.01, 83.25, deadline=545) for i in range(5)]
    vehs = [_veh("V1", 17.69, 83.24, 3), _veh("V2", 17.75, 83.30, 3)]
    snapshot = copy.deepcopy((emps, vehs))
    first = simulate_routes(emps, vehs)
    second = simulate_routes(emps, vehs)
    assert (emps, vehs) == snapshot
    assert first == second


def test_response_keeps_existing_fields():
    result = simulate_routes([_emp("E1", 17.70, 83.25)], [_veh("V1", 17.69, 83.24, 4)])
    assert {"mode", "traffic_factor", "summary", "routes", "unassigned", "distance_source"} <= set(result)
    assert {"employees_total", "employees_assigned", "employees_unassigned",
            "distance_km", "duration_min"} <= set(result["summary"])
    route = result["routes"][0]
    assert {"vehicle_id", "capacity", "assigned_count", "employee_ids", "stops",
            "distance_km", "duration_min"} <= set(route)
    assert {"sequence", "type", "employee_id", "lat", "lng", "leg_distance_km",
            "travel_time_min", "eta_min", "encoded_polyline", "distance_source"} <= set(route["stops"][0])


def test_adi_seat_convention_excludes_driver_and_guard():
    assert router._passenger_seats(10, False) == 9
    assert router._passenger_seats(4, True) == 2
    assert router._passenger_seats(1, False) == 0


# ---------------------------------------------------------------- 7-8 HTTP client
@pytest.fixture
def http(monkeypatch):
    """Route google_routes' httpx traffic to a handler; never the network."""
    real = httpx.Client
    state = {"handler": None, "requests": []}

    def client(**kwargs):
        def handle(request):
            state["requests"].append(request)
            return state["handler"](request)
        return real(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(google_routes.httpx, "Client", client)
    monkeypatch.setattr(google_routes.time, "sleep", lambda s: None)
    monkeypatch.setattr(google_routes, "_api_key", lambda: "test-key")
    google_routes.clear_cache()
    return state


def test_matrix_success_is_parsed(http):
    http["handler"] = lambda r: httpx.Response(200, json=[
        {"originIndex": 0, "destinationIndex": 1, "condition": "ROUTE_EXISTS",
         "distanceMeters": 5200, "duration": "780s", "staticDuration": "600s", "status": {}},
        {"originIndex": 1, "destinationIndex": 0, "condition": "ROUTE_EXISTS",
         "distanceMeters": 5000, "duration": "720s", "staticDuration": "600s", "status": {}},
        {"originIndex": 0, "destinationIndex": 0, "condition": "ROUTE_EXISTS",
         "distanceMeters": 0, "duration": "0s", "status": {}},
    ])
    m = REAL_MATRIX([{"lat": 17.70, "lng": 83.25}, OFFICE])
    assert m[(0, 1)] == {"distance_km": 5.2, "duration_min": 13.0, "static_duration_min": 10.0}
    assert m[(1, 0)]["duration_min"] == 12.0
    assert (0, 0) not in m
    sent = http["requests"][0]
    assert sent.headers["X-Goog-Api-Key"] == "test-key"
    body = json.loads(sent.content)
    assert body["routingPreference"] == "TRAFFIC_AWARE"
    # cached: identical request does not call Google again
    REAL_MATRIX([{"lat": 17.70, "lng": 83.25}, OFFICE])
    assert len(http["requests"]) == 1


def test_route_legs_success_is_parsed_and_duplicates_collapsed(http):
    http["handler"] = lambda r: httpx.Response(200, json={"routes": [{"legs": [
        {"distanceMeters": 1000, "duration": "120s", "polyline": {"encodedPolyline": "abc"}},
        {"distanceMeters": 2000, "duration": "240s", "polyline": {"encodedPolyline": "def"}},
    ]}]})
    a, b, c = {"lat": 17.70, "lng": 83.25}, {"lat": 17.71, "lng": 83.26}, OFFICE
    legs = REAL_LEGS([a, b, b, c])
    assert [round(x["distance_km"], 3) for x in legs] == [1.0, 0.0, 2.0]
    assert [x["encoded_polyline"] for x in legs] == ["abc", "", "def"]
    assert len(json.loads(http["requests"][0].content)["intermediates"]) == 1


def test_http_429_is_retried_once_then_reported(http):
    http["handler"] = lambda r: httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
    with pytest.raises(GoogleRoutesError) as err:
        REAL_MATRIX([{"lat": 17.70, "lng": 83.25}, OFFICE])
    assert err.value.kind == "rate_limited" and err.value.status == 429
    assert len(http["requests"]) == 2
    assert "test-key" not in str(err.value)


def test_http_403_is_billing_or_permission_and_not_retried(http):
    http["handler"] = lambda r: httpx.Response(403, json={})
    with pytest.raises(GoogleRoutesError) as err:
        REAL_ROUTE({"lat": 17.70, "lng": 83.25}, OFFICE)
    assert err.value.kind == "permission_or_billing"
    assert len(http["requests"]) == 1


def test_timeout_is_retried_once_then_reported(http):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    http["handler"] = timeout
    with pytest.raises(GoogleRoutesError) as err:
        REAL_MATRIX([{"lat": 17.70, "lng": 83.25}, OFFICE])
    assert err.value.kind == "timeout"
    assert len(http["requests"]) == 2


def test_invalid_google_response_is_rejected(http):
    http["handler"] = lambda r: httpx.Response(200, json={"unexpected": True})
    with pytest.raises(GoogleRoutesError) as err:
        REAL_MATRIX([{"lat": 17.70, "lng": 83.25}, OFFICE])
    assert err.value.kind == "invalid_response"


def test_relaxing_limits_never_seats_fewer_employees():
    # ADI-like shape: paired pickups in five areas, one 9-seat van at the office.
    areas = [(17.739, 83.302), (17.728, 83.305), (17.744, 83.324), (17.821, 83.355), (17.690, 83.208)]
    emps = [_emp(f"E{a}{k}", lat, lng, deadline=540 + 5 * k)
            for a, (lat, lng) in enumerate(areas) for k in range(2)]
    vehs = [_veh("VAN", OFFICE["lat"], OFFICE["lng"], 9)]
    seated = [
        simulate_routes(emps, vehs, max_ride_min=ride, max_route_min=route)["summary"]["employees_assigned"]
        for ride, route in [(75, 180), (120, 240), (None, None)]
    ]
    assert seated == sorted(seated), seated
    assert seated[-1] == 9  # capacity is the only limit left
