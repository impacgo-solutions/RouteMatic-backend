"""Audit fixes: row ownership for drivers / employees, delete rights, account takeover, company rules,
fleet staff in company workspaces. Uses its own company (gamma_corp) so other tests are unaffected."""
from app import db

from .conftest import BASE, login, make_user

PW = "Passw0rd!x"
T = "gamma_corp"


def push(client, h, changes, tenant=T):
    return client.post(f"{BASE}/{tenant}/sync/push", headers=h, json={"changes": changes})


def put(resource, id_, data):
    return {"resource": resource, "op": "put", "id": id_, "data": data}


def dele(resource, id_):
    return {"resource": resource, "op": "delete", "id": id_}


def setup_gamma(client, world):
    if getattr(setup_gamma, "done", None):
        return setup_gamma.done
    fm = world["fm"]
    r = client.post(f"{BASE}/fleet/companies", headers=fm, json={
        "slug": T, "employer": {"code": "GAMMA", "name": "Gamma Corp"},
        "admin": {"email": "ea.gamma@example.com", "displayName": "Gamma Admin", "password": PW}})
    assert r.status_code == 201, r.text
    p = lambda res, body: client.post(f"{BASE}/{T}/{res}", headers=fm, json=body)  # noqa: E731
    for i, typ in (("G-PK", "pickup"), ("G-OF", "office")):
        assert p("locations", {"id": i, "code": i, "name": i, "type": typ, "employerId": "GAMMA"}).status_code == 201
    for e in ("GE1", "GE2"):
        assert p("employees", {"id": e, "employeeCode": e, "employerId": "GAMMA", "name": e,
                               "pickupLocationId": "G-PK", "destinationId": "G-OF"}).status_code == 201
    for d, v in (("GD1", "GV1"), ("GD2", "GV2")):
        assert client.post(f"{BASE}/fleet/drivers", headers=fm, json={
            "id": d, "driverCode": d, "name": d, "licenceNumber": f"LIC-{d}"}).status_code == 201
        assert client.post(f"{BASE}/fleet/vehicles", headers=fm, json={
            "id": v, "vehicleCode": v, "fictionalRegistration": f"KA-{v}", "totalSeats": 4}).status_code == 201
    for t, d, v, e in (("GT1", "GD1", "GV1", "GE1"), ("GT2", "GD2", "GV2", "GE2")):
        assert p("routes", {"id": f"R-{t}", "routeCode": f"R{t}", "name": t, "employerId": "GAMMA"}).status_code == 201
        assert p("route_stops", {"id": f"S-{t}", "routeId": f"R-{t}", "sequence": 1, "locationId": "G-PK", "kind": "pickup"}
                 ).status_code == 201
        r = p("trips", {"id": t, "tripCode": t, "employerId": "GAMMA", "serviceDate": "2026-10-20T00:00:00Z",
                        "scheduledStart": "2026-10-20T07:30:00Z", "originLocationId": "G-PK",
                        "destinationLocationId": "G-OF", "routeId": f"R-{t}", "assignedDriverId": d,
                        "assignedVehicleId": v, "employeeIds": [e], "status": "assigned"})
        assert r.status_code == 201, r.text
        assert p("bookings", {"id": f"B-{e}", "employeeId": e, "tripId": t, "reference": f"B{e}", "requestedPickupLocationId": "G-PK", "requestedDestinationId": "G-OF", "requestedDate": "2026-10-20T00:00:00Z", "requestedArrivalMinutes": 540}).status_code == 201
    for who, role, scope in (("ge1", "employee", "GE1"), ("ge2", "employee", "GE2"),
                             ("gd1", "driver", "GD1"), ("gd2", "driver", "GD2")):
        make_user(f"{who}@example.com", role, T, scope=scope)
    setup_gamma.done = {k: login(client, f"{k}@example.com") for k in ("ge1", "ge2", "gd1", "gd2")}
    setup_gamma.done["ea"] = login(client, "ea.gamma@example.com")
    return setup_gamma.done


def get(client, h, res, id_):
    return client.get(f"{BASE}/{T}/{res}/{id_}", headers=h)


