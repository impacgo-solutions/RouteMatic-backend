"""Row ownership for drivers and employees (and the employer admin's post-write scope).

The app runs its business rules on the device and mirrors the resulting rows. Role permissions (rbac.py)
say WHICH tables a role may write; this module says WHICH ROWS:

* a driver touches only its own trips and what hangs off them (route, stops, bookings, billing line,
  passenger events), its own shifts / inspections / alcohol tests, the operational fields of the vehicle
  it drives and its own driver row (never its identity);
* an employee touches only its own profile / bookings / settings and the trips it rides in (no
  re-assigning drivers or vehicles; resource rows only change status).

A batch is judged against what belonged to the person BEFORE it ("before" sets, taken first) and the
final state after it, so multi-row operations (leave a trip -> its route is rebuilt) are accepted while
rows of other people are refused.
"""
from __future__ import annotations

from typing import Any

from . import repo

# role -> resources it may delete (rbac.DELETE_ROLES may delete anything they can write).
# Re-planning a trip replaces its route and stops.
EXTRA_DELETES = {
    "driver": set(),
    "employee": {"routes", "route_stops"},
    "manager": {"routes", "route_stops"},
}

VEHICLE_OPS = {"status", "odometerKm", "batteryOrFuelPercent", "lastInspectionAt", "diagnosticCodes", "notes",
               "updatedAt"}
DRIVER_IDENTITY = {"name", "driverCode", "licenceNumber", "licenceClass", "licenceExpiry", "email", "address",
                   "gender", "active", "fictionalContact",
                   # the vehicle a driver is assigned to is a dispatch decision (it decides which vehicle,
                   # trips and passengers the driver may then reach)
                   "assignedVehicleId"}
EMPLOYEE_FIXED = {"employerId", "employeeCode", "status"}
TRIP_ASSIGNMENT = {"assignedDriverId", "assignedVehicleId", "employerId"}
# fields only the driver / dispatch set while a trip runs (never a passenger)
TRIP_RUN_FIELDS = {"billableStatus", "actualStart", "actualArrival", "startOdometerKm", "endOdometerKm",
                   "completionNotes", "completionException", "inspectionId"}
# a driver records boarding / drop / no-show, but never changes who is booked on the trip
DRIVER_FIXED_PASSENGERS = ("employeeIds", "absentEmployeeIds")
# changes an incident's other parties may make (a driver asks for a replacement vehicle)
INCIDENT_DRIVER_UPDATE = {"replacementRequested", "updatedAt"}
TRIP_PASSENGER_LISTS = ("employeeIds", "boardedPassengerIds", "alightedPassengerIds", "noShowPassengerIds",
                        "absentEmployeeIds")
# company profile fields drivers / employees do not need (rates and trip settings stay readable)
EMPLOYER_PRIVATE = {"contactName": "", "contactEmail": "", "address": ""}

ROLES = ("driver", "employee")


def applies(ctx) -> bool:
    return ctx.role in ROLES and ctx.kind == "company"


def personal_in_fleet(ctx) -> bool:
    """A driver / employee membership of the FLEET workspace has no personal rows to work on there
    (row ownership lives in company workspaces): such a login reads and writes nothing."""
    return ctx.role in ROLES and ctx.kind == "fleet"


# roles whose written rows are field-checked (fleet admin / system are trusted with every field)
FIELD_CHECKED = ("employerAdmin", "manager", "driver", "employee")


def snapshot(cur, e, id_: str) -> dict | None:
    try:
        return repo.get(cur, e, id_)
    except repo.ApiError:
        return None


def _ids(cur, sql: str, params: list) -> set:
    cur.execute(sql, params)
    return {r["id"] if isinstance(r, dict) else r[0] for r in cur.fetchall()} - {None}


def _trips_sql(role: str) -> str:
    if role == "driver":
        return 'SELECT id FROM "trips" WHERE assigned_driver_id = %s'
    return 'SELECT trip_id AS id FROM "trip_passengers" WHERE employee_id = %s'


