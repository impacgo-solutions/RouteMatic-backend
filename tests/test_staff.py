"""Manager Management (Fleet Admin): fleet-level managers (no company) who sign in as "Fleet Manager" to the
same Fleet workspace as the Fleet Admin, with role-based permissions; driver creation by Fleet Admin / Manager."""
from app import db

from .conftest import BASE, login
from .test_api import seed_fleet  # noqa: F401

PW = "Passw0rd!x"


def signin(client, email, tenant, role, password=PW):
    return client.post("/api/v1/auth/signin", json={"email": email, "password": password,
                                                    "tenant": tenant, "role": role})


def token(client, email, tenant, role, password=PW):
    r = signin(client, email, tenant, role, password)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['accessToken']}"}


def new_manager(client, h, email, name="Mia Manager", **extra):
    return client.post(f"{BASE}/fleet/staff/managers", headers=h, json={
        "name": name, "email": email, "password": PW, "phone": "+91 90000 00001", **extra})


def test_fleet_admin_creates_a_manager_who_signs_in_to_the_fleet_workspace(client, world):
    fm = world["fm"]
    r = new_manager(client, fm, "mia.manager@example.com")
    assert r.status_code == 201, r.text
    m = r.json()
    assert (m["name"], m["email"], m["status"], m["phone"]) == (
        "Mia Manager", "mia.manager@example.com", "active", "+91 90000 00001")
    assert m["id"].startswith("MG-") and "password" not in m and "company" not in m
    # sign-in: Fleet workspace -> Fleet Manager -> email + password
    s = signin(client, "mia.manager@example.com", "fleet", "manager")
    assert s.status_code == 200, s.text
    assert s.json()["membership"] == {"slug": "fleet", "name": "Fleet Admin", "role": "manager", "scopeId": None}
    # not a company role, not the Fleet Admin role, wrong password refused
    for tenant in ("alpha_corp", "beta_corp"):
        assert signin(client, "mia.manager@example.com", tenant, "manager").status_code == 403
    bad = signin(client, "mia.manager@example.com", "fleet", "fleetManager")
    assert bad.status_code == 403
    assert signin(client, "mia.manager@example.com", "fleet", "manager", "wrong-pass").status_code == 401
    # data: role-specific row in the fleet schema; identity only in public (password hashed)
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT phone, status FROM fleet_workspace.managers WHERE id=%s", (m["id"],))
        assert dict(cur.fetchone()) == {"phone": "+91 90000 00001", "status": "active"}
        cur.execute("SELECT count(*) n FROM information_schema.columns WHERE table_schema='fleet_workspace' "
                    "AND table_name='managers' AND column_name IN ('email','password_hash','name')")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT password_hash FROM public.platform_users WHERE email='mia.manager@example.com'")
        assert PW not in cur.fetchone()["password_hash"]
        cur.execute("SELECT m.role, m.scope_id FROM public.tenant_memberships m JOIN public.tenants t "
                    "ON t.id=m.tenant_id JOIN public.platform_users u ON u.id=m.user_id "
                    "WHERE u.email='mia.manager@example.com'")
        assert [dict(r) for r in cur.fetchall()] == [{"role": "manager", "scope_id": None}]


def test_validation_and_duplicates(client, world):
    fm = world["fm"]
    assert new_manager(client, fm, "mia.manager@example.com").status_code == 409       # already a manager
    assert new_manager(client, fm, "not-an-email").status_code == 422
    assert client.post(f"{BASE}/fleet/staff/managers", headers=fm, json={
        "name": "X Y", "email": "nopw@example.com"}).status_code == 422                # new login needs pw
    assert client.post(f"{BASE}/fleet/staff/managers", headers=fm, json={
        "name": "X Y", "email": "shortpw@example.com", "password": "short"}).status_code == 422
    assert client.post(f"{BASE}/fleet/staff/managers", headers=fm, json={
        "name": "X", "email": "shortname@example.com", "password": PW}).status_code == 422
    # a company field is not part of the form any more (ignored if an old client sends it)
    r = new_manager(client, fm, "legacy.client@example.com", name="Legacy Client", company="ALPHA")
    assert r.status_code == 201 and "company" not in r.json()
    assert signin(client, "legacy.client@example.com", "fleet", "manager").status_code == 200
    assert signin(client, "legacy.client@example.com", "alpha_corp", "manager").status_code == 403


