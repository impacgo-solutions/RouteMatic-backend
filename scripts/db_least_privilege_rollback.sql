-- =============================================================================
-- Routematic: least-privilege database roles (ROLLBACK of db_least_privilege.sql)
-- =============================================================================
-- Restores the pre-change state: :runtime_role owns all app objects again, may
-- create schemas, PUBLIC regains CREATE on schema public and CONNECT/TEMP on the
-- database (PostgreSQL 14 defaults), and :owner_role is removed.
-- The explicit DML grants given to :runtime_role stay (harmless: it is the owner).
--
-- Run as the same admin role, same database, same -v options:
--   psql "host=... dbname=<db> user=<admin>" -v ON_ERROR_STOP=1 \
--        -v runtime_role=routematic_user -v owner_role=routematic_owner \
--        -f backend/scripts/db_least_privilege_rollback.sql
-- Then remove DB_OWNER_ROLE from the app configuration (or leave it empty).
-- Note: if the owner role exists in OTHER databases of the cluster, DROP ROLE
-- fails until it is cleaned there too; the transaction then rolls back cleanly.
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
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = own) THEN
    RAISE NOTICE 'owner role % does not exist: nothing to roll back', own;
    RETURN;
  END IF;
  EXECUTE format('REVOKE %I FROM %I', own, rt);
  EXECUTE format('ALTER ROLE %I INHERIT', rt);
  EXECUTE format('REASSIGN OWNED BY %I TO %I', own, rt);
  -- remaining privileges / default-privilege entries of the owner (it owns nothing now)
  EXECUTE format('DROP OWNED BY %I', own);
  EXECUTE format('GRANT CREATE ON DATABASE %I TO %I', db, rt);
  EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO PUBLIC', db);
  EXECUTE 'GRANT CREATE ON SCHEMA public TO PUBLIC';
  EXECUTE format('DROP ROLE %I', own);
  RAISE NOTICE 'rolled back: % owns the app objects again; % dropped', rt, own;
END
$lp$;

SELECT :'runtime_role' AS runtime_role,
       (SELECT count(*) FROM pg_namespace WHERE pg_get_userbyid(nspowner) = :'runtime_role') AS schemas_owned_by_runtime,
       has_database_privilege(:'runtime_role', current_database(), 'CREATE') AS runtime_can_create_schema,
       has_schema_privilege('public', 'public', 'CREATE') AS everyone_can_create_in_public;
COMMIT;
