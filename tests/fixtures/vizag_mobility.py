"""Synthetic Visakhapatnam corporate-mobility dataset for optimisation tests.

TEST DATA ONLY. Every employee, vehicle, depot and office here is fictional:
ids start with ``TEST-VZG-``, emails use the reserved ``routematic.test``
domain and every record carries ``NOTE``. Coordinates are APPROXIMATE
neighbourhood centroids (hand-picked, not geocoded, not verified addresses),
offset deterministically so people in one area are not stacked on one point.
No traffic observations are included: travel times come from the test's
mocked router. Nothing here is written to any database.

Scenarios (see ``SCENARIOS``):

* ``all_feasible``: 24 employees, 5 vehicles (28 passenger seats), loose
  windows; every employee must be seated.
* ``constrained``: the same 24 plus 4 deliberately infeasible employees, with
  the 9-seat van removed and a 6-seat vehicle in maintenance (13 seats): seat
  capacity binds, and each infeasible case must come back with its reason.
"""
from __future__ import annotations

import copy

MARK = "routematic-mobility-test-v1"
NOTE = f"[TEST DATA {MARK}] synthetic record; approximate coordinates, not a real address"
DOMAIN = "vzg.routematic.test"

# Approximate neighbourhood centroids in Visakhapatnam (not verified addresses).
OFFICE = {
    "id": "TEST-VZG-OFFICE",
    "name": "Synthetic Office, Rushikonda IT area (test)",
    "point": {"lat": 17.7820, "lng": 83.3830},
    "notes": NOTE,
}

NEIGHBOURHOODS = [
    ("MVP Colony", 17.7425, 83.3330),
    ("Madhurawada", 17.8185, 83.3535),
    ("Gajuwaka", 17.6900, 83.2100),
    ("Seethammadhara", 17.7420, 83.3030),
    ("Dwaraka Nagar", 17.7280, 83.3050),
    ("Pendurthi", 17.8020, 83.2030),
    ("Arilova", 17.7680, 83.3170),
    ("NAD Kotharoad", 17.7440, 83.2400),
]

DEPOTS = [
    {"id": "TEST-VZG-DEPOT-MVP", "name": "Synthetic depot, MVP Colony (test)", "point": {"lat": 17.7400, "lng": 83.3350}},
    {"id": "TEST-VZG-DEPOT-GAJ", "name": "Synthetic depot, Gajuwaka (test)", "point": {"lat": 17.6950, "lng": 83.2150}},
    {"id": "TEST-VZG-DEPOT-MDW", "name": "Synthetic depot, Madhurawada (test)", "point": {"lat": 17.8150, "lng": 83.3500}},
    {"id": "TEST-VZG-DEPOT-NAD", "name": "Synthetic depot, NAD Kotharoad (test)", "point": {"lat": 17.7450, "lng": 83.2450}},
]
_DEPOT = {d["id"]: d for d in DEPOTS}

# Times are minutes after midnight (India Standard Time).
SHIFT_0900, SHIFT_0930 = 9 * 60, 9 * 60 + 30
EARLIEST_PICKUP = 7 * 60  # nobody is collected before 07:00


def _employees() -> list[dict]:
    """24 employees: 3 per neighbourhood, alternating 09:00 / 09:30 arrival."""
    out, n = [], 0
    for area, lat, lng in NEIGHBOURHOODS:
        for k in range(3):
            n += 1
            out.append({
                "id": f"TEST-VZG-E{n:02d}",
                "name": f"Test Employee {n:02d}",
                "email": f"employee{n:02d}@{DOMAIN}",
                "area": area,
                "pickup": {"lat": round(lat + 0.0025 * (k - 1), 6), "lng": round(lng + 0.0020 * (k - 1), 6)},
                "dropoff": dict(OFFICE["point"]),
                "arrival_deadline_min": SHIFT_0900 if n % 2 else SHIFT_0930,
                "earliest_pickup_min": EARLIEST_PICKUP,
                "latest_pickup_min": None,
                "notes": NOTE,
            })
    return out


