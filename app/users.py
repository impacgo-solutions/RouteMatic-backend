"""Shared helpers for platform users and tenant memberships."""
from __future__ import annotations

from . import security
from .repo import ApiError


def ensure_user(cur, email: str, display_name: str, password: str | None) -> tuple[str, bool]:
    """Return (user_id, created). Existing users keep their password."""
    cur.execute("SELECT id::text FROM public.platform_users WHERE lower(email)=lower(%s)", (email,))
    row = cur.fetchone()
    if row:
        return row["id"], False
    if not password:
        raise ApiError(422, f"A password is required to create the login for {email}.")
    try:
        h = security.hash_password(password)
    except ValueError as exc:
        raise ApiError(422, str(exc))
    cur.execute("INSERT INTO public.platform_users(email, password_hash, display_name) "
                "VALUES (%s,%s,%s) RETURNING id::text", (email, h, display_name))
    return cur.fetchone()["id"], True


def add_membership(cur, user_id: str, tenant_id, role: str, scope_id: str | None) -> None:
    cur.execute("INSERT INTO public.tenant_memberships(user_id, tenant_id, role, scope_id) "
                "VALUES (%s::uuid,%s,%s,%s) ON CONFLICT (user_id, tenant_id) DO NOTHING",
                (user_id, tenant_id, role, scope_id))


def owned_only_here(cur, user_id: str, tenant_id, role: str, scope_id: str | None = None) -> bool:
    """May an admin of THIS tenant manage the account's credentials?

    Only if the account exists solely for this one membership (not shared with other
    companies / roles): otherwise the person must change their own password.
    """
    if _privileged(cur, user_id):
        return False      # platform admins / Fleet Admins manage their own credentials
    # A DRIVER's logins at other companies are the same person (driver ids are fleet-wide, one row in
    # fleet_workspace.drivers), so they do not make the account "shared". Employee ids are per company
    # (E1 exists in many companies): an employee login anywhere else always makes the account shared,
    # otherwise another company's admin could create a colliding id and reset this person's password.
    same_person_elsewhere = role == "driver" and scope_id is not None
    cur.execute("SELECT count(*) AS n FROM public.tenant_memberships WHERE user_id=%s::uuid "
                "AND NOT (tenant_id=%s AND role=%s) "
                "AND NOT (%s AND role=%s AND scope_id=%s)",
                (user_id, tenant_id, role, same_person_elsewhere, role, scope_id))
    return cur.fetchone()["n"] == 0


def _privileged(cur, user_id: str) -> bool:
    """Platform admin or Fleet Admin account: never attachable / resettable by a tenant admin."""
    cur.execute('SELECT (u.is_platform_admin OR EXISTS (SELECT 1 FROM "fleet_workspace"."admins" a '
                "WHERE a.user_id = u.id)) AS p FROM public.platform_users u WHERE u.id = %s::uuid", (user_id,))
    row = cur.fetchone()
    return bool(row and row["p"])


# One message for every reason an email cannot be used (taken, privileged, other role here, …):
# the response never says which, so it cannot be used to discover accounts.
GENERIC_REFUSAL = "This email can't be used for this login. Use a different email address."


def upsert_scoped_login(cur, tenant_id, role: str, scope_id: str, email: str, password: str | None,
                        display_name: str) -> dict:
    """Create / update the login of ONE person (employee or driver) in ONE company.

    * new email            -> new account (password required), membership ``role`` scoped to ``scope_id``
    * email of an account  -> membership added, the account's own password is NOT touched
    * person already has a login here -> password / email updated, but only if the account is theirs alone
    The password is hashed and stored only in ``public.platform_users``.
    """
    cur.execute("SELECT m.user_id::text AS uid FROM public.tenant_memberships m "
                "WHERE m.tenant_id=%s AND m.role=%s AND m.scope_id=%s", (tenant_id, role, scope_id))
    mine = cur.fetchone()
    cur.execute("SELECT id::text AS id FROM public.platform_users WHERE lower(email)=lower(%s)", (email,))
    other = cur.fetchone()
    if mine:
        uid = mine["uid"]
        if not owned_only_here(cur, uid, tenant_id, role, scope_id):
            raise ApiError(409, "This login can't be changed here; the person changes it themselves.")
        if other and other["id"] != uid:
            raise ApiError(409, "This email can't be used for this login. Use a different email address.")
        cur.execute("UPDATE public.tenant_memberships SET active = TRUE WHERE user_id = %s::uuid "
                    "AND tenant_id = %s AND role = %s", (uid, tenant_id, role))
        if password:
            try:
                h = security.hash_password(password)
            except ValueError as exc:
                raise ApiError(422, str(exc))
            from .admins import set_password
            set_password(cur, uid, h)
        cur.execute("UPDATE public.platform_users SET email=%s, display_name=coalesce(%s, display_name) "
                    "WHERE id=%s::uuid", (email, display_name, uid))
        return {"email": email, "passwordChanged": bool(password)}
    # a NEW login always needs a valid password, whether or not the email already has an account, so the
    # answer does not tell the caller which is the case
    if not password:
        raise ApiError(422, "A password (at least 8 characters) is required to create the login.")
    try:
        new_hash = security.hash_password(password)
    except ValueError as exc:
        raise ApiError(422, str(exc))
    if other:
        cur.execute("SELECT 1 FROM public.tenant_memberships WHERE user_id=%s::uuid AND tenant_id=%s",
                    (other["id"], tenant_id))
        if cur.fetchone():
            raise ApiError(409, GENERIC_REFUSAL)
        # an existing account may be shared only if it is an ordinary, active login (never a platform
        # admin / Fleet Admin, and never an orphan account without any role, whose password a tenant
        # admin could otherwise take over on the next call)
        cur.execute("SELECT count(*) AS n FROM public.tenant_memberships WHERE user_id=%s::uuid", (other["id"],))
        roles = cur.fetchone()["n"]
        if roles == 0 or _privileged(cur, other["id"]):
            raise ApiError(409, GENERIC_REFUSAL)
        add_membership(cur, other["id"], tenant_id, role, scope_id)
        # the person keeps their own password: the admin learns only that the password was not applied
        return {"email": email, "passwordChanged": False}
    del new_hash
    uid, _ = ensure_user(cur, email, display_name, password)
    add_membership(cur, uid, tenant_id, role, scope_id)
    return {"email": email, "passwordChanged": True}
