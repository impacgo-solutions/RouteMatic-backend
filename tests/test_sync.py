"""Sync endpoints used by the Flutter write-through mirror: rows are routed to the schema that owns them."""
from app import db

from .conftest import BASE, login, make_user


def put(res, id_, data):
    return {"resource": res, "op": "put", "id": id_, "data": data}


def push(client, h, scope, changes, **extra):
    return client.post(f"{BASE}/{scope}/sync/push", json={"changes": changes, **extra}, headers=h)


def rows(sql_, *args):
    with db.transaction(dict_rows=True) as cur:
        cur.execute(sql_, args)
        return cur.fetchall()


def test_fleet_workspace_push_goes_to_fleet_schema(client, world):
    fm = world["fm"]
    r = push(client, fm, "fleet", [
        put("vehicles", "SV1", {"vehicleCode": "SV1", "fictionalRegistration": "SYNC-1",
                                "homeParkingId": "SP1"}),            # forward reference to the parking row
        put("locations", "SP1", {"code": "SP1", "name": "Sync Depot", "type": "depot", "capacity": 5}),
        put("drivers", "SD1", {"driverCode": "SD1", "name": "Sync Driver", "licenceNumber": "L-SYNC"}),
        put("geofences", "SG1", {"name": "Zone", "kind": "serviceArea", "vehicleIds": ["SV1"]}),
    ])
    assert r.status_code == 200 and r.json()["applied"] == 4, r.text
    assert rows("SELECT id FROM fleet_workspace.vehicles WHERE id='SV1'")
    assert rows("SELECT type FROM fleet_workspace.locations WHERE id='SP1'")[0]["type"] == "depot"
    assert rows("SELECT vehicle_id FROM fleet_workspace.geofence_vehicles WHERE geofence_id='SG1'")
    # nothing leaked into any company schema
    for sch in ("tenant_alpha_corp", "tenant_beta_corp"):
        assert not rows(f"SELECT 1 FROM {sch}.locations WHERE id='SP1'")
    # re-push is an upsert, not a duplicate
    assert push(client, fm, "fleet", [put("drivers", "SD1", {"driverCode": "SD1", "name": "Renamed",
                                                              "licenceNumber": "L-SYNC"})]).status_code == 200
    assert client.get(f"{BASE}/fleet/drivers/SD1", headers=fm).json()["name"] == "Renamed"


def test_company_rows_are_refused_in_the_fleet_workspace(client, world):
    fm = world["fm"]
    r = push(client, fm, "fleet", [put("employees", "EX1", {"employeeCode": "EX1", "employerId": "ALPHA",
                                                             "name": "x", "pickupLocationId": "L", "destinationId": "L"})])
    assert r.status_code == 422 and "belongs to a company" in r.json()["error"]["message"]
    r = push(client, fm, "fleet", [put("locations", "PK-X", {"code": "PK-X", "name": "pickup", "type": "pickup"})])
    assert r.status_code == 422 and "company" in r.json()["error"]["message"]
    r = push(client, fm, "fleet", [put("trips", "TX", {"tripCode": "TX"})])
    assert r.status_code == 422


def test_company_workspace_push_is_routed_by_owner(client, world):
    fm = world["fm"]
    changes = [
        put("employees", "S-E1", {"employeeCode": "S-E1", "employerId": "BETA", "name": "Sync One",
                                   "pickupLocationId": "S-L1", "destinationId": "S-L1"}),   # before its location
        put("locations", "S-L1", {"code": "S-L1", "name": "Pickup", "type": "pickup", "employerId": "BETA"}),
        put("locations", "S-P2", {"code": "S-P2", "name": "Fleet Lot", "type": "parking", "capacity": 3}),
        put("vehicles", "SV2", {"vehicleCode": "SV2", "fictionalRegistration": "SYNC-2"}),
    ]
    r = push(client, fm, "beta_corp", changes)
    assert r.status_code == 200, r.text
    assert rows("SELECT name FROM tenant_beta_corp.employees WHERE id='S-E1'")
    assert rows("SELECT 1 FROM tenant_beta_corp.locations WHERE id='S-L1'")
    # parking is a FLEET asset even when pushed from a company workspace
    assert rows("SELECT 1 FROM fleet_workspace.locations WHERE id='S-P2'")
    assert not rows("SELECT 1 FROM tenant_beta_corp.locations WHERE id='S-P2'")
    assert rows("SELECT 1 FROM fleet_workspace.vehicles WHERE id='SV2'")
    # a location still referenced by an employee cannot be moved to the fleet schema
    r = push(client, fm, "beta_corp", [put("locations", "S-L1", {"code": "S-L1", "name": "Now a lot",
                                                                  "type": "parking", "capacity": 1})])
    assert r.status_code == 409, r.text
    assert rows("SELECT 1 FROM tenant_beta_corp.locations WHERE id='S-L1'")
    # an unreferenced one MOVES between schemas on a type change (never two rows with one id)
    assert push(client, fm, "beta_corp", [put("locations", "S-L3", {
        "code": "S-L3", "name": "Spare", "type": "pickup", "employerId": "BETA"})]).status_code == 200
    r = push(client, fm, "beta_corp", [put("locations", "S-L3", {"code": "S-L3", "name": "Now a lot",
                                                                  "type": "parking", "capacity": 1})])
    assert r.status_code == 200, r.text
    assert rows("SELECT 1 FROM fleet_workspace.locations WHERE id='S-L3'")
    assert not rows("SELECT 1 FROM tenant_beta_corp.locations WHERE id='S-L3'")
    r = push(client, fm, "beta_corp", [put("locations", "S-L3", {"code": "S-L3", "name": "Pickup again",
                                                                  "type": "pickup", "employerId": "BETA"})])
    assert r.status_code == 200, r.text
    assert rows("SELECT name FROM tenant_beta_corp.locations WHERE id='S-L3'")[0]["name"] == "Pickup again"
    assert not rows("SELECT 1 FROM fleet_workspace.locations WHERE id='S-L3'")


