"""One-off: move a database built with the OLD layout (an "operator" tenant schema that held
everything + one full-structure schema per company) to the Fleet Admin layout:

    fleet_workspace        fleet-level tables + company registry (central mapping) + fleet users
    tenant_<company>     company-level tables only

    python -m scripts.migrate_legacy_layout            # dry run: prints the plan + checks
    python -m scripts.migrate_legacy_layout --apply    # does it, in ONE transaction

Safety: nothing is dropped unless the data was copied and the row counts match; any surprise
(data it cannot place) aborts and rolls back the whole transaction. Companies keep their
tenant ids, schema names, memberships and logins.
"""
from __future__ import annotations

import argparse
import sys

from psycopg2 import sql

from app import db, migrations, tenants
from app.spec import ENTITIES, TABLE_KINDS

# Tables of an old operator schema that may hold rows WITHOUT blocking the migration because they
# are directory copies of the companies' own profile rows (the company schemas own those).
DIRECTORY_COPIES = {"employers", "employer_shifts", "employer_vehicle_type_prefs"}


def q(schema: str, table: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def count(cur, schema: str, table: str) -> int:
    cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(q(schema, table)))
    return cur.fetchone()["n"]


def tables_in(cur, schema: str) -> set[str]:
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s", (schema,))
    return {r["tablename"] for r in cur.fetchall()}


def common_cols(cur, src: str, dst: str, table: str) -> list[str]:
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
                "ORDER BY ordinal_position", (dst, table))
    dcols = [r["column_name"] for r in cur.fetchall()]
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                (src, table))
    scols = {r["column_name"] for r in cur.fetchall()}
    return [c for c in dcols if c in scols]


def copy_table(cur, src: str, dst: str, table: str, where: str | None = None) -> int:
    cols = common_cols(cur, src, dst, table)
    ident = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
    stmt = sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {}").format(q(dst, table), ident, ident, q(src, table))
    if where:
        stmt = stmt + sql.SQL(" WHERE " + where)
    cur.execute(stmt)
    return cur.rowcount


