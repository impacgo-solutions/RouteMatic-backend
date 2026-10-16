"""Tenant registration + provisioning service."""
from __future__ import annotations

import re

from psycopg2 import errors, sql

from . import migrations
from . import db
from .db import assert_safe_target, transaction
from .repo import ApiError

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
RESERVED_SLUGS = {"fleet", "fleet_workspace", "fleet_manager", "public", "platform", "admin", "system"}


class TenantError(ValueError):
    """Invalid tenant request (maps to HTTP 422)."""


class TenantExists(TenantError):
    """Duplicate slug / schema (maps to HTTP 409)."""


class ProvisioningError(RuntimeError):
    pass


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if s and not s[0].isalpha():
        s = "t_" + s
    return s[:40]


def validate_request(name: str, slug: str | None) -> tuple[str, str, str]:
    name = (name or "").strip()
    if not 1 <= len(name) <= 120:
        raise TenantError("Tenant name must be 1-120 characters.")
    slug = (slug or slugify(name)).strip().lower()
    if not SLUG_RE.match(slug):
        raise TenantError(
            "Tenant identifier must start with a letter and contain 3-40 of [a-z0-9_]."
        )
    if slug in RESERVED_SLUGS:
        raise TenantError(f"'{slug}' is reserved; choose another identifier.")
    return name, slug, f"tenant_{slug}"


def _log(slug: str, outcome: str, detail: str) -> None:
    try:
        with transaction() as cur:
            cur.execute("INSERT INTO public.tenant_provision_log(slug, outcome, detail) "
                        "VALUES (%s,%s,%s)", (slug, outcome, detail[:4000]))
    except Exception:  # logging must never mask the real error
        pass


def provision_tenant(name: str, slug: str | None = None, on_ready=None) -> dict:
    """Register + create + migrate a tenant atomically.

    Everything (registry row, schema, tables, defaults, ready flag) happens in ONE
    PostgreSQL transaction, so a failure leaves no half-provisioned tenant and the
    tenant is only visible as ``ready`` after full initialisation.

    ``on_ready(cur, tenant_id, schema)`` runs inside the same transaction after the
    schema is built (search_path already pinned to it) and before the tenant is marked
    ready; any exception it raises rolls the whole provisioning back.
    """
    name, slug, schema = validate_request(name, slug)
    try:
        with transaction(dict_rows=True) as cur:
            assert_safe_target(cur)
            db.as_schema_owner(cur)       # DDL below runs as the schema owner (least-privilege layout)
            migrations.apply_shared(cur)  # idempotent; ensures registry exists
            cur.execute("SELECT 1 FROM public.tenants WHERE kind='fleet' AND status='ready'")
            if not cur.fetchone():
                raise TenantError("The fleet_workspace schema is not initialised; run `python -m scripts.init_db`.")
            cur.execute("SELECT 1 FROM public.tenants WHERE slug=%s OR schema_name=%s", (slug, schema))
            if cur.fetchone():
                raise TenantExists(f"Tenant '{slug}' already exists.")
            cur.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,))
            if cur.fetchone():
                raise TenantExists(
                    f"Schema '{schema}' already exists but is not registered; refusing to adopt it.")
            cur.execute(
                "INSERT INTO public.tenants(slug, name, schema_name, kind, status) "
                "VALUES (%s,%s,%s,'company','provisioning') RETURNING id", (slug, name, schema))
            tenant_id = cur.fetchone()["id"]
            ident = sql.Identifier(schema)
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(ident))
            cur.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(ident))
            db.pin_search_path(cur, schema, "company")
            applied = migrations.apply_tenant(cur, "company")
            version = migrations.tenant_version(cur)
            extra = on_ready(cur, tenant_id, schema) if on_ready else None
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cur.execute(
                "UPDATE public.tenants SET status='ready', schema_version=%s, ready_at=now(), "
                "updated_at=now() WHERE id=%s", (version, tenant_id))
    except (TenantError, ApiError):
        raise
    except errors.UniqueViolation as exc:
        raise TenantExists(f"Tenant '{slug}' already exists.") from exc
    except Exception as exc:
        _log(slug, "failed", f"{type(exc).__name__}: {exc}")
        raise ProvisioningError(f"Provisioning tenant '{slug}' failed: {exc}") from exc
    _log(slug, "ready", f"migrations applied: {applied}")
    out = {"id": str(tenant_id), "slug": slug, "name": name, "schemaName": schema,
           "status": "ready", "schemaVersion": version}
    if extra:
        out["extra"] = extra
    return out


