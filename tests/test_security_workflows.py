"""Routematic_Security_Workflow_Fixes_Prompt.md — regression tests for the items added in that pass."""
import psycopg2
import pytest

from app import db

from .conftest import BASE
from .test_ownership import T, dele, get, push, put, setup_gamma

PW = "Passw0rd!x"


# ------------------------------------------------------------------ company record integrity (database level)
def _sql_error(sql, params=()):
    with pytest.raises(psycopg2.Error) as exc:
        with db.transaction() as cur:
            cur.execute(sql, params)
    return str(exc.value)


def test_database_protects_the_company_record(client, world):
    setup_gamma(client, world)
    s = "tenant_gamma_corp"
    assert "never deleted" in _sql_error(f'DELETE FROM "{s}".employers')
    assert "cannot be changed" in _sql_error(f"UPDATE \"{s}\".employers SET code = 'OTHER'")
    assert "uq_employers_single_company" in _sql_error(
        f"INSERT INTO \"{s}\".employers (id, code, name) VALUES ('X2', 'X2', 'Second')")
    assert "ck_company_locations_type" in _sql_error(
        f"INSERT INTO \"{s}\".locations (id, code, name, type) VALUES ('PKX', 'PKX', 'P', 'parking')")
    # ordinary profile edits still work
    with db.transaction() as cur:
        cur.execute(f"UPDATE \"{s}\".employers SET name = name WHERE code = 'GAMMA'")
    # ...and through the API the same rules give clear, safe errors
    fm = world["fm"]
    assert push(client, fm, [dele("employers", "GAMMA")]).status_code == 405
    r = client.post(f"{BASE}/{T}/employers", headers=fm, json={"id": "X2", "code": "X2", "name": "X"})
    assert r.status_code == 422 and "GAMMA" in r.json()["error"]["message"]


# ------------------------------------------------------------------ incident references (fleet migration 0004)
def _inspection(i, driver, vehicle, incident=None):
    return put("inspections", i, {"vehicleId": vehicle, "driverId": driver, "date": "2026-10-20T06:00:00Z",
                                  "overallStatus": "failed" if incident else "passed",
                                  **({"defectIncidentId": incident} if incident else {})})


def _incident(i, driver, vehicle):
    return put("incidents", i, {"reference": i, "type": "vehicleDefect", "driverId": driver, "vehicleId": vehicle,
                                "description": "Tyre puncture found at inspection", "severity": "high",
                                "status": "submittedDemo", "reportedByRole": "driver", "reportedById": driver,
                                "createdAt": "2026-10-20T06:00:00Z", "updatedAt": "2026-10-20T06:00:00Z"})


def test_incident_and_non_incident_inspections_and_alcohol_tests(client, world):
    u = setup_gamma(client, world)
    d = u["gd1"]
    # no incident: fine
    r = push(client, d, [_inspection("INS-OK1", "GD1", "GV1")])
    assert r.status_code == 200, r.text
    # incident recorded in the company schema in the SAME save, referenced from the fleet inspection: fine
    r = push(client, d, [_inspection("INS-DEF1", "GD1", "GV1", "IC-G1"), _incident("IC-G1", "GD1", "GV1")])
    assert r.status_code == 200, r.text
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT defect_incident_id FROM fleet_workspace.inspections WHERE id='INS-DEF1'")
        assert cur.fetchone()["defect_incident_id"] == "IC-G1"
        cur.execute(f"SELECT count(*) n FROM tenant_gamma_corp.incidents WHERE id='IC-G1'")
        assert cur.fetchone()["n"] == 1
    # an alcohol test pointing at that incident: fine; at an incident that does not exist: refused
    at = {"driverId": "GD1", "result": "fail", "testedAt": "2026-10-20T06:05:00Z", "readingMg": 25,
          "thresholdMg": 20}
    assert push(client, d, [put("alcohol_tests", "AT-G1", {**at, "incidentId": "IC-G1"})]).status_code == 200
    r = push(client, d, [put("alcohol_tests", "AT-G2", {**at, "incidentId": "IC-NOPE"})])
    assert r.status_code == 422 and "IC-NOPE" in r.text
    r = push(client, d, [_inspection("INS-BAD", "GD1", "GV1", "IC-NOPE")])
    assert r.status_code == 422


