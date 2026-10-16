"""Session endpoints: renew with a refresh token, sign out everywhere, change your own password."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import admins, ratelimit, refresh_tokens, security, security_log
from ..config import get_settings
from ..db import transaction
from ..deps import Principal, current_user
from .platform import DUMMY_HASH, session_tokens

SESSION_EXPIRED = "Session expired. Please sign in again."

router = APIRouter(prefix="/api/v1/auth", tags=["session"])


class ChangePasswordIn(BaseModel):
    currentPassword: str = Field(min_length=1, max_length=security.MAX_PASSWORD)
    newPassword: str = Field(min_length=security.MIN_PASSWORD, max_length=security.MAX_PASSWORD)


class RefreshIn(BaseModel):
    refreshToken: str = Field(min_length=20, max_length=200)


@router.post("/refresh")
def refresh(body: RefreshIn, request: Request):
    """Swap a refresh token for a new access token + a new refresh token (the old one is used up).

    A refresh token that was already used (a copy being replayed) ends every session of the account."""
    with transaction(dict_rows=True) as cur:
        o = refresh_tokens.consume(cur, body.refreshToken)
        out = None
        if o.ok:
            out = {"accessToken": security.create_token(o.user_id, o.token_version),
                   "refreshToken": refresh_tokens.issue(cur, o.user_id, o.token_version, o.family_id),
                   "expiresIn": get_settings().access_minutes * 60, "tokenType": "bearer"}
    # (the transaction above is committed: a reuse revocation must stick even though we answer 401)
    if o.reuse:
        security_log.event("refresh.reuse_detected", "sessions_revoked", request=request, user_id=o.user_id,
                           email=o.email, persist=True, family=o.family_id)
    if out is None:
        if not o.reuse:
            security_log.event("refresh.invalid", "refused", request=request, user_id=o.user_id)
        raise HTTPException(401, SESSION_EXPIRED, headers={"WWW-Authenticate": "Bearer"})
    security_log.event("refresh.success", "success", request=request, user_id=o.user_id)
    return out


@router.post("/logout")
def logout(request: Request, user: Principal = Depends(current_user)):
    """End this account's sessions: every token issued so far (access and refresh, on any device) stops
    working."""
    with transaction(dict_rows=True) as cur:
        admins.revoke_sessions(cur, user.user_id)        # also revokes the refresh tokens
    security_log.event("logout", "success", request=request, user_id=user.user_id, email=user.email)
    return {"signedOut": True}


@router.post("/change-password")
def change_password(body: ChangePasswordIn, request: Request, user: Principal = Depends(current_user)):
    """Change your own password. Requires the current password; ends all other sessions and returns a
    fresh token for this one."""
    client = request.client.host if request.client else "?"
    wait = ratelimit.retry_after(client, user.email)
    if wait:
        raise HTTPException(429, f"Too many failed attempts. Try again in {max(1, wait // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})
    new = body.newPassword
    if new == body.currentPassword:
        raise HTTPException(422, "The new password must be different from the current one.")
    if new.strip().lower() == user.email.strip().lower():
        raise HTTPException(422, "The new password must not be your email address.")
    with transaction(dict_rows=True) as cur:
        u = admins.authenticate(cur, user.email, body.currentPassword, DUMMY_HASH)
        if not u or u["id"] != user.user_id:
            ratelimit.failed(client, user.email)
            failed = True
        else:
            failed = False
            ratelimit.succeeded(client, user.email)
            # bumps token_version and revokes every refresh token: all other sessions end
            admins.set_password(cur, user.user_id, security.hash_password(new))
            cur.execute("SELECT token_version FROM public.platform_users WHERE id = %s::uuid", (user.user_id,))
            version = cur.fetchone()["token_version"]
            tokens = session_tokens(cur, user.user_id, version)
    if failed:
        security_log.event("password.change", "failure", request=request, user_id=user.user_id, email=user.email,
                           reason="wrong current password")
        raise HTTPException(403, "The current password is not correct.")
    security_log.event("password.change", "success", request=request, user_id=user.user_id, email=user.email,
                       persist=True)
    return {"passwordChanged": True, **tokens}
