"""Routematic_Full_End_to_End_Audit_and_Fixes.md — regression tests for the findings of that audit."""
import pytest

from app import db, ratelimit
from app.config import Settings, validate

from .conftest import BASE, make_user
from .test_ownership import T, setup_gamma

PW = "Passw0rd!x"


def signin(client, email, password, tenant="fleet", role="fleetManager"):
    return client.post("/api/v1/auth/signin", json={"email": email, "password": password, "tenant": tenant,
                                                    "role": role})


# ------------------------------------------------------------------ brute-force protection
def test_repeated_failed_sign_ins_are_locked_out(client, world):
    ratelimit.reset()
    make_user("lockout@example.com", "fleetManager", "fleet")
    try:
        for _ in range(10):
            assert signin(client, "lockout@example.com", "wrong-pass-1").status_code == 401
        r = signin(client, "lockout@example.com", PW)          # even the right password, while locked
        assert r.status_code == 429 and "Too many failed sign-in attempts" in r.json()["error"]["message"]
        assert int(r.headers["Retry-After"]) > 0
        r = client.post("/api/v1/auth/login", json={"email": "lockout@example.com", "password": PW})
        assert r.status_code == 429
        # other accounts are not affected
        assert signin(client, "fm@example.com", PW).status_code == 200
    finally:
        ratelimit.reset()
    # a successful sign-in clears earlier failures
    for _ in range(9):
        signin(client, "lockout@example.com", "wrong-pass-1")
    assert signin(client, "lockout@example.com", PW).status_code == 200
    for _ in range(9):
        assert signin(client, "lockout@example.com", "wrong-pass-1").status_code == 401
    assert signin(client, "lockout@example.com", PW).status_code == 200
    ratelimit.reset()


# ------------------------------------------------------------------ configuration and protected databases
def test_startup_refuses_incomplete_or_unsafe_configuration():
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        validate(Settings(jwt_secret="short", db_name="Routematic"))
    with pytest.raises(RuntimeError, match="protected"):
        validate(Settings(jwt_secret="x" * 32, db_name="impac_test_dev"))
    validate(Settings(jwt_secret="x" * 32, db_name="Routematic"))      # a complete configuration passes
    for name in ("impaco_test_dev", "impac_test_dev", "IMPAC_TEST_DEV"):
        with pytest.raises(db.DatabaseSafetyError):
            db.validate_db_name(name)


def test_cors_allows_local_development_origins_only(client):
    ok = client.options("/api/v1/auth/organizations", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
    bad = client.options("/api/v1/auth/organizations", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert bad.headers.get("access-control-allow-origin") is None


# ------------------------------------------------------------------ audit trail of administrative actions
def _audit(schema, action):
    with db.transaction(dict_rows=True) as cur:
        cur.execute(f'SELECT actor_id, entity_id, summary, related::text AS related FROM "{schema}".audit_events '
                    "WHERE action = %s ORDER BY id", (action,))
        return [dict(r) for r in cur.fetchall()]


def test_administrative_actions_are_audited_without_secrets(client, world):
    u = setup_gamma(client, world)
    fm = world["fm"]
    # manager lifecycle (Fleet workspace)
    m = client.post(f"{BASE}/fleet/staff/managers", headers=fm, json={
        "name": "Audited Mgr", "email": "audited.mgr@example.com", "password": PW}).json()
    assert client.patch(f"{BASE}/fleet/staff/managers/{m['id']}", headers=fm,
                        json={"password": "Changed-pass-1"}).status_code == 200
    assert client.post(f"{BASE}/fleet/staff/managers/{m['id']}/deactivate", headers=fm).status_code == 200
    created = _audit("fleet_workspace", "manager.create")
    assert any(r["entity_id"] == m["id"] and r["actor_id"] == "fm@example.com" for r in created)
    updated = [r for r in _audit("fleet_workspace", "manager.update") if r["entity_id"] == m["id"]]
    assert updated and "password" in updated[-1]["summary"]
    assert any(r["entity_id"] == m["id"] for r in _audit("fleet_workspace", "manager.deactivate"))
    # leave the shared test data as it was (other tests list inactive managers)
    assert client.post(f"{BASE}/fleet/staff/managers/{m['id']}/activate", headers=fm).status_code == 200
    assert any(r["entity_id"] == m["id"] for r in _audit("fleet_workspace", "manager.activate"))
    # employee login set-up (company workspace)
    assert client.post(f"{BASE}/{T}/employees", headers=fm, json={
        "id": "AUD-E", "employeeCode": "AUD-E", "employerId": "GAMMA", "name": "Audited",
        "pickupLocationId": "G-PK", "destinationId": "G-OF"}).status_code == 201
    assert client.put(f"{BASE}/{T}/employees/AUD-E/login", headers=u["ea"], json={
        "email": "audited.emp@example.com", "password": PW}).status_code == 200
    rows = [r for r in _audit("tenant_gamma_corp", "login.set") if r["entity_id"] == "AUD-E"]
    assert rows and rows[-1]["actor_id"] == "ea.gamma@example.com"
    # driver transfer
    assert client.post(f"{BASE}/fleet/drivers", headers=fm, json={
        "id": "AUD-D", "driverCode": "AUD-D", "name": "Moved", "licenceNumber": "LIC-AUDD"}).status_code == 201
    for company in ("ALPHA", "GAMMA"):
        assert client.put(f"{BASE}/fleet/drivers/AUD-D/login", headers=fm, json={
            "email": "audited.drv@example.com", "password": PW, "company": company}).status_code == 200
    assert any(r["entity_id"] == "AUD-D" for r in _audit("fleet_workspace", "driver.transfer"))
    # never a password, hash or token in the audit trail
    with db.transaction(dict_rows=True) as cur:
        for schema in ("fleet_workspace", "tenant_gamma_corp", "tenant_alpha_corp"):
            cur.execute(f'SELECT string_agg(row_to_json(a)::text, \' \') t FROM "{schema}".audit_events a')
            text = cur.fetchone()["t"] or ""
            for secret in (PW, "Changed-pass-1", "pbkdf2_sha256", "Bearer"):
                assert secret not in text, (schema, secret)
