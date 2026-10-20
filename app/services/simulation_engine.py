"""Corporate mobility route simulation: assignment, pickup order, road routing, ETA.

Planning uses ONE Google Route Matrix request (all pickup, drop-off and depot
points, deduplicated); each vehicle that gets passengers then needs ONE Google
route request for its road geometry. If Google fails, the request fails
(``GoogleRoutesError``) unless the caller passes ``allow_estimate=True``: then
a straight-line x detour estimate is used and the response says so in
``routing`` and on every stop (``distance_source``). Estimates are never
presented as road routes. Inputs are never mutated.
"""
from __future__ import annotations

import math
from math import asin, cos, radians, sin, sqrt
from typing import Any

from app.services import google_routes
from app.services.google_routes import GoogleRoutesError
from app.services.route_optimizer import Employee, Optimizer, Vehicle

EARTH_RADIUS_KM = 6371.0088
BOARDING_MIN = 3.0
ALIGHTING_MIN = 1.0
ESTIMATE_SOURCE = "estimate_straight_line"


def distance_km(a: dict, b: dict) -> float:
    """Calculate geographic straight-line distance in kilometres."""
    lat1, lat2 = radians(a["lat"]), radians(b["lat"])
    dlat = lat2 - lat1
    dlng = radians(b["lng"] - a["lng"])
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(min(1.0, sqrt(h)))


