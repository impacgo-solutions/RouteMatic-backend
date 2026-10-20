"""Create (or reset) the platform administrator.

    python -m scripts.create_admin --email admin@example.com --name "Platform Admin"
The password is read from the ROUTEMATIC_ADMIN_PASSWORD env var, or prompted.
"""
from __future__ import annotations

import argparse
import getpass
import os

from app import admins, db, security


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", default="Platform Admin")
    a = ap.parse_args()
    pw = os.environ.get("ROUTEMATIC_ADMIN_PASSWORD") or getpass.getpass("Password (min 8 chars): ")
    h = security.hash_password(pw)
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO public.platform_users(email, password_hash, display_name, is_platform_admin) "
            "VALUES (%s,%s,%s,TRUE) ON CONFLICT (lower(email)) DO UPDATE SET "
            "password_hash=EXCLUDED.password_hash, is_platform_admin=TRUE, active=TRUE, "
            "token_version=public.platform_users.token_version + 1",
            (a.email, h, a.name))
        # one password per person: if this account is also a Fleet Admin, update that copy too
        cur.execute(f"UPDATE {admins.ADMINS} SET password_hash=%s, updated_at=now() "
                    "WHERE user_id=(SELECT id FROM public.platform_users WHERE lower(email)=lower(%s))", (h, a.email))
    print(f"Platform admin '{a.email}' ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