def test_employee_writes_only_its_own_rows(client, world):
    u = setup_gamma(client, world)
    me, other = u["ge1"], u["ge2"]
    emp = get(client, world["fm"], "employees", "GE1").json()
    # own profile: yes; another employee / own company or status: no
    assert push(client, me, [put("employees", "GE1", {**emp, "name": "GE1 renamed"})]).status_code == 200
    emp2 = get(client, world["fm"], "employees", "GE2").json()
    assert push(client, me, [put("employees", "GE2", {**emp2, "name": "Hijack"})]).status_code == 403
    assert push(client, me, [put("employees", "GE1", {**emp, "status": "inactive"})]).status_code == 403
    # bookings: own only, both ways
    b2 = get(client, world["fm"], "bookings", "B-GE2").json()
    assert push(client, me, [put("bookings", "B-GE2", {**b2, "status": "cancelled"})]).status_code in (403, 404)
    assert push(client, me, [put("bookings", "B-X", {"employeeId": "GE2", "reference": "BX", "requestedPickupLocationId": "G-PK", "requestedDestinationId": "G-OF", "requestedDate": "2026-10-20T00:00:00Z", "requestedArrivalMinutes": 540})]).status_code == 403
    assert push(client, me, [put("bookings", "B-NEW1", {"employeeId": "GE1", "reference": "BN1", "requestedPickupLocationId": "G-PK", "requestedDestinationId": "G-OF", "requestedDate": "2026-10-20T00:00:00Z", "requestedArrivalMinutes": 540})]).status_code == 200
    # another person's trip / settings: no; deleting bookings: no
    t2 = get(client, world["fm"], "trips", "GT2").json()
    assert push(client, me, [put("trips", "GT2", {**t2, "status": "cancelled"})]).status_code == 403
    assert push(client, me, [put("user_settings", "employee:GE2", {"largeText": True})]).status_code in (403, 404)
    assert push(client, me, [put("user_settings", "employee:GE1", {"largeText": True})]).status_code == 200
    assert push(client, me, [dele("bookings", "B-NEW1")]).status_code == 403
    # own trip: status yes, re-assigning its driver / vehicle no
    t1 = get(client, world["fm"], "trips", "GT1").json()
    assert push(client, me, [put("trips", "GT1", {**t1, "assignedDriverId": "GD2"})]).status_code == 403
    # reads: only its own bookings / settings
    ids = {b["id"] for b in client.get(f"{BASE}/{T}/sync/pull", headers=me).json()["bookings"]}
    assert "B-GE2" not in ids and "B-GE1" in ids
    assert client.get(f"{BASE}/{T}/user_settings/employee:GE2", headers=me).status_code == 404
    assert client.get(f"{BASE}/{T}/dashboard", headers=me).status_code == 403


def test_employee_leaving_a_trip_rebuilds_the_route(client, world):
    """An absence: the employee leaves its trip; the trip's route and stops are replaced."""
    u = setup_gamma(client, world)
    me = u["ge1"]
    t1 = get(client, world["fm"], "trips", "GT1").json()
    stop = get(client, world["fm"], "route_stops", "S-GT1").json()
    r = push(client, me, [
        dele("route_stops", "S-GT1"), dele("routes", "R-GT1"),
        put("routes", "R-GT1B", {"routeCode": "RGT1B", "name": "rebuilt", "employerId": "GAMMA"}),
        put("route_stops", "S-GT1B", {**{k: v for k, v in stop.items() if k != "id"}, "routeId": "R-GT1B"}),
        put("trips", "GT1", {**t1, "employeeIds": [], "routeId": "R-GT1B"}),
    ])
    assert r.status_code == 200, r.text
    # ...but not another person's route
    assert push(client, me, [dele("route_stops", "S-GT2")]).status_code == 403
    # restore for the driver tests
    t1b = get(client, world["fm"], "trips", "GT1").json()
    assert client.put(f"{BASE}/{T}/trips/GT1", headers=world["fm"],
                      json={**{k: v for k, v in t1b.items() if k != "id"}, "employeeIds": ["GE1"]}).status_code == 200


