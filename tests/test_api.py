"""API tests: auth, fleet vs company scopes, isolation, CRUD, validation, workflows."""
from app import db

from .conftest import BASE


def post(client, h, scope, res, body, expect=201):
    r = client.post(f"{BASE}/{scope}/{res}", json=body, headers=h)
    assert r.status_code == expect, (scope, res, r.text)
    return r.json()


def seed_fleet(client, world):
    """Fleet-level data: one depot, one driver, one vehicle. Created once per session."""
    if getattr(seed_fleet, "done", False):
        return
    fm = world["fm"]
    post(client, fm, "fleet", "parking_locations", {
        "id": "P1", "code": "P1", "name": "Depot", "type": "depot", "capacity": 1, "fleetOwned": True})
    post(client, fm, "fleet", "drivers", {"id": "DR1", "driverCode": "DR1", "name": "Ravi",
                                          "licenceNumber": "LIC-1"})
    post(client, fm, "fleet", "vehicles", {
        "id": "C1V", "vehicleCode": "C1", "fictionalRegistration": "KA-01-0001", "totalSeats": 5,
        "homeParkingId": "P1", "diagnosticCodes": ["P0420"]})
    seed_fleet.done = True


def seed_company(client, h, scope, code, emp_code="E1"):
    """Company-level data: pickup + office locations and one employee."""
    post(client, h, scope, "locations", {"id": f"{code}-PK", "code": f"{code}-PK", "name": "Pickup",
                                         "type": "pickup", "employerId": code})
    post(client, h, scope, "locations", {"id": f"{code}-G1", "code": f"{code}-G1", "name": "Gate",
                                         "type": "office", "employerId": code})
    return post(client, h, scope, "employees", {
        "id": emp_code, "employeeCode": emp_code, "employerId": code, "name": f"{code} Person",
        "pickupLocationId": f"{code}-PK", "destinationId": f"{code}-G1",
        "travelPreferences": {"travelTime": 3, "detour": 1}})


def test_auth_required_and_tenant_membership(client, world):
    assert client.get(f"{BASE}/fleet/vehicles").status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": "root@example.com", "password": "bad"}
                       ).status_code == 401
    assert client.get(f"{BASE}/fleet/vehicles", headers={"Authorization": "Bearer x"}).status_code == 401
    # a company admin may not enter the fleet workspace nor another company; unknown tenants look the same
    r1 = client.get(f"{BASE}/fleet/vehicles", headers=world["ea_a"])
    r2 = client.get(f"{BASE}/beta_corp/employees", headers=world["ea_a"])
    r3 = client.get(f"{BASE}/nonexistent/employees", headers=world["ea_a"])
    assert r1.status_code == r2.status_code == r3.status_code == 403
    assert r2.json()["error"]["message"] == r3.json()["error"]["message"]
    # a schema name is not a valid tenant key
    assert client.get(f"{BASE}/tenant_alpha_corp/employees", headers=world["fm"]).status_code == 403
    assert client.get(f"{BASE}/fleet_workspace/vehicles", headers=world["fm"]).status_code == 403


def test_fleet_data_lives_in_the_fleet_schema_only(client, world):
    seed_fleet(client, world)
    assert client.get(f"{BASE}/fleet/vehicles/C1V", headers=world["fm"]).json()["homeParkingId"] == "P1"
    # company workspaces READ the same fleet row (no copy)...
    seed_company(client, world["fm"], "alpha_corp", "ALPHA")
    assert client.get(f"{BASE}/alpha_corp/vehicles/C1V", headers=world["fm"]).status_code == 200
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT count(*) n FROM information_schema.tables WHERE table_name IN "
                    "('vehicles','drivers') AND table_schema <> 'fleet_workspace'")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) n FROM fleet_workspace.vehicles")
        assert cur.fetchone()["n"] == 1
    # ...and company data is not reachable from the fleet workspace
    assert client.get(f"{BASE}/fleet/employees", headers=world["fm"]).status_code == 404
    assert client.get(f"{BASE}/fleet/trips", headers=world["fm"]).status_code == 404
    assert client.get(f"{BASE}/fleet/locations", headers=world["fm"]).status_code == 404