def mine(cur, ctx) -> dict[str, set]:
    """What belongs to the person in the CURRENT state: trips and the routes / vehicles / drivers serving them."""
    me = ctx.scope_id or ""
    trips = _ids(cur, _trips_sql(ctx.role), [me])
    t = list(trips) or [""]
    out = {
        "trips": trips,
        "routes": _ids(cur, 'SELECT route_id AS id FROM "trips" WHERE id = ANY(%s)', [t]),
        "vehicles": _ids(cur, 'SELECT assigned_vehicle_id AS id FROM "trips" WHERE id = ANY(%s)', [t]),
        "drivers": _ids(cur, 'SELECT assigned_driver_id AS id FROM "trips" WHERE id = ANY(%s)', [t]),
    }
    if ctx.role == "driver":
        out["drivers"].add(me)
        out["vehicles"] |= _ids(cur, 'SELECT id FROM "vehicles" WHERE assigned_driver_id = %s', [me])
        out["vehicles"] |= _ids(cur, 'SELECT assigned_vehicle_id AS id FROM "drivers" WHERE id = %s', [me])
    out["incidents"] = _ids(cur, 'SELECT id FROM "incidents" WHERE reported_by_id = %s OR driver_id = %s', [me, me])
    return out


class Batch:
    """Ownership checks for one push / REST write."""

    def __init__(self, cur, ctx):
        self.cur, self.ctx = cur, ctx
        if personal_in_fleet(ctx):
            raise repo.ApiError(403, "This login has no records to change in the Fleet workspace.")
        self.on = applies(ctx)
        self.fields = ctx.role in FIELD_CHECKED
        self.before = mine(cur, ctx) if self.on else {}
        self.pending: list[tuple] = []

    def _fn(self):
        return _ok_driver if self.ctx.role == "driver" else _ok_employee

    def note(self, e, id_: str) -> None:
        """Call BEFORE applying a change to (e, id_): an existing row must already be the person's."""
        if not (self.on or self.fields):
            return
        old = snapshot(self.cur, e, id_)
        if not self.on:
            self.pending.append((e, id_, old))
            return
        if old is not None and not self._fn()(self.ctx.scope_id or "", e.table, old, old, None, self.before):
            raise repo.ApiError(403, f"You may not change {e.table} '{id_}'.")
        self.pending.append((e, id_, old))

    def verify(self) -> None:
        """Call AFTER all changes are applied (same transaction)."""
        if self.fields:
            for e, id_, old in self.pending:
                if old is not None:
                    new = snapshot(self.cur, e, id_)
                    if new is not None:
                        field_rules(self.ctx, e.table, old, new)
        if not self.on:
            return
        after = mine(self.cur, self.ctx)
        owned = {k: self.before.get(k, set()) | after.get(k, set()) for k in after}
        # Routes: only those the person had before, or created in THIS batch (a re-planned trip gets a new
        # route). Pointing an own trip at somebody else's existing route must not make that route "owned".
        created_routes = {i for e, i, old in self.pending if e.table == "routes" and old is None}
        before_routes = set(self.before.get("routes", set()))
        owned["routes"] = before_routes | (after.get("routes", set()) & created_routes)
        # trips that were the person's before the batch keep their (possibly rebuilt) route / resources
        t = list(self.before.get("trips", set())) or [""]
        self.cur.execute('SELECT route_id, assigned_vehicle_id, assigned_driver_id FROM "trips" '
                         "WHERE id = ANY(%s)", [t])
        for r in self.cur.fetchall():
            if r["route_id"] in created_routes or r["route_id"] in before_routes:
                owned["routes"].add(r["route_id"])
            owned["vehicles"].add(r["assigned_vehicle_id"])
            owned["drivers"].add(r["assigned_driver_id"])
        for e, id_, old in self.pending:
            new = snapshot(self.cur, e, id_)
            fn = self._fn()
            me = self.ctx.scope_id or ""
            if old is not None and not fn(me, e.table, old, old, new, owned):
                raise repo.ApiError(403, f"You may not change {e.table} '{id_}'.")
            if new is not None and not fn(me, e.table, new, old, new, owned):
                raise repo.ApiError(403, f"You may not change {e.table} '{id_}'.")