def test_driver_runs_its_own_trip_only(client, world):
    u = setup_gamma(client, world)
    me = u["gd1"]
    t1 = get(client, world["fm"], "trips", "GT1").json()
    route = t1["routeId"]
    stops = client.get(f"{BASE}/{T}/route_stops", params={"routeId": route}, headers=world["fm"]).json()["items"]
    v1 = get(client, world["fm"], "vehicles", "GV1").json()
    b1 = get(client, world["fm"], "bookings", "B-GE1").json()
    # business rules apply to synced changes too: a trip cannot jump from assigned to completed
    r = push(client, me, [put("trips", "GT1", {**t1, "status": "completed"})])
    assert r.status_code == 409 and "cannot change" in r.text
    assert push(client, me, [put("trips", "GT1", {**t1, "status": "inProgress"})]).status_code == 200
    t1 = get(client, world["fm"], "trips", "GT1").json()
    # the complete-trip batch: stops, route, booking, billing line, trip, vehicle odometer
    r = push(client, me, [
        put("route_stops", stops[0]["id"], {**stops[0], "status": "completed"}),
        put("routes", route, {**get(client, world["fm"], "routes", route).json()}),
        put("bookings", "B-GE1", {**b1, "status": "completed"}),
        put("billing_records", "BL-GT1", {"tripId": "GT1", "employerId": "GAMMA", "tripCode": "GT1",
                                          "completedAt": "2026-10-20T09:00:00Z"}),
        put("trips", "GT1", {**t1, "status": "completed"}),
        put("vehicles", "GV1", {**v1, "odometerKm": 123.0}),
    ])
    assert r.status_code == 200, r.text
    # another driver's trip / vehicle / alcohol test / driver row: refused
    t2 = get(client, world["fm"], "trips", "GT2").json()
    assert push(client, me, [put("trips", "GT2", {**t2, "status": "completed"})]).status_code == 403
    v2 = get(client, world["fm"], "vehicles", "GV2").json()
    assert push(client, me, [put("vehicles", "GV2", {**v2, "status": "maintenance"})]).status_code == 403
    assert push(client, me, [put("vehicles", "GV1", {**v1, "totalSeats": 9})]).status_code == 403
    d2 = get(client, world["fm"], "drivers", "GD2").json()
    assert push(client, me, [put("drivers", "GD2", {**d2, "active": False})]).status_code == 403
    d1 = get(client, world["fm"], "drivers", "GD1").json()
    assert push(client, me, [put("drivers", "GD1", {**d1, "licenceNumber": "FAKE"})]).status_code == 403
    assert push(client, me, [put("alcohol_tests", "AT-X", {"driverId": "GD2", "result": "pass", "testedAt": "2026-10-20T06:00:00Z", "readingMg": 0, "thresholdMg": 20})]).status_code == 403
    assert push(client, me, [dele("trips", "GT1")]).status_code == 403
    # REST transition of someone else's trip is refused too
    assert client.post(f"{BASE}/{T}/trips/GT2/transition", headers=me, json={"status": "cancelled"}
                       ).status_code == 403


def test_employer_admin_rules(client, world):
    u = setup_gamma(client, world)
    ea = u["ea"]
    # bookings for another company's employee: refused (new FK values are checked)
    assert push(client, ea, [put("bookings", "B-OUT", {"employeeId": "E1", "reference": "BO", "requestedPickupLocationId": "G-PK", "requestedDestinationId": "G-OF", "requestedDate": "2026-10-20T00:00:00Z", "requestedArrivalMinutes": 540})]).status_code in (403, 409)
    # company profile: never deleted, code never changed, no second company in this workspace
    assert client.delete(f"{BASE}/{T}/employers/GAMMA", headers=ea).status_code == 405
    prof = get(client, ea, "employers", "GAMMA").json()
    assert client.put(f"{BASE}/{T}/employers/GAMMA", headers=ea,
                      json={**{k: v for k, v in prof.items() if k != "id"}, "code": "OTHER"}).status_code == 422
    assert client.post(f"{BASE}/{T}/employers", headers=ea, json={"id": "X2", "code": "X2", "name": "X"}
                       ).status_code in (403, 422)
    assert push(client, ea, [dele("employers", "GAMMA")]).status_code == 405
    # parking is a fleet asset, not a company location
    assert client.post(f"{BASE}/{T}/locations", headers=ea, json={
        "code": "PX", "name": "P", "type": "parking", "employerId": "GAMMA"}).status_code == 422
    # billing: trips of its company may be updated (distance, billed)
    t1 = get(client, ea, "trips", "GT1").json()
    assert push(client, ea, [put("trips", "GT1", {**t1, "distanceKm": 12.5})]).status_code == 200


