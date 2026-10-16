"""Initialise the Routematic database (idempotent, never destructive).

    python -m scripts.init_db            # create DB if missing, shared schema, fleet_workspace schema,
                                         # and upgrade every registered company schema
    python -m scripts.init_db --check    # health + protected-DB verification only

Companies are NOT created here: they are registered through the application / API
(POST /api/v1/t/fleet/companies) so the fleet registry always maps them.
"""
from __future__ import annotations

import argparse
import sys

from app import db, migrations, tenants
from app.config import get_settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="Only run health checks")
    args = ap.parse_args(argv)
    s = get_settings()

    try:
        db.validate_db_name(s.db_name)
        if not args.check:
            created = db.ensure_database()
            print(f"Database '{s.db_name}': {'CREATED' if created else 'already exists'}")
        info = db.health()
        print(f"Connected to '{info['database']}' ({info['server'].split(',')[0]})")
        if args.check:
            return 0
        with db.transaction() as cur:
            db.as_schema_owner(cur)
            applied = migrations.apply_shared(cur)
        print(f"Shared schema migrations applied: {applied or 'none (up to date)'}")
        fleet = tenants.provision_fleet()
        print(f"Fleet schema '{fleet['schemaName']}': {'CREATED' if fleet['created'] else 'present'} "
              f"(version {fleet['schemaVersion']})")
        upgraded = tenants.upgrade_all_tenants()
        for slug, ap_ in upgraded.items():
            print(f"Schema of '{slug}': {ap_ or 'up to date'}")
    except (db.DatabaseSafetyError, migrations.MigrationError, tenants.TenantError,
            tenants.ProvisioningError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
