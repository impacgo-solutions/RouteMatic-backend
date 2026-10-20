"""Company registration = tenant provisioning + central registry + per-company login/isolation."""
from app import db
from app.spec import TABLE_KINDS

from .conftest import BASE, login


def company_body(code, name, admin=None, **emp):
    return {"employer": {"code": code, "name": name, **emp},
            **({"admin": admin} if admin else {})}


def test_registering_a_company_creates_its_schema_and_registry_row(client, world):
    admin = {"email": "admin@acme.example", "displayName": "Acme Admin", "password": "Acme-pass-1"}
    r = client.post(f"{BASE}/fleet/companies", headers=world["fm"],
                    json=company_body("ACME", "Acme Logistics", admin, billingCycle="monthly",
                                      defaultDistanceRate=22))
    assert r.status_code == 201, r.text
    j = r.json()
    slug = j["tenant"]["slug"]
    assert slug == "acme_logistics" and j["tenant"]["schemaName"] == "tenant_acme_logistics"
    assert j["company"]["id"] == "ACME" and j["company"]["billingCycle"] == "monthly"
    assert {m["role"] for m in j["members"]} == {"fleetManager", "employerAdmin"}

    with db.transaction(dict_rows=True) as cur:
        # the company schema exists with every company-level table, all EMPTY except its profile
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
                    ("tenant_acme_logistics",))
        names = {x["table_name"] for x in cur.fetchall()}
        company_tables = {t for t, ks in TABLE_KINDS.items() if "company" in ks}
        assert company_tables <= names
        for t in sorted(names - {"schema_migrations", "employers", "audit_events"}):
            cur.execute(f'SELECT count(*) n FROM tenant_acme_logistics."{t}"')
            assert cur.fetchone()["n"] == 0, t
        # the only other row: the server's audit record of the registration (no personal data)
        cur.execute('SELECT action, entity_id, actor_id FROM tenant_acme_logistics."audit_events"')
        assert [tuple(r.values()) for r in cur.fetchall()] == [("company.register", "ACME", "fm@example.com")]
        cur.execute("SELECT id FROM tenant_acme_logistics.employers")
        assert [x["id"] for x in cur.fetchall()] == ["ACME"]
        # the central mapping in the fleet schema points at the tenant schema
        cur.execute('SELECT code, name, tenant_slug, schema_name, status, tenant_status '
                    'FROM fleet_workspace.company_tenants WHERE code=%s', ("ACME",))
        assert dict(cur.fetchone()) == {
            "code": "ACME", "name": "Acme Logistics", "tenant_slug": slug,
            "schema_name": "tenant_acme_logistics", "status": "active", "tenant_status": "ready"}
        # no fleet-level data was copied into the company schema
        cur.execute("SELECT count(*) n FROM information_schema.tables WHERE table_name='vehicles' "
                    "AND table_schema <> 'fleet_workspace'")
        assert cur.fetchone()["n"] == 0

    # the company admin logs in and operates ONLY in the company's tenant
    h = login(client, "admin@acme.example", "Acme-pass-1")
    me = client.post("/api/v1/auth/login", json={"email": "admin@acme.example", "password": "Acme-pass-1"}
                     ).json()["memberships"]
    assert [(m["slug"], m["role"], m["scopeId"]) for m in me] == [(slug, "employerAdmin", "ACME")]
    assert client.get(f"{BASE}/{slug}/employers", headers=h).json()["total"] == 1
    assert client.post(f"{BASE}/{slug}/locations", headers=h, json={
        "id": "L1", "code": "L1", "name": "Gate", "type": "office", "employerId": "ACME"}).status_code == 201
    # ...and cannot touch the fleet workspace, another company, or register companies
    assert client.get(f"{BASE}/fleet/vehicles", headers=h).status_code == 403
    assert client.get(f"{BASE}/alpha_corp/employees", headers=h).status_code == 403
    assert client.get(f"{BASE}/beta_corp/employees", headers=h).status_code == 403
    assert client.post(f"{BASE}/fleet/companies", headers=h, json=company_body("X", "X Co")).status_code == 403
    assert client.post(f"{BASE}/{slug}/companies", headers=h, json=company_body("X", "X Co")).status_code == 403
    # the fleet admin sees the new company from one place
    items = client.get(f"{BASE}/fleet/companies", headers=world["fm"]).json()["items"]
    assert "ACME" in {i["code"] for i in items}
    assert next(i for i in items if i["code"] == "ACME")["schemaName"] == "tenant_acme_logistics"
    assert client.get(f"{BASE}/{slug}/locations", headers=world["fm"]).json()["total"] == 1


