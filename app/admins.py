"""Fleet Admin accounts: ``fleet_workspace.admins`` is the source of truth for Fleet Admin authentication.

An admin row holds the account (id, name, email, PBKDF2 password hash, role, status, timestamps) and is
linked by ``user_id`` to the person's platform identity, which tokens and tenant memberships refer to.
For a Fleet Admin, credentials are always verified against ``admins.password_hash``; the copy in
``public.platform_users`` is kept identical by :func:`set_password` so the person has one password.
"""
from __future__ import annotations

from . import refresh_tokens, security
from .db import FLEET_SCHEMA

ADMINS = f'"{FLEET_SCHEMA}"."admins"'


def by_email(cur, email: str) -> dict | None:
    cur.execute(f"SELECT id, user_id::text AS user_id, name, email, password_hash, role, status "
                f"FROM {ADMINS} WHERE lower(email) = lower(%s)", (email,))
    row = cur.fetchone()
    return dict(row) if row else None


def by_user(cur, user_id: str) -> dict | None:
    cur.execute(f"SELECT id, user_id::text AS user_id, name, email, password_hash, role, status "
                f"FROM {ADMINS} WHERE user_id = %s::uuid", (user_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def ensure_admin(cur, user_id: str) -> dict:
    """Make the platform user a Fleet Admin account (idempotent; an existing row is left as it is)."""
    cur.execute(f"INSERT INTO {ADMINS} (user_id, name, email, password_hash) "
                "SELECT id, display_name, email, password_hash FROM public.platform_users WHERE id = %s::uuid "
                "ON CONFLICT (user_id) DO NOTHING", (user_id,))
    return by_user(cur, user_id)


def set_password(cur, user_id: str, password_hash: str) -> None:
    """Change a person's password everywhere it is checked (admins + platform identity)."""
    cur.execute(f"UPDATE {ADMINS} SET password_hash = %s, updated_at = now() WHERE user_id = %s::uuid",
                (password_hash, user_id))
    cur.execute("UPDATE public.platform_users SET password_hash = %s, token_version = token_version + 1 "
                "WHERE id = %s::uuid", (password_hash, user_id))
    refresh_tokens.revoke_all(cur, user_id)


def set_status(cur, user_id: str, active: bool) -> None:
    cur.execute(f"UPDATE {ADMINS} SET status = %s, updated_at = now() WHERE user_id = %s::uuid",
                ("active" if active else "inactive", user_id))


def authenticate(cur, email: str, password: str, dummy_hash: str) -> dict | None:
    """Verify email + password. Returns the active platform user row, or None.

    Fleet Admin accounts are checked against ``admins`` (by email); every other account against its
    platform identity. The work factor is the same whether or not the email exists.
    """
    admin = by_email(cur, email)
    if admin:
        cur.execute("SELECT id::text, email, display_name, password_hash, is_platform_admin, token_version "
                    "FROM public.platform_users WHERE id = %s::uuid AND active", (admin["user_id"],))
        user = cur.fetchone()
        stored = admin["password_hash"]
    else:
        cur.execute("SELECT id::text, email, display_name, password_hash, is_platform_admin, token_version "
                    "FROM public.platform_users WHERE lower(email) = lower(%s) AND active", (email,))
        user = cur.fetchone()
        stored = user["password_hash"] if user else dummy_hash
    ok = security.verify_password(password, stored)
    if not (user and ok):
        return None
    if admin:
        cur.execute(f"UPDATE {ADMINS} SET last_login_at = now() WHERE id = %s", (admin["id"],))
    if security.needs_rehash(stored) and security.MIN_PASSWORD <= len(password) <= security.MAX_PASSWORD:
        rehash(cur, user["id"], security.hash_password(password))
    return dict(user)


def rehash(cur, user_id: str, password_hash: str) -> None:
    """Upgrade the stored hash to the current work factor (same password: sessions stay valid)."""
    cur.execute(f"UPDATE {ADMINS} SET password_hash = %s WHERE user_id = %s::uuid", (password_hash, user_id))
    cur.execute("UPDATE public.platform_users SET password_hash = %s WHERE id = %s::uuid",
                (password_hash, user_id))


def revoke_sessions(cur, user_id: str) -> int:
    """End every session of this account (all tokens carry the old version). Returns the new version."""
    cur.execute("UPDATE public.platform_users SET token_version = token_version + 1 WHERE id = %s::uuid "
                "RETURNING token_version", (user_id,))
    row = cur.fetchone()
    refresh_tokens.revoke_all(cur, user_id)
    return int(row["token_version"] if isinstance(row, dict) else row[0])


def fleet_admin_block(cur, user_id: str | None) -> str | None:
    """Why a fleetManager membership may not be used (None = fine)."""
    if not user_id:
        return None
    admin = by_user(cur, user_id)
    if admin is None:
        return "This account is not a registered Fleet Admin."
    if admin["status"] != "active":
        return "This Fleet Admin account has been deactivated."
    return None
