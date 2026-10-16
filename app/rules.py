"""Business rules enforced server-side (ported from the Flutter domain layer).

Sources: ``lib/domain/rules/trip_state_machine.dart``, the v2/v3 service rules in
``V2_DOMAIN_API.md`` (fleet allocation overlap, parking capacity).
Anything the Flutter services compute client-side and that is not listed here is
documented as not yet enforced in ``docs/ARCHITECTURE.md``.
"""
from __future__ import annotations

import contextlib
from contextvars import ContextVar

from .repo import ApiError
from .spec import Entity

# Batch mode (sync push): set-based checks (parking capacity, allocation overlap) run once on the
# FINAL state of the batch, so moving several vehicles / windows in one save is not refused halfway.
_deferred: ContextVar[list | None] = ContextVar("rules_deferred", default=None)
# Fleet Admin / platform admin may create trips in any status (importing existing records).
_imports: ContextVar[bool] = ContextVar("rules_imports", default=False)


@contextlib.contextmanager
def batch(allow_imports: bool = False):
    t1, t2 = _deferred.set([]), _imports.set(allow_imports)
    try:
        yield
    finally:
        _deferred.reset(t1)
        _imports.reset(t2)


def finish(cur) -> None:
    """Run the deferred set-based checks of the current batch (call after all rows are written)."""
    items = _deferred.get() or []
    for parking in sorted({v for k, v in items if k == "parking"}):
        _check_parking(cur, parking)
    for ref in sorted({v for k, v in items if k == "incident"}):
        if not _incident_exists(cur, ref):
            raise ApiError(422, f"Incident '{ref}' does not exist.")
    for alloc in sorted({v for k, v in items if k == "allocation"}):
        cur.execute('SELECT resource_type, resource_id, employer_id, window_start_minutes AS s, '
                    'window_end_minutes AS e, active FROM "fleet_allocations" WHERE id = %s', (alloc,))
        r = cur.fetchone()
        if r and r["active"]:
            _check_overlap(cur, alloc, r["resource_type"], r["resource_id"], r["employer_id"], r["s"], r["e"])