def _changed(old: dict | None, new: dict | None) -> set[str]:
    if old is None or new is None:
        return set()
    return {k for k in set(old) | set(new) if old.get(k) != new.get(k)}


def _own_side_rows(table: str, me: str, role: str, row: dict, old: dict | None) -> bool | None:
    """Rows any member writes about themselves. None = not handled here."""
    if table == "user_settings":
        return row.get("id") == f"{role}:{me}"
    if table == "support_tickets":
        return row.get("createdById") == me
    if table == "audit_events":
        return old is None          # append-only: a member adds audit entries, never edits them
    if table == "notifications":
        # the app notifies other people (new rows); existing rows: only the member's own (mark read)
        return old is None or (old.get("recipientRole") == role and old.get("recipientId") == me)
    return None


def _ok_driver(d: str, table: str, row: dict, old, new, owned: dict) -> bool:
    own = _own_side_rows(table, d, "driver", row, old)
    if own is not None:
        return own
    if table == "trips":
        # a driver runs trips dispatch gave it; it never creates one or changes who rides
        if old is None:
            return False
        ch = _changed(old, new)
        if ch & TRIP_ASSIGNMENT or ch & set(DRIVER_FIXED_PASSENGERS):
            return False
        if "routeId" in ch and (new or {}).get("routeId") not in owned["routes"] | {None}:
            return False
        return row.get("assignedDriverId") == d
    if table == "routes":
        return row["id"] in owned["routes"]
    if table == "route_stops":
        return row.get("routeId") in owned["routes"]
    if table == "billing_records":
        # the trip-completion line is created once; its amounts are never edited by the driver afterwards
        return row.get("tripId") in owned["trips"] and (old is None or not _changed(old, new))
    if table in ("bookings", "passenger_events"):
        return row.get("tripId") in owned["trips"]
    if table in ("shifts", "inspections", "alcohol_tests"):
        return row.get("driverId") == d
    if table == "vehicle_events":
        return row.get("driverId") == d or row.get("vehicleId") in owned["vehicles"]
    if table == "drivers":
        return row["id"] == d and not (_changed(old, new) & DRIVER_IDENTITY)
    if table == "vehicles":
        return row["id"] in owned["vehicles"] and not (_changed(old, new) - VEHICLE_OPS)
    if table == "incidents":
        if row.get("reportedById") == d:
            return True
        # an incident about the driver filed by someone else: the driver may only ask for a replacement
        # vehicle, never edit, re-status or resolve it
        return (row.get("driverId") == d and old is not None
                and not (_changed(old, new) - INCIDENT_DRIVER_UPDATE))
    if table == "sos_dispatches":
        return row.get("incidentId") in owned["incidents"]
    return False


def _ok_employee(emp: str, table: str, row: dict, old, new, owned: dict) -> bool:
    own = _own_side_rows(table, emp, "employee", row, old)
    if own is not None:
        return own
    if table == "employees":
        return row["id"] == emp and not (_changed(old, new) & EMPLOYEE_FIXED)
    if table in ("bookings", "passenger_events"):
        return row.get("employeeId") == emp
    if table == "trips":
        # employees join / leave trips dispatch created; they never create one (with any passengers,
        # driver or vehicle of their choosing)
        if old is None:
            return False
        rides = emp in ((old or {}).get("employeeIds") or []) or emp in ((new or {}).get("employeeIds") or [])
        # passenger lists: only the employee's own entry may be added / removed
        others_changed = any(
            (set((old or {}).get(k) or []) ^ set((new or {}).get(k) or [])) - {emp}
            for k in TRIP_PASSENGER_LISTS) if new is not None else False
        ch = _changed(old, new)
        if "routeId" in ch and (new or {}).get("routeId") not in owned["routes"] | {None}:
            return False
        return rides and not others_changed and not (ch & TRIP_ASSIGNMENT) and not (ch & TRIP_RUN_FIELDS)
    if table == "routes":
        return row["id"] in owned["routes"]
    if table == "route_stops":
        return row.get("routeId") in owned["routes"]
    if table == "vehicles":
        return row["id"] in owned["vehicles"] and not (_changed(old, new) - {"status", "updatedAt"})
    if table == "drivers":
        return row["id"] in owned["drivers"] and not (_changed(old, new) - {"status", "updatedAt"})
    if table == "incidents":
        return row.get("reportedById") == emp
    if table == "sos_dispatches":
        return row.get("incidentId") in owned["incidents"]
    return False


