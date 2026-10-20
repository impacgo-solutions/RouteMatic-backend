"""Authentication + tenant-authorisation dependencies."""
from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import security, security_log
from .access import blocked_reason, fleet_staff_role
from .db import pin_search_path, transaction

bearer = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    user_id: str
    email: str
    display_name: str
    is_platform_admin: bool


@dataclass
class TenantCtx:
    slug: str
    role: str
    scope_id: str | None
    actor: str
    user: Principal
    kind: str = "company"             # 'fleet' (fleet_workspace schema) | 'company' (tenant_<slug>)
    company_code: str | None = None   # company scopes: the company's code in the fleet registry

    @property
    def employer_scope(self) -> str | None:
        """Employer admins only ever see their own employer's rows."""
        return self.scope_id if self.role == "employerAdmin" else None


def current_user(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    if creds is None:
        raise HTTPException(401, "Authentication required.", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = security.decode_claims(creds.credentials)
        uid = claims["sub"]
    except (jwt.PyJWTError, KeyError):
        raise HTTPException(401, "Invalid or expired token.", headers={"WWW-Authenticate": "Bearer"})
    with transaction(dict_rows=True) as cur:
        cur.execute("SELECT id::text, email, display_name, is_platform_admin, token_version "
                    "FROM public.platform_users WHERE id = %s::uuid AND active", (uid,))
        r = cur.fetchone()
    if not r:
        raise HTTPException(401, "Account not found or disabled.")
    if int(claims.get("tv", 0)) != r["token_version"]:
        # the password was changed after this token was issued
        raise HTTPException(401, "Your session ended because the password was changed. Please sign in again.",
                            headers={"WWW-Authenticate": "Bearer"})
    request.state.user_id = r["id"]          # for the security log of this request
    return Principal(r["id"], r["email"], r["display_name"], r["is_platform_admin"])


def platform_admin(user: Principal = Depends(current_user)) -> Principal:
    if not user.is_platform_admin:
        raise HTTPException(403, "Platform administrator access required.")
    return user


def tenant_access(tenant: str, request: Request, user: Principal = Depends(current_user)) -> TenantCtx:
    """Authorise the caller for the tenant slug in the URL.

    The slug is only a lookup key into ``public.tenants``; the schema name is never
    taken from the client. Unknown tenants and unauthorised tenants return the same
    403 so tenant existence is not disclosed.
    """
    request.state.tenant = tenant
    denied = HTTPException(403, "Access to this tenant is not permitted.")

    def refuse(reason: str) -> HTTPException:
        # a signed-in user reaching for a workspace that is not theirs: stored for review
        security_log.event("tenant.denied", "refused", request=request, user_id=user.user_id, email=user.email,
                           tenant=tenant, persist=True, reason=reason)
        request.state.security_logged = True
        return denied
    with transaction(dict_rows=True) as cur:
        cur.execute("SELECT t.id, t.kind FROM public.tenants t WHERE t.slug = %s AND t.status = 'ready'",
                    (tenant,))
        t = cur.fetchone()
        if not t:
            raise refuse("unknown tenant")
        code = None
        if t["kind"] == "company":
            cur.execute("SELECT code FROM fleet_workspace.companies WHERE tenant_id = %s", (t["id"],))
            c = cur.fetchone()
            code = c["code"] if c else None
        cur.execute("SELECT role, scope_id, active FROM public.tenant_memberships "
                    "WHERE user_id = %s::uuid AND tenant_id = %s", (user.user_id, t["id"]))
        m = cur.fetchone()
        if m and m["role"] == "employee":
            cur.execute("SELECT schema_name FROM public.tenants WHERE id = %s", (t["id"],))
            pin_search_path(cur, cur.fetchone()["schema_name"], t["kind"])
        if m and blocked_reason(cur, m["role"], m["scope_id"], m["active"], user.user_id):
            m = None          # deactivated logins are refused immediately (even with a valid token)
            if not user.is_platform_admin:
                raise refuse("membership blocked")
        if m is None and t["kind"] == "company":
            staff = fleet_staff_role(cur, user.user_id)   # Fleet Admin / Fleet Manager: every company
            if staff:
                m = {"role": staff, "scope_id": None}
    if m:
        return TenantCtx(tenant, m["role"], m["scope_id"], user.email, user, t["kind"], code)
    if user.is_platform_admin:
        return TenantCtx(tenant, "system", None, user.email, user, t["kind"], code)
    raise refuse("no membership")
