"""Versioned, idempotent migration runner (shared schema + per-tenant schemas).

Rules:
* Every statement is idempotent (IF NOT EXISTS / guarded DO blocks), so re-running
  never duplicates objects or touches data.
* Applied versions are recorded (with checksum + duration) and skipped next time.
* Any failure aborts the surrounding transaction and raises
  :class:`MigrationError` naming the migration, the exact statement and the DB
  error - nothing is swallowed. There is no drop-and-recreate path.
* Schema changes after release MUST be added as a new numbered migration; never
  edit an applied one (``verify_tenant`` reports checksum drift).
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Callable

from .db import assert_safe_target
from .ddl import (FLEET_ONLY_STATEMENTS, SHARED_STATEMENTS, SHARED_STATEMENTS_0002,
                  SHARED_STATEMENTS_0003, SHARED_STATEMENTS_0004, SHARED_STATEMENTS_0005,
                  SHARED_STATEMENTS_0006, SHARED_STATEMENTS_0007, COMPANY_STATEMENTS_0004, FLEET_STATEMENTS_0003, FLEET_STATEMENTS_0004,
                  COMPANY_STATEMENTS_0003,
                  TENANT_BOOKKEEPING, tenant_statements)


# 0008: refresh tokens (stored as SHA-256 hashes only) and the persistent security event log. Additive only.
SHARED_STATEMENTS_0008 = [
    """CREATE TABLE IF NOT EXISTS public.refresh_tokens (
        id            BIGSERIAL PRIMARY KEY,
        user_id       UUID NOT NULL REFERENCES public.platform_users(id) ON DELETE CASCADE,
        family_id     TEXT NOT NULL,
        token_hash    TEXT NOT NULL UNIQUE,
        token_version INTEGER NOT NULL DEFAULT 0,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at    TIMESTAMPTZ NOT NULL,
        used_at       TIMESTAMPTZ,
        revoked_at    TIMESTAMPTZ)""",
    "CREATE INDEX IF NOT EXISTS refresh_tokens_user_idx ON public.refresh_tokens(user_id)",
    "CREATE INDEX IF NOT EXISTS refresh_tokens_family_idx ON public.refresh_tokens(family_id)",
    """CREATE TABLE IF NOT EXISTS public.security_events (
        id          BIGSERIAL PRIMARY KEY,
        at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        event       TEXT NOT NULL,
        outcome     TEXT NOT NULL DEFAULT '',
        user_id     UUID,
        email       TEXT NOT NULL DEFAULT '',
        client_ip   TEXT NOT NULL DEFAULT '',
        tenant      TEXT NOT NULL DEFAULT '',
        request_id  TEXT NOT NULL DEFAULT '',
        details     JSONB NOT NULL DEFAULT '{}'::jsonb)""",
    "CREATE INDEX IF NOT EXISTS security_events_at_idx ON public.security_events(at DESC)",
    "CREATE INDEX IF NOT EXISTS security_events_event_idx ON public.security_events(event, at DESC)",
]


class MigrationError(RuntimeError):
    def __init__(self, migration: "Migration", statement: str, cause: Exception):
        self.migration, self.statement, self.cause = migration, statement, cause
        super().__init__(
            f"Migration {migration.version} ({migration.name}) failed: {cause}\n"
            f"--- statement ---\n{statement.strip()[:1500]}"
        )


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    statements: Callable[[], list[str]]

    def checksum(self) -> str:
        return hashlib.sha256("\n;\n".join(self.statements()).encode()).hexdigest()


SHARED_MIGRATIONS = [
    Migration("0001", "shared_schema", lambda: SHARED_STATEMENTS),
    Migration("0002", "company_registry", lambda: SHARED_STATEMENTS_0002),
    Migration("0003", "tenant_kinds_fleet_registry", lambda: SHARED_STATEMENTS_0003),
    Migration("0004", "manager_role_membership_active", lambda: SHARED_STATEMENTS_0004),
    Migration("0005", "fleet_manager_role_name", lambda: SHARED_STATEMENTS_0005),
    Migration("0006", "rename_fleet_schema_to_fleet_workspace", lambda: SHARED_STATEMENTS_0006),
    Migration("0007", "token_version", lambda: SHARED_STATEMENTS_0007),
    Migration("0008", "refresh_tokens_security_events", lambda: SHARED_STATEMENTS_0008),
]
# Company schemas (tenant_<slug>): company-level tables only.
TENANT_MIGRATIONS = [
    Migration("0001", "company_core_schema", lambda: tenant_statements("company")),
    # 0002: the company's own address (stored in its employers row; the fleet registry keeps no copy)
    Migration("0002", "employer_address",
              lambda: ["ALTER TABLE \"employers\" ADD COLUMN IF NOT EXISTS \"address\" TEXT NOT NULL DEFAULT ''"]),
    # 0003: managers of the company (Fleet Admin creates them; they manage the company's drivers)
    Migration("0003", "managers", lambda: COMPANY_STATEMENTS_0003),
    # 0004: company record integrity in the database (single company, no delete, fixed code, no parking)
    Migration("0004", "company_record_integrity", lambda: COMPANY_STATEMENTS_0004),
]
# The single fleet_workspace schema: fleet-level tables + company registry + fleet users.
FLEET_MIGRATIONS = [
    Migration("0001", "fleet_core_schema",
              lambda: tenant_statements("fleet") + FLEET_ONLY_STATEMENTS),
    # 0002: managers are fleet staff (no company): role-specific data only (phone, status);
    # name / email / password live once in public.platform_users.
    Migration("0002", "fleet_managers", lambda: COMPANY_STATEMENTS_0003),
    # 0003: Fleet Admin accounts (authentication source for the Fleet Admin role)
    Migration("0003", "fleet_admins", lambda: FLEET_STATEMENTS_0003),
    # 0004: incident references from fleet tables are soft (the incident may live in a company schema)
    Migration("0004", "soft_incident_refs", lambda: FLEET_STATEMENTS_0004),
]
LATEST_TENANT_VERSION = TENANT_MIGRATIONS[-1].version
LATEST_FLEET_VERSION = FLEET_MIGRATIONS[-1].version


def _run(cur, m: Migration) -> int:
    t0 = time.perf_counter()
    for stmt in m.statements():
        cur.execute("SAVEPOINT mig_stmt")
        try:
            cur.execute(stmt)
        except Exception as exc:
            cur.execute("ROLLBACK TO SAVEPOINT mig_stmt")
            raise MigrationError(m, stmt, exc) from exc
        cur.execute("RELEASE SAVEPOINT mig_stmt")
    return int((time.perf_counter() - t0) * 1000)


def apply_shared(cur) -> list[str]:
    """Create/upgrade the shared ``public`` structures. Returns applied versions."""
    assert_safe_target(cur)
    cur.execute("SELECT pg_advisory_xact_lock(727001)")
    # Bootstrap the tracking table (first statement of migration 0001).
    cur.execute(SHARED_STATEMENTS[0])
    applied: list[str] = []
    for m in SHARED_MIGRATIONS:
        cur.execute("SELECT 1 FROM public.schema_migrations WHERE scope='public' AND version=%s",
                    (m.version,))
        if cur.fetchone():
            continue
        ms = _run(cur, m)
        cur.execute(
            "INSERT INTO public.schema_migrations(scope, version, name, checksum, duration_ms) "
            "VALUES ('public', %s, %s, %s, %s)", (m.version, m.name, m.checksum(), ms))
        applied.append(m.version)
    return applied


def apply_tenant(cur, kind: str = "company") -> list[str]:
    """Apply pending migrations to the schema ``search_path`` is pinned to.

    ``kind`` is 'company' (tenant_<slug>) or 'fleet' (fleet_workspace).
    """
    cur.execute(TENANT_BOOKKEEPING)
    applied: list[str] = []
    for m in (FLEET_MIGRATIONS if kind == "fleet" else TENANT_MIGRATIONS):
        cur.execute('SELECT 1 FROM "schema_migrations" WHERE version=%s', (m.version,))
        if cur.fetchone():
            continue
        ms = _run(cur, m)
        cur.execute(
            'INSERT INTO "schema_migrations"(version, name, checksum, duration_ms) VALUES (%s,%s,%s,%s)',
            (m.version, m.name, m.checksum(), ms))
        applied.append(m.version)
    return applied


def tenant_version(cur) -> str | None:
    cur.execute('SELECT max(version) FROM "schema_migrations"')
    row = cur.fetchone()
    v = row["max"] if isinstance(row, dict) else row[0]
    return v
