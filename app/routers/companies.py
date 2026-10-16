"""Company registration: every company is its own tenant (PostgreSQL schema).

``POST /api/v1/t/fleet/companies`` (Fleet Admin / authorised admin of the FLEET workspace,
or a platform admin) atomically:
  1. validates the request (company profile, admin login),
  2. creates ``tenant_<slug>`` with every company-level table/FK/index/constraint (all empty),
  3. inserts the company's own profile row into that schema's ``employers`` table,
  4. records the company in the central registry ``fleet_workspace.companies`` (the mapping
     from the Fleet Admin schema to the tenant schema),
  5. creates the company admin login (``employerAdmin``, scoped to that company) and gives
     the creator ``fleetManager`` access to the new tenant,
and only then marks the tenant ``ready``. Any failure rolls everything back.
"""
from __future__ import annotations

import logging
import re
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg2 import sql
from pydantic import BaseModel, Field

from .. import audit, repo, security_log, tenants, users
from ..db import tenant_cursor, transaction
from ..deps import TenantCtx, tenant_access
from ..spec import ENTITIES

log = logging.getLogger("routematic")

router = APIRouter(prefix="/api/v1/t/{tenant}/companies", tags=["companies"])

CODE_RE = re.compile(r"^[A-Za-z0-9-]{1,12}$")
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class AdminIn(BaseModel):
    email: str = Field(pattern=EMAIL_RE, max_length=254)
    displayName: str = Field(min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=8, max_length=200)


class CompanyIn(BaseModel):
    slug: str | None = Field(default=None, max_length=40)
    employer: dict[str, Any]            # the app's Employer JSON (code, name, ...)
    admin: AdminIn | None = None


def _authorised(ctx: TenantCtx) -> None:
    if ctx.kind != "fleet":
        raise HTTPException(403, "Companies are registered from the Fleet workspace "
                                 "(switch organization to the fleet).")
    if not (ctx.user.is_platform_admin or ctx.role in ("fleetManager", "system")):
        raise HTTPException(403, "Only a Fleet Admin or platform admin can create companies.")


@router.post("", status_code=201)
def create_company(body: CompanyIn, request: Request, ctx: TenantCtx = Depends(tenant_access)) -> dict:
    _authorised(ctx)
    emp = dict(body.employer)
    code = str(emp.get("code", "")).strip().upper()
    if not CODE_RE.match(code):
        raise repo.ApiError(422, "Company ID must be 1-12 letters, digits or dashes.")
    emp["code"] = code
    emp["id"] = code
    name = str(emp.get("name", "")).strip()
    # validate the profile up front (fail before touching the database)
    repo.validate(ENTITIES["employers"], {k: v for k, v in emp.items() if k != "id"}, partial=False)

    with transaction(dict_rows=True) as cur:
        cur.execute('SELECT 1 FROM "fleet_workspace"."companies" WHERE lower(code)=lower(%s)', (code,))
        if cur.fetchone():
            raise repo.ApiError(409, f"Company ID {code} already exists.")
        # slug: requested, else derived from the name; fall back to <name>_<code> on collision
        if body.slug and body.slug.strip().lower() in tenants.RESERVED_SLUGS:
            raise repo.ApiError(422, f"'{body.slug}' is reserved; choose another identifier.")
        base = tenants.slugify(body.slug or name) or code.lower()
        candidates = [base] if body.slug else [base, f"{base[:30]}_{code.lower()}"[:40]]
        cur.execute("SELECT slug FROM public.tenants WHERE slug = ANY(%s)", (candidates,))
        taken = {r["slug"] for r in cur.fetchall()}
    slug = next((c for c in candidates if c not in taken), None)
    if slug is None:
        raise repo.ApiError(
            409, f"Tenant identifier '{candidates[0]}' is already in use; send a different slug.")

    def build(cur, tenant_id, schema):
        # search_path is pinned to the NEW company schema here
        company = repo.create(cur, ENTITIES["employers"], emp, ctx.actor, trusted=True, defer=True)
        cur.execute('INSERT INTO "fleet_workspace"."companies"(code, name, tenant_id, created_by) '
                    "VALUES (%s,%s,%s,%s::uuid)", (code, name, tenant_id, ctx.user.user_id))
        users.add_membership(cur, ctx.user.user_id, tenant_id, "fleetManager", None)
        members = [{"email": ctx.user.email, "role": "fleetManager"}]
        if body.admin:
            uid, created = users.ensure_user(cur, body.admin.email, body.admin.displayName,
                                             body.admin.password)
            if uid != ctx.user.user_id:
                users.add_membership(cur, uid, tenant_id, "employerAdmin", code)
                members.append({"email": body.admin.email, "role": "employerAdmin",
                                "newLogin": created})
        audit.record(cur, schema, actor=ctx.actor, role=ctx.role, action="company.register",
                     entity_type="company", entity_id=code, summary=f"Company {code} ({name}) registered")
        audit.record(cur, "fleet_workspace", actor=ctx.actor, role=ctx.role, action="company.register",
                     entity_type="company", entity_id=code,
                     summary=f"Company {code} ({name}) registered as workspace {slug}")
        return {"company": company, "members": members}

    try:
        res = tenants.provision_tenant(name, slug, on_ready=build)
    except tenants.TenantExists as exc:
        raise repo.ApiError(409, str(exc))
    except tenants.TenantError as exc:
        raise repo.ApiError(422, str(exc))
    except tenants.ProvisioningError:
        ref = secrets.token_hex(6)
        log.exception("company provisioning failed [ref %s]", ref)     # details stay in the server log
        raise repo.ApiError(500, f"Company creation failed and was rolled back (reference {ref}).")
    extra = res.pop("extra")
    if body.admin:
        security_log.privilege_change(request, ctx.user.user_id, "company_admin_create",
                                      target_email=body.admin.email, tenant=slug, role="employerAdmin",
                                      company=code)
    return {"tenant": res, **extra}


