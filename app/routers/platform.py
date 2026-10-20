"""Authentication, tenant registry and membership endpoints (shared schema)."""
from __future__ import annotations

import logging
import secrets

import psycopg2.errors
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import admins, audit, ratelimit, refresh_tokens, security, security_log, tenants
from ..config import get_settings
from ..access import blocked_reason
from ..db import pin_search_path, transaction
from ..deps import Principal, current_user, platform_admin, tenant_access, TenantCtx
from ..spec import ENUMS

router = APIRouter(prefix="/api/v1", tags=["platform"])
log = logging.getLogger("routematic")

DUMMY_HASH = security.hash_password("not-a-real-password")


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, max_length=40)


class MemberIn(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    displayName: str = Field(min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    role: str
    scopeId: str | None = Field(default=None, max_length=64)


def _memberships(cur, user_id: str, is_admin: bool = False) -> list[dict]:
    cur.execute(
        "SELECT t.slug, t.name, m.role, m.scope_id AS \"scopeId\" FROM public.tenant_memberships m "
        "JOIN public.tenants t ON t.id = m.tenant_id WHERE m.user_id = %s::uuid AND t.status='ready' AND m.active "
        "AND (m.role <> 'fleetManager' OR EXISTS (SELECT 1 FROM \"fleet_workspace\".\"admins\" a "
        "WHERE a.user_id = m.user_id AND a.status = 'active')) "
        "ORDER BY t.slug", (user_id,))
    rows = [dict(r) for r in cur.fetchall()]
    from ..access import fleet_staff_role
    staff = fleet_staff_role(cur, user_id)
    if staff:     # Fleet Admin / Fleet Manager work in every company workspace with the same role
        have = {r["slug"] for r in rows}
        cur.execute("SELECT slug, name FROM public.tenants WHERE status='ready' AND kind='company' ORDER BY slug")
        rows += [{"slug": t["slug"], "name": t["name"], "role": staff, "scopeId": None}
                 for t in cur.fetchall() if t["slug"] not in have]
        rows.sort(key=lambda r: r["slug"])
    if is_admin:   # platform admins may open every ready tenant (role: system)
        have = {r["slug"] for r in rows}
        cur.execute("SELECT slug, name FROM public.tenants WHERE status='ready' ORDER BY slug")
        rows += [{"slug": t["slug"], "name": t["name"], "role": "system", "scopeId": None}
                 for t in cur.fetchall() if t["slug"] not in have]
        rows.sort(key=lambda r: r["slug"])
    return rows


def _privileged_account(cur, user_id: str) -> bool:
    cur.execute('SELECT (u.is_platform_admin OR EXISTS (SELECT 1 FROM "fleet_workspace"."admins" a '
                "WHERE a.user_id = u.id)) AS p FROM public.platform_users u WHERE u.id = %s::uuid", (user_id,))
    row = cur.fetchone()
    return bool(row and row["p"])


# --- one-screen sign-in: company + role + credentials, validated together -------------------
# UI role -> server role (tenant_memberships.role)
SIGNIN_ROLES = {"employee": "employee", "driver": "driver", "manager": "manager",
                "employerAdmin": "employerAdmin", "fleetManager": "fleetManager"}


class SignInIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)
    tenant: str = Field(min_length=1, max_length=40)       # the selected company (or "fleet")
    role: str = Field(min_length=1, max_length=40)         # the selected role


@router.get("/auth/organizations")
def organizations():
    """Names the sign-in screen offers in its company selector (public, names only).

    Returns the fleet workspace and the registered companies: slug + display name + kind.
    Nothing else (no ids, schemas, users or counts) is exposed before authentication.
    """
    with transaction(dict_rows=True) as cur:
        cur.execute("SELECT slug, name, kind FROM public.tenants WHERE status='ready' "
                    "ORDER BY (kind='fleet') DESC, lower(name)")
        return {"items": [dict(r) for r in cur.fetchall()]}


def _guard(request: Request, email: str) -> str:
    client = request.client.host if request.client else "?"
    wait = ratelimit.retry_after(client, email)
    if wait:
        security_log.event("login.locked", "refused", request=request, email=email, retryAfter=wait)
        raise HTTPException(429, f"Too many failed sign-in attempts. Try again in {max(1, wait // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})
    return client


def _failed(request: Request, client: str, email: str) -> None:
    """A wrong email/password: counted, logged, and stored when it triggers a lock-out (an attack burst)."""
    ratelimit.failed(client, email)
    locked = ratelimit.retry_after(client, email) > 0
    security_log.event("login.failed", "failure", request=request, email=email)
    if locked:
        security_log.event("login.lockout_triggered", "locked", request=request, email=email, persist=True)


def session_tokens(cur, user_id: str, token_version: int) -> dict:
    """Access token + refresh token fields returned by sign-in, refresh and password change."""
    out = {"accessToken": security.create_token(user_id, token_version), "tokenType": "bearer",
           "expiresIn": get_settings().access_minutes * 60}
    raw = refresh_tokens.issue(cur, user_id, token_version)
    if raw:      # absent only while migration 0008 is not applied on this database
        out["refreshToken"] = raw
    return out


@router.post("/auth/signin")
def signin(body: SignInIn, request: Request):
    """Validate credentials, the selected company and the selected role in one step.

    * wrong email/password  -> 401 (same message whether or not the email exists)
    * valid login, but no such role in that company -> 403 with a specific message
    Platform admins may enter any workspace as fleet admin / employer admin.
    """
    if body.role not in SIGNIN_ROLES:
        raise HTTPException(422, f"role must be one of {sorted(SIGNIN_ROLES)}")
    with transaction(dict_rows=True) as cur:
        client = _guard(request, body.email)
        u = admins.authenticate(cur, body.email, body.password, DUMMY_HASH)   # Fleet Admins: admins table
        if not u:
            _failed(request, client, body.email)
            raise HTTPException(401, "Invalid email or password.")
        ratelimit.succeeded(client, body.email)
        cur.execute("SELECT id, name, kind FROM public.tenants WHERE slug = %s AND status = 'ready'",
                    (body.tenant,))
        t = cur.fetchone()
        if not t:
            raise HTTPException(403, "The selected company is not available.")
        cur.execute("SELECT role, scope_id, active FROM public.tenant_memberships "
                    "WHERE user_id = %s::uuid AND tenant_id = %s", (u["id"], t["id"]))
        m = cur.fetchone()
        want = SIGNIN_ROLES[body.role]
        if m and m["role"] == "employee":
            cur.execute("SELECT schema_name, kind FROM public.tenants WHERE id = %s", (t["id"],))
            sch = cur.fetchone()
            pin_search_path(cur, sch["schema_name"], sch["kind"])
        why = blocked_reason(cur, m["role"], m["scope_id"], m["active"], u["id"]) if m and m["role"] == want             else None
        if m and m["role"] == want and not why:
            membership = {"slug": body.tenant, "name": t["name"], "role": want, "scopeId": m["scope_id"]}
        elif u["is_platform_admin"] and want in ("fleetManager", "employerAdmin"):
            membership = {"slug": body.tenant, "name": t["name"], "role": "system", "scopeId": None}
        elif why:
            security_log.event("login.denied", "refused", request=request, user_id=u["id"], email=body.email,
                               tenant=body.tenant, role=want, reason="blocked")
            raise HTTPException(403, why)
        else:
            security_log.event("login.denied", "refused", request=request, user_id=u["id"], email=body.email,
                               tenant=body.tenant, role=want, reason="no such role")
            label = {"employee": "an Employee", "driver": "a Driver", "manager": "a Fleet Manager",
                     "employerAdmin": "an Employer Admin", "fleetManager": "a Fleet Admin"}[want]
            raise HTTPException(403, f"This account is not {label} of {t['name']}.")
        security_log.event("login.success", "success", request=request, user_id=u["id"], email=body.email,
                           tenant=body.tenant, role=membership["role"])
        return {**session_tokens(cur, u["id"], u["token_version"]),
                "user": {"id": u["id"], "email": u["email"], "displayName": u["display_name"],
                         "isPlatformAdmin": u["is_platform_admin"]},
                "membership": membership,
                "memberships": _memberships(cur, u["id"], u["is_platform_admin"])}


@router.post("/auth/login")
def login(body: LoginIn, request: Request):
    with transaction(dict_rows=True) as cur:
        client = _guard(request, body.email)
        u = admins.authenticate(cur, body.email, body.password, DUMMY_HASH)
        if not u:
            _failed(request, client, body.email)
            raise HTTPException(401, "Invalid email or password.")
        ratelimit.succeeded(client, body.email)
        security_log.event("login.success", "success", request=request, user_id=u["id"], email=body.email)
        return {**session_tokens(cur, u["id"], u["token_version"]),
                "user": {"id": u["id"], "email": u["email"], "displayName": u["display_name"],
                         "isPlatformAdmin": u["is_platform_admin"]},
                "memberships": _memberships(cur, u["id"], u["is_platform_admin"])}


@router.get("/auth/me")
def me(user: Principal = Depends(current_user)):
    with transaction(dict_rows=True) as cur:
        return {"id": user.user_id, "email": user.email, "displayName": user.display_name,
                "isPlatformAdmin": user.is_platform_admin,
                "memberships": _memberships(cur, user.user_id, user.is_platform_admin)}


@router.get("/tenants")
def list_tenants(user: Principal = Depends(current_user)):
    if user.is_platform_admin:
        return {"items": tenants.list_tenants()}
    with transaction(dict_rows=True) as cur:
        return {"items": _memberships(cur, user.user_id)}


@router.post("/tenants", status_code=201)
def register_tenant(body: TenantIn, _: Principal = Depends(platform_admin)):
    try:
        return tenants.provision_tenant(body.name, body.slug)
    except tenants.TenantExists as exc:
        raise HTTPException(409, str(exc))
    except tenants.TenantError as exc:
        raise HTTPException(422, str(exc))
    except tenants.ProvisioningError:
        ref = secrets.token_hex(6)
        log.exception("tenant provisioning failed [ref %s]", ref)      # details stay in the server log
        raise HTTPException(500, f"Tenant provisioning failed and was rolled back (reference {ref}).")


@router.post("/tenants/{tenant}/members", status_code=201)
def add_member(body: MemberIn, request: Request, ctx: TenantCtx = Depends(tenant_access)):
    if not (ctx.user.is_platform_admin or ctx.role == "fleetManager"):
        raise HTTPException(403, "Only platform admins or the tenant fleet admin can add members.")
    codes = {c for c in ENUMS["UserRole"] if c != "system"}
    if ctx.kind == "fleet":
        # the fleet workspace only has Fleet Admins here: drivers get their logins (and row-level limits)
        # through a company workspace (PUT /drivers/{id}/login); Fleet Managers through /staff/managers
        codes = {"fleetManager"}
    if body.role not in codes:
        raise HTTPException(422, f"role must be one of {sorted(codes)}")
    if body.role in ("employerAdmin", "employee", "driver") and not body.scopeId:
        raise HTTPException(422, "scopeId (employer / employee / driver id) is required for this role.")
    with transaction(dict_rows=True) as cur:
        cur.execute("SELECT id FROM public.tenants WHERE slug=%s", (ctx.slug,))
        tenant_id = cur.fetchone()["id"]
        if body.role == "employerAdmin":
            # the scope must be THIS company's code (never another company's)
            if ctx.kind != "company" or body.scopeId != ctx.company_code:
                raise HTTPException(422, "scopeId must be this company's code for an Employer Admin.")
        cur.execute("SELECT id::text, is_platform_admin FROM public.platform_users WHERE lower(email)=lower(%s)",
                    (body.email,))
        u = cur.fetchone()
        if u is not None and not ctx.user.is_platform_admin and _privileged_account(cur, u["id"])                 and body.role != "fleetManager":
            # platform admins / Fleet Admins are never attached to other roles by someone else
            raise HTTPException(409, "This email cannot be used for this role.")
        if u is None:
            if not body.password:
                raise HTTPException(422, "password is required to create a new user.")
            cur.execute("INSERT INTO public.platform_users(email, password_hash, display_name) "
                        "VALUES (%s,%s,%s) RETURNING id::text",
                        (body.email, security.hash_password(body.password), body.displayName))
            u = cur.fetchone()
        try:
            cur.execute("INSERT INTO public.tenant_memberships(user_id, tenant_id, role, scope_id) "
                        "VALUES (%s::uuid,%s,%s,%s)", (u["id"], tenant_id, body.role, body.scopeId))
        except psycopg2.errors.UniqueViolation:
            raise HTTPException(409, "User is already a member of this tenant.")
        if ctx.kind == "fleet":   # fleet staff are listed in the fleet schema itself
            cur.execute('INSERT INTO "fleet_workspace"."fleet_users"(user_id, role) VALUES (%s::uuid, %s) '
                        "ON CONFLICT (user_id) DO NOTHING", (u["id"], body.role))
        if body.role == "fleetManager":   # Fleet Admins authenticate through fleet_workspace.admins
            admins.ensure_admin(cur, u["id"])
        audit.record(cur, audit.schema_of(cur, ctx.slug), actor=ctx.actor, role=ctx.role, action="member.add",
                     entity_type="membership", entity_id=body.email, summary=f"{body.role} access granted",
                     related={"grantedRole": body.role})
    security_log.privilege_change(request, ctx.user.user_id, "member_add", target_email=body.email,
                                  tenant=ctx.slug, role=body.role)
    return {"userId": u["id"], "tenant": ctx.slug, "role": body.role, "scopeId": body.scopeId}