def test_two_companies_store_independently_and_are_isolated(client, world):
    seed_company(client, world["fm"], "beta_corp", "BETA", emp_code="E1")    # same employee id, other company
    client.patch(f"{BASE}/beta_corp/employees/E1", json={"name": "Beta Person X"}, headers=world["ea_b"])
    a = client.get(f"{BASE}/alpha_corp/employees/E1", headers=world["ea_a"]).json()
    b = client.get(f"{BASE}/beta_corp/employees/E1", headers=world["ea_b"]).json()
    assert a["name"] == "ALPHA Person" and b["name"] == "Beta Person X"
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT name FROM tenant_alpha_corp.employees")
        assert [r["name"] for r in cur.fetchall()] == ["ALPHA Person"]
        cur.execute("SELECT name FROM tenant_beta_corp.employees")
        assert [r["name"] for r in cur.fetchall()] == ["Beta Person X"]
    # hostile search text is just data; cross-company access stays refused
    r = client.get(f"{BASE}/alpha_corp/employees", params={"q": "'; DROP SCHEMA tenant_beta_corp; --"},
                   headers=world["ea_a"])
    assert r.status_code == 200 and r.json()["total"] == 0
    assert client.get(f"{BASE}/beta_corp/employees/E1", headers=world["ea_a"]).status_code == 403
    with db.transaction() as cur:
        cur.execute("SHOW search_path")
        assert "tenant_" not in cur.fetchone()[0]


def test_platform_admin_can_access_any_scope(client, world):
    assert client.get(f"{BASE}/beta_corp/employees", headers=world["root"]).status_code == 200
    assert client.get(f"{BASE}/fleet/vehicles", headers=world["root"]).status_code == 200


def test_crud_roundtrip_with_nested_lists(client, world):
    emp = client.get(f"{BASE}/alpha_corp/employees/E1", headers=world["ea_a"]).json()
    assert emp["travelPreferences"] == {"travelTime": 3, "detour": 1}
    r = client.patch(f"{BASE}/alpha_corp/employees/E1", json={"travelPreferences": {"distance": 2}},
                     headers=world["ea_a"])
    assert r.json()["travelPreferences"] == {"distance": 2} and r.json()["name"] == "ALPHA Person"
    e = client.get(f"{BASE}/alpha_corp/employers/ALPHA", headers=world["ea_a"]).json()
    assert e["name"] == "Alpha Corp"
    v = client.get(f"{BASE}/fleet/vehicles/C1V", headers=world["fm"]).json()
    assert v["diagnosticCodes"] == ["P0420"] and v["status"] == "available"
    # referenced records cannot be deleted; unreferenced ones can
    assert client.delete(f"{BASE}/alpha_corp/locations/ALPHA-PK", headers=world["ea_a"]).status_code == 409
    post(client, world["ea_a"], "alpha_corp", "locations", {"id": "TMP", "code": "TMP", "name": "t",
                                                            "type": "pickup", "employerId": "ALPHA"})
    assert client.delete(f"{BASE}/alpha_corp/locations/TMP", headers=world["ea_a"]).status_code == 204
    assert client.get(f"{BASE}/alpha_corp/locations/TMP", headers=world["ea_a"]).status_code == 404


def test_list_pagination_sorting_filter_search(client, world):
    h, t = world["ea_a"], "alpha_corp"
    for i in range(2, 8):
        r = client.post(f"{BASE}/{t}/employees", headers=h, json={
            "employeeCode": f"E{i}", "employerId": "ALPHA", "name": f"Person {i}",
            "pickupLocationId": "ALPHA-PK", "destinationId": "ALPHA-G1",
            "status": "inactive" if i % 2 else "active"})
        assert r.status_code == 201, r.text
        assert r.json()["id"].startswith("E")       # server-generated id
    j = client.get(f"{BASE}/{t}/employees", params={"pageSize": 3, "sort": "name", "order": "desc"},
                   headers=h).json()
    assert j["total"] == 7 and len(j["items"]) == 3 and j["page"] == 1
    assert j["items"][0]["name"] > j["items"][1]["name"]
    j2 = client.get(f"{BASE}/{t}/employees", params={"status": "inactive"}, headers=h).json()
    assert j2["total"] == 3 and all(i["status"] == "inactive" for i in j2["items"])
    assert client.get(f"{BASE}/{t}/employees", params={"q": "person 4"}, headers=h).json()["total"] == 1
    assert client.get(f"{BASE}/{t}/employees", params={"sort": "nope"}, headers=h).status_code == 422
    assert client.get(f"{BASE}/{t}/employees", params={"status": "bogus"}, headers=h).status_code == 422
    assert client.get(f"{BASE}/{t}/employees", params={"pageSize": 0}, headers=h).status_code == 422