# records an employer admin may write but never delete (the audit trail and money records)
NO_DELETE = {"employerAdmin": {"audit_events", "billing_records", "invoices"}}


def may_delete(ctx, resource: str) -> bool:
    from .rbac import DELETE_ROLES
    if resource in NO_DELETE.get(ctx.role, set()):
        return False
    return ctx.role in DELETE_ROLES or resource in EXTRA_DELETES.get(ctx.role, set())


# ------------------------------------------------------------------ field rules (every non-fleet-admin role)
INVOICE_NEXT = {"generated": {"reviewed", "disputed"}, "reviewed": {"paidDemo", "disputed"},
                "disputed": {"reviewed"}, "paidDemo": set()}
BILLING_NEXT = {"draft": {"generated"}, "generated": {"reviewed", "disputed"},
                "reviewed": {"paidDemo", "disputed"}, "disputed": {"reviewed"}, "paidDemo": set()}
# after an invoice / billing line exists, only its review state changes (amounts are fixed when generated)
INVOICE_REVIEW_FIELDS = {"status", "disputeReason", "reviewedAt", "notes"}
BILLING_REVIEW_FIELDS = {"status", "disputeReason", "reviewedAt", "invoiceId"}


def field_rules(ctx, table: str, old: dict, new: dict) -> None:
    """Updates of existing rows by roles other than the Fleet Admin: the audit trail is append-only,
    issued money records only move through their review states."""
    ch = _changed(old, new)
    if not ch:
        return
    if table == "audit_events":
        raise repo.ApiError(403, "Audit entries cannot be changed.")
    if table == "support_tickets" and ch & {"createdById", "createdByRole"}:
        raise repo.ApiError(403, "The author of a ticket cannot be changed.")
    if table == "incidents" and ch & {"reportedById", "reportedByRole"}:
        raise repo.ApiError(403, "The reporter of an incident cannot be changed.")
    if ctx.role == "employerAdmin" and table == "invoices":
        if ch - INVOICE_REVIEW_FIELDS:
            raise repo.ApiError(403, "An issued invoice's amounts cannot be changed; dispute it instead.")
        if "status" in ch and new.get("status") not in INVOICE_NEXT.get(old.get("status"), set()):
            raise repo.ApiError(403, f"An invoice cannot move from {old.get('status')} to {new.get('status')}.")
    if ctx.role == "employerAdmin" and table == "billing_records":
        if ch - BILLING_REVIEW_FIELDS or ("invoiceId" in ch and old.get("invoiceId")):
            raise repo.ApiError(403, "A billing line's amounts cannot be changed; dispute it instead.")
        if "status" in ch and new.get("status") not in BILLING_NEXT.get(old.get("status"), set()):
            raise repo.ApiError(403, f"A billing line cannot move from {old.get('status')} to {new.get('status')}.")


def stamp(ctx, table: str, data: dict) -> None:
    """Who-did-it fields of NEW rows come from the signed-in person, not from the request
    (an employee cannot file an audit entry, ticket or incident in someone else's name)."""
    if ctx.role in ("fleetManager", "system"):
        return
    me = ctx.scope_id if ctx.role in ROLES else None
    # the stored role uses the app's UserRole values: a Fleet Manager ("manager") is fleet staff
    role = "fleetManager" if ctx.role == "manager" else ctx.role
    if table == "audit_events":
        data["actorRole"] = role
        if me:
            data["actorId"] = me
    elif table == "support_tickets" and ctx.role in ROLES:
        data["createdByRole"] = ctx.role
        data["createdById"] = me
    elif table == "incidents" and ctx.role in ROLES:
        data["reportedByRole"] = ctx.role
        data["reportedById"] = me