def test_only_fleet_admin_can_manage_managers(client, world):
    ea, emp, eb = world["ea_a"], world["emp_a"], world["ea_b"]
    mgr = token(client, "mia.manager@example.com", "fleet", "manager")
    body = {"name": "Eve Evil", "email": "eve@example.com", "password": PW}
    for h in (ea, emp, eb, mgr):
        assert client.post(f"{BASE}/fleet/staff/managers", headers=h, json=body).status_code == 403
        assert client.get(f"{BASE}/fleet/staff/managers", headers=h).status_code == 403
    assert client.post(f"{BASE}/fleet/staff/managers/X/deactivate", headers=mgr).status_code == 403
    # not from inside a company workspace either (even for the fleet admin)
    assert client.get(f"{BASE}/alpha_corp/staff/managers", headers=world["fm"]).status_code == 403
    assert client.post(f"{BASE}/alpha_corp/staff/managers", headers=world["fm"], json=body).status_code == 403
    assert client.get(f"{BASE}/fleet/staff/managers").status_code == 401


def test_list_view_edit_activate_deactivate(client, world):
    fm = world["fm"]
    b = new_manager(client, fm, "ben.manager@example.com", name="Ben Manager").json()
    items = client.get(f"{BASE}/fleet/staff/managers", headers=fm).json()["items"]
    assert {i["email"] for i in items} >= {"mia.manager@example.com", "ben.manager@example.com"}
    assert client.get(f"{BASE}/fleet/staff/managers/{b['id']}", headers=fm).json()["name"] == "Ben Manager"
    assert client.get(f"{BASE}/fleet/staff/managers/MG-NOPE", headers=fm).status_code == 404
    # edit: name, phone, email, password
    r = client.patch(f"{BASE}/fleet/staff/managers/{b['id']}", headers=fm, json={
        "name": "Benjamin Manager", "phone": "+91 99999 11111", "email": "benjamin@example.com",
        "password": "Brand-new-pw-1"})
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["phone"], r.json()["email"]) == (
        "Benjamin Manager", "+91 99999 11111", "benjamin@example.com")
    assert signin(client, "benjamin@example.com", "fleet", "manager", "Brand-new-pw-1").status_code == 200
    assert signin(client, "ben.manager@example.com", "fleet", "manager").status_code == 401
    assert signin(client, "benjamin@example.com", "fleet", "manager", PW).status_code == 401
    assert client.patch(f"{BASE}/fleet/staff/managers/{b['id']}", headers=fm,
                        json={"email": "mia.manager@example.com"}).status_code == 409           # taken
    # deactivate: sign-in refused, EXISTING tokens refused, listed as inactive; activate restores
    live = token(client, "benjamin@example.com", "fleet", "manager", "Brand-new-pw-1")
    assert client.get(f"{BASE}/fleet/drivers", headers=live).status_code == 200
    d = client.post(f"{BASE}/fleet/staff/managers/{b['id']}/deactivate", headers=fm)
    assert d.status_code == 200 and d.json()["status"] == "inactive"
    r = signin(client, "benjamin@example.com", "fleet", "manager", "Brand-new-pw-1")
    assert r.status_code == 403 and "deactivated" in r.json()["error"]["message"]
    assert client.get(f"{BASE}/fleet/drivers", headers=live).status_code == 403
    me = client.post("/api/v1/auth/login", json={"email": "benjamin@example.com", "password": "Brand-new-pw-1"})
    assert me.json()["memberships"] == []                           # inactive workspace not offered
    inactive = client.get(f"{BASE}/fleet/staff/managers", params={"status": "inactive"}, headers=fm).json()["items"]
    assert [i["id"] for i in inactive] == [b["id"]]
    assert client.get(f"{BASE}/fleet/staff/managers", params={"status": "bogus"}, headers=fm).status_code == 422
    a = client.post(f"{BASE}/fleet/staff/managers/{b['id']}/activate", headers=fm)
    assert a.json()["status"] == "active"
    assert signin(client, "benjamin@example.com", "fleet", "manager", "Brand-new-pw-1").status_code == 200
    assert client.get(f"{BASE}/fleet/drivers", headers=live).status_code == 200