def _vehicles() -> list[dict]:
    """5 vehicles, 28 passenger seats (capacity already excludes the driver)."""
    rows = [
        ("TEST-VZG-V1", "sedan", 3, "TEST-VZG-DEPOT-MVP", 6 * 60 + 30),
        ("TEST-VZG-V2", "suv", 4, "TEST-VZG-DEPOT-GAJ", 6 * 60 + 30),
        ("TEST-VZG-V3", "van", 6, "TEST-VZG-DEPOT-MDW", 6 * 60 + 30),
        ("TEST-VZG-V4", "van", 6, "TEST-VZG-DEPOT-NAD", 6 * 60 + 45),
        ("TEST-VZG-V5", "minibus", 9, "TEST-VZG-DEPOT-MVP", 6 * 60 + 30),
    ]
    return [
        {
            "id": vid,
            "type": vtype,
            "capacity": seats,
            "depot_id": depot,
            "depot": dict(_DEPOT[depot]["point"]),
            "speed_kmph": 25,
            "available": True,
            "available_from_min": available_from,
            "notes": NOTE,
        }
        for vid, vtype, seats, depot, available_from in rows
    ]


def _infeasible_extras() -> list[dict]:
    """Four employees that no plan can seat, each for a different reason."""
    base = {"dropoff": dict(OFFICE["point"]), "notes": NOTE, "latest_pickup_min": None}
    return [
        {  # must arrive 06:00, but no vehicle is available before 06:30
            **base, "id": "TEST-VZG-X01", "name": "Test Infeasible Early Shift",
            "area": "MVP Colony", "pickup": {"lat": 17.7430, "lng": 83.3340},
            "arrival_deadline_min": 6 * 60, "earliest_pickup_min": None,
        },
        {  # pickup window opens at 08:55 in Gajuwaka for a 09:00 arrival
            **base, "id": "TEST-VZG-X02", "name": "Test Infeasible Late Window",
            "area": "Gajuwaka", "pickup": {"lat": 17.6910, "lng": 83.2120},
            "arrival_deadline_min": SHIFT_0900, "earliest_pickup_min": 8 * 60 + 55,
        },
        {  # Anakapalle, ~40 km out: beyond the maximum ride/route time
            **base, "id": "TEST-VZG-X03", "name": "Test Infeasible Far Away",
            "area": "Anakapalle (outside city)", "pickup": {"lat": 17.6913, "lng": 83.0037},
            "arrival_deadline_min": SHIFT_0930, "earliest_pickup_min": EARLIEST_PICKUP,
        },
        {  # no GPS on record
            **base, "id": "TEST-VZG-X04", "name": "Test Missing Coordinates",
            "area": "unknown", "pickup": None,
            "arrival_deadline_min": SHIFT_0930, "earliest_pickup_min": EARLIEST_PICKUP,
        },
    ]


def _all_feasible() -> dict:
    return {
        "employees": _employees(),
        "vehicles": _vehicles(),
        "max_ride_min": 90.0,
        "max_route_min": 180.0,
        "expect": {"assigned": 24, "unassigned": {}},
    }


def _constrained() -> dict:
    vehicles = [v for v in _vehicles() if v["id"] != "TEST-VZG-V5"]
    for v in vehicles:
        if v["id"] == "TEST-VZG-V3":
            v["available"] = False
            v["unavailable_reason"] = "In maintenance (test)"
    return {
        "employees": _employees() + _infeasible_extras(),
        "vehicles": vehicles,
        "max_ride_min": 75.0,
        "max_route_min": 150.0,
        # 3 + 4 + 6 seats available: capacity binds for the 24 regular staff.
        "expect": {
            "assigned": 13,
            "unassigned": {
                "TEST-VZG-X01": "not available early enough",
                "TEST-VZG-X02": ("pickup window", "required arrival time"),
                # too far to reach in time from any depot, whichever limit binds first
                "TEST-VZG-X03": ("maximum ride time", "maximum vehicle route duration", "not available early enough"),
                "TEST-VZG-X04": "coordinates",
            },
        },
    }


SCENARIOS = {"all_feasible": _all_feasible, "constrained": _constrained}


def scenario(name: str) -> dict:
    """A fresh deep copy of a scenario (callers may not share mutable state)."""
    return copy.deepcopy(SCENARIOS[name]())


def simulation_input(name: str) -> dict:
    """Keyword arguments for ``simulate_routes`` (extra descriptive fields dropped)."""
    s = scenario(name)
    keep_e = ("id", "pickup", "dropoff", "arrival_deadline_min", "earliest_pickup_min", "latest_pickup_min")
    keep_v = ("id", "depot", "capacity", "speed_kmph", "available", "unavailable_reason", "available_from_min")
    return {
        "employees": [{k: e.get(k) for k in keep_e} for e in s["employees"]],
        "vehicles": [{k: v[k] for k in keep_v if k in v} for v in s["vehicles"]],
        "max_ride_min": s["max_ride_min"],
        "max_route_min": s["max_route_min"],
    }
