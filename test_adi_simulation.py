from app.db import transaction
from app.services.simulation_engine import simulate_routes

# Load ADI Solutions employees and their pickup/drop-off coordinates.
with transaction(dict_rows=True) as cur:
    cur.execute("""
        SELECT
            e.id,
            e.employee_code,
            e.name,
            p.latitude AS pickup_lat,
            p.longitude AS pickup_lng,
            d.latitude AS dropoff_lat,
            d.longitude AS dropoff_lng
        FROM tenant_adi_solutions.employees e
        JOIN tenant_adi_solutions.locations p
            ON p.id = e.pickup_location_id
        JOIN tenant_adi_solutions.locations d
            ON d.id = e.destination_id
        WHERE e.employer_id = '123'
          AND e.status = 'active'
          AND p.latitude IS NOT NULL
          AND p.longitude IS NOT NULL
          AND d.latitude IS NOT NULL
          AND d.longitude IS NOT NULL
        ORDER BY e.employee_code
    """)
    employee_rows = cur.fetchall()

    cur.execute("""
        SELECT
            v.id,
            v.total_seats,
            l.latitude AS depot_lat,
            l.longitude AS depot_lng
        FROM fleet_workspace.vehicles v
        JOIN fleet_workspace.locations l
            ON l.id = v.home_parking_id
        WHERE v.vehicle_code LIKE 'ADI-VEH-%'
          AND v.active = TRUE
          AND v.status = 'available'
          AND v.total_seats > 0
          AND l.latitude IS NOT NULL
          AND l.longitude IS NOT NULL
        ORDER BY v.vehicle_code
    """)
    vehicle_rows = cur.fetchall()

employees = [
    {
        "id": str(e["employee_code"]),
        "pickup": {
            "lat": float(e["pickup_lat"]),
            "lng": float(e["pickup_lng"]),
        },
        "dropoff": {
            "lat": float(e["dropoff_lat"]),
            "lng": float(e["dropoff_lng"]),
        },
    }
    for e in employee_rows
]

vehicles = [
    {
        "id": str(v["id"]),
        "depot": {
            "lat": float(v["depot_lat"]),
            "lng": float(v["depot_lng"]),
        },
        "capacity": int(v["total_seats"]),
        "speed_kmph": 25,
    }
    for v in vehicle_rows
]

print(f"Employees loaded: {len(employees)}")
print(f"ADI vehicles loaded: {len(vehicles)}")

if not employees or not vehicles:
    raise SystemExit("No eligible employees or vehicles found. Check the database filters.")

result = simulate_routes(employees, vehicles)

print("\nSIMULATION SUMMARY")
print(result.get("summary"))

print("\nROUTE DETAILS")
for route in result.get("routes", []):
    print(route)

print("\nUNASSIGNED")
print(result.get("unassigned", []))