def test_shared_account_credentials_cannot_be_changed_by_the_fleet_admin(client, world):
    fm = world["fm"]
    # an existing Employer Admin of Alpha is also made a Fleet Manager: one account, their own password kept
    assert client.post("/api/v1/tenants/alpha_corp/members", headers=fm, json={
        "email": "shared.mgr@example.com", "displayName": "Sam Shared", "password": PW,
        "role": "employerAdmin", "scopeId": "ALPHA"}).status_code == 201
    r = client.post(f"{BASE}/fleet/staff/managers", headers=fm, json={
        "name": "Sam Shared", "email": "shared.mgr@example.com"})
    assert r.status_code == 201 and "newLogin" not in r.json()     # existence of the account is not disclosed
    assert signin(client, "shared.mgr@example.com", "fleet", "manager").status_code == 200
    assert signin(client, "shared.mgr@example.com", "alpha_corp", "employerAdmin").status_code == 200
    # the fleet admin cannot reset that person's credentials (they are shared)
    assert client.patch(f"{BASE}/fleet/staff/managers/{r.json()['id']}", headers=fm,
                        json={"password": "Hijack-pass-1"}).status_code == 409
    assert client.patch(f"{BASE}/fleet/staff/managers/{r.json()['id']}", headers=fm,
                        json={"phone": "+91 1"}).status_code == 200          # role-specific data is fine
    assert signin(client, "shared.mgr@example.com", "fleet", "manager").status_code == 200


def test_manager_has_no_fleet_admin_privileges(client, world):
    seed_fleet(client, world)
    mgr = token(client, "mia.manager@example.com", "fleet", "manager")
    # same app: reads the fleet workspace like the Fleet Admin
    assert client.get(f"{BASE}/fleet/vehicles", headers=mgr).status_code == 200
    assert client.get(f"{BASE}/fleet/dashboard", headers=mgr).status_code in (200, 404)
    pull = client.get(f"{BASE}/fleet/sync/pull", headers=mgr)
    assert pull.status_code == 200 and "drivers" in pull.json() and "vehicles" in pull.json()
    # fleet staff work inside every company workspace (dispatch, billing) with the same role...
    assert client.get(f"{BASE}/alpha_corp/drivers", headers=mgr).status_code == 200
    me = client.get("/api/v1/auth/me", headers=mgr).json()["memberships"]
    assert {(m["slug"], m["role"]) for m in me} >= {("fleet", "manager"), ("alpha_corp", "manager"),
                                                     ("beta_corp", "manager")}
    for res, body in (("contracts", {"contractCode": "X"}), ("vehicles", {"vehicleCode": "ZZ"}),
                      ("fleet_policy", {"id": "fleet"})):
        assert client.post(f"{BASE}/alpha_corp/{res}", headers=mgr, json=body).status_code == 403, res
    # ...and no Fleet Admin-only actions
    assert client.post(f"{BASE}/fleet/companies", headers=mgr, json={"employer": {"code": "X", "name": "X"}}
                       ).status_code == 403
    assert client.post(f"{BASE}/fleet/vehicles", headers=mgr, json={"vehicleCode": "ZZ"}).status_code == 403
    assert client.delete(f"{BASE}/fleet/vehicles/C1V", headers=mgr).status_code == 403
    for res, body in (("contracts", {"contractCode": "X"}), ("fleet_allocations", {"employerId": "ALPHA"}),
                      ("fleet_policy", {"id": "fleet"}), ("geofences", {"name": "G", "kind": "serviceArea"}),
                      ("optimization_configs", {"name": "N"}),
                      ("parking_locations", {"code": "PX", "name": "P", "type": "parking"}),
                      ("traffic_state", {"id": "current"})):
        assert client.post(f"{BASE}/fleet/{res}", headers=mgr, json=body).status_code == 403, res
    assert client.post("/api/v1/tenants/fleet/members", headers=mgr, json={
        "email": "x@example.com", "displayName": "X", "password": PW, "role": "fleetManager"}).status_code == 403
    push = client.post(f"{BASE}/fleet/sync/push", headers=mgr, json={"changes": [
        {"resource": "employers", "op": "put", "id": "ZZ", "data": {"code": "ZZ", "name": "ZZ"}}]})
    assert push.status_code == 403


