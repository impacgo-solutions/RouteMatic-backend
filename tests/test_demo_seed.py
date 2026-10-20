"""Routematic_Demo_Tenants_Operational_QA_Prompt.md — the demo seeder and operational / adversarial QA on its data.

The seeder runs through the API of the throw-away test database (never Routematic). The tests then sign in as
every demo role and exercise the workflows and the tenant / role boundaries on the seeded data.
"""
import datetime as dt

import pytest

from app import db, ratelimit
from app.config import PROTECTED_DATABASES
from scripts import seed_demo

from .conftest import BASE

PW = "Demo-pass-123"
ANCHOR = dt.date(2026, 10, 14)
NST, BPC = "demo_northstar", "demo_bluepeak"
D = seed_demo.DOMAIN


def signin(client, email, tenant, role, password=PW):
    return client.post("/api/v1/auth/signin", json={"email": email, "password": password, "tenant": tenant,
                                                    "role": role})


def auth(client, email, tenant, role):
    r = signin(client, email, tenant, role)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['accessToken']}"}


def push(client, h, slug, changes):
    return client.post(f"{BASE}/{slug}/sync/push", headers=h, json={"changes": [
        {"resource": r, "op": "put", "id": i, "data": d} for r, i, d in changes]})


def get(client, h, slug, resource, id_):
    r = client.get(f"{BASE}/{slug}/{resource}/{id_}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def demo(client):
    ratelimit.reset()
    report = seed_demo.seed(client, PW, ANCHOR, out=lambda *a: None)
    assert not report.problems, report.problems
    ratelimit.reset()
    return {
        "report": report,
        "fa": auth(client, seed_demo.FLEET_ADMIN_EMAIL, "fleet", "fleetManager"),
        "fm": auth(client, seed_demo.FLEET_MANAGER_EMAIL, "fleet", "manager"),
        "ea": auth(client, f"admin@northstar.{D}", NST, "employerAdmin"),
        "ea_b": auth(client, f"admin@bluepeak.{D}", BPC, "employerAdmin"),
        "emp": auth(client, f"aarav.menon@northstar.{D}", NST, "employee"),
        "drv": auth(client, f"driver1@northstar.{D}", NST, "driver"),
    }


# ------------------------------------------------------------------ the seeder itself
def test_seeder_refuses_production_protected_and_unknown_databases():
    for name in ("impac_test_dev", "impaco_test_dev", "IMPAC_TEST_DEV"):
        with pytest.raises(seed_demo.SeedRefused, match="protected"):
            seed_demo.check_target(name, name, PROTECTED_DATABASES)
    for name in ("Routematic", "postgres", "customer_prod"):
        with pytest.raises(seed_demo.SeedRefused, match="not a development/demo database"):
            seed_demo.check_target(name, None, PROTECTED_DATABASES)
    assert seed_demo.check_target("Routematic", "Routematic", PROTECTED_DATABASES) == "confirmed"
    assert seed_demo.check_target("routematic_test_demo", None, PROTECTED_DATABASES) == "demo"
    # the command line refuses before it opens any connection
    assert seed_demo.main(["--database", "Routematic", "--dry-run"]) == 2
    assert seed_demo.main(["--database", "impac_test_dev", "--dry-run"]) == 2


def test_seed_created_three_tenants_and_rerun_creates_nothing(client, demo):
    rep = demo["report"]
    assert rep.rows[("DEMO-NST", "company (tenant)")]["created"] == 1
    for code in ("DEMO-NST", "DEMO-BPC", "DEMO-GDS"):
        assert rep.rows[(code, "trips")]["created"] == 12
        assert rep.rows[(code, "employees")]["created"] == 12
    assert rep.rows[("fleet", "vehicles")]["created"] == 15
    again = seed_demo.seed(client, PW, ANCHOR, out=lambda *a: None)
    assert again.total("created") == 0 and again.total("failed") == 0 and not again.problems
    assert again.total("existing") >= 530
    dry = seed_demo.seed(client, PW, ANCHOR, dry_run=True, out=lambda *a: None)
    assert dry.total("created") == 0
    lines = []
    assert seed_demo.verify(out=lines.append), "\n".join(lines)


def test_demo_marker_and_no_secrets_in_seeded_data(demo):
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT name FROM fleet_workspace.companies WHERE code LIKE 'DEMO-%%' ORDER BY code")
        assert [r["name"] for r in cur.fetchall()] == [
            "BluePeak Consulting (Demo)", "Greenfield Digital Services (Demo)", "Northstar Technologies (Demo)"]
        for schema in ("fleet_workspace", "tenant_demo_northstar"):
            cur.execute(f'SELECT string_agg(row_to_json(a)::text, \' \') t FROM "{schema}".audit_events a')
            text = cur.fetchone()["t"] or ""
            assert "demo.seed" in text and PW not in text and "pbkdf2" not in text
        cur.execute("SELECT count(*) n FROM tenant_demo_northstar.employees WHERE fictional_email NOT LIKE %s",
                    (f"%@northstar.{D}",))
        assert cur.fetchone()["n"] == 0


# ------------------------------------------------------------------ sign-in, landing workspace, switcher
def test_every_demo_role_signs_in_only_to_its_own_workspace(client, demo):
    ok = [(seed_demo.FLEET_ADMIN_EMAIL, "fleet", "fleetManager"), (seed_demo.FLEET_MANAGER_EMAIL, "fleet", "manager"),
          (f"admin@northstar.{D}", NST, "employerAdmin"), (f"aarav.menon@northstar.{D}", NST, "employee"),
          (f"driver1@northstar.{D}", NST, "driver"), (f"driver1@bluepeak.{D}", BPC, "driver")]
    for email, tenant, role in ok:
        assert signin(client, email, tenant, role).status_code == 200, (email, tenant, role)
    bad = [(f"admin@northstar.{D}", BPC, "employerAdmin"), (f"aarav.menon@northstar.{D}", BPC, "employee"),
           (f"driver1@northstar.{D}", BPC, "driver"), (f"admin@northstar.{D}", NST, "fleetManager"),
           (seed_demo.FLEET_MANAGER_EMAIL, "fleet", "fleetManager"), (f"aarav.menon@northstar.{D}", NST, "driver")]
    for email, tenant, role in bad:
        assert signin(client, email, tenant, role).status_code in (401, 403), (email, tenant, role)
    ratelimit.reset()


def test_fleet_staff_switch_between_the_three_demo_companies(client, demo):
    reg = client.get(f"{BASE}/fleet/companies", headers=demo["fa"]).json()["items"]
    demo_rows = {r["code"]: r for r in reg if r["code"].startswith("DEMO-")}
    assert set(demo_rows) == {"DEMO-NST", "DEMO-BPC", "DEMO-GDS"}
    assert demo_rows["DEMO-NST"]["counts"] == {"employees": 12, "shifts": 3, "sites": 7, "vehicles": 5}
    for who in ("fa", "fm"):
        for slug, code in ((NST, "DEMO-NST"), (BPC, "DEMO-BPC"), ("demo_greenfield", "DEMO-GDS")):
            r = client.get(f"{BASE}/{slug}/trips?pageSize=100", headers=demo[who])
            assert r.status_code == 200 and r.json()["total"] == 12
            assert {t["employerId"] for t in r.json()["items"]} == {code}
    # the workspace context decides which company's rows are read: same id space, separate schemas
    assert client.get(f"{BASE}/{BPC}/trips/DEMO-NST-T01", headers=demo["fa"]).status_code == 404


# ------------------------------------------------------------------ tenant isolation (adversarial)
def test_employer_admin_is_confined_to_its_company(client, demo):
    ea = demo["ea"]
    assert client.get(f"{BASE}/{NST}/employees?pageSize=100", headers=ea).json()["total"] == 12
    for path in (f"{BPC}/employees", f"{BPC}/invoices", f"{BPC}/trips/DEMO-BPC-T01", "fleet/vehicles",
                 "fleet/companies"):
        assert client.get(f"{BASE}/{path}", headers=ea).status_code in (403, 404), path
    # client-supplied company ids cannot move data into another company
    emp = get(client, ea, NST, "employees", "DEMO-NST-E05")
    assert push(client, ea, NST, [("employees", "DEMO-NST-E05", {**emp, "employerId": "DEMO-BPC"})]).status_code in (
        403, 422)
    assert push(client, ea, BPC, [("employees", "X-1", {**emp, "id": "X-1"})]).status_code == 403
    assert push(client, ea, NST, [("employers", "DEMO-BPC", {"code": "DEMO-BPC", "name": "x"})]).status_code in (
        403, 422)
    # dashboard numbers are this company's and match the database
    dash = client.get(f"{BASE}/{NST}/dashboard", headers=ea).json()
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT status, count(*) n FROM tenant_demo_northstar.trips GROUP BY status")
        assert dash["tripsByStatus"] == {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT coalesce(sum(total), 0) t FROM tenant_demo_northstar.invoices")
        assert dash["invoicedTotal"] == float(cur.fetchone()["t"])


def test_employee_sees_only_own_private_records(client, demo):
    h = demo["emp"]
    me = "DEMO-NST-E01"
    books = client.get(f"{BASE}/{NST}/bookings?pageSize=100", headers=h).json()["items"]
    assert books and {b["employeeId"] for b in books} == {me}
    assert client.get(f"{BASE}/{NST}/bookings/DEMO-NST-B04", headers=h).status_code in (403, 404)  # E02's
    for res in ("billing_records", "invoices", "contracts"):
        assert client.get(f"{BASE}/{NST}/{res}", headers=h).status_code == 403, res
    others = client.get(f"{BASE}/{NST}/employees?pageSize=100", headers=h).json()["items"]
    assert all(o["fictionalPhone"] == "" for o in others if o["id"] != me)          # redacted
    assert client.get(f"{BASE}/{NST}/employees?fictionalPhone=x", headers=h).status_code == 403
    assert client.get(f"{BASE}/{NST}/employees?sort=fictional_email", headers=h).status_code == 403
    assert client.get(f"{BASE}/{BPC}/bookings", headers=h).status_code == 403


def test_driver_sees_only_its_own_trips(client, demo):
    h = demo["drv"]
    trips = client.get(f"{BASE}/{NST}/trips?pageSize=100", headers=h).json()["items"]
    assert trips and {t["assignedDriverId"] for t in trips} == {"DEMO-NST-D1"}
    assert client.get(f"{BASE}/{NST}/trips/DEMO-NST-T08", headers=h).status_code in (403, 404)    # D2's trip
    t8 = get(client, demo["fa"], NST, "trips", "DEMO-NST-T08")
    assert push(client, h, NST, [("trips", "DEMO-NST-T08", {**t8, "status": "completed"})]).status_code in (403, 404)
    for res in ("invoices", "contracts", "fleet_allocations"):
        assert client.get(f"{BASE}/{NST}/{res}", headers=h).status_code == 403, res


def test_unsafe_identifiers_and_injection_are_rejected(client, demo):
    h = demo["fa"]
    for slug in ("demo_northstar;drop", "tenant_demo_northstar", "DEMO_NORTHSTAR", "fleet_workspace"):
        assert client.get(f"{BASE}/{slug}/trips", headers=h).status_code in (400, 403, 404, 422), slug
    r = client.get(f"{BASE}/{NST}/trips", headers=h, params={"q": "' OR 1=1 --", "sort": "id; drop table trips"})
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        assert r.json()["total"] == 0
    assert client.get(f"{BASE}/{NST}/trips?pageSize=100", headers=h).json()["total"] == 12


# ------------------------------------------------------------------ operational workflows on the demo data
def test_driver_runs_its_in_progress_trip_to_completion_and_it_is_billed(client, demo):
    h, trip_id = demo["drv"], "DEMO-NST-T07"
    t = get(client, h, NST, "trips", trip_id)
    assert t["status"] == "inProgress"
    route = get(client, h, NST, "routes", t["routeId"])
    stops = [get(client, h, NST, "route_stops", s) for s in route["stopIds"]]
    changes = [("route_stops", s["id"], {**s, "status": "completed", "actualArrival": "2026-10-14T08:40:00",
                                         "departedAt": "2026-10-14T08:42:00"}) for s in stops
               if s["status"] != "completed"]
    rate = 18.0
    total = round(t["distanceKm"] * rate + 25.0, 2)
    changes += [("trips", trip_id, {**t, "status": "completed", "actualArrival": "2026-10-14T08:50:00",
                                    "boardedPassengerIds": t["employeeIds"], "alightedPassengerIds": t["employeeIds"],
                                    "endOdometerKm": t["startOdometerKm"] + t["distanceKm"],
                                    "billableStatus": "billed"}),
                ("billing_records", "BL-QA-NST-T07", {
                    "employerId": "DEMO-NST", "tripId": trip_id, "tripCode": trip_id,
                    "completedAt": "2026-10-14T08:50:00", "billableDistanceKm": t["distanceKm"], "ratePerKm": rate,
                    "additionalCharges": 25.0, "adjustment": 0, "totalAmount": total, "status": "generated"})]
    r = push(client, h, NST, changes)
    assert r.status_code == 200, r.text
    done = get(client, demo["ea"], NST, "trips", trip_id)
    assert done["status"] == "completed" and done["actualArrival"].startswith("2026-10-14T08:50")
    bill = get(client, demo["ea"], NST, "billing_records", "BL-QA-NST-T07")
    assert bill["totalAmount"] == total
    # a completed trip cannot be re-opened
    assert push(client, demo["fa"], NST, [("trips", trip_id, {**done, "status": "inProgress"})]).status_code == 409


def test_employee_reports_absence_and_may_not_touch_colleagues(client, demo):
    h, trip_id, me = demo["emp"], "DEMO-NST-T11", "DEMO-NST-E01"
    t = get(client, h, NST, "trips", trip_id)
    assert me in t["employeeIds"]
    left = {**t, "employeeIds": [e for e in t["employeeIds"] if e != me],
            "absentEmployeeIds": t["absentEmployeeIds"] + [me]}
    assert push(client, h, NST, [("trips", trip_id, left)]).status_code == 200
    after = get(client, demo["ea"], NST, "trips", trip_id)
    assert me in after["absentEmployeeIds"] and me not in after["employeeIds"]
    # removing a colleague, or re-assigning the driver, is refused
    t = get(client, h, NST, "trips", "DEMO-NST-T07")
    t11 = get(client, demo["ea"], NST, "trips", trip_id)
    assert push(client, h, NST, [("trips", trip_id, {**t11, "employeeIds": []})]).status_code == 403
    assert push(client, h, NST, [("trips", "DEMO-NST-T07", {**t, "assignedDriverId": "DEMO-NST-D2"})]).status_code \
        == 403


def test_fleet_rules_hold_on_the_demo_data(client, demo):
    fa = demo["fa"]
    # over-capacity parking (DEMO-PK-EAST holds 5 of 6)
    extra = [("vehicles", f"QA-PK-{i}", {"vehicleCode": f"QA-PK-{i}", "homeParkingId": "DEMO-PK-EAST"})
             for i in (1, 2)]
    r = push(client, fa, "fleet", extra)
    assert r.status_code == 409 and "full" in r.text
    # a vehicle of one company cannot be given to another in an overlapping window
    r = push(client, fa, "fleet", [("fleet_allocations", "QA-FA-1", {
        "employerId": "DEMO-BPC", "resourceType": "vehicle", "resourceId": "DEMO-NST-V1",
        "windowStartMinutes": 400, "windowEndMinutes": 600})])
    assert r.status_code == 409 and "overlaps" in r.text
    # inactive driver cannot be assigned; cancelling needs a reason
    d5 = get(client, fa, "fleet", "drivers", "DEMO-NST-D5")
    assert push(client, fa, "fleet", [("drivers", "DEMO-NST-D5", {**d5, "active": False})]).status_code == 200
    t12 = get(client, fa, NST, "trips", "DEMO-NST-T12")
    r = push(client, fa, NST, [("trips", "DEMO-NST-T12", {**t12, "status": "assigned",
                                                          "assignedDriverId": "DEMO-NST-D5",
                                                          "assignedVehicleId": "DEMO-NST-V4"})])
    assert r.status_code == 409 and "inactive" in r.text
    assert push(client, fa, "fleet", [("drivers", "DEMO-NST-D5", d5)]).status_code == 200      # restored
    assert push(client, fa, NST, [("trips", "DEMO-NST-T12", {**t12, "status": "cancelled"})]).status_code == 422
    # duplicate business identifiers are refused
    e1 = get(client, fa, NST, "employees", "DEMO-NST-E01")
    assert push(client, fa, NST, [("employees", "QA-DUP", {**e1, "id": "QA-DUP"})]).status_code == 409
    # valid dispatch: the planned trip gets an available driver and vehicle
    r = push(client, fa, NST, [("trips", "DEMO-NST-T12", {**t12, "status": "assigned",
                                                          "assignedDriverId": "DEMO-NST-D4",
                                                          "assignedVehicleId": "DEMO-NST-V4"})])
    assert r.status_code == 200, r.text


def test_fleet_manager_runs_operations_but_not_fleet_admin_work(client, demo):
    fm = demo["fm"]
    assert client.post(f"{BASE}/fleet/companies", headers=fm, json={
        "employer": {"code": "QA-X", "name": "QA X"}}).status_code == 403
    ct = get(client, fm, "fleet", "contracts", "DEMO-CT-NST")
    assert push(client, fm, "fleet", [("contracts", "DEMO-CT-NST", {**ct, "agreedVehicleCount": 9})]).status_code \
        == 403
    v = get(client, fm, "fleet", "vehicles", "DEMO-GDS-V2")
    assert push(client, fm, "fleet", [("vehicles", v["id"], {**v, "fictionalRegistration": "KA 00 QA 0001"})]) \
        .status_code == 403
    assert push(client, fm, "fleet", [("vehicles", v["id"], {**v, "batteryOrFuelPercent": 50})]).status_code == 200
    assert push(client, fm, "fleet", [("locations", "QA-PK", {"code": "QA-PK", "name": "QA", "type": "parking",
                                                              "capacity": 2})]).status_code == 403
    assert client.post(f"{BASE}/fleet/staff/managers", headers=fm, json={
        "name": "QA M", "email": "qa.m@example.com", "password": "Passw0rd!x"}).status_code == 403
    # day-to-day dispatch in a company workspace is allowed
    t = get(client, fm, "demo_greenfield", "trips", "DEMO-GDS-T12")
    r = push(client, fm, "demo_greenfield", [("trips", t["id"], {**t, "status": "assigned",
                                                                 "assignedDriverId": "DEMO-GDS-D4",
                                                                 "assignedVehicleId": "DEMO-GDS-V4"})])
    assert r.status_code == 200, r.text


def test_billing_data_is_restricted_to_its_company(client, demo):
    inv = client.get(f"{BASE}/{NST}/invoices", headers=demo["ea"]).json()["items"]
    assert [i["reference"] for i in inv] == ["INV-DEMO-NST-20261005W"]
    assert inv[0]["total"] == round(sum(
        get(client, demo["ea"], NST, "billing_records", b)["totalAmount"] for b in inv[0]["billingRecordIds"]), 2)
    inv_b = client.get(f"{BASE}/{BPC}/invoices", headers=demo["ea_b"]).json()["items"]
    assert [i["reference"] for i in inv_b] == ["INV-DEMO-BPC-2026-09"] and inv_b[0]["cycle"] == "monthly"
    for who in ("emp", "drv"):
        assert client.get(f"{BASE}/{NST}/invoices", headers=demo[who]).status_code == 403


def test_dashboard_counts_trips_of_the_business_day_not_the_utc_day(client, demo):
    """A trip on today's date in India starts at 18:30 UTC the day before; it is still today's trip."""
    fa = demo["fa"]
    before = client.get(f"{BASE}/{NST}/dashboard", headers=fa).json()["tripsToday"]
    today = dt.datetime.now(seed_demo.TZ).date()
    t = get(client, fa, NST, "trips", "DEMO-NST-T11")
    new = {k: v for k, v in t.items() if k not in ("id", "createdAt", "updatedAt")}
    new.update(tripCode="QA-TODAY-1", status="planned", absentEmployeeIds=[], employeeIds=[],
               serviceDate=seed_demo.ts(today), scheduledStart=seed_demo.ts(today, 600),
               scheduledArrival=seed_demo.ts(today, 640), routeId=None, orderedStopIds=[])
    assert push(client, fa, NST, [("trips", "QA-TODAY-1", new)]).status_code == 200
    assert client.get(f"{BASE}/{NST}/dashboard", headers=fa).json()["tripsToday"] == before + 1
