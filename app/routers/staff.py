"""Manager Management (Fleet Admin only).

A Manager is FLEET staff: it has no company. The Fleet Admin creates the login; the manager then signs in on the
single sign-in screen (Fleet workspace -> Fleet Manager -> email + password) and lands on the SAME dashboard as the
Fleet Admin, with permissions determined by its role (see rbac.py).

Where things live (nothing is duplicated):
  public.platform_users        name, email, password hash                       (identity, one row per person)
  public.tenant_memberships    role 'manager' in the fleet workspace, active flag   (access)
  fleet_workspace.managers       id, phone, status                                (what is specific to the role)

Deactivating a manager sets managers.status='inactive' AND membership.active=false: the login is refused at
sign-in and on every request from then on, even with a token issued earlier.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit, security, security_log
from ..db import tenant_cursor
from ..deps import TenantCtx, tenant_access
from ..repo import ApiError, out_value
from ..users import ensure_user, owned_only_here

router = APIRouter(prefix="/api/v1/t/{tenant}/staff", tags=["staff"])

EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class ManagerIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(pattern=EMAIL_RE, max_length=254)
    phone: str = Field(default="", max_length=40)
    password: str | None = Field(default=None, min_length=8, max_length=200)


class ManagerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    email: str | None = Field(default=None, pattern=EMAIL_RE, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    password: str | None = Field(default=None, min_length=8, max_length=200)


def _fleet_admin(ctx: TenantCtx) -> None:
    if ctx.kind != "fleet":
        raise HTTPException(403, "Managers are managed from the Fleet Admin workspace.")
    if not (ctx.user.is_platform_admin or ctx.role in ("fleetManager", "system")):
        raise HTTPException(403, "Only a Fleet Admin can manage managers.")


_SELECT = ('SELECT m.id, m.phone, m.status, m.created_at, m.updated_at, m.user_id::text AS user_id, '
           'u.email, u.display_name FROM "managers" m JOIN public.platform_users u ON u.id = m.user_id')


def _shape(row: dict) -> dict:
    return {"id": row["id"], "name": row["display_name"], "email": row["email"], "phone": row["phone"],
            "status": row["status"], "createdAt": out_value(row["created_at"]),
            "updatedAt": out_value(row["updated_at"])}


def _fleet_tenant_id(cur, slug: str):
    cur.execute("SELECT id FROM public.tenants WHERE slug=%s AND kind='fleet'", (slug,))
    return cur.fetchone()["id"]


def _find(cur, manager_id: str) -> dict:
    cur.execute(_SELECT + " WHERE m.id = %s", (manager_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(404, f"Manager '{manager_id}' not found.")
    return dict(row)


@router.get("/managers")
def list_managers(status: str | None = None, ctx: TenantCtx = Depends(tenant_access)) -> dict:
    _fleet_admin(ctx)
    if status not in (None, "active", "inactive"):
        raise ApiError(422, "status must be 'active' or 'inactive'.")
    with tenant_cursor(ctx.slug) as cur:
        cur.execute(_SELECT + " WHERE (%s::text IS NULL OR m.status = %s) ORDER BY u.display_name",
                    (status, status))
        items = [_shape(dict(r)) for r in cur.fetchall()]
    return {"items": items, "total": len(items)}


@router.post("/managers", status_code=201)
def create_manager(body: ManagerIn, request: Request, ctx: TenantCtx = Depends(tenant_access)) -> dict:
    _fleet_admin(ctx)
    with tenant_cursor(ctx.slug) as cur:
        fleet_id = _fleet_tenant_id(cur, ctx.slug)
        uid, created = ensure_user(cur, body.email, body.name, body.password)
        cur.execute("SELECT 1 FROM public.tenant_memberships WHERE user_id=%s::uuid AND tenant_id=%s",
                    (uid, fleet_id))
        if cur.fetchone():
            raise ApiError(409, "That email already has a role in the Fleet workspace.")
        cur.execute("INSERT INTO public.tenant_memberships(user_id, tenant_id, role, scope_id) "
                    "VALUES (%s::uuid,%s,'manager',NULL)", (uid, fleet_id))
        mid = "MG-" + uuid.uuid4().hex[:8].upper()
        cur.execute('INSERT INTO "managers"(id, user_id, phone, created_by) VALUES (%s,%s::uuid,%s,%s)',
                    (mid, uid, body.phone.strip(), ctx.actor))
        audit.record(cur, "fleet_workspace", actor=ctx.actor, role=ctx.role, action="manager.create",
                     entity_type="manager", entity_id=mid, summary=f"Fleet Manager {mid} created")
        result = _shape(_find(cur, mid))
    security_log.privilege_change(request, ctx.user.user_id, "manager_create", target_email=body.email,
                                  tenant=ctx.slug, role="manager", manager=mid)
    return result


@router.get("/managers/{manager_id}")
def get_manager(manager_id: str, ctx: TenantCtx = Depends(tenant_access)) -> dict:
    _fleet_admin(ctx)
    with tenant_cursor(ctx.slug) as cur:
        return _shape(_find(cur, manager_id))


@router.patch("/managers/{manager_id}")
def update_manager(manager_id: str, body: ManagerPatch, request: Request,
                   ctx: TenantCtx = Depends(tenant_access)) -> dict:
    _fleet_admin(ctx)
    with tenant_cursor(ctx.slug) as cur:
        row = _find(cur, manager_id)
        fleet_id = _fleet_tenant_id(cur, ctx.slug)
        identity = body.name is not None or body.email is not None or body.password is not None
        if identity and not owned_only_here(cur, row["user_id"], fleet_id, "manager"):
            raise ApiError(409, "This account is shared with another workspace/role; the person must change "
                                "their name, email or password themselves.")
        if body.email is not None:
            cur.execute("SELECT 1 FROM public.platform_users WHERE lower(email)=lower(%s) AND id <> %s::uuid",
                        (body.email, row["user_id"]))
            if cur.fetchone():
                raise ApiError(409, "That email already belongs to another account.")
            cur.execute("UPDATE public.platform_users SET email=%s WHERE id=%s::uuid", (body.email, row["user_id"]))
        if body.name is not None:
            cur.execute("UPDATE public.platform_users SET display_name=%s WHERE id=%s::uuid",
                        (body.name, row["user_id"]))
        if body.password is not None:
            from ..admins import set_password
            set_password(cur, row["user_id"], security.hash_password(body.password))
        changed = [k for k in ("name", "email", "phone", "password") if getattr(body, k) is not None]
        audit.record(cur, "fleet_workspace", actor=ctx.actor, role=ctx.role, action="manager.update",
                     entity_type="manager", entity_id=manager_id,
                     summary=f"Fleet Manager {manager_id} updated: {', '.join(changed) or 'no fields'}")
        if body.phone is not None:
            cur.execute('UPDATE "managers" SET phone=%s, updated_at=now() WHERE id=%s',
                        (body.phone.strip(), manager_id))
        else:
            cur.execute('UPDATE "managers" SET updated_at=now() WHERE id=%s', (manager_id,))
        result = _shape(_find(cur, manager_id))
    if body.password is not None or body.email is not None:
        security_log.privilege_change(request, ctx.user.user_id, "manager_credentials_change",
                                      target_email=result.get("email"), tenant=ctx.slug, role="manager",
                                      manager=manager_id, fields=[k for k in ("email", "password")
                                                                  if getattr(body, k) is not None])
    return result


def _set_status(manager_id: str, ctx: TenantCtx, active: bool, request: Request) -> dict:
    _fleet_admin(ctx)
    with tenant_cursor(ctx.slug) as cur:
        row = _find(cur, manager_id)
        fleet_id = _fleet_tenant_id(cur, ctx.slug)
        cur.execute('UPDATE "managers" SET status=%s, updated_at=now() WHERE id=%s',
                    ("active" if active else "inactive", manager_id))
        cur.execute("UPDATE public.tenant_memberships SET active=%s WHERE user_id=%s::uuid AND tenant_id=%s "
                    "AND role='manager'", (active, row["user_id"], fleet_id))
        audit.record(cur, "fleet_workspace", actor=ctx.actor, role=ctx.role,
                     action="manager.activate" if active else "manager.deactivate", entity_type="manager",
                     entity_id=manager_id,
                     summary=f"Fleet Manager {manager_id} {'activated' if active else 'deactivated'}")
        result = _shape(_find(cur, manager_id))
    security_log.privilege_change(request, ctx.user.user_id, "manager_activate" if active else "manager_deactivate",
                                  target_email=result.get("email"), tenant=ctx.slug, role="manager",
                                  manager=manager_id)
    return result


@router.post("/managers/{manager_id}/deactivate")
def deactivate_manager(manager_id: str, request: Request, ctx: TenantCtx = Depends(tenant_access)) -> dict:
    return _set_status(manager_id, ctx, False, request)


@router.post("/managers/{manager_id}/activate")
def activate_manager(manager_id: str, request: Request, ctx: TenantCtx = Depends(tenant_access)) -> dict:
    return _set_status(manager_id, ctx, True, request)