def test_company_users_cannot_push_fleet_tables_or_other_companies(client, world):
    ea = world["ea_b"]
    assert push(client, ea, "beta_corp", [put("vehicles", "Z", {"vehicleCode": "Z"})]).status_code == 403
    assert push(client, ea, "beta_corp", [put("locations", "S-P9", {"code": "S-P9", "name": "lot",
                                                                      "type": "depot"})]).status_code == 403
    assert push(client, ea, "beta_corp", [put("contracts", "CT-Z", {"contractCode": "Z"})]).status_code == 403
    assert push(client, ea, "fleet", []).status_code == 403
    assert push(client, ea, "alpha_corp", []).status_code == 403
    # own company rows are fine; another employer's rows are not
    ok = push(client, ea, "beta_corp", [put("employees", "S-E2", {
        "employeeCode": "S-E2", "employerId": "BETA", "name": "Mine", "pickupLocationId": "S-L1",
        "destinationId": "S-L1"})])
    assert ok.status_code == 200, ok.text
    bad = push(client, ea, "beta_corp", [put("employees", "S-E3", {
        "employeeCode": "S-E3", "employerId": "ALPHA", "name": "x", "pickupLocationId": "S-L1",
        "destinationId": "S-L1"})])
    assert bad.status_code == 403 or bad.status_code == 422


def test_bad_batch_rolls_back_entirely_and_trusted_state(client, world):
    fm = world["fm"]
    bad = [put("locations", "S-OK", {"code": "S-OK", "name": "fine", "type": "pickup", "employerId": "BETA"}),
           put("employees", "S-BAD", {"employeeCode": "S-BAD", "employerId": "MISSING", "name": "x",
                                       "pickupLocationId": "S-OK", "destinationId": "S-OK"})]
    r = push(client, fm, "beta_corp", bad)
    assert r.status_code == 409
    assert not rows("SELECT 1 FROM tenant_beta_corp.locations WHERE id='S-OK'")             # nothing applied
    r = push(client, fm, "beta_corp", [put("employees", "S-X", {"employeeCode": "S-X", "bogusField": 1})])
    assert r.status_code == 422 and "changes[0]" in r.json()["error"]["message"]
    # historical trip created straight in 'completed' (the app is authoritative for its own rules)
    trip = {"tripCode": "S-T", "employerId": "BETA", "serviceDate": "2026-10-19T00:00:00Z",
            "scheduledStart": "2026-10-19T07:00:00Z", "originLocationId": "S-L1",
            "destinationLocationId": "S-L1", "status": "completed"}
    # a completed trip is an existing record: loaded with the Fleet Admin's explicit import
    assert push(client, fm, "beta_corp", [put("trips", "S-T", trip)]).status_code == 422
    assert push(client, fm, "beta_corp", [put("trips", "S-T", trip)], importRecords=True).status_code == 200
    dele = {"resource": "trips", "op": "delete", "id": "S-T"}
    assert push(client, fm, "beta_corp", [dele, dele]).status_code == 200
    assert not rows("SELECT 1 FROM tenant_beta_corp.trips WHERE id='S-T'")