def employer_post_check(cur, ctx, e, id_: str, scope) -> None:
    """Employer admins: the written row must still be inside their company (new FK values included)."""
    if ctx.role != "employerAdmin" or scope is None or isinstance(scope, tuple):
        return
    try:
        repo.get(cur, e, id_, scope)
    except repo.ApiError:
        raise repo.ApiError(403, "Employer administrators may only write their own employer's records.")


# company admins: fleet shift / inspection records only of drivers serving their company
# (drivers with a login in this company; trip assignments are not used, an admin could otherwise assign any
# driver to a draft trip and then read that driver's shifts and inspections)
_SERVING_DRIVERS = ('"driver_id" IN (SELECT m.scope_id FROM public.tenant_memberships m JOIN public.tenants t '
                    "ON t.id = m.tenant_id WHERE t.slug = %s AND m.role = 'driver' AND m.active)")
_OWN_TRIPS = {"driver": 'SELECT id FROM "trips" WHERE assigned_driver_id = %s',
              "employee": 'SELECT trip_id FROM "trip_passengers" WHERE employee_id = %s'}
_NOTHING = ("sql", "FALSE", [])


def read_scope(ctx, e):
    """Extra row filter for personal data. None = no extra filter."""
    if ctx.role == "employerAdmin" and ctx.kind == "company" and e.table in ("shifts", "inspections"):
        return ("sql", _SERVING_DRIVERS, [ctx.slug])
    if personal_in_fleet(ctx):
        return _NOTHING
    if not applies(ctx):
        return None
    me = ctx.scope_id or ""
    t = e.table
    if t == "user_settings":
        return ("sql", '"id" = %s', [f"{ctx.role}:{me}"])
    if t == "notifications":
        # addressed to me, or to everyone in my role
        return ("sql", '"recipient_role" = %s AND ("recipient_id" = %s OR "recipient_id" IS NULL)',
                [ctx.role, me])
    if t == "support_tickets":
        return ("sql", '"created_by_id" = %s', [me])
    own_incidents = ('("reported_by_id" = %s OR "trip_id" IN (' + _OWN_TRIPS[ctx.role] + ')'
                     + (' OR "driver_id" = %s' if ctx.role == "driver" else "") + ")")
    inc_params = [me, me] + ([me] if ctx.role == "driver" else [])
    if t == "incidents":
        return ("sql", own_incidents, inc_params)
    if t == "sos_dispatches":
        return ("sql", '"incident_id" IN (SELECT id FROM "incidents" WHERE ' + own_incidents + ")", inc_params)
    if ctx.role == "driver":
        if t in ("shifts", "inspections", "alcohol_tests"):
            return ("sql", '"driver_id" = %s', [me])
        if t in ("bookings", "passenger_events"):
            return ("sql", '"trip_id" IN (SELECT id FROM "trips" WHERE assigned_driver_id = %s)', [me])
        if t == "trips":
            # its own trips, plus trips of the vehicle it drives (the app checks "vehicle busy on another trip");
            # other drivers' trips (and their passengers) are not the driver's business
            return ("sql", '("assigned_driver_id" = %s OR "assigned_vehicle_id" IN ('
                           'SELECT id FROM "vehicles" WHERE assigned_driver_id = %s '
                           'UNION SELECT assigned_vehicle_id FROM "drivers" WHERE id = %s))', [me, me, me])
        if t == "vehicle_events":
            return ("sql", '("driver_id" = %s OR "vehicle_id" IN (SELECT id FROM "vehicles" WHERE '
                           'assigned_driver_id = %s UNION SELECT assigned_vehicle_id FROM "drivers" WHERE id = %s))',
                    [me, me, me])
    if ctx.role == "employee" and t in ("bookings", "passenger_events"):
        return ("sql", '"employee_id" = %s', [me])
    return None