# ------------------------------------------------------------------ employees and trip passengers
def test_employee_changes_only_its_own_seat_on_a_shared_trip(client, world):
    u = setup_gamma(client, world)
    fm = world["fm"]
    base = get(client, fm, "trips", "GT2").json()
    shared = {**{k: v for k, v in base.items() if k != "id"}, "tripCode": "GT3", "employeeIds": ["GE1", "GE2"],
              "status": "planned", "assignedDriverId": None, "assignedVehicleId": None, "routeId": None}
    assert client.post(f"{BASE}/{T}/trips", headers=fm, json={"id": "GT3", **shared}).status_code == 201
    t3 = get(client, fm, "trips", "GT3").json()
    # removing another passenger: refused
    assert push(client, u["ge1"], [put("trips", "GT3", {**t3, "employeeIds": ["GE1"]})]).status_code == 403
    # marking another passenger absent: refused
    assert push(client, u["ge1"], [put("trips", "GT3", {**t3, "absentEmployeeIds": ["GE2"]})]).status_code == 403
    # leaving it yourself (absence): fine
    r = push(client, u["ge1"], [put("trips", "GT3", {**t3, "employeeIds": ["GE2"], "absentEmployeeIds": ["GE1"]})])
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ IDs: no overwriting rows one cannot see
def test_reusing_an_unseen_id_cannot_overwrite_another_persons_row(client, world):
    u = setup_gamma(client, world)
    ge2_booking = get(client, world["fm"], "bookings", "B-GE2").json()
    # GE1 cannot see B-GE2, and sending that ID (as an own booking) does not overwrite it
    r = push(client, u["ge1"], [put("bookings", "B-GE2", {**ge2_booking, "employeeId": "GE1"})])
    assert r.status_code in (403, 404)
    assert get(client, world["fm"], "bookings", "B-GE2").json()["employeeId"] == "GE2"
    # same for a driver and another driver's trip
    t2 = get(client, world["fm"], "trips", "GT2").json()
    r = push(client, u["gd1"], [put("trips", "GT2", {**t2, "assignedDriverId": "GD1"})])
    assert r.status_code == 403
    assert get(client, world["fm"], "trips", "GT2").json()["assignedDriverId"] == "GD2"


# ------------------------------------------------------------------ company profile for drivers / employees
def test_drivers_read_the_billing_rate_but_not_company_contacts(client, world):
    u = setup_gamma(client, world)
    fm = world["fm"]
    prof = get(client, fm, "employers", "GAMMA").json()
    assert client.put(f"{BASE}/{T}/employers/GAMMA", headers=fm, json={
        **{k: v for k, v in prof.items() if k != "id"}, "defaultDistanceRate": 21.5,
        "contactEmail": "accounts@gamma.example", "contactName": "Accounts", "address": "1 Gamma Road"}
    ).status_code == 200
    for who in ("gd1", "ge1"):
        e = client.get(f"{BASE}/{T}/sync/pull", headers=u[who]).json()["employers"][0]
        assert e["defaultDistanceRate"] == 21.5                     # needed to bill a completed trip
        assert (e["contactEmail"], e["contactName"], e["address"]) == ("", "", "")
        assert "billing_records" not in client.get(f"{BASE}/{T}/sync/pull", headers=u[who]).json()
        assert client.get(f"{BASE}/{T}/invoices", headers=u[who]).status_code == 403
    # the company admin still sees its own contacts
    assert get(client, u["ea"], "employers", "GAMMA").json()["contactEmail"] == "accounts@gamma.example"


# ------------------------------------------------------------------ safe errors
def test_database_errors_are_clear_and_do_not_leak_internals(client, world):
    fm = world["fm"]
    bad = [
        push(client, fm, [put("vehicles", "VBAD", {"vehicleCode": "VBAD", "totalSeats": 10 ** 12})]),
        push(client, fm, [put("vehicles", "VBAD2", {"vehicleCode": "VBAD2", "homeParkingId": "NOPE"})]),
        client.post(f"{BASE}/fleet/vehicles", headers=fm, json={"vehicleCode": "VBAD3", "totalSeats": "many"}),
    ]
    for r in bad:
        assert 400 <= r.status_code < 500, (r.status_code, r.text)
        body = r.text.lower()
        for leak in ("select ", "insert into", "psycopg2", "traceback", "password", "jwt"):
            assert leak not in body, (leak, r.text)