def test_company_profile_edits_from_the_fleet_workspace_reach_the_company_schema(client, world):
    fm = world["fm"]
    prof = client.get(f"{BASE}/beta_corp/employers/BETA", headers=world["ea_b"]).json()
    prof.pop("id")
    prof.update({"name": "Beta Renamed", "status": "inactive", "defaultDistanceRate": 21})
    assert push(client, fm, "fleet", [put("employers", "BETA", prof)]).status_code == 200
    assert rows("SELECT name, default_distance_rate FROM tenant_beta_corp.employers WHERE id='BETA'")[0][
        "default_distance_rate"] == 21
    reg = rows("SELECT name, status FROM fleet_workspace.companies WHERE code='BETA'")[0]
    assert (reg["name"], reg["status"]) == ("Beta Renamed", "inactive")
    prof.update({"name": "Beta Corp", "status": "active"})
    assert push(client, fm, "fleet", [put("employers", "BETA", prof)]).status_code == 200
    # unknown company / delete are refused
    assert push(client, fm, "fleet", [put("employers", "NOPE", prof)]).status_code == 422
    assert push(client, fm, "fleet", [{"resource": "employers", "op": "delete", "id": "BETA"}]
                ).status_code == 405
    # inside a company workspace only its own profile row may be written
    assert push(client, fm, "beta_corp", [put("employers", "ALPHA", prof)]).status_code == 422


def test_pull_composes_the_right_tables_per_scope(client, world):
    fm, ea = world["fm"], world["ea_b"]
    fleet = client.get(f"{BASE}/fleet/sync/pull", headers=fm).json()
    assert {"vehicles", "drivers", "locations", "employers", "contracts"} <= set(fleet)
    assert "employees" not in fleet and "trips" not in fleet and "bookings" not in fleet
    assert {e["id"] for e in fleet["employers"]} >= {"ALPHA", "BETA"}          # the company directory
    assert all(l["type"] in ("parking", "depot") for l in fleet["locations"])  # parking only
    comp = client.get(f"{BASE}/beta_corp/sync/pull", headers=fm).json()
    assert {"employees", "trips", "vehicles", "drivers", "locations", "employers"} <= set(comp)
    assert [e["id"] for e in comp["employers"]] == ["BETA"]                    # only this company
    types = {l["type"] for l in comp["locations"]}
    assert "pickup" in types and "parking" in types or "depot" in types       # company + fleet parking merged
    assert all(c["employerId"] == "BETA" for c in comp["contracts"])
    assert comp["fleet_allocations"] == [] or all(a["employerId"] == "BETA" for a in comp["fleet_allocations"])
    # an employer admin: own company + fleet reference data, no other company's contracts
    own = client.get(f"{BASE}/beta_corp/sync/pull", headers=ea).json()
    assert [e["id"] for e in own["employers"]] == ["BETA"]
    assert all(e["employerId"] == "BETA" for e in own["employees"])
    assert "audit_events" in own and "optimization_configs" in own
    # pull is read-only and per scope: a company admin cannot pull the fleet or another company
    assert client.get(f"{BASE}/fleet/sync/pull", headers=ea).status_code == 403
    assert client.get(f"{BASE}/alpha_corp/sync/pull", headers=ea).status_code == 403


def test_driver_logins_work_in_a_company_scope(client, world):
    make_user("drv.beta@example.com", "driver", "beta_corp", scope="SD1")
    h = login(client, "drv.beta@example.com")
    assert client.get(f"{BASE}/beta_corp/vehicles", headers=h).status_code == 200      # fleet data (read)
    assert client.get(f"{BASE}/beta_corp/billing_records", headers=h).status_code == 403
    # a driver cannot create vehicles (only update the vehicle it drives: see test_ownership.py)
    assert push(client, h, "beta_corp", [put("vehicles", "SV2", {"vehicleCode": "SV2", "fictionalRegistration": "SYNC-2",
                                                                  "odometerKm": 10})]).status_code == 403
    assert push(client, h, "beta_corp", [put("employees", "E9", {"employeeCode": "E9"})]).status_code in (403, 422)


def test_pull_etag_304_when_unchanged_and_gzip(client, world):
    """The app polls /sync/pull: an unchanged workspace answers 304 with no body; a change gives a new ETag."""
    fm = world["fm"]
    r1 = client.get(f"{BASE}/fleet/sync/pull", headers={**fm, "Accept-Encoding": "gzip"})
    assert r1.status_code == 200 and r1.headers.get("etag"), r1.text
    assert "vehicles" in r1.json()
    if len(r1.content) >= 2048:
        assert r1.headers.get("content-encoding") == "gzip"
    r2 = client.get(f"{BASE}/fleet/sync/pull", headers={**fm, "If-None-Match": r1.headers["etag"]})
    assert r2.status_code == 304 and r2.content == b""
    assert push(client, fm, "fleet", [put("drivers", "ETAG1", {"driverCode": "ETAG1", "name": "Etag Driver",
                                                                "licenceNumber": "L-ETAG"})]).status_code == 200
    r3 = client.get(f"{BASE}/fleet/sync/pull", headers={**fm, "If-None-Match": r1.headers["etag"]})
    assert r3.status_code == 200 and r3.headers["etag"] != r1.headers["etag"]
    assert any(d["id"] == "ETAG1" for d in r3.json()["drivers"])