def test_manager_runs_fleet_operations_with_operational_vehicle_fields_only(client, world):
    seed_fleet(client, world)
    mgr = token(client, "mia.manager@example.com", "fleet", "manager")
    ok = client.patch(f"{BASE}/fleet/vehicles/C1V", headers=mgr, json={"status": "maintenance", "odometerKm": 12.5})
    assert ok.status_code == 200 and ok.json()["status"] == "maintenance"
    for bad in ({"model": "Hacked"}, {"totalSeats": 9}, {"fictionalRegistration": "ZZ-9"}, {"homeParkingId": None},
                {"status": "available", "color": "red"}):
        assert client.patch(f"{BASE}/fleet/vehicles/C1V", headers=mgr, json=bad).status_code == 403, bad
    cur_v = client.get(f"{BASE}/fleet/vehicles/C1V", headers=mgr).json()
    assert client.put(f"{BASE}/fleet/vehicles/C1V", headers=mgr, json={
        **{k: v for k, v in cur_v.items() if k != "id"}, "status": "available"}).status_code == 200
    assert client.put(f"{BASE}/fleet/vehicles/C1V", headers=mgr, json={
        **{k: v for k, v in cur_v.items() if k != "id"}, "totalSeats": 7}).status_code == 403
    # the same limits through the sync stream the app uses
    full = {k: v for k, v in client.get(f"{BASE}/fleet/vehicles/C1V", headers=mgr).json().items() if k != "id"}
    put = lambda data: client.post(f"{BASE}/fleet/sync/push", headers=mgr, json={"changes": [  # noqa: E731
        {"resource": "vehicles", "op": "put", "id": "C1V", "data": data}]}).status_code
    assert put({**full, "status": "available", "odometerKm": 20}) == 200
    assert put({**full, "model": "Hacked"}) == 403
    assert client.post(f"{BASE}/fleet/sync/push", headers=mgr, json={"changes": [
        {"resource": "vehicles", "op": "put", "id": "NEWV", "data": {"vehicleCode": "NEWV"}}]}).status_code == 403


def test_manager_creates_drivers_and_assigns_them_to_companies(client, world):
    fm = world["fm"]
    mgr = token(client, "mia.manager@example.com", "fleet", "manager")
    # the manager creates a driver in the fleet workspace (fleet-owned row, not copied into a company)
    r = client.post(f"{BASE}/fleet/drivers", headers=mgr, json={
        "id": "DRA1", "driverCode": "DRA1", "name": "Alpha Driver", "licenceNumber": "LIC-A1"})
    assert r.status_code == 201, r.text
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT name FROM fleet_workspace.drivers WHERE id='DRA1'")
        assert cur.fetchone()["name"] == "Alpha Driver"
        cur.execute("SELECT count(*) n FROM information_schema.tables WHERE table_schema='tenant_alpha_corp' "
                    "AND table_name='drivers'")
        assert cur.fetchone()["n"] == 0
    # ...and assigns the driver login to a company (required, must be registered)
    assert client.put(f"{BASE}/fleet/drivers/DRA1/login", headers=mgr, json={
        "email": "alpha.driver@example.com", "password": PW}).status_code == 422
    assert client.put(f"{BASE}/fleet/drivers/DRA1/login", headers=mgr, json={
        "email": "alpha.driver@example.com", "password": PW, "company": "NOPE"}).status_code == 422
    r = client.put(f"{BASE}/fleet/drivers/DRA1/login", headers=mgr, json={
        "email": "alpha.driver@example.com", "password": PW, "company": "ALPHA"})
    assert r.status_code == 200 and r.json() == {"driverId": "DRA1", "company": "alpha_corp",
                                                 "email": "alpha.driver@example.com",
                                                 "passwordChanged": True, "previousCompanyLoginsDisabled": 0}
    # edit the driver; the Fleet Admin sees it too; the manager may not delete
    assert client.patch(f"{BASE}/fleet/drivers/DRA1", headers=mgr, json={"name": "Alpha Driver II"}
                        ).json()["name"] == "Alpha Driver II"
    assert client.get(f"{BASE}/fleet/drivers/DRA1", headers=fm).json()["name"] == "Alpha Driver II"
    assert client.delete(f"{BASE}/fleet/drivers/DRA1", headers=mgr).status_code == 403
    info = client.get(f"{BASE}/fleet/drivers/DRA1/login", headers=mgr).json()
    assert info["assignments"] == [{"slug": "alpha_corp", "email": "alpha.driver@example.com", "active": True}]
    # the driver signs in to THEIR company as Driver
    s = signin(client, "alpha.driver@example.com", "alpha_corp", "driver")
    assert s.status_code == 200 and s.json()["membership"]["scopeId"] == "DRA1"
    assert signin(client, "alpha.driver@example.com", "beta_corp", "driver").status_code == 403
    assert signin(client, "alpha.driver@example.com", "fleet", "manager").status_code == 403


