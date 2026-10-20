"""Create (or update) a Fleet Admin account: a row in fleet_workspace.admins (the account Fleet Admins
authenticate with) + platform identity + membership of the fleet workspace + fleet_workspace.fleet_users.

    python -m scripts.create_fleet_manager --email fm@example.com --name "Fleet Admin"
The password is read from ROUTEMATIC_FM_PASSWORD, or prompted.
"""
from __future__ import annotations

import argparse
import getpass
import os

from app import admins, db, security


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", default="Fleet Admin")
    a = ap.parse_args()
    pw = os.environ.get("ROUTEMATIC_FM_PASSWORD") or getpass.getpass("Password (min 8 chars): ")
    h = security.hash_password(pw)
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT id FROM public.tenants WHERE kind='fleet' AND status='ready'")
        fleet = cur.fetchone()
        if not fleet:
            raise SystemExit("Fleet schema not initialised: run `python -m scripts.init_db` first.")
        cur.execute(
            "INSERT INTO public.platform_users(email, password_hash, display_name) VALUES (%s,%s,%s) "
            "ON CONFLICT (lower(email)) DO UPDATE SET password_hash=EXCLUDED.password_hash, "
            "display_name=EXCLUDED.display_name, active=TRUE RETURNING id::text", (a.email, h, a.name))
        uid = cur.fetchone()["id"]
        cur.execute("INSERT INTO public.tenant_memberships(user_id, tenant_id, role) VALUES (%s,%s,'fleetManager') "
                    "ON CONFLICT (user_id, tenant_id) DO UPDATE SET role='fleetManager'", (uid, fleet["id"]))
        cur.execute('INSERT INTO "fleet_workspace"."fleet_users"(user_id, role) VALUES (%s,\'fleetManager\') '
                    "ON CONFLICT (user_id) DO UPDATE SET role='fleetManager', active=TRUE", (uid,))
        admins.ensure_admin(cur, uid)
        admins.set_password(cur, uid, h)
        admins.set_status(cur, uid, True)
        cur.execute(f"UPDATE {admins.ADMINS} SET name=%s, email=%s, updated_at=now() WHERE user_id=%s::uuid",
                    (a.name, a.email, uid))
    print(f"Fleet admin '{a.email}' ready (fleet_workspace.admins).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