def test_validation_errors_are_clear(client, world):
    fm, h = world["fm"], world["ea_a"]
    r = client.post(f"{BASE}/fleet/vehicles", headers=fm, json={"vehicleCode": "X", "type": "boat",
                                                              "totalSeats": 1, "bogus": 1})
    assert r.status_code == 422
    msgs = " ".join(r.json()["error"]["details"])
    assert "type: must be one of" in msgs and "totalSeats" in msgs and "bogus: unknown field" in msgs
    assert client.post(f"{BASE}/alpha_corp/employees", headers=h, json={"employeeCode": "Z"}).status_code == 422
    # database-level uniqueness (case-insensitive registration)
    assert client.post(f"{BASE}/fleet/vehicles", headers=fm, json={
        "vehicleCode": "C1", "fictionalRegistration": "other"}).status_code == 409
    assert client.post(f"{BASE}/fleet/vehicles", headers=fm, json={
        "vehicleCode": "C77", "fictionalRegistration": "ka-01-0001"}).status_code == 409
    # an employer admin writing another company's id is refused outright...
    assert client.post(f"{BASE}/alpha_corp/employees", headers=h, json={
        "employeeCode": "E87", "employerId": "NOPE", "name": "x",
        "pickupLocationId": "ALPHA-PK", "destinationId": "ALPHA-G1"}).status_code == 403
    # ...and a fleet admin referencing a missing employer hits the FK (inside the company schema)
    r = client.post(f"{BASE}/alpha_corp/employees", headers=fm, json={
        "employeeCode": "E88", "employerId": "NOPE", "name": "x",
        "pickupLocationId": "ALPHA-PK", "destinationId": "ALPHA-G1"})
    assert r.status_code == 409


def test_parking_capacity_rule(client, world):
    fm = world["fm"]       # depot P1 has capacity 1 and C1V is already parked there
    r = client.post(f"{BASE}/fleet/vehicles", headers=fm, json={
        "vehicleCode": "C2", "fictionalRegistration": "KA-02", "homeParkingId": "P1"})
    assert r.status_code == 409 and "full" in r.json()["error"]["message"]
    # the rule also holds when a fleet admin edits fleet data from inside a company workspace
    r = client.post(f"{BASE}/alpha_corp/vehicles", headers=fm, json={
        "vehicleCode": "C2", "fictionalRegistration": "KA-02", "homeParkingId": "P1"})
    assert r.status_code == 409 and "full" in r.json()["error"]["message"]


def _trip(client, h, t, **over):
    body = {"id": "TR-1", "tripCode": "TR-1", "employerId": "ALPHA", "serviceDate": "2026-10-19T00:00:00Z",
            "scheduledStart": "2026-10-19T07:30:00Z", "originLocationId": "ALPHA-PK",
            "destinationLocationId": "ALPHA-G1", "employeeIds": ["E1"],
            "orderedStopIds": ["ALPHA-PK", "ALPHA-G1"]}
    body.update(over)
    return client.post(f"{BASE}/{t}/trips", json=body, headers=h)


def test_trip_workflow_uses_fleet_vehicle_and_driver_across_schemas(client, world):
    h, t = world["fm"], "alpha_corp"
    assert _trip(client, h, t, status="completed").status_code == 422           # bad initial state
    assert _trip(client, h, t, status="assigned").status_code == 422           # needs driver+vehicle
    # a driver/vehicle that does not exist in the FLEET schema is refused by the cross-schema FK
    bad = _trip(client, h, t, id="TR-X", tripCode="TR-X", assignedDriverId="NOPE")
    assert bad.status_code == 409
    r = _trip(client, h, t)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "draft" and r.json()["orderedStopIds"] == ["ALPHA-PK", "ALPHA-G1"]
    ok = client.patch(f"{BASE}/{t}/trips/TR-1", json={
        "status": "assigned", "assignedDriverId": "DR1", "assignedVehicleId": "C1V",
        "assignmentHistory": [{"at": "2026-10-19T06:00:00Z", "actorRole": "fleetManager",
                               "driverId": "DR1", "vehicleId": "C1V", "reason": "initial"}]}, headers=h)
    assert ok.status_code == 200, ok.text
    assert ok.json()["assignmentHistory"][0]["reason"] == "initial"
    # the fleet-level vehicle cannot be deleted while a company trip references it
    assert client.delete(f"{BASE}/fleet/vehicles/C1V", headers=h).status_code == 409
    bad = client.post(f"{BASE}/{t}/trips/TR-1/transition", json={"status": "completed"}, headers=h)
    assert bad.status_code == 409 and "allowed" in bad.json()["error"]["details"]
    assert client.post(f"{BASE}/{t}/trips/TR-1/transition", json={"status": "inProgress"},
                       headers=h).json()["actualStart"] is not None
    assert client.post(f"{BASE}/{t}/trips/TR-1/transition", json={"status": "delayed", "reason": "traffic"},
                       headers=h).json()["delayReason"] == "traffic"
    assert client.post(f"{BASE}/{t}/trips/TR-1/transition", json={"status": "completed"},
                       headers=h).status_code == 200
    assert client.post(f"{BASE}/{t}/trips/TR-1/transition", json={"status": "inProgress"},
                       headers=h).status_code == 409                          # terminal
    hist = client.get(f"{BASE}/{t}/trips/TR-1/history", headers=h).json()
    assert [x["to"] for x in hist["items"]] == ["draft", "assigned", "inProgress", "delayed", "completed"]
    # trips are not visible in the other company or the fleet workspace
    assert client.get(f"{BASE}/beta_corp/trips/TR-1", headers=world["ea_b"]).status_code == 404
    assert client.get(f"{BASE}/fleet/trips/TR-1", headers=h).status_code == 404


