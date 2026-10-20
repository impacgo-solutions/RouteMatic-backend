"""Logins for people who work inside a company: employees (Employer Admin) and drivers (Manager / Fleet Admin).

``PUT /api/v1/t/{company}/employees/{id}/login``  – employee login for the Employee app
``PUT /api/v1/t/{tenant}/drivers/{id}/login``      – driver login for the Driver app

Both create (or update) the platform user and a membership of the company scoped to that person's id.
Passwords are hashed (PBKDF2) and live only in ``public.platform_users``: never in a company schema,
the sync stream or any log.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..db import tenant_cursor, transaction
from ..deps import TenantCtx, tenant_access
from ..repo import ApiError
from .. import audit, security_log
from ..users import upsert_scoped_login

router = APIRouter(prefix="/api/v1/t/{tenant}", tags=["logins"])

EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class LoginIn(BaseModel):
    email: str = Field(pattern=EMAIL_RE, max_length=254)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    displayName: str | None = Field(default=None, max_length=120)
    company: str | None = Field(default=None, max_length=12)     # drivers, from the Fleet workspace


# ------------------------------------------------------------------ employees
@router.put("/employees/{employee_id}/login")
def set_employee_login(employee_id: str, body: LoginIn, request: Request,
                       ctx: TenantCtx = Depends(tenant_access)) -> dict:
    if ctx.kind != "company":
        raise HTTPException(404, "Employee logins belong to a company workspace.")
    if ctx.role not in ("employerAdmin", "fleetManager", "system"):
        raise HTTPException(403, "Only the company's Employer Admin (or the fleet admin) can set employee logins.")
    # the employee must exist in THIS company (and, for an employer admin, belong to their employer)
    with tenant_cursor(ctx.slug) as cur:
        cur.execute("SELECT employer_id, name FROM employees WHERE id = %s", (employee_id,))
        emp = cur.fetchone()
    if not emp or (ctx.employer_scope and emp["employer_id"] != ctx.employer_scope):
        raise HTTPException(404, f"Employee '{employee_id}' not found.")
    with transaction(dict_rows=True) as cur:
        cur.execute("SELECT id FROM public.tenants WHERE slug=%s AND status='ready'", (ctx.slug,))
        tenant_id = cur.fetchone()["id"]
        res = upsert_scoped_login(cur, tenant_id, "employee", employee_id, body.email, body.password,
                                  body.displayName or emp["name"])
        audit.record(cur, audit.schema_of(cur, ctx.slug), actor=ctx.actor, role=ctx.role, action="login.set",
                     entity_type="employee", entity_id=employee_id,
                     summary=f"Login set for employee {employee_id}"
                             + (" (password set)" if res.get("passwordChanged") else ""))
    security_log.privilege_change(request, ctx.user.user_id, "employee_login_set", target_email=body.email,
                                  tenant=ctx.slug, role="employee", employee=employee_id,
                                  passwordSet=bool(res.get("passwordChanged")))
    return {"employeeId": employee_id, **res}


@router.get("/employees/{employee_id}/login")
def get_employee_login(employee_id: str, ctx: TenantCtx = Depends(tenant_access)) -> dict:
    """Does this employee have a login? (email only; never the password or its hash)"""
    if ctx.kind != "company" or ctx.role not in ("employerAdmin", "fleetManager", "system"):
        raise HTTPException(403, "Not permitted.")
    with tenant_cursor(ctx.slug) as cur:
        cur.execute("SELECT employer_id FROM employees WHERE id = %s", (employee_id,))
        emp = cur.fetchone()
    if not emp or (ctx.employer_scope and emp["employer_id"] != ctx.employer_scope):
        raise HTTPException(404, f"Employee '{employee_id}' not found.")
    with transaction(dict_rows=True) as cur:
        cur.execute("SELECT u.email FROM public.tenant_memberships m JOIN public.tenants t ON t.id=m.tenant_id "
                    "JOIN public.platform_users u ON u.id=m.user_id "
                    "WHERE t.slug=%s AND m.role='employee' AND m.scope_id=%s", (ctx.slug, employee_id))
        row = cur.fetchone()
    return {"employeeId": employee_id, "hasLogin": row is not None, "email": row["email"] if row else None}


# ------------------------------------------------------------------ drivers
def _driver_company(cur, ctx: TenantCtx, body: LoginIn) -> tuple:
    """Which company is the driver being assigned to? -> (tenant_id, slug)."""
    if ctx.kind == "company":
        cur.execute("SELECT id, slug FROM public.tenants WHERE slug=%s AND status='ready'", (ctx.slug,))
    else:
        if not body.company:
            raise ApiError(422, "Choose the company this driver is assigned to.")
        cur.execute('SELECT t.id, t.slug FROM "fleet_workspace"."companies" c JOIN public.tenants t '
                    "ON t.id = c.tenant_id WHERE lower(c.code)=lower(%s) AND t.status='ready'", (body.company,))
    row = cur.fetchone()
    if not row:
        raise ApiError(422, f"Company '{body.company}' is not registered.")
    return row["id"], row["slug"]


@router.put("/drivers/{driver_id}/login")
def set_driver_login(driver_id: str, body: LoginIn, request: Request,
                     ctx: TenantCtx = Depends(tenant_access)) -> dict:
    """Give a driver a login and ASSIGN them to a company (Fleet Admin and Fleet Manager: any company)."""
    if ctx.role not in ("fleetManager", "system", "manager"):
        raise HTTPException(403, "Only a Fleet Admin or a Fleet Manager can set driver logins.")
    with transaction(dict_rows=True) as cur:
        cur.execute('SELECT name FROM "fleet_workspace"."drivers" WHERE id = %s', (driver_id,))
        drv = cur.fetchone()
        if not drv:
            raise ApiError(404, f"Driver '{driver_id}' not found.")
        tenant_id, slug = _driver_company(cur, ctx, body)
        if ctx.role == "manager" and ctx.kind == "company":
            # a company-scoped manager may take over only unassigned drivers (just created) or its own company's
            cur.execute("SELECT t.slug FROM public.tenant_memberships m JOIN public.tenants t ON t.id=m.tenant_id "
                        "WHERE m.role='driver' AND m.scope_id=%s", (driver_id,))
            slugs = {r["slug"] for r in cur.fetchall()}
            if slugs and ctx.slug not in slugs:
                raise ApiError(404, f"Driver '{driver_id}' not found.")
        res = upsert_scoped_login(cur, tenant_id, "driver", driver_id, body.email, body.password,
                                  body.displayName or drv["name"])
        # a driver serves one company at a time: logins to other companies are switched off (kept, so
        # moving the driver back restores them)
        cur.execute("UPDATE public.tenant_memberships SET active = FALSE WHERE role = 'driver' "
                    "AND scope_id = %s AND tenant_id <> %s AND active", (driver_id, tenant_id))
        moved_from = cur.rowcount
        audit.record(cur, audit.schema_of(cur, slug), actor=ctx.actor, role=ctx.role, action="login.set",
                     entity_type="driver", entity_id=driver_id,
                     summary=f"Driver {driver_id} login assigned to this company"
                             + (" (password set)" if res.get("passwordChanged") else ""))
        if moved_from:
            audit.record(cur, "fleet_workspace", actor=ctx.actor, role=ctx.role, action="driver.transfer",
                         entity_type="driver", entity_id=driver_id,
                         summary=f"Driver {driver_id} moved to {slug}; {moved_from} previous company login(s) "
                                 "switched off")
    security_log.privilege_change(request, ctx.user.user_id, "driver_login_set", target_email=body.email,
                                  tenant=slug, role="driver", driver=driver_id,
                                  passwordSet=bool(res.get("passwordChanged")), transferred=bool(moved_from))
    return {"driverId": driver_id, "company": slug, **res, "previousCompanyLoginsDisabled": moved_from}


@router.get("/drivers/{driver_id}/login")
def get_driver_login(driver_id: str, ctx: TenantCtx = Depends(tenant_access)) -> dict:
    """Where is this driver assigned and with which login email (never the password)?"""
    if ctx.role not in ("fleetManager", "system", "manager"):
        raise HTTPException(403, "Not permitted.")
    with transaction(dict_rows=True) as cur:
        cur.execute("SELECT t.slug, u.email, m.active FROM public.tenant_memberships m "
                    "JOIN public.tenants t ON t.id=m.tenant_id JOIN public.platform_users u ON u.id=m.user_id "
                    "WHERE m.role='driver' AND m.scope_id=%s ORDER BY t.slug", (driver_id,))
        rows = [dict(r) for r in cur.fetchall()]
    if ctx.role == "manager" and ctx.kind == "company":
        rows = [r for r in rows if r["slug"] == ctx.slug]
    return {"driverId": driver_id, "assignments": rows}
