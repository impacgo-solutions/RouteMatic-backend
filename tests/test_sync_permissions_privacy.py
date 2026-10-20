"""Routematic_Sync_Permissions_Privacy_Fixes.md — regression tests for the items not covered elsewhere."""
from app import db

from .conftest import BASE, login, make_user
from .test_ownership import T, get, put, setup_gamma

PW = "Passw0rd!x"


def push(client, h, changes, tenant=T, **extra):
    return client.post(f"{BASE}/{tenant}/sync/push", headers=h, json={"changes": changes, **extra})


def signin(client, email, tenant, role, password=PW):
    return client.post("/api/v1/auth/signin", json={"email": email, "password": password, "tenant": tenant,
                                                    "role": role})


def bearer(r):
    return {"Authorization": f"Bearer {r.json()['accessToken']}"}


# ------------------------------------------------------------------ 1. business rules on synced changes
def test_inactive_driver_or_vehicle_cannot_be_assigned_through_sync(client, world):
    setup_gamma(client, world)
    fm = world["fm"]
    assert client.post(f"{BASE}/fleet/drivers", headers=fm, json={
        "id": "OFF-D", "driverCode": "OFF-D", "name": "Off Duty", "licenceNumber": "LIC-OFFD", "active": False}
    ).status_code == 201
    assert client.post(f"{BASE}/fleet/vehicles", headers=fm, json={
        "id": "OFF-V", "vehicleCode": "OFF-V", "fictionalRegistration": "KA-OFFV", "active": False}).status_code == 201
    base = {**{k: v for k, v in get(client, fm, "trips", "GT1").json().items() if k != "id"},
            "tripCode": "GTX", "status": "assigned", "routeId": None, "employeeIds": []}
    r = push(client, fm, [put("trips", "GTX", {**base, "assignedDriverId": "OFF-D"})])
    assert r.status_code == 409 and "inactive" in r.text
    r = push(client, fm, [put("trips", "GTX", {**base, "assignedVehicleId": "OFF-V"})])
    assert r.status_code == 409 and "inactive" in r.text


def test_a_failed_batch_leaves_nothing_behind(client, world):
    setup_gamma(client, world)
    fm = world["fm"]
    good = put("locations", "ATOM-OK", {"code": "ATOM-OK", "name": "Fine", "type": "pickup", "employerId": "GAMMA"})
    bad = put("trips", "ATOM-BAD", {"tripCode": "ATOM-BAD", "employerId": "GAMMA", "status": "assigned",
                                    "serviceDate": "2026-10-20T00:00:00Z", "scheduledStart": "2026-10-20T07:00:00Z",
                                    "originLocationId": "ATOM-OK", "destinationLocationId": "ATOM-OK"})
    r = push(client, fm, [good, bad])
    assert r.status_code == 422 and "changes[1]" in r.text              # names the rejected operation
    assert client.get(f"{BASE}/{T}/locations/ATOM-OK", headers=fm).status_code == 404   # rolled back


def test_allocation_overlap_is_checked_on_the_final_state_of_a_sync_batch(client, world):
    setup_gamma(client, world)
    fm = world["fm"]
    a = lambda i, emp, s, e: put("fleet_allocations", i, {  # noqa: E731
        "employerId": emp, "resourceType": "vehicle", "resourceId": "GV1", "windowStartMinutes": s,
        "windowEndMinutes": e, "active": True})
    assert push(client, fm, [a("AL-1", "GAMMA", 360, 600), a("AL-2", "ALPHA", 600, 900)], "fleet").status_code == 200
    # overlapping another company's window: refused
    assert push(client, fm, [a("AL-3", "ALPHA", 500, 700)], "fleet").status_code == 409
    # swapping both windows in ONE save is fine (each overlaps mid-batch, not at the end)
    r = push(client, fm, [a("AL-1", "GAMMA", 600, 900), a("AL-2", "ALPHA", 360, 600)], "fleet")
    assert r.status_code == 200, r.text


def test_historical_import_is_explicit_admin_only_and_audited(client, world):
    setup_gamma(client, world)
    fm, ea = world["fm"], setup_gamma.done["ea"]
    old = put("trips", "HIST-1", {"tripCode": "HIST-1", "employerId": "GAMMA", "status": "completed",
                                  "serviceDate": "2026-09-01T00:00:00Z", "scheduledStart": "2026-09-01T07:00:00Z",
                                  "originLocationId": "G-PK", "destinationLocationId": "G-OF"})
    assert push(client, fm, [old]).status_code == 422                     # normal save: status rules apply
    assert push(client, ea, [old], importRecords=True).status_code == 403  # not for company admins
    assert push(client, fm, [old], importRecords=True).status_code == 200
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT actor_id, summary FROM tenant_gamma_corp.audit_events WHERE action='records.import'")
        rows = [dict(r) for r in cur.fetchall()]
    assert rows and rows[-1]["actor_id"] == "fm@example.com" and "trips" in rows[-1]["summary"]
    # importing never bypasses the other rules: a later status change must still follow the state machine
    t = get(client, fm, "trips", "HIST-1").json()
    assert push(client, fm, [put("trips", "HIST-1", {**t, "status": "planned"})]).status_code == 409