def test_authorization_and_validation(client, world):
    ok = company_body("V1", "Valid Co")
    assert client.post(f"{BASE}/fleet/companies", json=ok).status_code == 401
    assert client.post(f"{BASE}/fleet/companies", json=ok, headers=world["ea_a"]).status_code == 403
    assert client.post(f"{BASE}/fleet/companies", json=ok, headers=world["emp_a"]).status_code == 403
    # a fleet admin working INSIDE a company workspace cannot register companies there
    assert client.post(f"{BASE}/alpha_corp/companies", json=ok, headers=world["fm"]).status_code == 403
    for bad in (company_body("", "No Code"), company_body("TOO-LONG-CODE-123", "Long"),
                company_body("B1", "Bad", defaultDistanceRate=-5),
                company_body("B2", "Bad2", bogus=1),
                company_body("B3", "Bad3", {"email": "x@y.example", "displayName": "X"})):
        r = client.post(f"{BASE}/fleet/companies", json=bad, headers=world["fm"])
        assert r.status_code == 422, (bad, r.text)
    with db.transaction() as cur:       # nothing leaked from the failed attempts
        cur.execute("SELECT count(*) FROM pg_namespace WHERE nspname IN "
                    "('tenant_bad','tenant_bad2','tenant_bad3','tenant_no_code','tenant_long')")
        assert cur.fetchone()[0] == 0


def test_duplicates_and_rollback(client, world):
    fm = world["fm"]
    r1 = client.post(f"{BASE}/fleet/companies", headers=fm, json=company_body("D1", "Dup Co"))
    assert r1.status_code == 201 and r1.json()["tenant"]["slug"] == "dup_co"
    assert client.post(f"{BASE}/fleet/companies", headers=fm,
                       json=company_body("d1", "Other Name")).status_code == 409     # codes are fleet-wide
    r2 = client.post(f"{BASE}/fleet/companies", headers=fm, json=company_body("D2", "Dup Co"))
    assert r2.status_code == 201 and r2.json()["tenant"]["slug"] == "dup_co_d2"        # name collision
    assert client.post(f"{BASE}/fleet/companies", headers=fm,
                       json={**company_body("D3", "Fixed"), "slug": "dup_co"}).status_code == 409
    assert client.post(f"{BASE}/fleet/companies", headers=fm,
                       json={**company_body("D4", "Reserved"), "slug": "fleet"}).status_code == 422
    # an admin-login failure after the schema is built rolls EVERYTHING back, registry included
    bad_admin = {"email": "new.admin@example.com", "displayName": "N"}        # new login without password
    r = client.post(f"{BASE}/fleet/companies", headers=fm, json=company_body("RB", "Rollback Co", bad_admin))
    assert r.status_code == 422
    with db.transaction() as cur:
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname='tenant_rollback_co'")
        assert cur.fetchone() is None
        cur.execute("SELECT 1 FROM public.tenants WHERE slug='rollback_co'")
        assert cur.fetchone() is None
        cur.execute("SELECT 1 FROM fleet_workspace.companies WHERE code='RB'")
        assert cur.fetchone() is None
        cur.execute("SELECT 1 FROM public.platform_users WHERE email='new.admin@example.com'")
        assert cur.fetchone() is None
    assert client.post(f"{BASE}/fleet/companies", headers=fm,
                       json=company_body("RB", "Rollback Co")).status_code == 201     # retriable


def test_platform_admin_can_register_and_company_data_stays_separate(client, world):
    h = world["root"]
    s1 = client.post(f"{BASE}/fleet/companies", headers=h, json=company_body("P1", "Platform Made")
                     ).json()["tenant"]["slug"]
    s2 = client.post(f"{BASE}/fleet/companies", headers=h, json=company_body("P2", "Second One")
                     ).json()["tenant"]["slug"]
    assert client.post(f"{BASE}/{s1}/locations", headers=h, json={
        "id": "ONLY1", "code": "ONLY1", "name": "x", "type": "pickup"}).status_code == 201
    assert client.get(f"{BASE}/{s1}/locations/ONLY1", headers=h).status_code == 200
    assert client.get(f"{BASE}/{s2}/locations/ONLY1", headers=h).status_code == 404


def test_shared_migrations_recorded(test_database):
    with db.transaction() as cur:
        cur.execute("SELECT version FROM public.schema_migrations WHERE scope='public' ORDER BY version")
        assert [r[0] for r in cur.fetchall()] == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"]
        cur.execute("SELECT to_regclass('public.companies')")
        assert cur.fetchone()[0] is None          # the registry lives in fleet_workspace now


