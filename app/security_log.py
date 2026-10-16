"""Security event log.

Every security-relevant event is written as one JSON line to the ``routematic.security`` logger (ship it to
your log store / SIEM). High-value events (``persist=True``: lock-outs, refresh-token reuse, privilege
changes, cross-tenant attempts, password changes) are also stored in ``public.security_events`` so they
survive restarts and can be reviewed by platform admins (``GET /api/v1/security/events``).

Never logged: passwords, tokens, request bodies. Emails are masked (``a***@d***.com``).
Logging never breaks a request: every failure here is swallowed (and reported on the normal logger).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("routematic.security")
_fallback = logging.getLogger("routematic")
# Emit even when the server configures no logging for this logger (uvicorn does not): one JSON line per
# event on stderr. A deployment may attach its own handlers (log shipping / SIEM) in addition.
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_h)

_SECRET_WORDS = ("password", "token", "secret", "authorization", "hash", "credential")


def mask_email(email: str | None) -> str:
    """``asha.rao@demo.example`` -> ``a***@d***.example``."""
    e = (email or "").strip()
    if "@" not in e:
        return (e[:1] + "***") if e else ""
    local, _, domain = e.partition("@")
    tld = domain.rsplit(".", 1)[1] if "." in domain else ""
    return f"{local[:1]}***@{domain[:1]}***" + (f".{tld}" if tld else "")


def _clean(details: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in details.items():
        if any(w in k.lower() for w in _SECRET_WORDS):
            continue
        if isinstance(v, str) and len(v) > 300:
            v = v[:300] + "…"
        out[k] = v
    return out


def _request_parts(request) -> tuple[str, str, str | None, str | None]:
    """(request id, client ip, user id, tenant) known for this request."""
    if request is None:
        return "", "", None, None
    try:
        from .hardening import request_id
        rid = request_id(request)
    except Exception:  # noqa: BLE001
        rid = ""
    ip = request.client.host if getattr(request, "client", None) else ""
    state = getattr(request, "state", None)
    uid = getattr(state, "user_id", None) if state is not None else None
    tenant = getattr(state, "tenant", None) if state is not None else None
    return rid, ip, uid, tenant


def event(name: str, outcome: str = "", *, request=None, user_id: str | None = None,
          email: str | None = None, tenant: str | None = None, client_ip: str | None = None,
          persist: bool = False, **details: Any) -> None:
    """Record one security event (JSON log line; also stored when ``persist``)."""
    try:
        rid, ip, uid, ten = _request_parts(request)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": name,
            "outcome": outcome,
            "requestId": rid,
            "clientIp": client_ip or ip,
            "userId": user_id or uid,
            "email": mask_email(email),
            "tenant": tenant or ten or "",
            "details": _clean(details),
        }
        log.info(json.dumps(rec, default=str, separators=(",", ":")))
        if persist:
            _store(rec)
    except Exception:  # noqa: BLE001 - logging must never break a request
        _fallback.warning("security event %s could not be recorded", name, exc_info=True)


def _store(rec: dict) -> None:
    from .db import transaction
    try:
        with transaction() as cur:
            cur.execute(
                "INSERT INTO public.security_events(event, outcome, user_id, email, client_ip, tenant, "
                "request_id, details) VALUES (%s,%s,%s::uuid,%s,%s,%s,%s,%s::jsonb)",
                (rec["event"], rec["outcome"], rec["userId"], rec["email"], rec["clientIp"] or "",
                 rec["tenant"], rec["requestId"], json.dumps(rec["details"], default=str)))
    except Exception:  # noqa: BLE001 - e.g. migration 0008 not applied yet
        _fallback.warning("security event %s not stored", rec.get("event"), exc_info=True)


def privilege_change(request, actor_user_id: str | None, action: str, *, target_email: str | None = None,
                     tenant: str | None = None, role: str | None = None, **details: Any) -> None:
    """Role / membership / account-status changes (granting or removing access). Always stored."""
    event("privilege." + action, "success", request=request, user_id=actor_user_id, tenant=tenant,
          persist=True, target=mask_email(target_email), role=role, **details)
