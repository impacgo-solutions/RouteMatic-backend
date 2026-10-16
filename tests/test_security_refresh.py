"""Refresh tokens: short access tokens renewed with single-use, rotated refresh tokens; replay ends everything."""
import hashlib

import jwt

from app import db
from app.config import get_settings

from .conftest import make_user

PW = "Passw0rd!x"
REFRESH = "/api/v1/auth/refresh"


def _login(client, email):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()


def _me(client, access):
    return client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access}"}).status_code


def test_sign_in_returns_a_refresh_token_and_a_short_access_token(client, world):
    make_user("rt.basic@example.com", None, None)
    j = _login(client, "rt.basic@example.com")
    assert len(j["refreshToken"]) >= 40 and j["tokenType"] == "bearer"
    minutes = get_settings().access_minutes
    assert j["expiresIn"] == minutes * 60
    claims = jwt.decode(j["accessToken"], options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == minutes * 60
    # only the SHA-256 hash is stored, never the token itself
    with db.transaction() as cur:
        cur.execute("SELECT token_hash FROM public.refresh_tokens WHERE token_hash IN (%s, %s)",
                    (j["refreshToken"], hashlib.sha256(j["refreshToken"].encode()).hexdigest()))
        assert [r[0] for r in cur.fetchall()] == [hashlib.sha256(j["refreshToken"].encode()).hexdigest()]


def test_signin_endpoint_also_returns_a_refresh_token(client, world):
    r = client.post("/api/v1/auth/signin", json={"email": "ea.alpha@example.com", "password": PW,
                                                 "tenant": "alpha_corp", "role": "employerAdmin"})
    assert r.status_code == 200, r.text
    assert r.json()["refreshToken"] and r.json()["expiresIn"] > 0


def test_refresh_rotates_and_replaying_a_used_token_ends_every_session(client, world):
    make_user("rt.rotate@example.com", None, None)
    first = _login(client, "rt.rotate@example.com")
    r = client.post(REFRESH, json={"refreshToken": first["refreshToken"]})
    assert r.status_code == 200, r.text
    second = r.json()
    assert set(second) >= {"accessToken", "refreshToken", "expiresIn", "tokenType"}
    assert second["refreshToken"] != first["refreshToken"]
    assert _me(client, second["accessToken"]) == 200
    # the new refresh token works once more (legitimate chain)
    third = client.post(REFRESH, json={"refreshToken": second["refreshToken"]}).json()
    assert _me(client, third["accessToken"]) == 200
    # a stolen copy of an already-used token is replayed -> 401 and the whole account is signed out
    replay = client.post(REFRESH, json={"refreshToken": first["refreshToken"]})
    assert replay.status_code == 401
    assert replay.json()["error"]["message"] == "Session expired. Please sign in again."
    assert _me(client, third["accessToken"]) == 401
    assert client.post(REFRESH, json={"refreshToken": third["refreshToken"]}).status_code == 401
    # the owner can still sign in again with the password
    assert _me(client, _login(client, "rt.rotate@example.com")["accessToken"]) == 200


def test_unknown_or_malformed_refresh_tokens_are_refused(client, world):
    assert client.post(REFRESH, json={"refreshToken": "x" * 64}).status_code == 401
    assert client.post(REFRESH, json={"refreshToken": "short"}).status_code == 422
    assert client.post(REFRESH, json={}).status_code == 422


def test_logout_and_password_change_revoke_refresh_tokens(client, world):
    make_user("rt.logout@example.com", None, None)
    a = _login(client, "rt.logout@example.com")
    b = _login(client, "rt.logout@example.com")          # a second device
    out = client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {a['accessToken']}"})
    assert out.status_code == 200
    for dev in (a, b):
        assert client.post(REFRESH, json={"refreshToken": dev["refreshToken"]}).status_code == 401

    c = _login(client, "rt.logout@example.com")
    d = _login(client, "rt.logout@example.com")
    ch = client.post("/api/v1/auth/change-password", headers={"Authorization": f"Bearer {c['accessToken']}"},
                     json={"currentPassword": PW, "newPassword": "N3w-Passw0rd!"})
    assert ch.status_code == 200, ch.text
    assert ch.json()["refreshToken"] and ch.json()["accessToken"]
    assert client.post(REFRESH, json={"refreshToken": d["refreshToken"]}).status_code == 401
    # the token returned by the change keeps this device signed in
    assert client.post(REFRESH, json={"refreshToken": ch.json()["refreshToken"]}).status_code == 200


def test_expired_tokens_and_disabled_accounts_cannot_refresh(client, world):
    make_user("rt.expire@example.com", None, None)
    j = _login(client, "rt.expire@example.com")
    h = hashlib.sha256(j["refreshToken"].encode()).hexdigest()
    with db.transaction() as cur:
        cur.execute("UPDATE public.refresh_tokens SET expires_at = now() - interval '1 minute' WHERE token_hash=%s",
                    (h,))
    assert client.post(REFRESH, json={"refreshToken": j["refreshToken"]}).status_code == 401

    make_user("rt.disabled@example.com", None, None)
    k = _login(client, "rt.disabled@example.com")
    with db.transaction() as cur:
        cur.execute("UPDATE public.platform_users SET active = FALSE WHERE email = 'rt.disabled@example.com'")
    try:
        assert client.post(REFRESH, json={"refreshToken": k["refreshToken"]}).status_code == 401
    finally:
        with db.transaction() as cur:
            cur.execute("UPDATE public.platform_users SET active = TRUE WHERE email = 'rt.disabled@example.com'")


def test_a_token_version_change_invalidates_outstanding_refresh_tokens(client, world):
    """Any path that bumps token_version (deactivation, admin reset) also ends refresh renewals."""
    make_user("rt.version@example.com", None, None)
    j = _login(client, "rt.version@example.com")
    with db.transaction() as cur:
        cur.execute("UPDATE public.platform_users SET token_version = token_version + 1 "
                    "WHERE email = 'rt.version@example.com'")
    assert client.post(REFRESH, json={"refreshToken": j["refreshToken"]}).status_code == 401