def test_cancellation_requires_reason(client, world):
    h, t = world["fm"], "alpha_corp"
    assert _trip(client, h, t, id="TR-2", tripCode="TR-2").status_code == 201
    assert client.post(f"{BASE}/{t}/trips/TR-2/transition", json={"status": "cancelled"},
                       headers=h).status_code == 422
    r = client.post(f"{BASE}/{t}/trips/TR-2/transition", json={"status": "cancelled", "reason": "no show"},
                    headers=h)
    assert r.status_code == 200 and r.json()["cancellationReason"] == "no show"


def test_fleet_allocation_overlap_rule(client, world):
    fm = world["fm"]
    mk = lambda emp, s, e, id_: client.post(f"{BASE}/fleet/fleet_allocations", headers=fm, json={  # noqa
        "id": id_, "employerId": emp, "resourceType": "vehicle", "resourceId": "C1V",
        "windowStartMinutes": s, "windowEndMinutes": e})
    assert mk("ALPHA", 360, 720, "FA-1").status_code == 201
    assert mk("BETA", 700, 900, "FA-2").status_code == 409          # overlaps another company
    assert mk("BETA", 720, 900, "FA-3").status_code == 201          # back-to-back is fine
    assert mk("BETA", 1380, 60, "FA-4").status_code == 201          # overnight window 23:00-01:00
    assert client.post(f"{BASE}/fleet/fleet_allocations", headers=fm, json={
        "employerId": "ALPHA", "resourceType": "vehicle", "resourceId": "NOPE",
        "windowStartMinutes": 1, "windowEndMinutes": 2}).status_code == 422
    assert client.post(f"{BASE}/fleet/fleet_allocations", headers=fm, json={
        "employerId": "UNREGISTERED", "resourceType": "vehicle", "resourceId": "C1V",
        "windowStartMinutes": 1, "windowEndMinutes": 2}).status_code == 422


def test_rbac_and_company_scoping(client, world):
    t = "alpha_corp"
    emp, ea, fm = world["emp_a"], world["ea_a"], world["fm"]
    # employee: reads fleet reference data + locations, cannot write vehicles / read billing
    assert client.get(f"{BASE}/{t}/locations", headers=emp).status_code == 200
    assert client.post(f"{BASE}/{t}/vehicles", headers=emp, json={"vehicleCode": "Z"}).status_code == 403
    assert client.get(f"{BASE}/{t}/billing_records", headers=emp).status_code == 403
    assert client.delete(f"{BASE}/{t}/locations/ALPHA-G1", headers=emp).status_code == 403
    # a company admin cannot change fleet-level data (vehicles, contracts) even though it can read it
    assert client.get(f"{BASE}/{t}/vehicles", headers=ea).status_code == 200
    assert client.post(f"{BASE}/{t}/vehicles", headers=ea, json={"vehicleCode": "Z"}).status_code == 403
    assert client.post(f"{BASE}/{t}/contracts", headers=ea, json={"contractCode": "X"}).status_code == 403
    # company contracts are fleet-level but each company only sees its own
    for code, cid in (("ALPHA", "CT-A"), ("BETA", "CT-B")):
        post(client, fm, "fleet", "contracts", {
            "id": cid, "contractCode": cid, "employerId": code, "status": "active",
            "validFrom": "2026-01-01T00:00:00Z", "validUntil": "2027-01-01T00:00:00Z"})
    assert {c["id"] for c in client.get(f"{BASE}/fleet/contracts", headers=fm).json()["items"]} == {"CT-A", "CT-B"}
    assert {c["id"] for c in client.get(f"{BASE}/alpha_corp/contracts", headers=ea).json()["items"]} == {"CT-A"}
    assert {c["id"] for c in client.get(f"{BASE}/alpha_corp/contracts", headers=fm).json()["items"]} == {"CT-A"}
    assert client.get(f"{BASE}/alpha_corp/contracts/CT-B", headers=ea).status_code == 404
    # employer admin of ALPHA only sees / writes its own employer
    assert [e["id"] for e in client.get(f"{BASE}/{t}/employers", headers=ea).json()["items"]] == ["ALPHA"]
    r = client.post(f"{BASE}/{t}/employees", headers=ea, json={
        "employeeCode": "EX", "employerId": "BETA", "name": "Cross", "pickupLocationId": "ALPHA-PK",
        "destinationId": "ALPHA-G1"})
    assert r.status_code == 403