def test_tenant_admins_cannot_take_over_privileged_accounts(client, world):
    u = setup_gamma(client, world)
    ea = u["ea"]
    for email in ("root@example.com", "fm@example.com"):
        r = client.put(f"{BASE}/{T}/employees/GE2/login", headers=ea, json={"email": email})
        assert r.status_code == 409, (email, r.text)
    # the platform admin's password still works
    assert client.post("/api/v1/auth/login", json={"email": "root@example.com", "password": PW}).status_code == 200


def test_deactivated_employee_is_refused(client, world):
    u = setup_gamma(client, world)
    fm = world["fm"]
    emp = get(client, fm, "employees", "GE2").json()
    assert client.put(f"{BASE}/{T}/employees/GE2", headers=fm,
                      json={**{k: v for k, v in emp.items() if k != "id"}, "status": "inactive"}).status_code == 200
    assert client.get(f"{BASE}/{T}/trips", headers=u["ge2"]).status_code == 403
    r = client.post("/api/v1/auth/signin", json={"email": "ge2@example.com", "password": PW,
                                                 "tenant": T, "role": "employee"})
    assert r.status_code == 403 and "deactivated" in r.json()["error"]["message"]
    assert client.put(f"{BASE}/{T}/employees/GE2", headers=fm,
                      json={**{k: v for k, v in emp.items() if k != "id"}, "status": "active"}).status_code == 200
    assert client.get(f"{BASE}/{T}/trips", headers=u["ge2"]).status_code == 200


def test_every_fleet_admin_works_in_every_company(client, world):
    setup_gamma(client, world)
    make_user("fa.second@example.com", "fleetManager", "fleet")
    h = login(client, "fa.second@example.com")
    assert client.get(f"{BASE}/{T}/trips", headers=h).status_code == 200      # not the company's creator
    me = client.get("/api/v1/auth/me", headers=h).json()["memberships"]
    assert (T, "fleetManager") in {(m["slug"], m["role"]) for m in me}
    # deactivating the Fleet Admin account removes that access as well
    with db.transaction(dict_rows=True) as cur:
        cur.execute("UPDATE fleet_workspace.admins SET status='inactive' WHERE email='fa.second@example.com'")
    assert client.get(f"{BASE}/{T}/trips", headers=h).status_code == 403


def test_password_change_ends_existing_sessions(client, world):
    u = setup_gamma(client, world)
    fm = world["fm"]
    old = login(client, "ge1@example.com")
    assert client.get(f"{BASE}/{T}/trips", headers=old).status_code == 200
    # the company admin resets the employee's password
    r = client.put(f"{BASE}/{T}/employees/GE1/login", headers=u["ea"],
                   json={"email": "ge1@example.com", "password": "Reset-pass-123"})
    assert r.status_code == 200 and r.json()["passwordChanged"] is True
    r = client.get(f"{BASE}/{T}/trips", headers=old)
    assert r.status_code == 401 and "password was changed" in r.json()["error"]["message"]
    new = login(client, "ge1@example.com", "Reset-pass-123")
    assert client.get(f"{BASE}/{T}/trips", headers=new).status_code == 200
    # other people's sessions are unaffected
    assert client.get(f"{BASE}/fleet/vehicles", headers=fm).status_code == 200
    # restore the fixture password (and its session) for later tests
    client.put(f"{BASE}/{T}/employees/GE1/login", headers=u["ea"],
               json={"email": "ge1@example.com", "password": PW})
    setup_gamma.done["ge1"] = login(client, "ge1@example.com")