def test_company_address_is_stored_in_the_company_schema_and_visible_to_the_fleet(client, world):
    fm = world["fm"]
    addr = "12 Residency Road, Bengaluru 560025"
    r = client.post(f"{BASE}/fleet/companies", headers=fm,
                    json=company_body("ADDR1", "Address Co", address=addr))
    assert r.status_code == 201, r.text
    slug = r.json()["tenant"]["slug"]
    assert r.json()["company"]["address"] == addr
    with db.transaction(dict_rows=True) as cur:
        cur.execute(f'SELECT address FROM tenant_{slug}.employers WHERE id=%s', ("ADDR1",))
        assert cur.fetchone()["address"] == addr
        # the registry holds no copy: one source of truth (the company's own row)
        cur.execute("SELECT count(*) n FROM information_schema.columns WHERE table_schema='fleet_workspace' "
                    "AND table_name='companies' AND column_name='address'")
        assert cur.fetchone()["n"] == 0
    # the fleet workspace reads it from the company schema; the company admin can edit it
    fleet = client.get(f"{BASE}/fleet/sync/pull", headers=fm).json()
    assert next(e for e in fleet["employers"] if e["id"] == "ADDR1")["address"] == addr
    prof = client.get(f"{BASE}/{slug}/employers/ADDR1", headers=fm).json()
    prof.pop("id")
    prof["address"] = "New HQ, Pune"
    assert client.post(f"{BASE}/fleet/sync/push", headers=fm, json={"changes": [
        {"resource": "employers", "op": "put", "id": "ADDR1", "data": prof}]}).status_code == 200
    assert client.get(f"{BASE}/{slug}/employers/ADDR1", headers=fm).json()["address"] == "New HQ, Pune"
    # default for companies registered without one
    r2 = client.post(f"{BASE}/fleet/companies", headers=fm, json=company_body("ADDR2", "No Address Co"))
    assert r2.json()["company"]["address"] == ""


def test_upgrade_adds_the_address_column_to_existing_company_schemas(client, world):
    from app import tenants
    with db.transaction(dict_rows=True) as cur:       # simulate a schema created before migration 0002
        cur.execute("ALTER TABLE tenant_beta_corp.employers DROP COLUMN address")
        cur.execute("DELETE FROM tenant_beta_corp.schema_migrations WHERE version='0002'")
        cur.execute("UPDATE tenant_beta_corp.employers SET name = name")
    assert tenants.upgrade_all_tenants()["beta_corp"] == ["0002"]
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT id, name, address FROM tenant_beta_corp.employers")
        row = cur.fetchone()
        assert row["id"] == "BETA" and row["name"] == "Beta Corp" and row["address"] == ""   # data kept


def test_company_registry_lists_live_counts(client, world):
    """Fleet workspace: employee / shift / site / vehicle counts per company, read from each schema."""
    fm = world["fm"]
    slug = client.post(f"{BASE}/fleet/companies", headers=fm, json=company_body("CNT1", "Counted Co")
                       ).json()["tenant"]["slug"]
    for i, typ in (("CNT-PK", "pickup"), ("CNT-OF", "office")):
        assert client.post(f"{BASE}/{slug}/locations", headers=fm, json={
            "id": i, "code": i, "name": i, "type": typ, "employerId": "CNT1"}).status_code == 201
    for e in ("CE1", "CE2"):
        assert client.post(f"{BASE}/{slug}/employees", headers=fm, json={
            "id": e, "employeeCode": e, "employerId": "CNT1", "name": e,
            "pickupLocationId": "CNT-PK", "destinationId": "CNT-OF"}).status_code == 201
    prof = client.get(f"{BASE}/{slug}/employers/CNT1", headers=fm).json()
    assert client.put(f"{BASE}/{slug}/employers/CNT1", headers=fm, json={
        **{k: v for k, v in prof.items() if k != "id"},
        "shifts": [{"id": "S1", "label": "Morning", "startMinutes": 540, "endMinutes": 1080}]}).status_code == 200
    assert client.post(f"{BASE}/fleet/vehicles", headers=fm, json={
        "id": "CNTV", "vehicleCode": "CNTV", "fictionalRegistration": "KA-CNT", "assignedEmployerId": "CNT1"}
        ).status_code == 201
    items = {i["code"]: i for i in client.get(f"{BASE}/fleet/companies", headers=fm).json()["items"]}
    assert items["CNT1"]["counts"] == {"employees": 2, "shifts": 1, "sites": 2, "vehicles": 1}
    # company roles cannot read the registry
    assert client.get(f"{BASE}/fleet/companies", headers=world["ea_a"]).status_code == 403