def test_singletons_and_dashboards(client, world):
    fm = world["fm"]
    assert client.get(f"{BASE}/fleet/fleet_policy/fleet", headers=fm).status_code == 404   # nothing seeded
    assert client.post(f"{BASE}/fleet/fleet_policy", headers=fm, json={"id": "fleet"}).status_code == 201
    r = client.patch(f"{BASE}/fleet/fleet_policy/fleet", headers=fm,
                     json={"alcoholMonitoringEnabled": True, "sosRecipients": ["police"]})
    assert r.json()["alcoholMonitoringEnabled"] is True and r.json()["sosRecipients"] == ["police"]
    assert client.delete(f"{BASE}/fleet/fleet_policy/fleet", headers=fm).status_code == 405
    d = client.get(f"{BASE}/fleet/dashboard", headers=fm).json()
    assert d["scope"] == "fleet" and d["vehicles"] == {"available": 1}
    assert d["companies"].get("active", 0) >= 2 and d["parkingLocations"] == 1
    c = client.get(f"{BASE}/alpha_corp/dashboard", headers=world["ea_a"]).json()
    assert c["scope"] == "company" and c["company"] == "ALPHA"
    assert c["tripsByStatus"].get("completed") == 1 and c["tripsByStatus"].get("cancelled") == 1
    assert sum(c["employees"].values()) == client.get(f"{BASE}/alpha_corp/employees",
                                                       headers=world["ea_a"]).json()["total"]
    assert client.get(f"{BASE}/beta_corp/dashboard", headers=world["ea_b"]).json()["tripsByStatus"] == {}
    assert client.get(f"{BASE}/alpha_corp/meta/enums", headers=world["fm"]).json()["TripStatus"][0] == "draft"


def test_member_and_tenant_endpoints(client, world):
    assert client.post("/api/v1/tenants", json={"name": "Nope"}, headers=world["fm"]).status_code == 403
    body = {"email": "drv@example.com", "displayName": "Drv", "password": "Passw0rd!x",
            "role": "fleetManager"}
    # fleet staff are added in the fleet workspace and recorded in fleet_workspace.fleet_users
    assert client.post("/api/v1/tenants/fleet/members", json=body, headers=world["fm"]).status_code == 201
    with db.transaction() as cur:
        cur.execute("SELECT count(*) FROM fleet_workspace.fleet_users f JOIN public.platform_users u "
                    "ON u.id=f.user_id WHERE u.email='drv@example.com'")
        assert cur.fetchone()[0] == 1
    # drivers are not members of the fleet workspace (no row-level policy there): company logins only
    assert client.post("/api/v1/tenants/fleet/members", headers=world["fm"], json={
        **body, "email": "drv2@example.com", "role": "driver", "scopeId": "DR1"}).status_code == 422
    assert client.post("/api/v1/tenants/fleet/members", headers=world["fm"],
                       json={**body, "email": "x@example.com", "role": "employee", "scopeId": "E1"}
                       ).status_code == 422                          # not a fleet role
    assert client.post("/api/v1/tenants/alpha_corp/members", headers=world["ea_a"],
                       json={**body, "email": "z@example.com"}).status_code == 403
    mine = client.get("/api/v1/tenants", headers=world["ea_a"]).json()["items"]
    assert [x["slug"] for x in mine] == ["alpha_corp"]
    allt = client.get("/api/v1/tenants", headers=world["root"]).json()["items"]
    assert allt[0]["kind"] == "fleet" and {"alpha_corp", "beta_corp"} <= {x["slug"] for x in allt}


def test_health(client):
    r = client.get("/health")
    # liveness only: no database name, server version or host is disclosed
    assert r.status_code == 200 and r.json() == {"status": "ok"}