def provision_fleet_in(cur) -> dict:
    """Create (once) or upgrade the ``fleet_workspace`` schema inside the caller's transaction."""
    schema, slug = db.FLEET_SCHEMA, db.FLEET_SLUG
    db.as_schema_owner(cur)
    migrations.apply_shared(cur)
    cur.execute("SELECT id, status FROM public.tenants WHERE kind='fleet'")
    row = cur.fetchone()
    created = row is None
    if created:
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,))
        if cur.fetchone():
            raise TenantExists(f"Schema '{schema}' exists but is not registered; refusing to adopt it.")
        cur.execute(
            "INSERT INTO public.tenants(slug, name, schema_name, kind, status) "
            "VALUES (%s,'Fleet Admin',%s,'fleet','provisioning') RETURNING id", (slug, schema))
        tenant_id = cur.fetchone()["id"]
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        cur.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
    else:
        tenant_id = row["id"]
    db.pin_search_path(cur, schema, "fleet")
    applied = migrations.apply_tenant(cur, "fleet")
    version = migrations.tenant_version(cur)
    cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
    cur.execute("UPDATE public.tenants SET status='ready', schema_version=%s, "
                "ready_at=coalesce(ready_at, now()), updated_at=now() WHERE id=%s", (version, tenant_id))
    return {"id": str(tenant_id), "slug": slug, "schemaName": schema, "kind": "fleet",
            "status": "ready", "schemaVersion": version, "created": created, "applied": applied}


def provision_fleet() -> dict:
    """Create (once) or upgrade the single ``fleet_workspace`` schema. Idempotent."""
    try:
        with transaction(dict_rows=True) as cur:
            assert_safe_target(cur)
            res = provision_fleet_in(cur)
    except TenantError:
        raise
    except Exception as exc:
        _log(db.FLEET_SLUG, "failed", f"fleet: {type(exc).__name__}: {exc}")
        raise ProvisioningError(f"Provisioning the fleet schema failed: {exc}") from exc
    if res["created"] or res["applied"]:
        _log(db.FLEET_SLUG, "ready", f"fleet migrations applied: {res['applied']}")
    res.pop("applied")
    return res


def upgrade_all_tenants() -> dict[str, list[str]]:
    """Apply pending migrations to the fleet schema and every ready company (own txn each)."""
    with transaction(dict_rows=True) as cur:
        cur.execute("SELECT slug, schema_name, kind FROM public.tenants WHERE status='ready' "
                    "ORDER BY (kind='fleet') DESC, slug")
        tenants = cur.fetchall()
    result: dict[str, list[str]] = {}
    for t in tenants:
        with transaction(dict_rows=True) as cur:
            db.as_schema_owner(cur)
            db.pin_search_path(cur, t["schema_name"], t["kind"])
            applied = migrations.apply_tenant(cur, t["kind"])
            v = migrations.tenant_version(cur)
            cur.execute("UPDATE public.tenants SET schema_version=%s, updated_at=now() WHERE slug=%s",
                        (v, t["slug"]))
            result[t["slug"]] = applied
    return result


def list_tenants() -> list[dict]:
    with transaction(dict_rows=True) as cur:
        cur.execute("SELECT id::text, slug, name, schema_name AS \"schemaName\", kind, status, "
                    "schema_version AS \"schemaVersion\", created_at AS \"createdAt\" "
                    "FROM public.tenants ORDER BY (kind='fleet') DESC, slug")
        return [dict(r) for r in cur.fetchall()]
