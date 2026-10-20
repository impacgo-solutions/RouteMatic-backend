"""Security regression tests: sessions, sign-in abuse limits, tokens, HTTP hardening, safe errors."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
import psycopg2
import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app import config, db, ratelimit, repo, security
from app.config import Settings, validate
from app.hardening import BodySizeLimit

from .conftest import BASE, login, make_user

PW = "Passw0rd!x"


def signin(client, email, password=PW, tenant="fleet", role="fleetManager"):
    return client.post("/api/v1/auth/signin", json={"email": email, "password": password,
                                                    "tenant": tenant, "role": role})


# ------------------------------------------------------------------ logout / change password
def test_logout_revokes_every_token_of_the_account(client, world):
    make_user("logout@example.com", "fleetManager", "fleet")
    t1, t2 = login(client, "logout@example.com"), login(client, "logout@example.com")   # two devices
    assert client.get("/api/v1/auth/me", headers=t1).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=t1).status_code == 200
    for t in (t1, t2):
        assert client.get("/api/v1/auth/me", headers=t).status_code == 401
    assert client.post("/api/v1/auth/logout").status_code == 401          # needs a session
    assert client.get("/api/v1/auth/me", headers=login(client, "logout@example.com")).status_code == 200


def test_change_password_requires_the_current_one_and_ends_old_sessions(client, world):
    ratelimit.reset()
    make_user("chpw@example.com", "fleetManager", "fleet")
    old = login(client, "chpw@example.com")
    url = "/api/v1/auth/change-password"
    r = client.post(url, headers=old, json={"currentPassword": "wrong-pass-9", "newPassword": "New-pass-123"})
    assert r.status_code == 403
    assert client.post(url, headers=old, json={"currentPassword": PW, "newPassword": PW}).status_code == 422
    assert client.post(url, headers=old, json={"currentPassword": PW, "newPassword": "short"}).status_code == 422
    assert client.post(url, json={"currentPassword": PW, "newPassword": "New-pass-123"}).status_code == 401
    r = client.post(url, headers=old, json={"currentPassword": PW, "newPassword": "New-pass-123"})
    assert r.status_code == 200, r.text
    fresh = {"Authorization": f"Bearer {r.json()['accessToken']}"}
    assert client.get("/api/v1/auth/me", headers=old).status_code == 401          # old session ended
    assert client.get("/api/v1/auth/me", headers=fresh).status_code == 200
    assert signin(client, "chpw@example.com", PW).status_code == 401
    assert signin(client, "chpw@example.com", "New-pass-123").status_code == 200
    ratelimit.reset()


# ------------------------------------------------------------------ brute force
def test_password_spraying_from_one_client_is_limited(client, world, monkeypatch):
    ratelimit.reset()
    monkeypatch.setattr(config.get_settings(), "login_ip_max_failures", 5)
    try:
        for i in range(5):
            assert signin(client, f"nobody{i}@example.com", "wrong-pass-1").status_code == 401
        r = signin(client, "fm@example.com")              # another account, same client: blocked too
        assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    finally:
        ratelimit.reset()


def test_one_account_is_limited_across_clients_and_memory_stays_bounded(monkeypatch):
    ratelimit.reset()
    monkeypatch.setattr(config.get_settings(), "login_account_max_failures", 4)
    for i in range(4):
        ratelimit.failed(f"10.0.0.{i}", "Victim@Example.com")
    assert ratelimit.retry_after("10.9.9.9", "victim@example.com") > 0      # a fresh client is still blocked
    ratelimit.reset()
    for i in range(500):                      # look-ups never create entries
        assert ratelimit.retry_after(f"10.1.{i // 250}.{i % 250}", f"u{i}@example.com") == 0
    assert ratelimit.size() == 0
    ratelimit.succeeded("10.0.0.1", "x@example.com")
    assert ratelimit.size() == 0
    ratelimit.reset()


# ------------------------------------------------------------------ tokens and configuration
def _forge(claims: dict, key: str | None = None) -> dict:
    tok = jwt.encode(claims, key or config.get_settings().jwt_secret, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


def test_tokens_must_carry_issuer_audience_and_expiry(client, world):
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT id::text FROM public.platform_users WHERE email='fm@example.com'")
        uid = cur.fetchone()["id"]
    s = config.get_settings()
    now = datetime.now(timezone.utc)
    good = {"sub": uid, "tv": 0, "iat": now, "exp": now + timedelta(minutes=5),
            "iss": s.jwt_issuer, "aud": s.jwt_audience}
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT token_version FROM public.platform_users WHERE id=%s::uuid", (uid,))
        good["tv"] = cur.fetchone()["token_version"]
    assert client.get("/api/v1/auth/me", headers=_forge(good)).status_code == 200
    for drop in ("iss", "aud", "exp", "iat"):
        bad = {k: v for k, v in good.items() if k != drop}
        assert client.get("/api/v1/auth/me", headers=_forge(bad)).status_code == 401, drop
    assert client.get("/api/v1/auth/me", headers=_forge({**good, "aud": "other-app"})).status_code == 401
    assert client.get("/api/v1/auth/me", headers=_forge({**good, "iss": "evil"})).status_code == 401
    assert client.get("/api/v1/auth/me", headers=_forge(good, key="x" * 40)).status_code == 401
    unsigned = jwt.encode(good, None, algorithm="none")
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {unsigned}"}).status_code == 401
    claims = security.decode_claims(security.create_token(uid, good["tv"]))
    assert claims["iss"] == s.jwt_issuer and claims["aud"] == s.jwt_audience and claims["jti"]


def test_startup_refuses_weak_or_placeholder_jwt_secrets():
    with pytest.raises(RuntimeError, match="32"):
        validate(Settings(jwt_secret="x" * 20, db_name="Routematic"))
    with pytest.raises(RuntimeError, match="placeholder"):
        validate(Settings(jwt_secret="change-me-to-a-long-random-string", db_name="Routematic"))
    with pytest.raises(RuntimeError, match="DB_SSLMODE"):
        validate(Settings(jwt_secret="y" * 40, db_name="Routematic", db_sslmode="sometimes"))
    validate(Settings(jwt_secret=secrets.token_urlsafe(48), db_name="Routematic"))


def test_remote_database_connections_require_tls():
    assert Settings(db_host="203.0.113.7").effective_sslmode == "require"
    assert Settings(db_host="localhost").effective_sslmode == "prefer"
    assert Settings(db_host="127.0.0.1").effective_sslmode == "prefer"
    assert Settings(db_host="203.0.113.7", db_sslmode="verify-full").effective_sslmode == "verify-full"
    assert db._connect_kwargs("x")["sslmode"] == config.get_settings().effective_sslmode


def test_production_defaults_are_strict():
    f = Settings.model_fields
    assert f["cors_allow_localhost"].default is False
    assert f["enable_docs"].default is False
    assert f["password_hash_iterations"].default >= 600_000


# ------------------------------------------------------------------ password hashing
def test_old_password_hashes_are_upgraded_on_sign_in_without_ending_sessions(client, world):
    make_user("rehash@example.com", "fleetManager", "fleet")
    salt = secrets.token_hex(16)
    weak = f"pbkdf2_sha256$50000${salt}${hashlib.pbkdf2_hmac('sha256', PW.encode(), bytes.fromhex(salt), 50000).hex()}"
    with db.transaction() as cur:
        cur.execute("UPDATE public.platform_users SET password_hash=%s WHERE email='rehash@example.com'", (weak,))
        cur.execute('UPDATE "fleet_workspace"."admins" SET password_hash=%s WHERE email=%s',
                    (weak, "rehash@example.com"))
    before = login(client, "rehash@example.com")           # old hash still accepted ...
    with db.transaction() as cur:
        cur.execute("SELECT password_hash FROM public.platform_users WHERE email='rehash@example.com'")
        stored = cur.fetchone()[0]
    assert int(stored.split("$")[1]) == config.get_settings().password_hash_iterations   # ... and upgraded
    assert client.get("/api/v1/auth/me", headers=before).status_code == 200            # session kept
    assert security.verify_password(PW, stored)


# ------------------------------------------------------------------ HTTP hardening
def test_security_headers_and_request_id(client):
    r = client.get("/health")
    assert r.json() == {"status": "ok"}
    h = r.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert "strict-transport-security" not in h          # only when HSTS_ENABLED (HTTPS deployments)
    assert len(h["x-request-id"]) >= 8
    assert client.get("/health", headers={"X-Request-ID": "trace-12345678"}).headers["x-request-id"] == \
        "trace-12345678"
    assert client.get("/health", headers={"X-Request-ID": "<script>"}).headers["x-request-id"] != "<script>"


def test_api_docs_are_not_public_by_default(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_oversized_request_bodies_are_refused(client):
    big = b"x" * (config.get_settings().max_body_bytes + 1)
    r = client.post("/api/v1/auth/login", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["status"] == 413

    async def echo(request):
        return JSONResponse({"n": len(await request.body())})

    small = TestClient(BodySizeLimit(Starlette(routes=[Route("/", echo, methods=["POST"])]), max_bytes=100))
    assert small.post("/", content=b"y" * 100).json() == {"n": 100}
    assert small.post("/", content=b"y" * 101).status_code == 413
    # streamed body without Content-Length is counted too
    assert small.post("/", content=iter([b"y" * 60, b"y" * 60])).status_code == 413


def test_overlong_text_values_are_refused(client, world):
    r = client.post(f"{BASE}/fleet/drivers", headers=world["fm"], json={
        "id": "LONG1", "driverCode": "LONG1", "name": "n" * (repo.MAX_TEXT + 1), "licenceNumber": "L-LONG1"})
    assert r.status_code == 422 and "at most" in str(r.json()["error"]["details"])


# ------------------------------------------------------------------ safe errors
def test_database_errors_do_not_reveal_constraints_or_values():
    with pytest.raises(psycopg2.errors.UniqueViolation) as caught:
        with db.transaction() as cur:
            cur.execute("CREATE TEMP TABLE t_sec(secret_code text UNIQUE)")
            cur.execute("INSERT INTO t_sec VALUES ('TOP-SECRET-1'), ('TOP-SECRET-1')")
    err = repo.map_db_error(caught.value)
    text = f"{err.message} {err.details}"
    assert err.status == 409 and "secretCode" in text
    assert "TOP-SECRET-1" not in text and "t_sec_secret_code_key" not in text and "Key (" not in text


def test_duplicate_through_the_api_is_a_clean_409(client, world):
    fm = world["fm"]
    body = {"id": "DUPV1", "vehicleCode": "DUPV1", "fictionalRegistration": "DUP-REG-1"}
    assert client.post(f"{BASE}/fleet/vehicles", headers=fm, json=body).status_code == 201
    r = client.post(f"{BASE}/fleet/vehicles", headers=fm, json={**body, "id": "DUPV2", "vehicleCode": "DUPV2"})
    assert r.status_code == 409
    msg = str(r.json()["error"])
    assert "DUP-REG-1" not in msg and "constraint" not in msg


# ------------------------------------------------------------------ membership endpoint
def test_add_member_cannot_attach_privileged_accounts_or_foreign_company_scopes(client, world):
    fm = world["fm"]
    # a platform admin's account cannot be attached to another role by a Fleet Admin
    r = client.post("/api/v1/tenants/alpha_corp/members", headers=fm, json={
        "email": "root@example.com", "displayName": "Root", "role": "employee", "scopeId": "E77"})
    assert r.status_code == 409
    # an Employer Admin's scope must be this company's own code
    r = client.post("/api/v1/tenants/alpha_corp/members", headers=fm, json={
        "email": "ea.cross@example.com", "displayName": "X", "password": PW,
        "role": "employerAdmin", "scopeId": "BETA"})
    assert r.status_code == 422
    r = client.post("/api/v1/tenants/alpha_corp/members", headers=fm, json={
        "email": "ea.second@example.com", "displayName": "Second EA", "password": PW,
        "role": "employerAdmin", "scopeId": "ALPHA"})
    assert r.status_code == 201, r.text
