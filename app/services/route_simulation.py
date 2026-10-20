from __future__ import annotations

from math import hypot
from typing import Any


def _distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Calculate distance between demo coordinates."""
    return hypot(
        float(a["x"]) - float(b["x"]),
        float(a["y"]) - float(b["y"]),
    )


def optimize_routes(
    employees: list[dict[str, Any]],
    vehicles: list[dict[str, Any]],
    depot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Demo route optimizer.

    Employee format:
      {"id": "E1", "x": 2, "y": 3}

    Vehicle format:
      {"id": "V1", "capacity": 4, "x": 0, "y": 0}

    Coordinates are simulated units, not real road distances.
    """

    if not vehicles and employees:
        raise ValueError("At least one vehicle is required.")

    if any(int(v.get("capacity", 0)) < 1 for v in vehicles):
        raise ValueError("Every vehicle must have capacity >= 1.")

    employee_ids = [str(e["id"]) for e in employees]
    vehicle_ids = [str(v["id"]) for v in vehicles]

    if len(set(employee_ids)) != len(employee_ids):
        raise ValueError("Employee IDs must be unique.")

    if len(set(vehicle_ids)) != len(vehicle_ids):
        raise ValueError("Vehicle IDs must be unique.")

    if any(not all(k in e for k in ("id", "x", "y")) for e in employees):
        raise ValueError("Each employee needs id, x, and y.")

    if any(not all(k in v for k in ("id", "capacity", "x", "y")) for v in vehicles):
        raise ValueError("Each vehicle needs id, capacity, x, and y.")

    start = depot or {"x": 0, "y": 0}
    remaining = employees.copy()
    routes = []
    unassigned = []

    # Process vehicles with the smallest capacity first.
    ordered_vehicles = sorted(
        vehicles,
        key=lambda v: (int(v["capacity"]), str(v["id"])),
    )

    for vehicle in ordered_vehicles:
        current = {"x": float(vehicle["x"]), "y": float(vehicle["y"])}
        capacity = int(vehicle["capacity"])
        stops = []
        route_distance = 0.0

        while remaining and len(stops) < capacity:
            nearest = min(
                remaining,
                key=lambda e: (
                    _distance(current, e),
                    str(e["id"]),
                ),
            )

            route_distance += _distance(current, nearest)
            stops.append({
                "employee_id": str(nearest["id"]),
                "x": float(nearest["x"]),
                "y": float(nearest["y"]),
                "stop_order": len(stops) + 1,
            })

            current = nearest
            remaining.remove(nearest)

        if stops:
            route_distance += _distance(current, start)

        routes.append({
            "vehicle_id": str(vehicle["id"]),
            "capacity": capacity,
            "assigned_count": len(stops),
            "stops": stops,
            "estimated_distance_units": round(route_distance, 2),
        })

    unassigned = [str(e["id"]) for e in remaining]

    return {
        "simulation": True,
        "distance_unit": "demo_coordinate_units",
        "employee_count": len(employees),
        "vehicle_count": len(vehicles),
        "assigned_count": len(employees) - len(unassigned),
        "unassigned_employee_ids": unassigned,
        "routes": routes,
    }
