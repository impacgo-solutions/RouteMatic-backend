from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.route_simulation import optimize_routes

router = APIRouter(
    prefix="/api/v1/simulation",
    tags=["Route Simulation"],
)


class EmployeePoint(BaseModel):
    id: str
    x: float
    y: float


class VehiclePoint(BaseModel):
    id: str
    capacity: int = Field(ge=1)
    x: float
    y: float


class DepotPoint(BaseModel):
    x: float = 0
    y: float = 0


class SimulationRequest(BaseModel):
    employees: list[EmployeePoint]
    vehicles: list[VehiclePoint]
    depot: DepotPoint = Field(default_factory=DepotPoint)


@router.post("/optimize")
def run_route_simulation(payload: SimulationRequest):
    try:
        result = optimize_routes(
            employees=[
                employee.model_dump()
                for employee in payload.employees
            ],
            vehicles=[
                vehicle.model_dump()
                for vehicle in payload.vehicles
            ],
            depot=payload.depot.model_dump(),
        )
        return result
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


# Geographic simulation endpoint; existing /optimize endpoint remains unchanged.
from fastapi import Query  # noqa: E402

from app.services.google_routes import GoogleRoutesError  # noqa: E402
from app.services.simulation_engine import simulate_routes  # noqa: E402


class GeographicPoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class GeographicEmployee(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    pickup: GeographicPoint
    dropoff: GeographicPoint
    # Optional time constraints, minutes after midnight.
    arrival_deadline_min: float | None = Field(default=None, ge=0, le=2880)
    earliest_pickup_min: float | None = Field(default=None, ge=0, le=2880)
    latest_pickup_min: float | None = Field(default=None, ge=0, le=2880)


class GeographicVehicle(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    depot: GeographicPoint
    capacity: int = Field(ge=1, le=100)  # passenger seats
    speed_kmph: float = Field(default=25, ge=5, le=100)
    available: bool = True
    available_from_min: float | None = Field(default=None, ge=0, le=2880)


class GeographicSimulationRequest(BaseModel):
    employees: list[GeographicEmployee] = Field(min_length=1, max_length=500)
    vehicles: list[GeographicVehicle] = Field(min_length=1, max_length=100)
    traffic_factor: float = Field(default=1.0, ge=0.5, le=3.0)
    road_detour_factor: float = Field(default=1.25, ge=1.0, le=2.0)
    allow_estimate: bool = False
    max_ride_min: float | None = Field(default=None, ge=5, le=600)
    max_route_min: float | None = Field(default=None, ge=5, le=1440)


def _routing_error(exc: GoogleRoutesError) -> HTTPException:
    # Message text unchanged for existing clients; the kind is in a header.
    return HTTPException(
        status_code=502,
        detail=str(exc),
        headers={"X-Routing-Error-Kind": exc.kind},
    )


@router.post("/simulate")
def run_geographic_simulation(payload: GeographicSimulationRequest):
    try:
        return simulate_routes(
            employees=[
                {
                    "id": employee.id,
                    "pickup": employee.pickup.model_dump(),
                    "dropoff": employee.dropoff.model_dump(),
                    "arrival_deadline_min": employee.arrival_deadline_min,
                    "earliest_pickup_min": employee.earliest_pickup_min,
                    "latest_pickup_min": employee.latest_pickup_min,
                }
                for employee in payload.employees
            ],
            vehicles=[
                {
                    "id": vehicle.id,
                    "depot": vehicle.depot.model_dump(),
                    "capacity": vehicle.capacity,
                    "speed_kmph": vehicle.speed_kmph,
                    "available": vehicle.available,
                    "available_from_min": vehicle.available_from_min,
                }
                for vehicle in payload.vehicles
            ],
            traffic_factor=payload.traffic_factor,
            road_detour_factor=payload.road_detour_factor,
            allow_estimate=payload.allow_estimate,
            max_ride_min=payload.max_ride_min,
            max_route_min=payload.max_route_min,
        )
    except GoogleRoutesError as exc:
        raise _routing_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


ADI_EMPLOYER_ID = "123"
ADI_MAX_RIDE_MIN = 75.0
ADI_MAX_ROUTE_MIN = 180.0


def _num(value):
    return None if value is None else float(value)


def _passenger_seats(total_seats, requires_guard) -> int:
    # App seat convention (capacity_rules.dart): total seats INCLUDE the
    # driver; a required security guard takes one more seat.
    return max(0, int(total_seats or 0) - 1 - (1 if requires_guard else 0))


@router.post("/adi/simulate")
def simulate_adi_routes(
    allow_estimate: bool = Query(
        False,
        description="If Google Routes fails, return a clearly labelled straight-line estimate instead of 502.",
    ),
    max_ride_min: float = Query(ADI_MAX_RIDE_MIN, ge=5, le=600),
    max_route_min: float = Query(ADI_MAX_ROUTE_MIN, ge=5, le=1440),
):
    """Read-only route simulation using ADI Solutions database records."""
    from app.db import transaction

    try:
        with transaction(dict_rows=True) as cur:
            cur.execute("""
                SELECT
                    e.employee_code AS id,
                    e.required_arrival_minutes,
                    p.latitude AS pickup_lat,
                    p.longitude AS pickup_lng,
                    d.latitude AS dropoff_lat,
                    d.longitude AS dropoff_lng
                FROM tenant_adi_solutions.employees e
                LEFT JOIN tenant_adi_solutions.locations p
                    ON p.id = e.pickup_location_id
                LEFT JOIN tenant_adi_solutions.locations d
                    ON d.id = e.destination_id
                WHERE e.employer_id = %s
                  AND e.status = 'active'
                ORDER BY e.employee_code
            """, (ADI_EMPLOYER_ID,))
            employee_rows = cur.fetchall()

            cur.execute("""
                SELECT
                    v.vehicle_code AS id,
                    v.total_seats,
                    v.requires_security_guard,
                    v.status,
                    v.active,
                    l.latitude AS depot_lat,
                    l.longitude AS depot_lng
                FROM fleet_workspace.vehicles v
                LEFT JOIN fleet_workspace.locations l
                    ON l.id = v.home_parking_id
                WHERE v.vehicle_code LIKE 'ADI-VEH-%%'
                ORDER BY v.vehicle_code
            """)
            vehicle_rows = cur.fetchall()

        def point(lat, lng):
            if lat is None or lng is None:
                return None
            return {"lat": float(lat), "lng": float(lng)}

        employees = [
            {
                "id": str(e["id"]),
                "pickup": point(e["pickup_lat"], e["pickup_lng"]),
                "dropoff": point(e["dropoff_lat"], e["dropoff_lng"]),
                "arrival_deadline_min": _num(e["required_arrival_minutes"]),
            }
            for e in employee_rows
        ]

        vehicles = []
        for v in vehicle_rows:
            reason = None
            if not v["active"]:
                reason = "Vehicle is inactive"
            elif v["status"] != "available":
                reason = f"Vehicle status is '{v['status']}'"
            vehicles.append({
                "id": str(v["id"]),
                "depot": point(v["depot_lat"], v["depot_lng"]),
                "capacity": _passenger_seats(v["total_seats"], v["requires_security_guard"]),
                "speed_kmph": 25,
                "available": reason is None,
                "unavailable_reason": reason,
            })

        usable_employees = [e for e in employees if e["pickup"] and e["dropoff"]]
        usable_vehicles = [v for v in vehicles if v["available"] and v["depot"]]
        if not usable_employees or not usable_vehicles:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "No eligible ADI employees or vehicles with GPS.",
                    "employees_found": len(usable_employees),
                    "vehicles_found": len(usable_vehicles),
                },
            )

        result = simulate_routes(
            employees=employees,
            vehicles=vehicles,
            traffic_factor=1.0,
            road_detour_factor=1.25,
            allow_estimate=allow_estimate,
            max_ride_min=max_ride_min,
            max_route_min=max_route_min,
        )

        result["source"] = "adi_solutions_database"
        result["summary"]["employees_loaded"] = len(employees)
        result["summary"]["vehicles_loaded"] = len(vehicles)
        return result

    except HTTPException:
        raise
    except GoogleRoutesError as exc:
        raise _routing_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        # Avoid exposing database credentials or internal exception details.
        raise HTTPException(
            status_code=500,
            detail="ADI route simulation failed. Check backend logs.",
        ) from None
