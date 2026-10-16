"""Secure connection handling.

* Every database-changing entry point calls :func:`assert_safe_target`, which
  verifies ``current_database()`` on the *live* connection (not just env vars).
* Tenant data is reached only through :func:`tenant_cursor`, which validates the
  tenant against the shared registry and pins ``search_path`` with
  ``SET LOCAL`` so it is discarded automatically at COMMIT/ROLLBACK (pooled
  connections never leak a tenant context).
"""
from __future__ import annotations

import contextlib
import threading
from typing import Iterator

import psycopg2
import psycopg2.extras
import psycopg2.pool
from psycopg2 import sql

from .config import APP_DATABASES, PROTECTED_DATABASES, get_settings


class DatabaseSafetyError(RuntimeError):
    """Raised when an operation would target the wrong / a protected database."""


class TenantNotFound(LookupError):
    pass


def validate_db_name(name: str) -> None:
    if name.lower() in PROTECTED_DATABASES:
        raise DatabaseSafetyError(f"Refusing to use protected database '{name}'.")
    if name not in APP_DATABASES and not name.startswith(get_settings().allow_test_db_prefix):
        raise DatabaseSafetyError(
            f"Target database must be one of {sorted(APP_DATABASES)} (got '{name}')."
        )


def assert_safe_target(cur) -> str:
    """Runtime check on the live connection: SELECT current_database()."""
    cur.execute("SELECT current_database()")
    row = cur.fetchone()
    actual = row["current_database"] if isinstance(row, dict) else row[0]
    validate_db_name(actual)
    if actual != get_settings().db_name:
        raise DatabaseSafetyError(
            f"Connected to '{actual}' but configured DB_NAME is '{get_settings().db_name}'."
        )
    return actual


def _connect_kwargs(dbname: str) -> dict:
    s = get_settings()
    kw = dict(
        host=s.db_host, port=s.db_port, user=s.db_user, password=s.db_password,
        dbname=dbname, options="-c timezone=UTC", connect_timeout=5,
        # a remote database is only reached over TLS (no silent plaintext fallback)
        sslmode=s.effective_sslmode,
    )
    if s.db_sslrootcert:
        kw["sslrootcert"] = s.db_sslrootcert
    return kw


def admin_connect():
    """Connection to the maintenance DB, used only to create the app database."""
    s = get_settings()
    if s.db_admin_db.lower() in PROTECTED_DATABASES:
        raise DatabaseSafetyError("Maintenance database may not be a protected database.")
    return psycopg2.connect(**_connect_kwargs(s.db_admin_db))


def ensure_database() -> bool:
    """Create the target database if missing (never touches existing ones)."""
    s = get_settings()
    validate_db_name(s.db_name)
    conn = admin_connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (s.db_name,))
            if cur.fetchone():
                return False
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(s.db_name)))
            return True
    finally:
        conn.close()


_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            s = get_settings()
            validate_db_name(s.db_name)
            try:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    1, 10, **_connect_kwargs(s.db_name)
                )
            except psycopg2.OperationalError as exc:
                raise RuntimeError(
                    f"Cannot connect to PostgreSQL {s.db_host}:{s.db_port} "
                    f"db '{s.db_name}' as '{s.db_user}': {exc}. Check backend/.env and run "
                    "`python -m scripts.init_db`."
                ) from exc
        return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


@contextlib.contextmanager
def transaction(dict_rows: bool = False) -> Iterator[psycopg2.extensions.cursor]:
    """One transaction on a pooled connection, verified against the target DB."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor if dict_rows else None)
        try:
            assert_safe_target(cur)
            yield cur
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            cur.close()
    finally:
        # Session state is transaction-scoped (SET LOCAL): nothing to reset.
        pool.putconn(conn)


FLEET_SCHEMA = "fleet_workspace"
FLEET_SLUG = "fleet"


def resolve_scope(cur, slug: str) -> tuple[str, str]:
    """Map a tenant slug to (schema, kind), only if registered and ready.

    kind is 'fleet' (the single fleet_workspace schema) or 'company' (tenant_<slug>).
    """
    cur.execute(
        "SELECT schema_name, kind FROM public.tenants WHERE slug = %s AND status = 'ready'",
        (slug,),
    )
    row = cur.fetchone()
    if not row:
        raise TenantNotFound(f"Unknown or unavailable tenant '{slug}'.")
    if isinstance(row, dict):
        return row["schema_name"], row["kind"]
    return row[0], row[1]


def resolve_tenant_schema(cur, slug: str) -> str:
    return resolve_scope(cur, slug)[0]


def as_schema_owner(cur) -> None:
    """For DDL (provisioning, migrations): act as the schema-owner role for the rest of this
    transaction when DB_OWNER_ROLE is configured (least-privilege layout). No-op otherwise."""
    role = get_settings().db_owner_role.strip()
    if role:
        cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))


def pin_search_path(cur, schema: str, kind: str) -> None:
    """Pin the (transaction-local) search_path for a scope.

    A company scope sees its own tables first, then the fleet_workspace schema (fleet-level
    tables such as vehicles/drivers resolve there), then public. The fleet scope sees only
    fleet_workspace + public: company tables are not reachable without an explicit switch.
    """
    if kind == "fleet":
        cur.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
    else:
        cur.execute(sql.SQL("SET LOCAL search_path TO {}, {}, public").format(
            sql.Identifier(schema), sql.Identifier(FLEET_SCHEMA)))


@contextlib.contextmanager
def tenant_cursor(slug: str, dict_rows: bool = True) -> Iterator[psycopg2.extensions.cursor]:
    """Transaction whose search_path is pinned to the validated tenant schema.

    The client never supplies a schema name, only a slug that is looked up in
    ``public.tenants``. ``public`` stays on the path *after* the tenant schema so
    tenant tables always win name resolution.
    """
    with transaction(dict_rows=dict_rows) as cur:
        schema, kind = resolve_scope(cur, slug)
        pin_search_path(cur, schema, kind)
        yield cur


def health() -> dict:
    """Database check for start-up logging (internal; the public /health endpoint shows no details)."""
    with transaction() as cur:
        db = assert_safe_target(cur)
        cur.execute("SELECT version()")
        row = cur.fetchone()
        return {"database": db, "server": row[0]}
