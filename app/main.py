"""FastAPI application entry point.

    uvicorn app.main:app --reload --port 8000     (run from the backend/ folder)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import psycopg2
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from . import db, security_log
from .config import get_settings, validate
from .hardening import BodySizeLimit, SecurityHeaders, request_id
from .repo import ApiError
from .routers import companies, logins, platform, resources, security_events, session, staff, sync

log = logging.getLogger("routematic")
from app.routers import route_simulation

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Fail fast with an actionable message if the configuration is incomplete or the database is wrong.
    validate(get_settings())
    info = db.health()
    log.info("Connected to database '%s'", info["database"])
    yield
    db.close_pool()


def cors_options(s) -> dict:
    """CORS: an explicit allow-list (CORS_ORIGINS), plus any http://localhost port only when
    CORS_ALLOW_LOCALHOST=true (development). Never credentials: the API uses Bearer tokens."""
    return dict(allow_origins=s.origins,
                allow_origin_regex=(r"http://(localhost|127\.0\.0\.1)(:\d+)?" if s.cors_allow_localhost else None),
                allow_credentials=False,
                allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type", "If-None-Match", "X-Request-ID"],
                expose_headers=["ETag", "X-Request-ID", "Retry-After"])


_s = get_settings()
# API docs (Swagger UI / ReDoc / openapi.json) only when ENABLE_DOCS=true
app = FastAPI(title="Routematic API", version="1.0.0", lifespan=lifespan,
              docs_url="/docs" if _s.enable_docs else None,
              redoc_url="/redoc" if _s.enable_docs else None,
              openapi_url="/openapi.json" if _s.enable_docs else None)
# large JSON (a whole workspace on /sync/pull) compresses about ten-fold
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.add_middleware(CORSMiddleware, **cors_options(_s))
app.add_middleware(BodySizeLimit, max_bytes=_s.max_body_bytes)
if _s.hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_s.hosts)
# outermost: every response (including CORS preflight and size refusals) gets a request id + security headers
app.add_middleware(SecurityHeaders, hsts=_s.hsts_enabled, docs=_s.enable_docs)


def _err(status: int, message: str, details=None, rid: str | None = None) -> JSONResponse:
    body = {"error": {"status": status, "message": message, "details": details}}
    if rid:
        body["error"]["requestId"] = rid
    return JSONResponse(body, status_code=status, headers={"X-Request-ID": rid} if rid else None)


_DENIED_EVENTS = {401: "auth.denied", 403: "access.denied", 413: "upload.rejected"}


def _log_refusal(request: Request, status: int, message) -> None:
    """Central security log of refused requests (401 / 403 / 413) from every route."""
    name = _DENIED_EVENTS.get(status)
    if name is None or getattr(request.state, "security_logged", False):
        return
    security_log.event(name, "refused", request=request, method=request.method, path=request.url.path,
                       status=status, reason=str(message)[:200])


@app.exception_handler(ApiError)
async def api_error(request: Request, exc: ApiError):
    _log_refusal(request, exc.status, exc.message)
    return _err(exc.status, exc.message, exc.details)


@app.exception_handler(db.TenantNotFound)
async def tenant_missing(_: Request, exc: db.TenantNotFound):
    return _err(403, "Access to this tenant is not permitted.")


@app.exception_handler(psycopg2.OperationalError)
async def db_down(request: Request, exc: psycopg2.OperationalError):
    rid = request_id(request)
    log.error("database unavailable [request %s]: %s", rid, exc)
    return _err(503, "The service is temporarily unavailable. Please try again shortly.", rid=rid)


@app.exception_handler(db.DatabaseSafetyError)
async def unsafe_db(request: Request, exc: db.DatabaseSafetyError):
    rid = request_id(request)
    log.error("database safety violation [request %s]: %s", rid, exc)
    return _err(500, "Internal server error.", rid=rid)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    rid = request_id(request)
    log.exception("unhandled error [request %s]", rid)
    return _err(500, "Internal server error.", rid=rid)


from fastapi.exceptions import RequestValidationError  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    _log_refusal(request, exc.status_code, exc.detail)
    return JSONResponse({"error": {"status": exc.status_code, "message": exc.detail, "details": None}},
                        status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    return _err(422, "Request validation failed.", [
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()])


@app.get("/health", tags=["meta"])
def health():
    """Liveness + database reachability. No database name, version or host is disclosed."""
    db.health()
    return {"status": "ok"}


app.include_router(platform.router)
app.include_router(session.router)
app.include_router(resources.router)
app.include_router(sync.router)
app.include_router(companies.router)
app.include_router(logins.router)
app.include_router(staff.router)
app.include_router(security_events.router)
app.include_router(route_simulation.router)