def test_moving_a_driver_switches_off_its_old_company_login(client, world):
    setup_gamma(client, world)
    fm = world["fm"]
    assert client.post(f"{BASE}/fleet/drivers", headers=fm, json={
        "id": "MVD1", "driverCode": "MVD1", "name": "Mover", "licenceNumber": "LIC-MVD1"}).status_code == 201
    assert client.put(f"{BASE}/fleet/drivers/MVD1/login", headers=fm, json={
        "email": "mover@example.com", "password": PW, "company": "ALPHA"}).status_code == 200
    signin = lambda t: client.post("/api/v1/auth/signin", json={  # noqa: E731
        "email": "mover@example.com", "password": PW, "tenant": t, "role": "driver"}).status_code
    assert signin("alpha_corp") == 200
    # move to Gamma: Alpha login switched off (kept), Gamma login works
    r = client.put(f"{BASE}/fleet/drivers/MVD1/login", headers=fm, json={
        "email": "mover@example.com", "password": "Another-pass-1", "company": "GAMMA"})
    assert r.status_code == 200 and r.json()["previousCompanyLoginsDisabled"] == 1
    assert signin(T) == 200 and signin("alpha_corp") == 403
    info = client.get(f"{BASE}/fleet/drivers/MVD1/login", headers=fm).json()["assignments"]
    assert {(a["slug"], a["active"]) for a in info} == {("alpha_corp", False), (T, True)}
    # and back again
    assert client.put(f"{BASE}/fleet/drivers/MVD1/login", headers=fm, json={
        "email": "mover@example.com", "company": "ALPHA"}).status_code == 200
    assert signin("alpha_corp") == 200 and signin(T) == 403


def test_personal_fields_are_hidden_from_other_roles(client, world):
    u = setup_gamma(client, world)
    fm = world["fm"]
    emp = client.get(f"{BASE}/{T}/employees/GE2", headers=fm).json()
    assert client.put(f"{BASE}/{T}/employees/GE2", headers=fm, json={
        **{k: v for k, v in emp.items() if k != "id"}, "fictionalPhone": "+91 98888 00002",
        "fictionalEmail": "ge2.private@example.com"}).status_code == 200
    drv = client.get(f"{BASE}/fleet/drivers/GD1", headers=fm).json()
    assert drv["licenceNumber"] == "LIC-GD1"
    pull = lambda h: client.get(f"{BASE}/{T}/sync/pull", headers=h).json()  # noqa: E731
    # another employee: name + routing data, no phone / email
    other = {e["id"]: e for e in pull(u["ge1"])["employees"]}["GE2"]
    assert other["name"] == "GE2" and other["pickupLocationId"] == "G-PK"
    assert other["fictionalPhone"] == "" and other["fictionalEmail"] == ""
    # ...and no driver licence / address
    assert {d["id"]: d for d in pull(u["ge1"])["drivers"]}["GD1"]["licenceNumber"] == "••••GD1"   # masked
    # the driver keeps the phone of its own passengers (GE1 rides GT1), not of others
    by_id = {e["id"]: e for e in pull(u["gd2"])["employees"]}
    assert by_id["GE2"]["fictionalPhone"] == "+91 98888 00002"     # GD2 drives GT2 with GE2
    assert by_id["GE1"]["fictionalPhone"] == ""
    # its own driver row is complete
    assert {d["id"]: d for d in pull(u["gd1"])["drivers"]}["GD1"]["licenceNumber"] == "LIC-GD1"
    # the company admin sees its employees fully, but not drivers' licences
    assert client.get(f"{BASE}/{T}/employees/GE2", headers=u["ea"]).json()["fictionalPhone"] == "+91 98888 00002"
    assert client.get(f"{BASE}/{T}/drivers/GD1", headers=u["ea"]).json()["licenceNumber"] == "••••GD1"
    # the Fleet Admin sees everything
    assert client.get(f"{BASE}/{T}/drivers/GD1", headers=fm).json()["licenceNumber"] == "LIC-GD1"
    # no guessing through filters / search
    assert client.get(f"{BASE}/{T}/employees", params={"fictionalPhone": "+91 98888 00002"},
                      headers=u["ge1"]).status_code == 403
    assert client.get(f"{BASE}/{T}/drivers", params={"q": "LIC-GD1"}, headers=u["ea"]).status_code == 403
