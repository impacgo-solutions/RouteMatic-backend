"""Database initialisation, fleet + company schemas, idempotency and safety guards."""
import pytest

from app import db, ddl, migrations, tenants
from app.config import PROTECTED_DATABASES
from app.spec import TABLE_KINDS


def tables_of(cur, schema):
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s "
                "AND table_type='BASE TABLE'", (schema,))
    return {r["table_name"] for r in cur.fetchall()}


def test_protected_and_wrong_databases_are_refused():
    for name in PROTECTED_DATABASES | {"impaco_test_dev", "IMPACO_TEST_DEV", "postgres", "template1"}:
        with pytest.raises(db.DatabaseSafetyError):
            db.validate_db_name(name)
    db.validate_db_name("Routematic")


def test_runtime_check_uses_live_current_database(test_database):
    with db.transaction() as cur:
        assert db.assert_safe_target(cur) == test_database


def test_fleet_schema_holds_fleet_level_tables_only():
    with db.transaction(dict_rows=True) as cur:
        names = tables_of(cur, "fleet_workspace")
        fleet_tables = {t for t, ks in TABLE_KINDS.items() if "fleet" in ks}
        assert fleet_tables <= names
        assert {"companies", "fleet_users", "vehicles", "drivers", "locations", "contracts",
                "fleet_allocations", "fleet_policy", "geofences", "shifts"} <= names
        # company-level data does NOT live in the fleet schema
        assert not ({"employees", "employers", "trips", "bookings", "invoices", "billing_records",
                     "routes"} & names)
        cur.execute("SELECT count(*) n FROM pg_views WHERE schemaname='fleet_workspace' "
                    "AND viewname='company_tenants'")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT status, schema_version, kind FROM public.tenants WHERE slug='fleet'")
        t = cur.fetchone()
        assert (t["status"], t["kind"]) == ("ready", "fleet")
        assert t["schema_version"] == migrations.LATEST_FLEET_VERSION


def test_fleet_provisioning_is_idempotent():
    r = tenants.provision_fleet()
    assert r["created"] is False and r["schemaName"] == "fleet_workspace"
    with db.transaction() as cur:
        assert migrations.apply_shared(cur) == []
    with db.transaction(dict_rows=True) as cur:
        db.pin_search_path(cur, "fleet_workspace", "fleet")
        for stmt in ddl.tenant_statements("fleet") + ddl.FLEET_ONLY_STATEMENTS:
            cur.execute(stmt)
    assert tenants.upgrade_all_tenants()["fleet"] == []


def test_company_schema_has_company_tables_and_cross_schema_fks():
    tenants.provision_tenant("Gamma", "gamma_co")
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT schema_name, status, schema_version, kind FROM public.tenants "
                    "WHERE slug='gamma_co'")
        t = cur.fetchone()
        assert (t["status"], t["kind"]) == ("ready", "company")
        assert t["schema_version"] == migrations.LATEST_TENANT_VERSION
        names = tables_of(cur, t["schema_name"])
        company_tables = {t_ for t_, ks in TABLE_KINDS.items() if "company" in ks}
        assert company_tables <= names
        assert {"employers", "employees", "trips", "bookings", "invoices"} <= names
        # fleet-level tables are NOT duplicated into the company schema
        assert not ({"vehicles", "drivers", "contracts", "fleet_allocations", "geofences", "shifts",
                     "fleet_policy", "companies", "fleet_users"} & names)
        # ...they are referenced across schemas instead
        cur.execute(
            "SELECT conname, confrelid::regclass::text AS ref FROM pg_constraint k "
            "JOIN pg_namespace n ON n.oid = k.connamespace WHERE n.nspname=%s AND k.contype='f' "
            "AND confrelid::regclass::text LIKE 'fleet_workspace.%%'", (t["schema_name"],))
        refs = {(r["conname"], r["ref"]) for r in cur.fetchall()}
        assert ("fk_trips_assigned_vehicle_id", "fleet_workspace.vehicles") in refs
        assert ("fk_trips_assigned_driver_id", "fleet_workspace.drivers") in refs
        cur.execute("SELECT count(*) n FROM pg_constraint k JOIN pg_namespace n ON n.oid=k.connamespace "
                    "WHERE n.nspname=%s AND k.contype='f'", (t["schema_name"],))
        assert cur.fetchone()["n"] > 40
        cur.execute("SELECT count(*) n FROM pg_indexes WHERE schemaname=%s", (t["schema_name"],))
        assert cur.fetchone()["n"] > 50
        # a new schema is COMPLETELY empty
        for name in sorted(names - {"schema_migrations"}):
            cur.execute(f'SELECT count(*) n FROM "{t["schema_name"]}"."{name}"')
            assert cur.fetchone()["n"] == 0, name