# ------------------------------------------------------------------ 2. Fleet Admin-only operations
def test_fleet_manager_is_refused_fleet_admin_operations_by_the_api(client, world):
    setup_gamma(client, world)
    fm = world["fm"]
    make_user("mgr.perm@example.com", "manager", "fleet")
    mgr = login(client, "mgr.perm@example.com")
    # billing jobs and company activation (inside a company workspace)
    inv = put("invoices", "INV-X", {"reference": "INV-X", "employerId": "GAMMA", "cycle": "weekly",
                                    "periodStart": "2026-10-12T00:00:00Z", "periodEnd": "2026-10-18T00:00:00Z"})
    assert push(client, mgr, [inv]).status_code == 403
    prof = get(client, fm, "employers", "GAMMA").json()
    assert push(client, mgr, [put("employers", "GAMMA", {**prof, "status": "inactive"})]).status_code == 403
    for res, body in (("billing_records", {"tripId": "GT1"}), ("contracts", {"contractCode": "X"}),
                      ("parking_locations", {"code": "P", "name": "P", "type": "parking"})):
        assert client.post(f"{BASE}/{T}/{res}", headers=mgr, json=body).status_code == 403, res
    # vehicle notes (incident resolution): allowed; identity fields: refused
    v = get(client, fm, "vehicles", "GV1").json()
    body = {k: val for k, val in v.items() if k != "id"}
    r = client.put(f"{BASE}/fleet/vehicles/GV1", headers=mgr, json={**body, "notes": "Restored after incident"})
    assert r.status_code == 200 and r.json()["notes"] == "Restored after incident"
    for bad in ({"fictionalRegistration": "KA-HACK"}, {"model": "Hacked"}, {"totalSeats": 12}):
        r = client.put(f"{BASE}/fleet/vehicles/GV1", headers=mgr, json={**body, **bad})
        assert r.status_code == 403 and "operational fields" in r.json()["error"]["message"], bad


# ------------------------------------------------------------------ 4. sessions
def test_changing_a_managers_password_ends_its_existing_sessions(client, world):
    fm = world["fm"]
    m = client.post(f"{BASE}/fleet/staff/managers", headers=fm, json={
        "name": "Session Mgr", "email": "session.mgr@example.com", "password": PW}).json()
    old = bearer(signin(client, "session.mgr@example.com", "fleet", "manager"))
    assert client.get(f"{BASE}/fleet/drivers", headers=old).status_code == 200
    assert client.patch(f"{BASE}/fleet/staff/managers/{m['id']}", headers=fm,
                        json={"password": "Brand-new-pass-1"}).status_code == 200
    r = client.get(f"{BASE}/fleet/drivers", headers=old)
    assert r.status_code == 401 and "password was changed" in r.json()["error"]["message"]
    new = bearer(signin(client, "session.mgr@example.com", "fleet", "manager", "Brand-new-pass-1"))
    assert client.get(f"{BASE}/fleet/drivers", headers=new).status_code == 200


# ------------------------------------------------------------------ 5. login set-up without enumeration
def test_login_setup_does_not_reveal_whether_an_account_exists(client, world):
    u = setup_gamma(client, world)
    ea = u["ea"]
    fm = world["fm"]
    for e in ("LE1", "LE2", "LE3", "LE4"):
        assert client.post(f"{BASE}/{T}/employees", headers=fm, json={
            "id": e, "employeeCode": e, "employerId": "GAMMA", "name": e, "pickupLocationId": "G-PK",
            "destinationId": "G-OF"}).status_code == 201
    login_ = lambda emp, body: client.put(f"{BASE}/{T}/employees/{emp}/login", headers=ea, json=body)  # noqa: E731
    # unknown email and an existing ordinary account elsewhere: same status, same keys
    unknown = login_("LE1", {"email": "brand.new@example.com", "password": PW})
    existing = login_("LE2", {"email": "emp.alpha@example.com", "password": PW})
    assert unknown.status_code == existing.status_code == 200
    assert set(unknown.json()) == set(existing.json())
    assert not {"created", "existingAccount", "newLogin"} & set(existing.json())
    # ... the admin only learns whether the password they typed was applied
    assert (unknown.json()["passwordChanged"], existing.json()["passwordChanged"]) == (True, False)
    # a missing password is refused the same way whether or not the email exists
    a = login_("LE3", {"email": "nobody.here@example.com"})
    b = login_("LE3", {"email": "ea.alpha@example.com"})
    assert a.status_code == b.status_code == 422 and a.json()["error"] == b.json()["error"]
    # every unusable email gives one identical answer (privileged / other role here / taken)
    msgs = {login_("LE4", {"email": email, "password": PW}).json()["error"]["message"]
            for email in ("root@example.com", "fm@example.com", "ea.gamma@example.com", "brand.new@example.com")}
    assert msgs == {"This email can't be used for this login. Use a different email address."}
    # unauthorized role and unauthorized company
    r = client.put(f"{BASE}/{T}/employees/LE4/login", headers=u["ge1"], json={"email": "x@example.com",
                                                                             "password": PW})
    assert r.status_code == 403
    r = client.put(f"{BASE}/{T}/employees/LE4/login", headers=world["ea_a"], json={"email": "x@example.com",
                                                                                  "password": PW})
    assert r.status_code == 403