def test_fleet_admin_creates_and_assigns_drivers_and_can_deactivate_them(client, world):
    fm = world["fm"]
    assert client.post(f"{BASE}/fleet/drivers", headers=fm, json={
        "id": "DRF1", "driverCode": "DRF1", "name": "Fleet Made", "licenceNumber": "LIC-F1"}).status_code == 201
    no_company = client.put(f"{BASE}/fleet/drivers/DRF1/login", headers=fm,
                            json={"email": "fleet.made@example.com", "password": PW})
    assert no_company.status_code == 422                                      # must be assigned to a company
    assert client.put(f"{BASE}/fleet/drivers/DRF1/login", headers=fm, json={
        "email": "fleet.made@example.com", "password": PW, "company": "NOPE"}).status_code == 422
    assert client.put(f"{BASE}/fleet/drivers/NOPE/login", headers=fm, json={
        "email": "z@example.com", "password": PW, "company": "ALPHA"}).status_code == 404
    assert client.put(f"{BASE}/fleet/drivers/DRF1/login", headers=fm, json={
        "email": "fleet.made@example.com", "password": "short", "company": "ALPHA"}).status_code == 422
    ok = client.put(f"{BASE}/fleet/drivers/DRF1/login", headers=fm, json={
        "email": "fleet.made@example.com", "password": PW, "company": "ALPHA"})
    assert ok.status_code == 200 and ok.json()["company"] == "alpha_corp"
    assert signin(client, "fleet.made@example.com", "alpha_corp", "driver").status_code == 200
    # employer admins / employees / drivers cannot create driver logins
    for h in (world["ea_a"], world["emp_a"]):
        assert client.put(f"{BASE}/alpha_corp/drivers/DRF1/login", headers=h, json={
            "email": "q@example.com", "password": PW}).status_code == 403
    # deactivating the driver blocks the login (existing token too)
    live = token(client, "fleet.made@example.com", "alpha_corp", "driver")
    assert client.get(f"{BASE}/alpha_corp/trips", headers=live).status_code == 200
    assert client.patch(f"{BASE}/fleet/drivers/DRF1", headers=fm, json={"active": False}).status_code == 200
    r = signin(client, "fleet.made@example.com", "alpha_corp", "driver")
    assert r.status_code == 403 and "deactivated" in r.json()["error"]["message"]
    assert client.get(f"{BASE}/alpha_corp/trips", headers=live).status_code == 403
    assert client.patch(f"{BASE}/fleet/drivers/DRF1", headers=fm, json={"active": True}).status_code == 200
    assert signin(client, "fleet.made@example.com", "alpha_corp", "driver").status_code == 200


def test_schema_and_roles_for_managers(test_database):
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT code, name FROM public.global_roles WHERE code IN ('manager','fleetManager') ORDER BY code")
        assert [dict(r) for r in cur.fetchall()] == [
            {"code": "fleetManager", "name": "Fleet Admin"}, {"code": "manager", "name": "Fleet Manager"}]
        cur.execute("SELECT name, schema_version FROM public.tenants WHERE kind='fleet'")
        t = cur.fetchone()
        assert t["name"] == "Fleet Admin" and t["schema_version"] == "0004"
        cur.execute("SELECT to_regclass('fleet_workspace.managers') IS NOT NULL AS ok")
        assert cur.fetchone()["ok"]
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
                    "AND table_name='tenant_memberships' AND column_name='active'")
        assert cur.fetchone() is not None
        cur.execute("SELECT version FROM public.schema_migrations WHERE scope='public' ORDER BY version")
        assert [r["version"] for r in cur.fetchall()] == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"]
