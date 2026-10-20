"""Tests run against a throw-away database (routematic_test_<random>) that is
created and dropped by the test session. They never touch ``Routematic`` data or
``impaco_test_dev``."""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEST_DB = f"routematic_test_{secrets.token_hex(4)}"
os.environ["DB_NAME"] = TEST_DB
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-0123456789")
# development-style settings for the test client (production defaults are stricter)
os.environ.setdefault("CORS_ALLOW_LOCALHOST", "true")
# lower PBKDF2 work factor keeps the suite fast; production uses the 600,000 default
os.environ.setdefault("PASSWORD_HASH_ITERATIONS", "100000")

from app import config  # noqa: E402

config.get_settings.cache_clear()
if len(config.get_settings().jwt_secret) < 32:
    os.environ["JWT_SECRET"] = "test-secret-test-secret-0123456789"
    config.get_settings.cache_clear()

from app import admins, db, migrations, security, tenants  # noqa: E402
from psycopg2 import sql  # noqa: E402

BASE = "/api/v1/t"


@pytest.fixture(scope="session", autouse=True)
def test_database():
    assert config.get_settings().db_name == TEST_DB
    db.ensure_database()
    with db.transaction() as cur:
        migrations.apply_shared(cur)
    tenants.provision_fleet()
    yield TEST_DB
    db.close_pool()
    conn = db.admin_connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            assert TEST_DB.startswith("routematic_test_")   # never drop anything else
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DB)))
    finally:
        conn.close()


@pytest.fixture(scope="session")
def client(test_database):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_user(email: str, role: str | None, tenant_slug: str | None, scope: str | None = None,
              admin: bool = False, password: str = "Passw0rd!x") -> None:
    with db.transaction(dict_rows=True) as cur:
        cur.execute("INSERT INTO public.platform_users(email, password_hash, display_name, is_platform_admin)"
                    " VALUES (%s,%s,%s,%s) ON CONFLICT (lower(email)) DO NOTHING RETURNING id",
                    (email, security.hash_password(password), email.split("@")[0], admin))
        r = cur.fetchone()
        if r and tenant_slug:
            cur.execute("INSERT INTO public.tenant_memberships(user_id, tenant_id, role, scope_id) "
                        "SELECT %s, id, %s, %s FROM public.tenants WHERE slug=%s",
                        (r["id"], role, scope, tenant_slug))
            if role == "fleetManager":
                admins.ensure_admin(cur, str(r["id"]))
            if tenant_slug == "fleet" and role in ("fleetManager", "driver"):
                cur.execute('INSERT INTO "fleet_workspace"."fleet_users"(user_id, role) VALUES (%s,%s) '
                            "ON CONFLICT (user_id) DO NOTHING", (r["id"], role))


def login(client, email: str, password: str = "Passw0rd!x") -> dict:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['accessToken']}"}


@pytest.fixture(scope="session")
def world(client):
    """The fleet workspace plus two companies registered the way the app does it.

    fm      Fleet Admin (fleet workspace; also fleetManager in each company it creates)
    root    platform admin
    ea_a    Employer Admin of company ALPHA (alpha_corp)     ea_b  Employer Admin of BETA
    emp_a   an employee login in alpha_corp (scope E1)
    """
    make_user("root@example.com", None, None, admin=True)
    make_user("fm@example.com", "fleetManager", "fleet")
    fm = login(client, "fm@example.com")
    for code, name, slug, email in (("ALPHA", "Alpha Corp", "alpha_corp", "ea.alpha@example.com"),
                                    ("BETA", "Beta Corp", "beta_corp", "ea.beta@example.com")):
        r = client.post(f"{BASE}/fleet/companies", headers=fm, json={
            "slug": slug, "employer": {"code": code, "name": name},
            "admin": {"email": email, "displayName": f"{code} Admin", "password": "Passw0rd!x"}})
        assert r.status_code == 201, r.text
    make_user("emp.alpha@example.com", "employee", "alpha_corp", scope="E1")
    return {
        "root": login(client, "root@example.com"),
        "fm": fm,
        "ea_a": login(client, "ea.alpha@example.com"),
        "ea_b": login(client, "ea.beta@example.com"),
        "emp_a": login(client, "emp.alpha@example.com"),
    }
