"""Refresh tokens: long-lived (REFRESH_TOKEN_DAYS), single-use, rotated on every refresh.

* Only the SHA-256 hash of a token is stored (``public.refresh_tokens``); the raw value exists only in the
  response that issued it and on the user's device.
* Every refresh consumes the presented token and issues a new one in the same *family*.
* Presenting a token that was already used (a stolen copy being replayed) revokes the whole family and
  ends every session of the account (``token_version`` bump).
* A token is also refused when the account is inactive or its ``token_version`` changed since issue
  (logout, password change, deactivation).
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg2

from .config import get_settings


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _tolerant(cur, sql: str, params: tuple) -> bool:
    """Run ``sql`` inside a savepoint; False when the table does not exist yet (migration 0008 not applied
    on this database). Any other error is raised."""
    cur.execute("SAVEPOINT rt_sp")
    try:
        cur.execute(sql, params)
    except psycopg2.errors.UndefinedTable:
        cur.execute("ROLLBACK TO SAVEPOINT rt_sp")
        return False
    cur.execute("RELEASE SAVEPOINT rt_sp")
    return True


def issue(cur, user_id: str, token_version: int, family_id: str | None = None) -> str | None:
    """New refresh token for ``user_id``. None when refresh tokens are not available on this database."""
    raw = secrets.token_urlsafe(48)
    expires = datetime.now(timezone.utc) + timedelta(days=get_settings().refresh_token_days)
    ok = _tolerant(cur, "INSERT INTO public.refresh_tokens(user_id, family_id, token_hash, token_version, expires_at) "
                        "VALUES (%s::uuid, %s, %s, %s, %s)",
                   (user_id, family_id or secrets.token_hex(12), _hash(raw), int(token_version or 0), expires))
    if not ok:
        return None
    # housekeeping: this account's long-expired rows
    _tolerant(cur, "DELETE FROM public.refresh_tokens WHERE user_id = %s::uuid AND expires_at < now() - interval '1 day'",
              (user_id,))
    return raw


def revoke_all(cur, user_id: str) -> None:
    """Revoke every refresh token of the account (logout, password change, deactivation)."""
    _tolerant(cur, "UPDATE public.refresh_tokens SET revoked_at = now() "
                   "WHERE user_id = %s::uuid AND revoked_at IS NULL", (user_id,))


@dataclass
class Outcome:
    ok: bool
    user_id: str | None = None
    email: str | None = None
    token_version: int = 0
    family_id: str | None = None
    reuse: bool = False          # an already-used token was presented: family revoked, sessions ended


def consume(cur, raw: str) -> Outcome:
    """Validate and consume ``raw`` (row-locked). The caller issues the next token on success."""
    cur.execute("SAVEPOINT rt_consume")
    try:
        cur.execute("SELECT r.id, r.user_id::text AS user_id, r.family_id, r.token_version, r.expires_at, "
                    "r.used_at, r.revoked_at, u.active, u.email, u.token_version AS current_version "
                    "FROM public.refresh_tokens r JOIN public.platform_users u ON u.id = r.user_id "
                    "WHERE r.token_hash = %s FOR UPDATE OF r", (_hash(raw),))
        row = cur.fetchone()
    except psycopg2.errors.UndefinedTable:
        cur.execute("ROLLBACK TO SAVEPOINT rt_consume")
        return Outcome(False)
    cur.execute("RELEASE SAVEPOINT rt_consume")
    if row is None:
        return Outcome(False)
    row = dict(row)
    if row["used_at"] is not None:
        # replay of a rotated token: assume theft, end everything for this account
        cur.execute("UPDATE public.refresh_tokens SET revoked_at = coalesce(revoked_at, now()) "
                    "WHERE family_id = %s OR user_id = %s::uuid", (row["family_id"], row["user_id"]))
        cur.execute("UPDATE public.platform_users SET token_version = token_version + 1 WHERE id = %s::uuid",
                    (row["user_id"],))
        return Outcome(False, row["user_id"], row["email"], reuse=True, family_id=row["family_id"])
    expired = row["expires_at"] <= datetime.now(timezone.utc)
    if (row["revoked_at"] is not None or expired or not row["active"]
            or int(row["token_version"]) != int(row["current_version"])):
        return Outcome(False, row["user_id"], row["email"])
    cur.execute("UPDATE public.refresh_tokens SET used_at = now() WHERE id = %s", (row["id"],))
    return Outcome(True, row["user_id"], row["email"], int(row["current_version"]), row["family_id"])