def test_reserved_identifiers_are_refused():
    for slug in ("fleet", "fleet_workspace"):
        with pytest.raises(tenants.TenantError):
            tenants.provision_tenant("Reserved", slug)


def test_rerun_is_idempotent_and_non_destructive():
    tenants.provision_tenant("Delta", "delta_co")
    with db.tenant_cursor("delta_co") as cur:
        cur.execute("INSERT INTO employers(id, code, name) VALUES ('C9','C9','Keep Me')")
        cur.execute("SELECT count(*) n FROM pg_indexes WHERE schemaname=current_schema()")
        idx_before = cur.fetchone()["n"]
    for _ in range(2):
        with db.transaction() as cur:
            assert migrations.apply_shared(cur) == []
        assert tenants.upgrade_all_tenants()["delta_co"] == []
    with db.tenant_cursor("delta_co") as cur:
        cur.execute("SELECT name FROM employers WHERE id='C9'")
        assert cur.fetchone()["name"] == "Keep Me"
        cur.execute("SELECT count(*) n FROM pg_indexes WHERE schemaname=current_schema()")
        assert cur.fetchone()["n"] == idx_before
        for stmt in ddl.tenant_statements("company"):     # raw DDL re-run is harmless
            cur.execute(stmt)
        cur.execute("SELECT count(*) n FROM employers")
        assert cur.fetchone()["n"] == 1


def test_duplicate_and_invalid_tenants_rejected():
    tenants.provision_tenant("Epsilon", "epsilon_co")
    with pytest.raises(tenants.TenantExists):
        tenants.provision_tenant("Epsilon again", "epsilon_co")
    for bad in ("1abc", "ab", "has-dash", "x" * 41, "public;drop", 'a"b', "public", "platform"):
        with pytest.raises(tenants.TenantError):
            tenants.provision_tenant("Bad", bad)


def test_failed_provisioning_leaves_nothing_behind(monkeypatch):
    def boom(cur, kind="company"):
        cur.execute("CREATE TABLE half_done (id int)")
        raise RuntimeError("simulated failure")
    monkeypatch.setattr(migrations, "apply_tenant", boom)
    with pytest.raises(tenants.ProvisioningError):
        tenants.provision_tenant("Broken", "broken_co")
    monkeypatch.undo()
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT 1 FROM public.tenants WHERE slug='broken_co'")
        assert cur.fetchone() is None
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname='tenant_broken_co'")
        assert cur.fetchone() is None
        cur.execute("SELECT outcome FROM public.tenant_provision_log WHERE slug='broken_co'")
        assert cur.fetchone()["outcome"] == "failed"
    tenants.provision_tenant("Broken", "broken_co")          # can be retried cleanly


def test_migration_error_names_statement():
    m = migrations.Migration("9999", "bad", lambda: ["SELECT 1", "CREATE TABLE nope (;"])
    with db.transaction() as cur:
        with pytest.raises(migrations.MigrationError) as ei:
            migrations._run(cur, m)
    assert "9999" in str(ei.value) and "CREATE TABLE nope" in str(ei.value)


def test_tenant_not_visible_until_ready(test_database):
    with db.transaction() as cur:
        cur.execute("INSERT INTO public.tenants(slug,name,schema_name,status) "
                    "VALUES ('pending_co','P','tenant_pending_co','provisioning')")
    with pytest.raises(db.TenantNotFound):
        with db.tenant_cursor("pending_co"):
            pass
    with pytest.raises(db.TenantNotFound):
        with db.tenant_cursor("tenant_pending_co"):   # schema names are not accepted as slugs
            pass


def test_search_path_is_pinned_per_scope_and_does_not_leak():
    with db.tenant_cursor("fleet") as cur:
        cur.execute("SHOW search_path")
        assert cur.fetchone()["search_path"] == "fleet_workspace, public"
    with db.tenant_cursor("gamma_co") as cur:
        cur.execute("SHOW search_path")
        assert cur.fetchone()["search_path"] == "tenant_gamma_co, fleet_workspace, public"
    with db.transaction() as cur:       # pooled connections carry no scope
        cur.execute("SHOW search_path")
        sp = cur.fetchone()[0]
        assert "tenant_" not in sp and "fleet_workspace" not in sp


def test_only_one_fleet_schema_allowed(test_database):
    import psycopg2
    with pytest.raises(psycopg2.errors.UniqueViolation):
        with db.transaction() as cur:
            cur.execute("INSERT INTO public.tenants(slug,name,schema_name,kind,status) "
                        "VALUES ('fleet2','F2','fleet_workspace','fleet','ready')")