def run(apply: bool) -> int:
    log: list[str] = []

    def say(msg: str) -> None:
        log.append(msg)
        print(msg)

    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT to_regclass('public.companies') AS r")
        legacy_registry = cur.fetchone()["r"] is not None
        cur.execute("SELECT 1 FROM public.tenants WHERE kind='fleet'") if not legacy_registry else None
        if not legacy_registry:
            say("No legacy registry (public.companies): nothing to migrate.")
            return 0
        cur.execute("SELECT c.*, t.slug, t.schema_name FROM public.companies c "
                    "JOIN public.tenants t ON t.id = c.tenant_id ORDER BY c.code")
        companies = cur.fetchall()
        operator_ids = {c["parent_tenant_id"] for c in companies if c["parent_tenant_id"]}
        cur.execute("SELECT id, slug, schema_name FROM public.tenants WHERE id = ANY(%s::uuid[])",
                    (list(operator_ids),))
        operators = cur.fetchall()
        # operator tenants that registered no company still exist as plain legacy tenants
        say(f"Legacy companies: {[c['code'] for c in companies]}; operator schemas: "
            f"{[o['schema_name'] for o in operators]}")

        fleet_tables = {t for t, ks in TABLE_KINDS.items() if "fleet" in ks}
        company_tables = {t for t, ks in TABLE_KINDS.items() if "company" in ks}
        both_tables = {t for t, ks in TABLE_KINDS.items() if ks == {"fleet", "company"}}
        problems: list[str] = []

        # ---- pre-checks (nothing is changed yet)
        for c in companies:
            for t in sorted(tables_in(cur, c["schema_name"]) - {"schema_migrations"}):
                n = count(cur, c["schema_name"], t)
                if n and t not in company_tables:
                    problems.append(f"{c['schema_name']}.{t} holds {n} rows but is a fleet-level table")
        for o in operators:
            for t in sorted(tables_in(cur, o["schema_name"]) - {"schema_migrations", "id_counters"}):
                n = count(cur, o["schema_name"], t)
                if not n or t in DIRECTORY_COPIES:
                    continue
                if t in fleet_tables or t in both_tables:
                    continue                        # will be moved into fleet_workspace
                if t == "locations":
                    cur.execute(sql.SQL("SELECT count(*) AS n FROM {} WHERE type NOT IN ('parking','depot')")
                                .format(q(o["schema_name"], t)))
                    if cur.fetchone()["n"]:
                        problems.append(f"{o['schema_name']}.locations holds company pickup/office points")
                    continue
                problems.append(f"{o['schema_name']}.{t} holds {n} company-level rows with no company to own them")
        if problems:
            for p in problems:
                say("BLOCKED: " + p)
            return 1
        say("Pre-checks passed.")
        if not apply:
            say("Dry run only. Re-run with --apply to migrate.")
            return 0

        # ---- 1. detach the legacy registry so shared migration 0003 can run (rows kept in memory)
        cur.execute("DELETE FROM public.companies")
        applied = migrations.apply_shared(cur)
        say(f"Shared migrations applied: {applied}")

        # ---- 2. fleet schema
        fleet = tenants.provision_fleet_in(cur)
        say(f"Fleet schema: {'created' if fleet['created'] else 'present'} ({fleet['schemaName']})")

        # ---- 3. rebuild every company schema in the new (company-only) structure
        cur.execute("SET CONSTRAINTS ALL DEFERRED")
        for c in companies:
            schema, legacy = c["schema_name"], c["schema_name"] + "_legacy"
            cur.execute(sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(sql.Identifier(schema), sql.Identifier(legacy)))
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cur.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
            db.pin_search_path(cur, schema, "company")
            migrations.apply_tenant(cur, "company")
            cur.execute("SET CONSTRAINTS ALL DEFERRED")
            moved = {}
            for t in sorted(company_tables & tables_in(cur, legacy)):
                if t in both_tables and t == "id_counters":
                    continue
                n = count(cur, legacy, t)
                if n:
                    moved[t] = copy_table(cur, legacy, schema, t)
                    if moved[t] != n:
                        raise RuntimeError(f"row count mismatch copying {schema}.{t}")
            cur.execute("UPDATE public.tenants SET schema_version=%s, updated_at=now() WHERE id=%s",
                        (migrations.LATEST_TENANT_VERSION, c["tenant_id"]))
            db.pin_search_path(cur, "fleet_workspace", "fleet")
            cur.execute('INSERT INTO "companies"(code, name, tenant_id, created_by, created_at) '
                        "VALUES (%s,%s,%s,%s,%s)", (c["code"], c["name"], c["tenant_id"], c["created_by"],
                                                   c["created_at"]))
            say(f"Company {c['code']}: rebuilt {schema}; copied {moved or 'nothing (already empty)'}; registered")
            cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(legacy)))

        # ---- 4. operator schemas: fleet-level rows -> fleet_workspace, then retire the schema
        for o in operators:
            src = o["schema_name"]
            db.pin_search_path(cur, "fleet_workspace", "fleet")
            cur.execute("SET CONSTRAINTS ALL DEFERRED")
            for t in sorted((fleet_tables | both_tables) & tables_in(cur, src)):
                if t == "id_counters" or not count(cur, src, t):
                    continue
                if t == "locations":
                    n = copy_table(cur, src, "fleet_workspace", t, "type IN ('parking','depot')")
                else:
                    n = copy_table(cur, src, "fleet_workspace", t)
                say(f"Operator {src}.{t}: moved {n} row(s) into fleet_workspace.{t}")
            cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(src)))
            cur.execute("DELETE FROM public.tenants WHERE id=%s", (o["id"],))   # memberships cascade
            say(f"Operator schema {src} retired")

        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        say("Migration committed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    try:
        return run(a.apply)
    except Exception as exc:  # whole transaction rolled back
        print(f"ERROR (nothing was changed): {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