TRIP_ALLOWED: dict[str, set[str]] = {
    "draft": {"planned", "assigned", "cancelled"},
    "planned": {"assigned", "cancelled", "draft"},
    "assigned": {"driverAccepted", "ready", "inProgress", "planned", "cancelled",
                 "needsAttention", "assigned"},
    "driverAccepted": {"ready", "inProgress", "planned", "assigned", "cancelled", "needsAttention"},
    "ready": {"inProgress", "assigned", "planned", "cancelled", "needsAttention"},
    "inProgress": {"delayed", "completed", "needsAttention"},
    "delayed": {"inProgress", "completed", "needsAttention"},
    "needsAttention": {"inProgress", "delayed", "assigned", "planned", "completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
INITIAL_TRIP_STATUSES = {"draft", "planned", "assigned"}
NEEDS_DRIVER_AND_VEHICLE = {"assigned", "driverAccepted", "ready", "inProgress"}


def can_transition(frm: str, to: str) -> bool:
    return frm == to or to in TRIP_ALLOWED.get(frm, set())


def _segments(start: int, end: int) -> list[tuple[int, int]]:
    """Minutes-after-midnight window -> segments; end <= start means overnight."""
    if end > start:
        return [(start, end)]
    return [(start, 1440), (0, end)]


def windows_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return any(s1 < e2 and s2 < e1 for s1, e1 in _segments(*a) for s2, e2 in _segments(*b))


def before_write(cur, e: Entity, old: dict | None, id_: str, cols: dict, lists: dict,
                 actor: str) -> None:
    fn = _BEFORE.get(e.table)
    if fn:
        fn(cur, old, id_, cols, lists)


def after_write(cur, e: Entity, old: dict | None, id_: str, cols: dict, actor: str) -> None:
    if e.table == "trips":
        new_status = cols.get("status", old["status"] if old else "draft")
        old_status = old["status"] if old else None
        if old is None or new_status != old_status:
            cur.execute(
                'INSERT INTO "trip_status_history"(trip_id, from_status, to_status, changed_by) '
                "VALUES (%s,%s,%s,%s)", (id_, old_status, new_status, actor))


# ------------------------------------------------------------------ trips
def _trips(cur, old, id_, cols, lists):
    status = cols.get("status", old["status"] if old else "draft")
    if old is None:
        if status not in INITIAL_TRIP_STATUSES and not _imports.get():
            raise ApiError(422, f"A new trip must start as one of {sorted(INITIAL_TRIP_STATUSES)}.")
    elif not can_transition(old["status"], status):
        raise ApiError(409, f"Trip status cannot change from '{old['status']}' to '{status}'.",
                       {"allowed": sorted(TRIP_ALLOWED[old["status"]])})
    driver = cols.get("assigned_driver_id", old["assigned_driver_id"] if old else None)
    vehicle = cols.get("assigned_vehicle_id", old["assigned_vehicle_id"] if old else None)
    if status in NEEDS_DRIVER_AND_VEHICLE and (not driver or not vehicle):
        raise ApiError(422, f"A trip in status '{status}' needs both a driver and a vehicle.")
    if status == "cancelled":
        reason = cols.get("cancellation_reason", old["cancellation_reason"] if old else None)
        if not reason or not str(reason).strip():
            raise ApiError(422, "cancellationReason is required when cancelling a trip.")
    for res, table, label in ((vehicle, "vehicles", "vehicle"), (driver, "drivers", "driver")):
        if res and (old is None or res != old.get(f"assigned_{label}_id") or "status" in cols):
            cur.execute(f'SELECT active FROM "{table}" WHERE id = %s', (res,))
            r = cur.fetchone()
            if r and not r["active"] and status in NEEDS_DRIVER_AND_VEHICLE:
                raise ApiError(409, f"The {label} '{res}' is inactive and cannot be assigned.")


# ------------------------------------------------------------------ vehicles
def _vehicles(cur, old, id_, cols, lists):
    parking = cols.get("home_parking_id", old["home_parking_id"] if old else None)
    if parking:
        cur.execute('SELECT type, capacity FROM "fleet_workspace"."locations" WHERE id = %s', (parking,))
        loc = cur.fetchone()
        if loc:
            if loc["type"] not in ("parking", "depot"):
                raise ApiError(422, "homeParkingId must reference a parking or depot location.")
            deferred = _deferred.get()
            if deferred is not None:
                deferred.append(("parking", parking))
            elif loc["capacity"] is not None:
                cur.execute('SELECT count(*) AS n FROM "vehicles" WHERE home_parking_id = %s '
                            "AND id <> %s", (parking, id_))
                if cur.fetchone()["n"] + 1 > loc["capacity"]:
                    raise ApiError(409, f"Parking '{parking}' is full (capacity {loc['capacity']}).")


def _check_parking(cur, parking: str) -> None:
    cur.execute('SELECT capacity FROM "fleet_workspace"."locations" WHERE id = %s', (parking,))
    loc = cur.fetchone()
    if loc and loc["capacity"] is not None:
        cur.execute('SELECT count(*) AS n FROM "vehicles" WHERE home_parking_id = %s', (parking,))
        if cur.fetchone()["n"] > loc["capacity"]:
            raise ApiError(409, f"Parking '{parking}' is full (capacity {loc['capacity']}).")


# ------------------------------------------------------------------ allocations
def _allocations(cur, old, id_, cols, lists):
    g = lambda k: cols.get(k, old[k] if old else None)  # noqa: E731
    rtype, rid, emp = g("resource_type"), g("resource_id"), g("employer_id")
    start, end, active = g("window_start_minutes"), g("window_end_minutes"), g("active")
    cur.execute(f'SELECT 1 FROM "{"vehicles" if rtype == "vehicle" else "drivers"}" WHERE id = %s', (rid,))
    if not cur.fetchone():
        raise ApiError(422, f"{rtype} '{rid}' does not exist.")
    cur.execute('SELECT 1 FROM "fleet_workspace"."companies" WHERE code = %s', (emp,))
    if not cur.fetchone():
        raise ApiError(422, f"Company '{emp}' is not registered.")
    if not active:
        return
    deferred = _deferred.get()
    if deferred is not None:
        deferred.append(("allocation", id_))
        return
    _check_overlap(cur, id_, rtype, rid, emp, start, end)


def _check_overlap(cur, id_, rtype, rid, emp, start, end) -> None:
    cur.execute('SELECT id, employer_id, window_start_minutes AS s, window_end_minutes AS e '
                'FROM "fleet_allocations" WHERE resource_type=%s AND resource_id=%s AND active '
                "AND employer_id <> %s AND id <> %s", (rtype, rid, emp, id_))
    for r in cur.fetchall():
        if windows_overlap((start, end), (r["s"], r["e"])):
            raise ApiError(409, f"Window overlaps allocation {r['id']} of employer {r['employer_id']}.")


# ------------------------------------------------------------------ incident references
# Fleet migration 0004 made these references soft (the incident may be recorded in a company schema or
# in the fleet schema); this check keeps them valid: the incident must exist in one of the two.
INCIDENT_REFS = {"inspections": "defect_incident_id", "alcohol_tests": "incident_id",
                 "vehicle_events": "incident_id"}


def _incident_exists(cur, incident_id: str) -> bool:
    cur.execute('SELECT 1 FROM "incidents" WHERE id = %s UNION ALL '
                'SELECT 1 FROM "fleet_workspace"."incidents" WHERE id = %s LIMIT 1', (incident_id, incident_id))
    return cur.fetchone() is not None


def _incident_ref(table):
    col = INCIDENT_REFS[table]

    def check(cur, old, id_, cols, lists):
        ref = cols.get(col, old[col] if old else None)
        if not ref:
            return
        deferred = _deferred.get()
        if deferred is not None:
            deferred.append(("incident", ref))      # the incident may be created later in the same batch
        elif not _incident_exists(cur, ref):
            raise ApiError(422, f"Incident '{ref}' does not exist.")
    return check


_BEFORE = {"trips": _trips, "vehicles": _vehicles, "fleet_allocations": _allocations,
           **{t: _incident_ref(t) for t in INCIDENT_REFS}}