# ------------------------------------------------------------------ field-level privacy
EMPLOYEE_PRIVATE = {"fictionalPhone": "", "fictionalEmail": "", "notes": "", "gender": "notSpecified"}
# Licence number is MASKED (not blanked): the apps re-check that an assigned driver holds a licence, and the
# expiry date stays visible because assignment rules use it.
DRIVER_PRIVATE = {"licenceNumber": "", "address": "", "email": "", "gender": "notSpecified", "notes": ""}
# a driver's phone: visible to the company admin and to the passengers of that driver's trips only
DRIVER_CONTACT = "fictionalContact"


def _mask(v):
    s = str(v or "")
    return ("••••" + s[-3:]) if s else ""


def private_fields(ctx, e) -> set[str]:
    """Fields this role may not see on others' rows (and so may not filter / search on)."""
    if ctx.kind != "company":
        return set()
    if e.table == "employees" and ctx.role in ROLES:
        return set(EMPLOYEE_PRIVATE)
    if e.table == "drivers" and ctx.role in ("employee", "driver", "employerAdmin"):
        return set(DRIVER_PRIVATE) | ({DRIVER_CONTACT} if ctx.role in ROLES else set())
    if e.table == "employers" and ctx.role in ROLES:
        return set(EMPLOYER_PRIVATE)
    return set()


def redact(cur, ctx, e, items: list) -> list:
    """Hide other people's personal fields from roles that only need their names / routing data.

    * employees / drivers do not see other employees' phone, email, notes, gender
      (a driver keeps the phone of the passengers of its own trips);
    * employees, other drivers and company admins do not see a driver's licence, address or email.
    Routing fields (pickup / destination / shift) stay, the employee apps need them to plan shared trips.
    """
    if ctx.kind != "company" or not items:
        return items
    me = ctx.scope_id or ""
    if e.table == "employees" and ctx.role in ROLES:
        mine = {me} if ctx.role == "employee" else set()
        keep_phone = set()
        if ctx.role == "driver":
            # passengers of the driver's own trips, unless they switched "show my phone to the driver" off
            cur.execute('SELECT DISTINCT p.employee_id FROM "trip_passengers" p JOIN "trips" t ON t.id = p.trip_id '
                        'WHERE t.assigned_driver_id = %s AND NOT EXISTS (SELECT 1 FROM "user_settings" s '
                        "WHERE s.id = 'employee:' || p.employee_id AND s.show_phone_to_driver = FALSE)", (me,))
            keep_phone = {r["employee_id"] for r in cur.fetchall()}
        for it in items:
            if it["id"] in mine:
                continue
            for k, v in EMPLOYEE_PRIVATE.items():
                if k == "fictionalPhone" and it["id"] in keep_phone:
                    continue
                if k in it:
                    it[k] = v
    elif e.table == "employers" and ctx.role in ROLES:
        for it in items:
            for k, v in EMPLOYER_PRIVATE.items():
                if k in it:
                    it[k] = v
    elif e.table == "drivers" and ctx.role in ("employee", "driver", "employerAdmin"):
        my_drivers: set = set()
        if ctx.role == "employee":
            # the employee sees the phone of the drivers of the trips it rides in
            cur.execute('SELECT DISTINCT t.assigned_driver_id FROM "trips" t JOIN "trip_passengers" p '
                        "ON p.trip_id = t.id WHERE p.employee_id = %s", (me,))
            my_drivers = {r["assigned_driver_id"] for r in cur.fetchall()}
        for it in items:
            if ctx.role == "driver" and it["id"] == me:
                continue
            for k, v in DRIVER_PRIVATE.items():
                if k in it:
                    it[k] = _mask(it[k]) if k == "licenceNumber" else v
            if ctx.role in ROLES and it["id"] not in my_drivers and DRIVER_CONTACT in it:
                it[DRIVER_CONTACT] = ""
    return items
