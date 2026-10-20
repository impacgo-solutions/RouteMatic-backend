"""Security event log: structured, masked JSON lines; high-value events stored and readable by platform admins."""
import json
import logging

import pytest

from app import db, ratelimit
from app.config import get_settings
from app.security_log import mask_email

from .conftest import BASE, make_user

PW = "Passw0rd!x"


def _events(caplog, name):
    out = []
    for rec in caplog.records:
        if rec.name == "routematic.security":
            j = json.loads(rec.getMessage())
            if j["event"] == name:
                out.append(j)
    return out


def _stored(name, **match):
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT * FROM public.security_events WHERE event = %s ORDER BY id DESC", (name,))
        rows = [dict(r) for r in cur.fetchall()]
    return [r for r in rows if all(str(r.get(k)) == str(v) for k, v in match.items())]


def test_email_masking():
    assert mask_email("asha.rao@demo.example") == "a***@d***.example"
    assert mask_email("x@y") == "x***@y***"
    assert mask_email("") == ""


def test_failed_sign_in_is_logged_masked_and_without_the_password(client, world, caplog):
    caplog.set_level(logging.INFO, logger="routematic.security")
    ratelimit.reset()
    r = client.post("/api/v1/auth/login", json={"email": "ea.alpha@example.com", "password": "Wrong-Secret-42"})
    assert r.status_code == 401
    ev = _events(caplog, "login.failed")
    assert ev and ev[-1]["email"] == "e***@e***.com" and ev[-1]["requestId"]
    raw = " ".join(rec.getMessage() for rec in caplog.records if rec.name == "routematic.security")
    assert "Wrong-Secret-42" not in raw and "ea.alpha@example.com" not in raw
    ok = client.post("/api/v1/auth/login", json={"email": "ea.alpha@example.com", "password": PW})
    assert ok.status_code == 200
    assert _events(caplog, "login.success")
    assert ok.json()["accessToken"] not in " ".join(r.getMessage() for r in caplog.records)


def test_a_sign_in_burst_is_stored_as_a_lockout(client, world, monkeypatch):
    ratelimit.reset()
    monkeypatch.setattr(get_settings(), "login_max_failures", 3)
    make_user("burst@example.com", None, None)
    for _ in range(3):
        assert client.post("/api/v1/auth/login",
                           json={"email": "burst@example.com", "password": "nope-nope"}).status_code == 401
    locked = client.post("/api/v1/auth/login", json={"email": "burst@example.com", "password": PW})
    assert locked.status_code == 429
    assert _stored("login.lockout_triggered", email="b***@e***.com")
    ratelimit.reset()


def test_cross_tenant_attempts_and_denials_are_recorded(client, world, caplog):
    caplog.set_level(logging.INFO, logger="routematic.security")
    r = client.get(f"{BASE}/beta_corp/employees", headers=world["emp_a"])
    assert r.status_code == 403
    rows = _stored("tenant.denied", tenant="beta_corp")
    assert rows and rows[0]["user_id"] is not None and rows[0]["details"]["reason"] == "no membership"
    # a route-level 403 (role not allowed) and a 401 are logged centrally
    assert client.post(f"{BASE}/alpha_corp/companies", headers=world["emp_a"], json={}).status_code in (403, 404, 422)
    assert client.get("/api/v1/auth/me").status_code == 401
    assert _events(caplog, "auth.denied")


def test_privilege_changes_are_stored(client, world):
    r = client.post("/api/v1/tenants/alpha_corp/members", headers=world["root"], json={
        "email": "granted.emp@example.com", "displayName": "Granted", "password": PW,
        "role": "employee", "scopeId": "E77"})
    assert r.status_code == 201, r.text
    rows = _stored("privilege.member_add", tenant="alpha_corp")
    assert rows and rows[0]["details"]["role"] == "employee" and rows[0]["email"] == ""
    assert rows[0]["details"]["target"] == "g***@e***.com"


def test_refresh_reuse_is_stored(client, world):
    make_user("reuse.log@example.com", None, None)
    j = client.post("/api/v1/auth/login", json={"email": "reuse.log@example.com", "password": PW}).json()
    assert client.post("/api/v1/auth/refresh", json={"refreshToken": j["refreshToken"]}).status_code == 200
    assert client.post("/api/v1/auth/refresh", json={"refreshToken": j["refreshToken"]}).status_code == 401
    assert _stored("refresh.reuse_detected", email="r***@e***.com")


def test_oversized_upload_is_logged(client, caplog):
    caplog.set_level(logging.INFO, logger="routematic.security")
    big = b"x" * (get_settings().max_body_bytes + 1)
    r = client.post("/api/v1/auth/login", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert _events(caplog, "upload.rejected")


@pytest.mark.parametrize("who,status", [("root", 200), ("fm", 403), ("ea_a", 403), ("emp_a", 403)])
def test_only_platform_admins_read_stored_events(client, world, who, status):
    r = client.get("/api/v1/security/events?pageSize=5", headers=world[who])
    assert r.status_code == status, r.text
    if status == 200:
        j = r.json()
        assert j["total"] >= 1 and len(j["items"]) <= 5
        ats = [i["at"] for i in j["items"]]
        assert ats == sorted(ats, reverse=True)
        f = client.get("/api/v1/security/events?event=tenant.denied", headers=world[who]).json()
        assert all(i["event"] == "tenant.denied" for i in f["items"])


def test_events_endpoint_requires_sign_in(client):
    assert client.get("/api/v1/security/events").status_code == 401


def test_privilege_changes_by_staff_routes_are_recorded(client, world):
    """Manager creation and employee login setup leave a stored privilege event (no passwords)."""
    from app import db
    r = client.post(f"{BASE}/fleet/staff/managers", headers=world["fm"], json={
        "name": "Event Manager", "email": "event.manager@example.com", "password": "Passw0rd!x",
        "phone": "+91 90000 00003"})
    assert r.status_code == 201, r.text
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT event, details::text AS d FROM public.security_events "
                    "WHERE event = 'privilege.manager_create' ORDER BY at DESC LIMIT 1")
        row = cur.fetchone()
    assert row is not None
    assert "Passw0rd" not in row["d"] and "event.manager@example.com" not in row["d"]