# ------------------------------------------------------------------ 6. driver transfers
def test_driver_transfer_revokes_old_company_sessions_and_respects_deactivation(client, world):
    setup_gamma(client, world)
    fm = world["fm"]
    assert client.post(f"{BASE}/fleet/drivers", headers=fm, json={
        "id": "TRF1", "driverCode": "TRF1", "name": "Transfer", "licenceNumber": "LIC-TRF1"}).status_code == 201
    assert client.put(f"{BASE}/fleet/drivers/TRF1/login", headers=fm, json={
        "email": "transfer@example.com", "password": PW, "company": "ALPHA"}).status_code == 200
    alpha_session = bearer(signin(client, "transfer@example.com", "alpha_corp", "driver"))
    assert client.get(f"{BASE}/alpha_corp/trips", headers=alpha_session).status_code == 200
    # transfer to Gamma: the open Alpha session stops working immediately
    assert client.put(f"{BASE}/fleet/drivers/TRF1/login", headers=fm, json={
        "email": "transfer@example.com", "password": PW, "company": "GAMMA"}).status_code == 200
    assert client.get(f"{BASE}/alpha_corp/trips", headers=alpha_session).status_code == 403
    assert signin(client, "transfer@example.com", T, "driver").status_code == 200
    # a deactivated driver moved back is still refused (login eligibility is separate from membership)
    assert client.patch(f"{BASE}/fleet/drivers/TRF1", headers=fm, json={"active": False}).status_code == 200
    assert client.put(f"{BASE}/fleet/drivers/TRF1/login", headers=fm, json={
        "email": "transfer@example.com", "company": "ALPHA"}).status_code == 200
    r = signin(client, "transfer@example.com", "alpha_corp", "driver")
    assert r.status_code == 403 and "deactivated" in r.json()["error"]["message"]
    assert client.patch(f"{BASE}/fleet/drivers/TRF1", headers=fm, json={"active": True}).status_code == 200
    assert signin(client, "transfer@example.com", "alpha_corp", "driver").status_code == 200
    assert signin(client, "transfer@example.com", T, "driver").status_code == 403
    # its history is kept (no data deleted)
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT count(*) n FROM public.tenant_memberships WHERE role='driver' AND scope_id='TRF1'")
        assert cur.fetchone()["n"] == 2


# ------------------------------------------------------------------ 7. privacy
def test_hidden_fields_cannot_be_sorted_on(client, world):
    u = setup_gamma(client, world)
    r = client.get(f"{BASE}/{T}/employees", params={"sort": "fictionalPhone"}, headers=u["ge1"])
    assert r.status_code == 403
    r = client.get(f"{BASE}/{T}/drivers", params={"sort": "licenceNumber"}, headers=u["ea"])
    assert r.status_code == 403
    # allowed fields still sort, and the Fleet Admin may sort on anything
    assert client.get(f"{BASE}/{T}/employees", params={"sort": "name"}, headers=u["ge1"]).status_code == 200
    assert client.get(f"{BASE}/{T}/drivers", params={"sort": "licenceNumber"}, headers=world["fm"]).status_code == 200


def test_company_admins_see_shifts_and_inspections_only_of_their_drivers(client, world):
    u = setup_gamma(client, world)
    fm = world["fm"]
    assert client.post(f"{BASE}/fleet/drivers", headers=fm, json={
        "id": "ALPHA-ONLY", "driverCode": "ALPHA-ONLY", "name": "Alpha Only", "licenceNumber": "LIC-AO"}
    ).status_code == 201
    for i, d in (("SH-G", "GD1"), ("SH-A", "ALPHA-ONLY")):
        assert client.post(f"{BASE}/fleet/shifts", headers=fm, json={
            "id": i, "driverId": d, "shiftDate": "2026-10-20T00:00:00Z", "scheduledStart": "2026-10-20T06:00:00Z",
            "scheduledEnd": "2026-10-20T14:00:00Z"}).status_code == 201, i
    ea = u["ea"]
    ids = {s["id"] for s in client.get(f"{BASE}/{T}/shifts", headers=ea).json()["items"]}
    assert "SH-G" in ids and "SH-A" not in ids
    assert client.get(f"{BASE}/{T}/shifts/SH-A", headers=ea).status_code == 404         # direct lookup
    assert client.get(f"{BASE}/{T}/shifts", params={"driverId": "ALPHA-ONLY"}, headers=ea).json()["total"] == 0
    assert "SH-A" not in {s["id"] for s in client.get(f"{BASE}/{T}/sync/pull", headers=ea).json().get("shifts", [])}
