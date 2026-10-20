"""Report on protected databases WITHOUT connecting to them.

Only the cluster catalogue (pg_database) is read from the maintenance database; no
connection to, query of, or change to ``impaco_test_dev`` is ever made. The
application's connection layer additionally refuses to open it
(see ``app.db.validate_db_name``).
"""
from __future__ import annotations

from app import db
from app.config import PROTECTED_DATABASES


def main() -> int:
    conn = db.admin_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT datname, pg_get_userbyid(datdba) FROM pg_database ORDER BY 1")
            existing = {n: o for n, o in cur.fetchall()}
    finally:
        conn.close()
    for name in sorted(PROTECTED_DATABASES):
        state = f"present (owner {existing[name]}) - untouched" if name in existing \
            else "NOT present on this server - nothing to protect/modify"
        print(f"{name}: {state}")
    print("Routematic:", "present" if "Routematic" in existing else "missing")
    try:
        db.validate_db_name("impaco_test_dev")
    except db.DatabaseSafetyError as exc:
        print(f"Guard check OK: {exc}")
        return 0
    print("GUARD FAILURE")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
