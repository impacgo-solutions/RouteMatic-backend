"""Security regression tests (authorization): each test shows the exploit is refused AND the legitimate
flow still works. Uses its own company (delta_corp) so other tests are unaffected."""
from app import db

from .conftest import BASE, login, make_user

PW = "Passw0rd!x"
T = "delta_corp"


def push(client, h, changes, tenant=T):
    return client.post(f"{BASE}/{tenant}/sync/push", headers=h, json={"changes": changes})


def put(resource, id_, data):
    return {"resource": resource, "op": "put", "id": id_, "data": data}


def dele(resource, id_):
    return {"resource": resource, "op": "delete", "id": id_}


def get(client, h, res, id_, tenant=T):
    return client.get(f"{BASE}/{tenant}/{res}/{id_}", headers=h)


def pull(client, h, tenant=T):
    r = client.get(f"{BASE}/{tenant}/sync/pull", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def setup_delta(client, world):
    if getattr(setup_delta, "done", None):
        return setup_delta.done
    fm = world["fm"]
    r = client.post(f"{BASE}/fleet/companies", headers=fm, json={
        "slug": T, "employer": {"code": "DELTA", "name": "Delta Corp"},
        "admin": {"email": "ea.delta@example.com", "displayName": "Delta Admin", "password": PW}})
    assert r.status_code == 201, r.text
    p = lambda res, body: client.post(f"{BASE}/{T}/{res}", headers=fm, json=body)  # noqa: E731
    for i, typ in (("D-PK", "pickup"), ("D-OF", "office")):
        assert p("locations", {"id": i, "code": i, "name": i, "type": typ, "employerId": "DELTA"}).status_code == 201
    for e in ("DE1", "DE2"):
        assert p("employees", {"id": e, "employeeCode": e, "employerId": "DELTA", "name": e,
                               "fictionalPhone": f"+91 90000 0000{e[-1]}",
                               "pickupLocationId": "D-PK", "destinationId": "D-OF"}).status_code == 201
    for d, v in (("DD1", "DV1"), ("DD2", "DV2"), ("DD9", "DV9")):
        assert client.post(f"{BASE}/fleet/drivers", headers=fm, json={
            "id": d, "driverCode": d, "name": d, "licenceNumber": f"LIC-{d}",
            "fictionalContact": f"+91 80000 000{d[-1]}"}).status_code == 201
        assert client.post(f"{BASE}/fleet/vehicles", headers=fm, json={
            "id": v, "vehicleCode": v, "fictionalRegistration": f"KA-{v}", "totalSeats": 4}).status_code == 201
    for t, d, v, e in (("DT1", "DD1", "DV1", "DE1"), ("DT2", "DD2", "DV2", "DE2")):
        assert p("routes", {"id": f"R-{t}", "routeCode": f"R{t}", "name": t, "employerId": "DELTA"}).status_code == 201
        assert p("route_stops", {"id": f"S-{t}", "routeId": f"R-{t}", "sequence": 1, "locationId": "D-PK",
                                 "kind": "pickup"}).status_code == 201
        r = p("trips", {"id": t, "tripCode": t, "employerId": "DELTA", "serviceDate": "2026-10-20T00:00:00Z",
                        "scheduledStart": "2026-10-20T07:30:00Z", "originLocationId": "D-PK",
                        "destinationLocationId": "D-OF", "routeId": f"R-{t}", "assignedDriverId": d,
                        "assignedVehicleId": v, "employeeIds": [e], "status": "assigned"})
        assert r.status_code == 201, r.text
    for who, role, scope in (("de1", "employee", "DE1"), ("de2", "employee", "DE2"),
                             ("dd1", "driver", "DD1"), ("dd2", "driver", "DD2")):
        make_user(f"{who}@example.com", role, T, scope=scope)
    setup_delta.done = {k: login(client, f"{k}@example.com") for k in ("de1", "de2", "dd1", "dd2")}
    setup_delta.done["ea"] = login(client, "ea.delta@example.com")
    return setup_delta.done


# --------------------------------------------------------------------------- finding 1: account takeover
def test_admin_of_another_company_cannot_take_over_an_employee_login(client, world):
    """victim.alpha@example.com is employee E1 of alpha_corp. Beta's admin creates its own E1 (employee ids are
    per company, so they collide) and tries to attach + reset that account."""
    make_user("victim.alpha@example.com", "employee", "alpha_corp", scope="E1")
    ea_b = world["ea_b"]
    for i, typ in (("SB-PK", "pickup"), ("SB-OF", "office")):
        r = client.post(f"{BASE}/beta_corp/locations", headers=world["fm"],
                        json={"id": i, "code": i, "name": i, "type": typ, "employerId": "BETA"})
        assert r.status_code in (201, 409), r.text
    r = client.post(f"{BASE}/beta_corp/employees", headers=ea_b,
                    json={"id": "E1", "employeeCode": "E1", "employerId": "BETA", "name": "Colliding E1",
                          "pickupLocationId": "SB-PK", "destinationId": "SB-OF"})
    assert r.status_code in (201, 409), r.text
    url = f"{BASE}/beta_corp/employees/E1/login"
    client.put(url, headers=ea_b, json={"email": "victim.alpha@example.com", "password": "Attack3r-pass"})
    r = client.put(url, headers=ea_b, json={"email": "victim.alpha@example.com", "password": "Attack3r-pass"})
    assert r.status_code == 409, r.text
    # the victim still signs in with its own password; the attacker's password does not work
    assert client.post("/api/v1/auth/login", json={"email": "victim.alpha@example.com",
                                                   "password": PW}).status_code == 200
    assert client.post("/api/v1/auth/login", json={"email": "victim.alpha@example.com",
                                                   "password": "Attack3r-pass"}).status_code == 401


def test_admin_can_still_reset_a_login_that_belongs_only_to_its_company(client, world):
    u = setup_delta(client, world)
    ea = u["ea"]
    r = client.post(f"{BASE}/{T}/employees", headers=ea, json={
        "id": "DE3", "employeeCode": "DE3", "employerId": "DELTA", "name": "DE3",
        "pickupLocationId": "D-PK", "destinationId": "D-OF"})
    assert r.status_code == 201, r.text
    url = f"{BASE}/{T}/employees/DE3/login"
    r = client.put(url, headers=ea, json={"email": "de3.own@example.com", "password": "Own-pass-123"})
    assert r.status_code == 200, r.text
    r = client.put(url, headers=ea, json={"email": "de3.own@example.com", "password": "Own-pass-456"})
    assert r.status_code == 200, r.text
    assert client.post("/api/v1/auth/login", json={"email": "de3.own@example.com",
                                                   "password": "Own-pass-456"}).status_code == 200


# --------------------------------------------------------------------------- finding 2: ownership widening
def test_driver_cannot_create_trips_or_change_passengers_or_its_vehicle(client, world):
    u = setup_delta(client, world)
    dd1, fm = u["dd1"], world["fm"]
    t1 = get(client, fm, "trips", "DT1").json()
    # creating a trip with every employee on it (phone harvesting): refused
    new = {k: v for k, v in t1.items() if k != "id"} | {"tripCode": "DTX", "employeeIds": ["DE1", "DE2"],
                                                         "status": "assigned"}
    assert push(client, dd1, [put("trips", "DTX", new)]).status_code == 403
    # adding a passenger to its own trip: refused
    assert push(client, dd1, [put("trips", "DT1", {**t1, "employeeIds": ["DE1", "DE2"]})]).status_code == 403
    # pointing its own driver row at another vehicle: refused
    d1 = get(client, fm, "drivers", "DD1", tenant="fleet").json()
    assert push(client, dd1, [put("drivers", "DD1", {**d1, "assignedVehicleId": "DV2"})]).status_code == 403
    # legitimate: running its own trip (status, boarding) and its attendance
    r = push(client, dd1, [put("trips", "DT1", {**t1, "status": "driverAccepted",
                                                 "boardedPassengerIds": ["DE1"]})])
    assert r.status_code == 200, r.text
    assert push(client, dd1, [put("drivers", "DD1", {**d1, "attendanceStatus": "checkedIn"})]).status_code == 200


def test_employee_cannot_create_trips_pivot_routes_or_change_billing_fields(client, world):
    u = setup_delta(client, world)
    de1, fm = u["de1"], world["fm"]
    t1 = get(client, fm, "trips", "DT1").json()
    new = {k: v for k, v in t1.items() if k != "id"} | {"tripCode": "DTY", "employeeIds": ["DE1", "DE2"],
                                                         "assignedDriverId": "DD2", "assignedVehicleId": "DV2",
                                                         "status": "assigned", "boardedPassengerIds": []}
    assert push(client, de1, [put("trips", "DTY", new)]).status_code == 403
    # own trip pointed at ANOTHER trip's existing route: refused (would let it rewrite that route)
    assert push(client, de1, [put("trips", "DT1", {**t1, "routeId": "R-DT2"})]).status_code == 403
    # billing / run fields of a shared trip: refused
    assert push(client, de1, [put("trips", "DT1", {**t1, "billableStatus": "billed"})]).status_code == 403
    # legitimate: re-planning its trip with a NEW route created in the same batch
    stop = get(client, fm, "route_stops", "S-DT1").json()
    r = push(client, de1, [
        put("routes", "R-DT1B", {"routeCode": "RDT1B", "name": "rebuilt", "employerId": "DELTA"}),
        put("route_stops", "S-DT1B", {**{k: v for k, v in stop.items() if k != "id"}, "routeId": "R-DT1B"}),
        put("trips", "DT1", {**t1, "routeId": "R-DT1B"}),
    ])
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- finding 3/4: money + audit
def test_employer_admin_cannot_rewrite_or_delete_money_records_or_the_audit_trail(client, world):
    u = setup_delta(client, world)
    ea = u["ea"]
    inv = {"reference": "INV-D1", "employerId": "DELTA", "cycle": "monthly",
           "periodStart": "2026-09-01T00:00:00Z", "periodEnd": "2026-09-30T00:00:00Z", "sequence": 1,
           "distanceKm": 100, "distanceCharges": 1800, "additionalCharges": 0, "adjustments": 0,
           "total": 1800, "status": "generated", "generatedAt": "2026-10-01T00:00:00Z"}
    assert push(client, ea, [put("invoices", "INV-D1", inv)]).status_code == 200      # generating: allowed
    stored = get(client, ea, "invoices", "INV-D1").json()
    assert push(client, ea, [put("invoices", "INV-D1", {**stored, "total": 0})]).status_code == 403
    assert push(client, ea, [put("invoices", "INV-D1", {**stored, "status": "paidDemo"})]).status_code == 403
    assert push(client, ea, [dele("invoices", "INV-D1")]).status_code == 403
    assert client.delete(f"{BASE}/{T}/invoices/INV-D1", headers=ea).status_code == 403
    # legitimate review flow
    assert push(client, ea, [put("invoices", "INV-D1", {**stored, "status": "reviewed"})]).status_code == 200
    # audit trail: entries can be added, never changed or deleted
    a = {"action": "test", "entityType": "x", "entityId": "y", "timestamp": "2026-10-15T00:00:00Z",
         "summary": "s", "outcome": "success", "actorRole": "employerAdmin", "actorId": "EA"}
    assert push(client, ea, [put("audit_events", "AU-D1", a)]).status_code == 200
    assert push(client, ea, [put("audit_events", "AU-D1", {**a, "summary": "rewritten"})]).status_code == 403
    assert client.delete(f"{BASE}/{T}/audit_events/AU-D1", headers=ea).status_code == 403


def test_audit_and_ticket_authors_come_from_the_signed_in_person(client, world):
    u = setup_delta(client, world)
    de1 = u["de1"]
    forged = {"action": "trip.cancel", "entityType": "trip", "entityId": "DT2", "timestamp": "2026-10-15T00:00:00Z",
              "summary": "forged", "outcome": "success", "actorRole": "fleetManager", "actorId": "FM"}
    assert push(client, de1, [put("audit_events", "AU-D2", forged)]).status_code == 200
    stored = get(client, world["fm"], "audit_events", "AU-D2").json()
    assert (stored["actorRole"], stored["actorId"]) == ("employee", "DE1")
    t = {"reference": "TK-D1", "createdByRole": "employee", "createdById": "DE2", "category": "other",
         "subject": "help", "description": "need help please", "status": "open"}
    assert push(client, de1, [put("support_tickets", "TK-D1", t)]).status_code in (200, 403)
    row = get(client, world["fm"], "support_tickets", "TK-D1")
    assert row.status_code == 404 or row.json()["createdById"] == "DE1"


# --------------------------------------------------------------------------- finding 5: read scopes
def test_members_read_only_their_own_notifications_tickets_and_incidents(client, world):
    u = setup_delta(client, world)
    fm = world["fm"]
    p = lambda res, body: client.post(f"{BASE}/{T}/{res}", headers=fm, json=body)  # noqa: E731
    assert p("notifications", {"id": "N-D1", "recipientRole": "employee", "recipientId": "DE1", "title": "private",
                               "body": "for DE1", "category": "tripAssignment", "createdAt": "2026-10-15T00:00:00Z"}
             ).status_code == 201
    assert p("notifications", {"id": "N-DALL", "recipientRole": "employee", "title": "all", "body": "everyone",
                               "category": "tripAssignment", "createdAt": "2026-10-15T00:00:00Z"}).status_code == 201
    assert p("support_tickets", {"id": "TK-D9", "reference": "TK9", "createdByRole": "employee", "createdById": "DE1",
                                 "category": "other", "subject": "mine", "description": "private ticket",
                                 "status": "open"}).status_code == 201
    assert p("incidents", {"id": "IN-D1", "reference": "IN1", "type": "other", "tripId": "DT1",
                           "description": "private incident", "severity": "low", "status": "submittedDemo",
                           "reportedByRole": "employee", "reportedById": "DE1"}).status_code == 201
    other = pull(client, u["de2"])
    assert "N-D1" not in {n["id"] for n in other["notifications"]}
    assert "N-DALL" in {n["id"] for n in other["notifications"]}           # role-wide broadcast still arrives
    assert "TK-D9" not in {t["id"] for t in other["support_tickets"]}
    assert "IN-D1" not in {i["id"] for i in other["incidents"]}
    mine = pull(client, u["de1"])
    assert {"N-D1", "N-DALL"} <= {n["id"] for n in mine["notifications"]}
    assert "TK-D9" in {t["id"] for t in mine["support_tickets"]}
    assert "IN-D1" in {i["id"] for i in mine["incidents"]}
    # the driver of that trip sees the incident; another driver does not
    assert "IN-D1" in {i["id"] for i in pull(client, u["dd1"])["incidents"]}
    assert "IN-D1" not in {i["id"] for i in pull(client, u["dd2"])["incidents"]}


def test_driver_phone_only_reaches_its_own_passengers(client, world):
    u = setup_delta(client, world)
    drivers = {d["id"]: d for d in pull(client, u["de1"])["drivers"]}
    assert drivers["DD1"]["fictionalContact"]                 # the driver of DE1's trip
    assert drivers["DD2"]["fictionalContact"] == ""           # another trip's driver
    assert drivers["DD1"]["gender"] == "notSpecified" and drivers["DD1"]["notes"] == ""


# --------------------------------------------------------------------------- findings 6, 7, 8, 9
def test_trip_history_follows_the_trip_read_scope(client, world):
    u = setup_delta(client, world)
    assert client.get(f"{BASE}/{T}/trips/DT2/history", headers=u["dd1"]).status_code == 404
    assert client.get(f"{BASE}/{T}/trips/DT1/history", headers=u["dd1"]).status_code == 200


def test_manager_full_put_cannot_reset_vehicle_asset_fields(client, world):
    fm = world["fm"]
    r = client.post(f"{BASE}/fleet/staff/managers", headers=fm,
                    json={"name": "Sec Manager", "email": "sec.mgr@example.com", "password": PW})
    assert r.status_code == 201, r.text
    mgr = login(client, "sec.mgr@example.com")
    assert client.post(f"{BASE}/fleet/vehicles", headers=fm, json={
        "id": "MV1", "vehicleCode": "MV1", "fictionalRegistration": "KA-MV1", "totalSeats": 4,
        "rcNumber": "RC-MV1"}).status_code == 201
    assert client.patch(f"{BASE}/fleet/vehicles/MV1", headers=fm, json={"active": False}).status_code == 200
    # a full PUT that leaves out `active` / rcNumber would reactivate the vehicle and clear its RC
    bare = {"vehicleCode": "MV1", "fictionalRegistration": "KA-MV1", "totalSeats": 4, "status": "maintenance"}
    assert client.put(f"{BASE}/fleet/vehicles/MV1", headers=mgr, json=bare).status_code == 403
    assert push(client, mgr, [put("vehicles", "MV1", bare)], tenant="fleet").status_code == 403
    v = get(client, fm, "vehicles", "MV1", tenant="fleet").json()
    assert v["active"] is False and v["rcNumber"] == "RC-MV1"
    # legitimate: operational change with the full current record
    assert client.put(f"{BASE}/fleet/vehicles/MV1", headers=mgr,
                      json={**{k: x for k, x in v.items() if k != "id"}, "status": "maintenance"}).status_code == 200


def test_driver_login_in_the_fleet_workspace_reads_and_writes_nothing(client, world):
    make_user("fleet.drv@example.com", "driver", "fleet", scope="DD9")
    h = login(client, "fleet.drv@example.com")
    v = get(client, world["fm"], "vehicles", "DV9", tenant="fleet")
    if v.status_code != 200:          # delta setup creates DV9; make sure it exists
        setup_delta(client, world)
        v = get(client, world["fm"], "vehicles", "DV9", tenant="fleet")
    assert push(client, h, [put("vehicles", "DV9", {**v.json(), "status": "available"})],
                tenant="fleet").status_code == 403
    assert pull(client, h, tenant="fleet").get("drivers", []) == []


def test_employer_admin_reads_shifts_only_of_drivers_serving_it(client, world):
    u = setup_delta(client, world)
    fm, ea = world["fm"], u["ea"]
    for sid, d in (("SH-D1", "DD1"), ("SH-D9", "DD9")):
        assert client.post(f"{BASE}/fleet/shifts", headers=fm, json={
            "id": sid, "driverId": d, "shiftDate": "2026-10-15T00:00:00Z", "scheduledStart": "2026-10-15T07:00:00Z", "scheduledEnd": "2026-10-15T15:00:00Z", "status": "checkedIn"}).status_code == 201
    # the admin assigns an unrelated driver to a draft trip, hoping to read its shifts
    push(client, ea, [put("trips", "DT-DRAFT", {"tripCode": "DTD", "employerId": "DELTA",
                                                "serviceDate": "2026-10-21T00:00:00Z", "originLocationId": "D-PK",
                                                "destinationLocationId": "D-OF", "assignedDriverId": "DD9",
                                                "status": "draft"})])
    ids = {s["id"] for s in client.get(f"{BASE}/{T}/shifts?pageSize=500", headers=ea).json()["items"]}
    assert "SH-D1" in ids and "SH-D9" not in ids


def test_driver_may_request_replacement_but_not_resolve_an_incident_filed_by_others(client, world):
    u = setup_delta(client, world)
    fm, dd1 = world["fm"], u["dd1"]
    assert client.post(f"{BASE}/{T}/incidents", headers=fm, json={
        "id": "IN-D2", "reference": "IN2", "type": "breakdown", "tripId": "DT1", "driverId": "DD1", "vehicleId": "DV1",
        "description": "breakdown reported by dispatch", "severity": "high", "status": "submittedDemo",
        "reportedByRole": "fleetManager", "reportedById": "FM"}).status_code == 201
    inc = get(client, fm, "incidents", "IN-D2").json()
    assert push(client, dd1, [put("incidents", "IN-D2", {**inc, "status": "resolvedDemo",
                                                         "resolutionNotes": "nothing wrong"})]).status_code == 403
    assert push(client, dd1, [put("incidents", "IN-D2", {**inc, "replacementRequested": True})]).status_code == 200


def test_database_unchanged_by_refused_batches(client, world):
    """A refused batch leaves no partial rows behind (all-or-nothing)."""
    setup_delta(client, world)
    with db.transaction(dict_rows=True) as cur:
        cur.execute(f'SELECT count(*) AS n FROM "tenant_{T}".trips WHERE id IN (%s, %s)', ("DTX", "DTY"))
        assert cur.fetchone()["n"] == 0


def test_fleet_manager_audit_entries_are_stamped_with_a_valid_role(client, world):
    """Regression (found by the live end-to-end run): stamping must store a valid UserRole for a
    Fleet Manager ('manager' is not one), or every Fleet Manager action is refused."""
    r = client.post(f"{BASE}/fleet/staff/managers", headers=world["fm"], json={
        "name": "Stamp Manager", "email": "stamp.manager@example.com", "password": "Passw0rd!x",
        "phone": "+91 90000 00002"})
    assert r.status_code == 201, r.text
    mg = login(client, "stamp.manager@example.com")
    ev = {"action": "trip.dispatch", "entityType": "trip", "entityId": "T-STAMP", "timestamp": "2026-10-15T00:00:00Z",
          "summary": "dispatched", "outcome": "success", "actorRole": "employee", "actorId": "X"}
    p = client.post(f"{BASE}/alpha_corp/sync/push", headers=mg,
                    json={"changes": [{"resource": "audit_events", "op": "put", "id": "AU-STAMP", "data": ev}]})
    assert p.status_code == 200, p.text
    stored = client.get(f"{BASE}/alpha_corp/audit_events/AU-STAMP", headers=world["fm"]).json()
    assert stored["actorRole"] == "fleetManager"     # forged 'employee' replaced by the caller's role
