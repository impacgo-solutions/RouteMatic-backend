"""HTTP hardening middleware (pure ASGI): request ids, security headers, request body size cap."""
from __future__ import annotations

import json
import re
import secrets

from starlette.requests import Request

_RID_OK = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")

# The API only returns JSON: nothing may be framed, scripted or embedded from its responses.
_BASE_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-resource-policy", b"same-site"),
    (b"cache-control", b"no-store"),
]
_API_CSP = (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")
_HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")


def request_id(request: Request) -> str:
    """The id of this request (set by :class:`SecurityHeaders`), also returned as X-Request-ID."""
    state = request.scope.get("state") or {}
    rid = state.get("request_id") if isinstance(state, dict) else None
    return rid or getattr(request.state, "request_id", None) or secrets.token_hex(8)


class SecurityHeaders:
    """Adds a request id and security headers to every HTTP response."""

    def __init__(self, app, hsts: bool = False, docs: bool = False):
        self.app = app
        self.hsts = hsts
        self.docs = docs

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming if _RID_OK.match(incoming) else secrets.token_hex(8)
        scope.setdefault("state", {})["request_id"] = rid
        is_docs = self.docs and scope.get("path", "").startswith(_DOCS_PATHS)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in (b"x-request-id",)]
                present = {k.lower() for k, _ in headers}
                extra = list(_BASE_HEADERS)
                if not is_docs:       # Swagger UI needs its CDN scripts; everything else is JSON only
                    extra.append(_API_CSP)
                if self.hsts:
                    extra.append(_HSTS)
                headers += [(k, v) for k, v in extra if k not in present]
                headers.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


class _TooLarge(BaseException):
    """BaseException on purpose: the framework turns ordinary exceptions raised while reading the body
    into a generic 400, but this one must reach :class:`BodySizeLimit` to answer 413."""


class BodySizeLimit:
    """Refuses request bodies larger than ``max_bytes`` with 413 (declared or streamed size)."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def _refuse(self, send, scope=None):
        if scope is not None:
            from . import security_log
            client = scope.get("client") or ("", 0)
            security_log.event("upload.rejected", "refused", client_ip=client[0] or "",
                               path=scope.get("path", ""), method=scope.get("method", ""),
                               limit=self.max_bytes)
        body = json.dumps({"error": {"status": 413, "message": "The request is too large.",
                                     "details": None}}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    return await self._refuse(send, scope)
            except ValueError:
                return await self._refuse(send, scope)
        seen = 0
        started = False

        async def counted_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracking_send)
        except _TooLarge:
            if not started:
                await self._refuse(send, scope)
