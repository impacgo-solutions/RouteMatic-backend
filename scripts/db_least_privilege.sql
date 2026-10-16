-- =============================================================================
-- Routematic: least-privilege database roles (FORWARD).  PREPARED, NOT APPLIED.
-- =============================================================================
-- What it does (one transaction, all-or-nothing):
--   1. Creates :owner_role (NOLOGIN) and makes it the owner of every object the
--      runtime login owns today (all app schemas, tables, sequences, views,
--      functions, triggers) via REASSIGN OWNED. No data is touched.
--   2. :runtime_role (the API's login, e.g. routematic_user) keeps only:
--        CONNECT/TEMP on the database, USAGE on the app schemas,
--        SELECT/INSERT/UPDATE/DELETE on tables, USAGE/SELECT/UPDATE on sequences,
--        EXECUTE on functions.  It can no longer DROP/ALTER/CREATE schema objects.
--   3. ALTER DEFAULT PRIVILEGES: everything :owner_role creates later (new company
--      schemas and their tables) automatically gets the same runtime grants.
--   4. REVOKE CREATE ON SCHEMA public FROM PUBLIC; REVOKE ALL ON DATABASE FROM PUBLIC.
--   5. :runtime_role becomes a NOINHERIT member of :owner_role: it does NOT get the
--      owner's rights by default, but may `SET LOCAL ROLE <owner>` inside a
--      transaction. Company provisioning (CREATE SCHEMA + tables at runtime) needs
--      this - see "Required app change" below.
--
-- PREREQUISITES
--   * Run as a superuser / admin role that can CREATE ROLE and REASSIGN OWNED
--     (routematic_user cannot: it is not superuser and has no CREATEROLE).
--   * Take a fresh backup first:  pg_dump -Fc -f before_least_privilege.dump <db>
--   * Record the current state (for the rollback):
--       SELECT has_database_privilege('<runtime>', current_database(), 'CREATE');
--       SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace ORDER BY 1;
--   * Deploy the app change below BEFORE (or together with) this script, otherwise
--     registering a NEW company fails with "permission denied for database".
--     Existing companies keep working either way (sign-in, pull, push, billing ...).
--
-- RUN ORDER
--   1. pg_dump backup                               (above)
--   2. deploy the app with DB_OWNER_ROLE=routematic_owner   (app change below)
--   3. psql "host=... dbname=<db> user=<admin>" -v ON_ERROR_STOP=1 \
--          -v runtime_role=routematic_user -v owner_role=routematic_owner \
--          -f backend/scripts/db_least_privilege.sql
--   4. restart the API; smoke test: sign in, open a company, save a change,
--      register a test company.
--   Rollback: backend/scripts/db_least_privilege_rollback.sql (same -v options).
--
-- REQUIRED APP CHANGE (not applied - backend/app is outside this change set):
--   backend/app/db.py: new setting DB_OWNER_ROLE (empty = current behaviour) and
--       def as_schema_owner(cur):
--           role = get_settings().db_owner_role
--           if role: cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
--   call it as the first statement inside the transaction of
--     tenants.provision_tenant(), tenants.provision_fleet(), tenants.upgrade_all_tenants()
--   (SET LOCAL ends with the transaction, so ordinary requests never run as owner).
--   Admin scripts (init_db, seed_demo, create_* ) should connect as the admin role
--   or also use as_schema_owner.
--
-- TRADE-OFF: the runtime login can still become the owner with SET ROLE, so this
-- removes accidental / default schema-destruction rights and limits what most
-- injected SQL could do, but it is not a hard boundary against a fully
-- compromised API. The stronger variant is a SEPARATE migration login
-- (DB_MIGRATION_USER) used only by provisioning, with routematic_user getting no
-- membership at all; that needs a second connection pool in the app.
-- =============================================================================
\set ON_ERROR_STOP on
\if :{?runtime_role}
\else
  \set runtime_role routematic_user
\endif
\if :{?owner_role}
\else
  \set owner_role routematic_owner
\endif

BEGIN;
SELECT set_config('lp.runtime', :'runtime_role', true),
       set_config('lp.owner', :'owner_role', true);

DO $lp$
DECLARE
  rt  text := current_setting('lp.runtime');
  own text := current_setting('lp.owner');
  db  text := current_database();
  s   text;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = rt) THEN
    RAISE EXCEPTION 'runtime role % does not exist', rt;
  END IF;
  IF (SELECT rolsuper FROM pg_roles WHERE rolname = rt) THEN
    RAISE EXCEPTION 'runtime role % is a superuser; least privilege cannot apply', rt;
  END IF;
  IF rt = own THEN
    RAISE EXCEPTION 'owner and runtime role must differ';
  END IF;

  -- 1. owner role
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = own) THEN
    EXECUTE format('CREATE ROLE %I NOLOGIN NOINHERIT', own);
  END IF;
  EXECUTE format('GRANT CONNECT, CREATE, TEMPORARY ON DATABASE %I TO %I', db, own);

  -- remember which schemas the runtime owns now: the app schemas
  CREATE TEMP TABLE lp_app_schemas ON COMMIT DROP AS
    SELECT nspname::text AS nspname FROM pg_namespace
     WHERE pg_get_userbyid(nspowner) = rt
    UNION SELECT 'public';

  -- 2. move ownership of everything the runtime owns in this database
  EXECUTE format('REASSIGN OWNED BY %I TO %I', rt, own);

  -- 3. runtime grants on existing objects
  FOR s IN SELECT nspname FROM lp_app_schemas ORDER BY 1 LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', s, rt);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO %I', s, rt);
    EXECUTE format('GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA %I TO %I', s, rt);
    EXECUTE format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO %I', s, rt);
  END LOOP;

  -- 4. future objects created by the owner (new company schemas + their tables)
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I GRANT USAGE ON SCHEMAS TO %I', own, rt);
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', own, rt);
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %I', own, rt);
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I GRANT EXECUTE ON FUNCTIONS TO %I', own, rt);

  -- 5. no implicit rights for everyone
  EXECUTE 'REVOKE CREATE ON SCHEMA public FROM PUBLIC';
  -- the owner keeps the shared registry tables in public up to date (migrations)
  EXECUTE format('GRANT USAGE, CREATE ON SCHEMA public TO %I', own);
  EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', db);
  EXECUTE format('REVOKE CREATE ON DATABASE %I FROM %I', db, rt);
  EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', db, rt);

  -- 6. provisioning path: SET LOCAL ROLE <owner> allowed, owner rights NOT inherited
  EXECUTE format('ALTER ROLE %I NOINHERIT', rt);
  EXECUTE format('GRANT %I TO %I', own, rt);

  RAISE NOTICE 'least privilege applied: % owns the app objects, % is DML-only', own, rt;
END
$lp$;

-- verification (printed): the runtime must own nothing and must not be able to create in public
SELECT :'runtime_role' AS runtime_role,
       (SELECT count(*) FROM pg_namespace WHERE pg_get_userbyid(nspowner) = :'runtime_role') AS schemas_owned_by_runtime,
       has_schema_privilege(:'runtime_role', 'public', 'CREATE') AS runtime_can_create_in_public,
       has_database_privilege(:'runtime_role', current_database(), 'CREATE') AS runtime_can_create_schema,
       has_schema_privilege('public', 'public', 'CREATE') AS everyone_can_create_in_public;
COMMIT;
