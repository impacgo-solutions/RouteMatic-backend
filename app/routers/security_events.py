"""Stored security events (lock-outs, refresh-token reuse, privilege changes, cross-tenant attempts,
password changes) for platform administrators. Read-only; newest first."""
from __future__ import annotations

import psycopg2
from fastapi import APIRouter, Depends, Query

from ..db import transaction
from ..deps import Principal, platform_admin

router = APIRouter(prefix="/api/v1/security", tags=["security"])


@router.get("/events")
def list_events(page: int = Query(1, ge=1), pageSize: int = Query(50, ge=1, le=200),
                event: str | None = Query(None, max_length=64),
                _: Principal = Depends(platform_admin)) -> dict:
    where, params = "", []
    if event:
        where, params = "WHERE event = %s", [event]
    try:
        with transaction(dict_rows=True) as cur:
            cur.execute(f"SELECT count(*) AS n FROM public.security_events {where}", params)
            total = cur.fetchone()["n"]
            cur.execute(f"SELECT id, at, event, outcome, user_id::text AS \"userId\", email, client_ip AS "
                        f"\"clientIp\", tenant, request_id AS \"requestId\", details FROM public.security_events "
                        f"{where} ORDER BY at DESC, id DESC LIMIT %s OFFSET %s",
                        params + [pageSize, (page - 1) * pageSize])
            items = [dict(r) for r in cur.fetchall()]
    except psycopg2.errors.UndefinedTable:      # migration 0008 not applied on this database yet
        return {"items": [], "total": 0, "page": page, "pageSize": pageSize}
    for it in items:
        it["at"] = it["at"].isoformat()
    return {"items": items, "total": total, "page": page, "pageSize": pageSize}
