"""Is a tenant membership usable right now? (deactivated manager / driver / employee / Fleet Admin logins)"""
from __future__ import annotations

from .admins import fleet_admin_block


FLEET_STAFF = ("fleetManager", "manager")


def fleet_staff_role(cur, user_id: str) -> str | None:
    """The person's usable role in the fleet workspace if it is Fleet Admin / Fleet Manager, else None.

    Fleet staff work in every company workspace with that same role (dispatch, billing, optimisation),
    not only in the companies they registered themselves.
    """
    cur.execute("SELECT m.role, m.active FROM public.tenant_memberships m JOIN public.tenants t "
                "ON t.id = m.tenant_id WHERE t.kind = 'fleet' AND t.status = 'ready' AND m.user_id = %s::uuid "
                "AND m.role IN ('fleetManager', 'manager')", (user_id,))
    m = cur.fetchone()
    if not m:
        return None
    role, active = (m["role"], m["active"]) if isinstance(m, dict) else (m[0], m[1])
    return None if blocked_reason(cur, role, None, active, user_id) else role


def blocked_reason(cur, role: str, scope_id: str | None, member_active: bool,
                   user_id: str | None = None) -> str | None:
    """None when the login may be used; otherwise a short reason (shown only after valid credentials)."""
    if not member_active:
        return "This login has been deactivated."
    if role == "driver" and scope_id:
        cur.execute('SELECT active FROM "fleet_workspace"."drivers" WHERE id = %s', (scope_id,))
        row = cur.fetchone()
        if row is None:
            # the login outlived its driver record (deleted): it must not inherit a re-used id
            return "This driver record no longer exists. Ask your Fleet Admin for access."
        if not (row["active"] if isinstance(row, dict) else row[0]):
            return "This driver account has been deactivated."
    if role == "employee" and scope_id:
        cur.execute('SELECT status FROM "employees" WHERE id = %s', (scope_id,))
        row = cur.fetchone()
        if row is None:
            return "This employee record no longer exists. Ask your company admin for access."
        if (row["status"] if isinstance(row, dict) else row[0]) == "inactive":
            return "This employee account has been deactivated."
    if role == "fleetManager":
        # Fleet Admin access requires an active account in fleet_workspace.admins
        return fleet_admin_block(cur, user_id)
    return None
