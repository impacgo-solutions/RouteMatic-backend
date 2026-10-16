"""Server-side audit trail for sensitive administrative actions done through the API.

Entries go to the ``audit_events`` table of the workspace the action concerns (the Fleet workspace or a
company schema), so they appear in the app's existing audit views. They never contain passwords, hashes or
tokens — only who did what to which record.
"""
from __future__ import annotations

import json
import uuid

from psycopg2 import sql

from .spec import ENUMS

_ROLES = set(ENUMS["UserRole"])


def record(cur, schema: str, *, actor: str, role: str, action: str, entity_type: str, entity_id: str,
           summary: str, outcome: str = "success", related: dict | None = None) -> None:
    """Insert one audit entry into ``<schema>.audit_events`` (inside the caller's transaction)."""
    details = {"recordedBy": "api", "role": role, **(related or {})}
    cur.execute(
        sql.SQL('INSERT INTO {}."audit_events"(id, actor_role, actor_id, action, entity_type, entity_id, '
                '"timestamp", summary, outcome, related) VALUES (%s,%s,%s,%s,%s,%s,now(),%s,%s,%s)'
                ).format(sql.Identifier(schema)),
        ("AU-API-" + uuid.uuid4().hex[:12].upper(), role if role in _ROLES else "system", actor, action,
         entity_type, entity_id, summary[:500], outcome, json.dumps(details)))


def schema_of(cur, slug: str) -> str:
    cur.execute("SELECT schema_name FROM public.tenants WHERE slug = %s", (slug,))
    row = cur.fetchone()
    return row["schema_name"] if isinstance(row, dict) else row[0]