@router.get("")
def list_companies(ctx: TenantCtx = Depends(tenant_access)) -> dict:
    """The central mapping: every company, its tenant slug and schema, with live counts.

    Counts are read from each company's own schema (employees, shifts, sites) and from the fleet
    tables (vehicles allocated to / assigned to the company), so the Fleet workspace can show them
    without copying company data.
    """
    if ctx.kind != "fleet" or not (ctx.user.is_platform_admin or ctx.role in ("fleetManager", "manager", "system")):
        raise HTTPException(403, "The company registry is available to fleet staff in the Fleet workspace.")
    with tenant_cursor(ctx.slug) as cur:
        cur.execute(
            'SELECT code, name, tenant_slug AS "tenantSlug", schema_name AS "schemaName", '
            'status, tenant_status AS "tenantStatus", schema_version AS "schemaVersion" '
            'FROM "company_tenants" ORDER BY code')
        items = [{k: repo.out_value(v) for k, v in r.items()} for r in cur.fetchall()]
        for it in items:
            it["counts"] = _company_counts(cur, it["schemaName"], it["code"])                 if it["tenantStatus"] == "ready" else None
        return {"items": items}


def _company_counts(cur, schema: str, code: str) -> dict:
    s = sql.Identifier(schema)
    cur.execute(sql.SQL(
        "SELECT (SELECT count(*) FROM {s}.employees WHERE employer_id = %(c)s AND status = 'active') AS employees, "
        "(SELECT count(*) FROM {s}.employer_shifts WHERE employer_id = %(c)s) AS shifts, "
        "(SELECT count(*) FROM {s}.locations WHERE employer_id = %(c)s AND active) AS sites").format(s=s),
        {"c": code})
    out = dict(cur.fetchone())
    cur.execute(
        'SELECT count(DISTINCT v) AS n FROM ('
        '  SELECT resource_id AS v FROM "fleet_allocations" WHERE employer_id = %(c)s AND active '
        "    AND resource_type = 'vehicle' "
        '  UNION SELECT id FROM "vehicles" WHERE assigned_employer_id = %(c)s AND active) x', {"c": code})
    out["vehicles"] = cur.fetchone()["n"]
    return out