def _valid_point(point: Any) -> bool:
    try:
        lat, lng = float(point["lat"]), float(point["lng"])
    except (TypeError, KeyError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180


def _clock(minutes: float | None) -> str | None:
    if minutes is None or not math.isfinite(minutes):
        return None
    m = int(round(minutes)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def simulate_routes(
    employees: list[dict[str, Any]],
    vehicles: list[dict[str, Any]],
    traffic_factor: float = 1.0,
    road_detour_factor: float = 1.25,
    *,
    allow_estimate: bool = False,
    max_ride_min: float | None = None,
    max_route_min: float | None = None,
) -> dict[str, Any]:
    """Assign employees to vehicles, order pickups and compute road ETAs.

    Employee: ``{"id", "pickup": {"lat","lng"}, "dropoff": {...}}`` plus optional
    ``arrival_deadline_min`` / ``earliest_pickup_min`` / ``latest_pickup_min``
    (minutes after midnight).
    Vehicle: ``{"id", "depot": {...}, "capacity", "speed_kmph"}`` plus optional
    ``available`` (default True), ``unavailable_reason`` and ``available_from_min``.
    ``capacity`` is PASSENGER seats (driver/guard already excluded).
    """
    if not employees:
        raise ValueError("At least one employee is required")
    if not vehicles:
        raise ValueError("At least one vehicle is required")
    if not 0.5 <= traffic_factor <= 3.0:
        raise ValueError("traffic_factor must be between 0.5 and 3.0")
    if not 1.0 <= road_detour_factor <= 2.0:
        raise ValueError("road_detour_factor must be between 1.0 and 2.0")
    employee_ids = [e["id"] for e in employees]
    vehicle_ids = [v["id"] for v in vehicles]
    if len(set(employee_ids)) != len(employee_ids):
        raise ValueError("Employee IDs must be unique")
    if len(set(vehicle_ids)) != len(vehicle_ids):
        raise ValueError("Vehicle IDs must be unique")
    for vehicle in vehicles:
        if int(vehicle["capacity"]) < 0:
            raise ValueError(f"Vehicle {vehicle['id']} has invalid capacity")
        if not 5 <= float(vehicle.get("speed_kmph", 25)) <= 100:
            raise ValueError(f"Invalid speed for vehicle {vehicle['id']}")

    # ---- points (deduplicated) and input checks; inputs are only read.
    points: list[dict] = []
    index: dict[tuple, int] = {}

    def point_id(p: dict) -> int:
        key = (round(float(p["lat"]), 6), round(float(p["lng"]), 6))
        if key not in index:
            index[key] = len(points)
            points.append({"lat": key[0], "lng": key[1]})
        return index[key]

    unassigned: list[dict] = []
    opt_employees: list[Employee] = []
    by_id = {}
    for e in employees:
        if not (_valid_point(e.get("pickup")) and _valid_point(e.get("dropoff"))):
            unassigned.append({
                "employee_id": e["id"],
                "reason": "Missing or invalid pickup/drop-off coordinates",
            })
            continue
        oe = Employee(
            id=str(e["id"]),
            pickup=point_id(e["pickup"]),
            dropoff=point_id(e["dropoff"]),
            arrival_deadline_min=e.get("arrival_deadline_min"),
            earliest_pickup_min=e.get("earliest_pickup_min"),
            latest_pickup_min=e.get("latest_pickup_min"),
        )
        opt_employees.append(oe)
        by_id[oe.id] = e

    vehicles_excluded: list[dict] = []
    opt_vehicles: list[Vehicle] = []
    vehicle_by_id = {}
    for v in vehicles:
        reason = None
        if not v.get("available", True):
            reason = v.get("unavailable_reason") or "Vehicle is not available"
        elif not _valid_point(v.get("depot")):
            reason = "Missing or invalid depot coordinates"
        elif int(v["capacity"]) < 1:
            reason = "No passenger seats"
        if reason:
            vehicles_excluded.append({"vehicle_id": v["id"], "reason": reason})
            continue
        ov = Vehicle(
            id=str(v["id"]),
            depot=point_id(v["depot"]),
            capacity=int(v["capacity"]),
            available_from_min=v.get("available_from_min"),
        )
        opt_vehicles.append(ov)
        vehicle_by_id[ov.id] = v

    # ---- travel matrix: Google (one batched call) or a LABELLED estimate.
    routing: dict[str, Any] = {
        "provider": "google_routes_api",
        "status": "ok",
        "traffic_basis": "google_traffic_aware_at_request_time",
        "estimate": False,
        "points": len(points),
        "error": None,
    }
    matrix: dict = {}
    if opt_employees and opt_vehicles:
        try:
            matrix = google_routes.compute_route_matrix(points)
        except GoogleRoutesError as exc:
            if not allow_estimate:
                raise
            routing.update(
                provider=ESTIMATE_SOURCE,
                status="fallback_estimate",
                traffic_basis=f"none: straight line x {road_detour_factor} at vehicle speed x traffic_factor {traffic_factor}",
                estimate=True,
                error={"kind": exc.kind, "status": exc.status, "message": str(exc)},
            )

    speed_by_depot = {ov.depot: float(vehicle_by_id[ov.id].get("speed_kmph", 25)) for ov in opt_vehicles}
    default_speed = min(speed_by_depot.values()) if speed_by_depot else 25.0

    def travel(i: int, j: int):
        if i == j:
            return (0.0, 0.0)
        if not routing["estimate"]:
            m = matrix.get((i, j))
            return None if m is None else (m["distance_km"], m["duration_min"])
        km = distance_km(points[i], points[j]) * road_detour_factor
        return (km, km / default_speed * 60.0 * traffic_factor)

    solution = Optimizer(
        opt_employees,
        opt_vehicles,
        travel,
        boarding_min=BOARDING_MIN,
        alighting_min=ALIGHTING_MIN,
        max_ride_min=max_ride_min,
        max_route_min=max_route_min,
    ).solve()
    unassigned += [
        {"employee_id": eid, "reason": reason}
        for eid, reason in sorted(solution.unassigned.items())
    ]

    # ---- output (same fields as before, plus routing/optimisation metadata)
    has_clock = any(e.arrival_deadline_min is not None or e.earliest_pickup_min is not None
                    or e.latest_pickup_min is not None for e in opt_employees) or any(
        v.available_from_min is not None for v in opt_vehicles)
    routes, total_distance, total_duration, api_calls = [], 0.0, 0, 1 if matrix else 0
    for ov in opt_vehicles:
        plan = solution.plans[ov.id]
        v = vehicle_by_id[ov.id]
        waypoints = [points[ov.depot]] + [points[s["point"]] for s in plan.stops]
        geometry = [{"encoded_polyline": ""} for _ in plan.stops]
        if plan.stops and not routing["estimate"]:
            try:
                geometry = google_routes.compute_route_legs(waypoints)
                api_calls += 1
            except GoogleRoutesError as exc:
                if not allow_estimate:
                    raise
                routing["geometry_error"] = {"kind": exc.kind, "status": exc.status, "message": str(exc)}
        stops = []
        for seq, (s, geo) in enumerate(zip(plan.stops, geometry), start=1):
            eta_abs = plan.start_min + s["eta_offset_min"]
            stops.append({
                "sequence": seq,
                "type": s["type"],
                "employee_id": s["employee_id"],
                "lat": points[s["point"]]["lat"],
                "lng": points[s["point"]]["lng"],
                "leg_distance_km": round(s["leg_distance_km"], 2),
                "travel_time_min": round(s["travel_time_min"], 1),
                "eta_min": round(s["eta_offset_min"], 1),
                "eta_clock": _clock(eta_abs) if has_clock else None,
                "encoded_polyline": geo.get("encoded_polyline", ""),
                "distance_source": ESTIMATE_SOURCE if routing["estimate"] else "google_route_matrix",
            })
        duration = round(plan.duration_min)
        total_distance += plan.distance_km
        total_duration += duration
        routes.append({
            "vehicle_id": ov.id,
            "capacity": ov.capacity,
            "assigned_count": len(plan.order),
            "employee_ids": list(plan.order),
            "stops": stops,
            "distance_km": round(plan.distance_km, 2),
            "duration_min": duration,
            "departure_clock": _clock(plan.start_min) if has_clock and plan.order else None,
            "arrival_clock": _clock(plan.start_min + plan.duration_min) if has_clock and plan.order else None,
        })
    for ex in vehicles_excluded:
        v = next(v for v in vehicles if v["id"] == ex["vehicle_id"])
        routes.append({
            "vehicle_id": v["id"],
            "capacity": int(v["capacity"]),
            "assigned_count": 0,
            "employee_ids": [],
            "stops": [],
            "distance_km": 0.0,
            "duration_min": 0,
            "excluded_reason": ex["reason"],
        })

    routing["api_calls"] = api_calls
    employee_points = [points[e.pickup] for e in opt_employees] + [points[e.dropoff] for e in opt_employees]
    data_warnings = []
    for ov in opt_vehicles:
        if employee_points:
            nearest = min(distance_km(points[ov.depot], p) for p in employee_points)
            if nearest > 100:
                data_warnings.append({
                    "vehicle_id": ov.id,
                    "warning": f"Depot is {nearest:.0f} km (straight line) from the nearest employee location; check the vehicle's home parking.",
                })
    assigned = sum(len(p.order) for p in solution.plans.values())
    return {
        "mode": "simulation",
        "traffic_factor": traffic_factor,
        "summary": {
            "employees_total": len(employees),
            "employees_assigned": assigned,
            "employees_unassigned": len(employees) - assigned,
            "distance_km": round(total_distance, 2),
            "duration_min": total_duration,
            "vehicles_used": sum(1 for p in solution.plans.values() if p.order),
        },
        "routes": routes,
        "unassigned": unassigned,
        "vehicles_excluded": vehicles_excluded,
        "data_warnings": data_warnings,
        "distance_source": (
            "Straight-line ESTIMATE (Google Routes unavailable) - not road distance"
            if routing["estimate"]
            else "Google Routes API (traffic-aware)"
        ),
        "routing": routing,
        "optimization": {
            "method": solution.method,
            "proven_optimal": False,
            "objective": "total vehicle minutes + 0.5 x total passenger ride minutes",
            "objective_value": solution.objective,
            "local_search_passes": solution.iterations,
            "constraints": [
                "passenger capacity", "vehicle availability", "arrival deadline",
                "pickup window",
                "maximum ride time" if max_ride_min else "no ride-time cap",
                "maximum route duration" if max_route_min else "no route-duration cap",
            ],
            "max_ride_min": max_ride_min,
            "max_route_min": max_route_min,
            "boarding_min": BOARDING_MIN,
            "alighting_min": ALIGHTING_MIN,
        },
    }
