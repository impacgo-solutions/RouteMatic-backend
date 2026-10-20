"""Role-based access control: which tenant role may read / write which resource.

Derived from the Flutter workspaces (Employee, Driver, Employer Admin, Fleet Admin /
Fleet Manager). Fleet admins and the system role have full access within their tenant.
Employer admins are additionally row-scoped to their ``scope_id`` employer; drivers and
employees to their own rows (``policy.py``).
"""
from __future__ import annotations

from .spec import ENTITIES

ALL = set(ENTITIES)

READ: dict[str, set[str]] = {
    "fleetManager": ALL,
    "system": ALL,
    "employerAdmin": ALL - {"alcohol_tests", "geofences", "vehicle_events"},
    "driver": {"trips", "routes", "route_stops", "vehicles", "locations", "employees", "bookings",
               "passenger_events", "shifts", "inspections", "incidents", "notifications",
               "support_tickets", "alcohol_tests", "user_settings", "geofences", "fleet_policy",
               "traffic_state", "sos_dispatches", "vehicle_events", "drivers", "route_revisions",
               "employers"},   # its company's profile: trip completion bills at the company's rate
    "employee": {"trips", "routes", "route_stops", "locations", "bookings", "notifications",
                 "support_tickets", "incidents", "user_settings", "employees", "employers",
                 "traffic_state", "route_revisions", "passenger_events", "sos_dispatches",
                 "vehicles", "drivers"},
}
WRITE: dict[str, set[str]] = {
    "fleetManager": ALL,
    "system": ALL,
    # employer admins: their company only (row scope); trips for billing (distance, billed status) and the
    # company's optimisation profile (EV target / arrival window follow the company settings)
    "employerAdmin": {"employers", "employees", "locations", "bookings", "billing_records",
                      "invoices", "notifications", "support_tickets", "user_settings",
                      "audit_events", "incidents", "sos_dispatches", "trips", "optimization_configs"},
    # drivers / employees: only their OWN rows (see policy.py): a driver runs its trips (route, stops,
    # bookings, billing line); an employee books, leaves or joins the trips it rides in
    "driver": {"trips", "shifts", "inspections", "incidents", "passenger_events", "alcohol_tests",
               "notifications", "support_tickets", "user_settings", "vehicle_events",
               "audit_events", "sos_dispatches", "drivers", "vehicles",
               "routes", "route_stops", "bookings", "billing_records"},
    "employee": {"bookings", "support_tickets", "incidents", "notifications", "user_settings",
                 "passenger_events", "employees", "audit_events", "sos_dispatches",
                 "trips", "routes", "route_stops", "vehicles", "drivers"},
}
# A Fleet Manager uses the SAME application as the Fleet Admin (Fleet workspace + every company workspace):
# it reads everything the console shows and runs day-to-day operations (drivers, trips & dispatch,
# driver/safety operations). Fleet Admin privileges stay with the Fleet Admin: companies,
# managers, vehicle assets (managers may only update operational fields), parking, policies, geofences,
# optimisation profiles, contracts, allocations, billing and employee/location master data.
READ["manager"] = set(ALL)
WRITE["manager"] = {
    "drivers", "vehicles",                       # vehicles: operational fields only (see MANAGER_VEHICLE_FIELDS)
    "trips", "routes", "route_stops", "bookings", "passenger_events", "route_revisions", "optimization_runs",
    "shifts", "inspections", "alcohol_tests", "vehicle_events", "incidents", "sos_dispatches",
    "notifications", "support_tickets", "user_settings", "audit_events",
}

# Fields of a vehicle a Manager may change (status, odometer, battery, assigned driver, inspection date,
# diagnostics). Creating a vehicle or editing its identity/assets (registration, seats, parking, ...) is
# a Fleet Admin privilege.
MANAGER_VEHICLE_FIELDS = {"status", "odometerKm", "batteryOrFuelPercent", "assignedDriverId",
                          "lastInspectionAt", "diagnosticCodes", "securityGuardAssigned",
                          "notes"}   # e.g. "restored after incident …" when an incident is resolved


def manager_vehicle_update_allowed(old: dict, new: dict, partial: bool) -> bool:
    """True if ``new`` (a PUT/PATCH body) only changes operational vehicle fields compared with ``old``."""
    keys = [k for k in new if k != "id"]
    return all(k in MANAGER_VEHICLE_FIELDS or old.get(k) == new[k] for k in keys)


# Employees / drivers may not delete records (admins and the fleet admin can).
DELETE_ROLES = {"fleetManager", "system", "employerAdmin"}


# parking_locations (fleet-owned) mirror the read access of locations.
for _r, _res in list(READ.items()):
    if "locations" in _res and _r not in ("fleetManager", "system"):
        _res.add("parking_locations")
READ["employerAdmin"].add("parking_locations")


def can_read(role: str, resource: str) -> bool:
    return resource in READ.get(role, set())


def can_write(role: str, resource: str) -> bool:
    return resource in WRITE.get(role, set())
