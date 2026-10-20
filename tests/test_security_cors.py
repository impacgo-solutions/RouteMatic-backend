"""CORS allow-list and browser security headers, checked on real responses."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import cors_options

GOOD = "https://app.example.com"
EVIL = "https://evil.example.net"
LOCAL = "http://localhost:5000"


def _cors_app(**settings) -> TestClient:
    s = Settings(**settings)
    api = FastAPI()
    api.add_middleware(CORSMiddleware, **cors_options(s))

    @api.get("/ping")
    def ping():
        return {"ok": True}

    return TestClient(api)


def _preflight(c, origin, method="POST"):
    return c.options("/ping", headers={"Origin": origin, "Access-Control-Request-Method": method,
                                       "Access-Control-Request-Headers": "authorization,content-type"})


def test_allowed_origin_preflight_echoes_exactly_that_origin():
    c = _cors_app(cors_origins=GOOD, cors_allow_localhost=False)
    r = _preflight(c, GOOD)
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == GOOD
    assert "access-control-allow-credentials" not in r.headers
    assert "authorization" in r.headers["access-control-allow-headers"].lower()
    simple = c.get("/ping", headers={"Origin": GOOD})
    assert simple.headers["access-control-allow-origin"] == GOOD
    assert "access-control-allow-credentials" not in simple.headers


def test_other_origins_get_no_cors_permission():
    c = _cors_app(cors_origins=GOOD, cors_allow_localhost=False)
    for origin in (EVIL, GOOD + ".evil.net", "null", "https://app.example.com:8443"):
        assert "access-control-allow-origin" not in _preflight(c, origin).headers, origin
        assert "access-control-allow-origin" not in c.get("/ping", headers={"Origin": origin}).headers


def test_localhost_only_when_explicitly_enabled():
    off = _cors_app(cors_origins=GOOD, cors_allow_localhost=False)
    assert "access-control-allow-origin" not in _preflight(off, LOCAL).headers
    on = _cors_app(cors_origins=GOOD, cors_allow_localhost=True)
    assert _preflight(on, LOCAL).headers["access-control-allow-origin"] == LOCAL
    # the localhost rule matches the whole origin, not a prefix
    assert "access-control-allow-origin" not in _preflight(on, "http://localhost.evil.net").headers
    assert "access-control-allow-origin" not in _preflight(on, "https://localhost:5000").headers


def test_production_default_is_no_localhost():
    assert Settings.model_fields["cors_allow_localhost"].default is False


SECURITY_HEADERS = {"x-content-type-options": "nosniff", "x-frame-options": "DENY",
                    "referrer-policy": "no-referrer"}


def _has_security_headers(r):
    for k, v in SECURITY_HEADERS.items():
        assert r.headers.get(k) == v, (k, r.status_code)
    assert "frame-ancestors 'none'" in r.headers.get("content-security-policy", "")
    assert r.headers.get("x-request-id")


def test_security_headers_on_success_and_error_responses(client):
    _has_security_headers(client.get("/health"))                              # 200
    _has_security_headers(client.get("/api/v1/auth/me"))                      # 401
    _has_security_headers(client.get("/api/v1/does-not-exist"))               # 404
    _has_security_headers(client.post("/api/v1/auth/login", json={}))         # 422
    _has_security_headers(client.options("/api/v1/auth/login", headers={       # CORS preflight
        "Origin": LOCAL, "Access-Control-Request-Method": "POST"}))


def test_oversized_body_is_refused_with_413_and_headers(client):
    r = client.post("/api/v1/auth/login", content=b"{" + b" " * get_settings().max_body_bytes + b"}",
                    headers={"content-type": "application/json"})
    assert r.status_code == 413
    _has_security_headers(r)


def test_api_docs_are_off_by_default(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
