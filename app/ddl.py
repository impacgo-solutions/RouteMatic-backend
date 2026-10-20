"""Generate idempotent DDL (shared + tenant) from :mod:`app.spec`."""
from __future__ import annotations

from .spec import ENTITIES, ENUMS, Col, Derived, Entity, Ids, Kv, Objs, entities_for, fk_target

PG_TYPES = {"text": "TEXT", "int": "INTEGER", "num": "NUMERIC", "bool": "BOOLEAN",
            "ts": "TIMESTAMPTZ", "jsonb": "JSONB"}
AUTO_TS = {"createdAt", "updatedAt", "timestamp"}


def q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _check_clauses(table: str, c: Col) -> list[str]:
    out = []
    if c.enum:
        vals = ", ".join("'" + v + "'" for v in ENUMS[c.enum])
        out.append(f"CONSTRAINT {q(f'ck_{table}_{c.name}_enum')} CHECK ({q(c.name)} IN ({vals}))")
    if c.kind in ("int", "num"):
        if c.min is not None:
            out.append(f"CONSTRAINT {q(f'ck_{table}_{c.name}_min')} CHECK ({q(c.name)} >= {c.min:g})")
        if c.max is not None:
            out.append(f"CONSTRAINT {q(f'ck_{table}_{c.name}_max')} CHECK ({q(c.name)} <= {c.max:g})")
    return out


def _default_sql(c: Col) -> str | None:
    from .spec import _UNSET
    v = c.default
    if v is _UNSET or v is None:
        return None
    if c.kind == "jsonb":
        import json
        return "'" + json.dumps(v) + "'::jsonb"
    if c.kind == "bool":
        return "TRUE" if v else "FALSE"
    if c.kind in ("int", "num"):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _coldef(c: Col) -> str:
    d = f"{q(c.name)} {PG_TYPES[c.kind]}"
    if c.key in AUTO_TS and c.kind == "ts":
        return d + " NOT NULL DEFAULT now()"
    if not c.null:
        d += " NOT NULL"
    dv = _default_sql(c)
    if dv is not None:
        d += f" DEFAULT {dv}"
    if c.unique:
        d += " UNIQUE"
    return d


def entity_table_sql(e: Entity) -> str:
    parts = ['"id" TEXT PRIMARY KEY']
    checks: list[str] = []
    for c in e.cols:
        parts.append(_coldef(c))
        checks += _check_clauses(e.table, c)
    parts += ['"created_by" TEXT', '"updated_by" TEXT']
    parts += checks
    for i, raw in enumerate(e.checks):
        parts.append(f"CONSTRAINT {q(f'ck_{e.table}_rule{i + 1}')} CHECK {raw}")
    return f"CREATE TABLE IF NOT EXISTS {q(e.table)} (\n  " + ",\n  ".join(parts) + "\n)"


def _child_tables(kind: str = "company") -> dict[str, dict]:
    """Collect child tables of one schema kind (several Ids specs may share one table)."""
    tables: dict[str, dict] = {}
    for e in entities_for(kind):
        for l in e.lists:
            if isinstance(l, Derived):
                continue
            t = tables.setdefault(l.table, {"parent": e.table, "spec": l, "discr": {}})
            if t["parent"] != e.table:
                raise ValueError(f"child table {l.table} shared across entities")
            if isinstance(l, Ids) and l.discr:
                t["discr"].setdefault(l.discr[0], set()).add(l.discr[1])
    return tables


def child_table_sql(name: str, info: dict) -> str:
    l = info["spec"]
    owner = l.owner
    parts = [f"{q(owner)} TEXT NOT NULL"]
    checks: list[str] = []
    if isinstance(l, Ids):
        parts.append(f"{q(l.col)} TEXT NOT NULL")
        pk = [owner, l.col]
        for dcol, vals in info["discr"].items():
            parts.append(f"{q(dcol)} TEXT NOT NULL")
            vs = ", ".join("'" + v + "'" for v in sorted(vals))
            checks.append(f"CONSTRAINT {q(f'ck_{name}_{dcol}')} CHECK ({q(dcol)} IN ({vs}))")
            pk.append(dcol)
        parts.append('"position" INTEGER NOT NULL DEFAULT 0')
        if l.enum:
            vs = ", ".join("'" + v + "'" for v in ENUMS[l.enum])
            checks.append(f"CONSTRAINT {q(f'ck_{name}_{l.col}')} CHECK ({q(l.col)} IN ({vs}))")
    elif isinstance(l, Objs):
        parts.append('"position" INTEGER NOT NULL DEFAULT 0')
        for c in l.cols:
            parts.append(_coldef(c))
            checks += _check_clauses(name, c)
        pk = [owner, "position"]
    elif isinstance(l, Kv):
        parts.append(f"{q(l.kcol)} TEXT NOT NULL")
        parts.append(f"{q(l.vcol)} {PG_TYPES[l.vkind]} NOT NULL")
        pk = [owner, l.kcol]
    else:  # pragma: no cover
        raise TypeError(l)
    parts.append("PRIMARY KEY (" + ", ".join(q(p) for p in pk) + ")")
    parts += checks
    return f"CREATE TABLE IF NOT EXISTS {q(name)} (\n  " + ",\n  ".join(parts) + "\n)"


def _fk_sql(table: str, col: str, target: str, on_delete: str, name: str | None = None) -> str:
    """``target`` is an already-quoted (optionally schema-qualified) table reference."""
    cname = name or f"fk_{table}_{col}"
    return (
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = "
        f"'{cname}' AND conrelid = to_regclass({_lit(q(table))})) THEN "
        f"ALTER TABLE {q(table)} ADD CONSTRAINT {q(cname)} FOREIGN KEY ({q(col)}) "
        f"REFERENCES {target} (\"id\") ON DELETE {on_delete} DEFERRABLE INITIALLY DEFERRED; "
        "END IF; END $$"
    )


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def tenant_statements(kind: str = "company") -> list[str]:
    """DDL for one schema of ``kind`` ('company' = tenant_<slug>, 'fleet' = fleet_workspace)."""
    ents = entities_for(kind)
    stmts: list[str] = []
    for e in ents:
        stmts.append(entity_table_sql(e))
    children = _child_tables(kind)
    for name, info in children.items():
        stmts.append(child_table_sql(name, info))

    stmts.append(
        'CREATE TABLE IF NOT EXISTS "id_counters" ("prefix" TEXT PRIMARY KEY, '
        '"last_value" BIGINT NOT NULL DEFAULT 0)'
    )
    if kind == "company":
        stmts.append(
            'CREATE TABLE IF NOT EXISTS "trip_status_history" ('
            '"id" BIGSERIAL PRIMARY KEY, "trip_id" TEXT NOT NULL, "from_status" TEXT, '
            '"to_status" TEXT NOT NULL, "reason" TEXT, "changed_by" TEXT, '
            '"changed_at" TIMESTAMPTZ NOT NULL DEFAULT now())'
        )

    # Foreign keys (added after all tables exist: the model has cycles). References into
    # the other schema kind become cross-schema FKs (company -> fleet) or soft refs (fleet -> company).
    def fk(table: str, col: Col, nullable: bool) -> None:
        target = fk_target(kind, col.fk, col.soft)
        if target:
            # Cross-schema references (company -> fleet) must never be rewritten by a fleet-level
            # delete: the delete is refused instead (deactivate the vehicle/driver).
            cross = '"fleet_workspace".' in target
            stmts.append(_fk_sql(table, col.name, target,
                                 "SET NULL" if nullable and not cross else "NO ACTION"))

    for e in ents:
        for c in e.cols:
            if c.fk:
                fk(e.table, c, c.null)
    for name, info in children.items():
        l = info["spec"]
        stmts.append(_fk_sql(name, l.owner, f'"{info["parent"]}"', "CASCADE"))
        if isinstance(l, Ids) and l.fk:
            target = fk_target(kind, l.fk)
            if target:
                stmts.append(_fk_sql(name, l.col, target, "NO ACTION"))
        if isinstance(l, Objs):
            for c in l.cols:
                if c.fk:
                    fk(name, c, c.null)
    if kind == "company":
        stmts.append(_fk_sql("trip_status_history", "trip_id", '"trips"', "CASCADE"))

    # Indexes.
    for e in ents:
        cols = {c.name for c in e.cols if c.fk} | set(e.indexes)
        for col in sorted(cols):
            stmts.append(
                f"CREATE INDEX IF NOT EXISTS {q(f'idx_{e.table}_{col}')} ON {q(e.table)} ({q(col)})"
            )
        for col in e.unique_nonempty:
            stmts.append(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {q(f'uq_{e.table}_{col}')} "
                f"ON {q(e.table)} (lower({q(col)})) WHERE {q(col)} <> ''"
            )
    for name, info in children.items():
        l = info["spec"]
        if isinstance(l, Ids) and l.fk:
            stmts.append(
                f"CREATE INDEX IF NOT EXISTS {q(f'idx_{name}_{l.col}')} ON {q(name)} ({q(l.col)})"
            )
        if isinstance(l, Objs):
            for c in l.cols:
                if c.fk:
                    stmts.append(
                        f"CREATE INDEX IF NOT EXISTS {q(f'idx_{name}_{c.name}')} ON {q(name)} ({q(c.name)})"
                    )
    if kind == "company":
        stmts.append(
            'CREATE INDEX IF NOT EXISTS "idx_trip_status_history_trip" ON "trip_status_history" ("trip_id", "changed_at")'
        )
    return stmts


# Fleet-only structures: the company registry (central mapping to every tenant schema)
# and the fleet admin users. Reference public.* (identity / tenant registry) by FK.
FLEET_ONLY_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS "companies" (
        "id" UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        "code" TEXT NOT NULL UNIQUE,
        "name" TEXT NOT NULL,
        "tenant_id" UUID NOT NULL UNIQUE REFERENCES public.tenants(id) ON DELETE RESTRICT,
        "status" TEXT NOT NULL DEFAULT 'active' CHECK ("status" IN ('active','inactive')),
        "created_by" UUID REFERENCES public.platform_users(id) ON DELETE SET NULL,
        "created_at" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updated_at" TIMESTAMPTZ NOT NULL DEFAULT now())""",
    # slug + schema come from the tenant registry (no duplicated columns)
    """CREATE OR REPLACE VIEW "company_tenants" AS
        SELECT c.id AS company_id, c.code, c.name, c.status, t.id AS tenant_id,
               t.slug AS tenant_slug, t.schema_name, t.status AS tenant_status, t.schema_version
        FROM "companies" c JOIN public.tenants t ON t.id = c.tenant_id""",
    """CREATE TABLE IF NOT EXISTS "fleet_users" (
        "user_id" UUID PRIMARY KEY REFERENCES public.platform_users(id) ON DELETE CASCADE,
        "role" TEXT NOT NULL DEFAULT 'fleetManager' CHECK ("role" IN ('fleetManager','driver')),
        "active" BOOLEAN NOT NULL DEFAULT TRUE,
        "created_at" TIMESTAMPTZ NOT NULL DEFAULT now())""",
]


# Per-tenant migration bookkeeping lives in the tenant schema itself.
TENANT_BOOKKEEPING = (
    'CREATE TABLE IF NOT EXISTS "schema_migrations" ("version" TEXT PRIMARY KEY, "name" TEXT NOT NULL, '
    '"checksum" TEXT NOT NULL, "applied_at" TIMESTAMPTZ NOT NULL DEFAULT now(), "duration_ms" INTEGER)'
)

# Shared migration 0002: which tenant (company) was created by which operator tenant/user.
SHARED_STATEMENTS_0002 = [
    """CREATE TABLE IF NOT EXISTS public.companies (
        tenant_id UUID PRIMARY KEY REFERENCES public.tenants(id) ON DELETE CASCADE,
        code TEXT NOT NULL, name TEXT NOT NULL,
        parent_tenant_id UUID REFERENCES public.tenants(id) ON DELETE SET NULL,
        created_by UUID REFERENCES public.platform_users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_parent_code "
    "ON public.companies (parent_tenant_id, lower(code))",
    "CREATE INDEX IF NOT EXISTS idx_companies_parent ON public.companies (parent_tenant_id)",
]

SHARED_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS public.schema_migrations (
        scope TEXT NOT NULL, version TEXT NOT NULL, name TEXT NOT NULL, checksum TEXT NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), duration_ms INTEGER,
        PRIMARY KEY (scope, version))""",
    """CREATE TABLE IF NOT EXISTS public.global_roles (
        code TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
        tenant_scoped BOOLEAN NOT NULL DEFAULT TRUE)""",
    """CREATE TABLE IF NOT EXISTS public.tenants (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        slug TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9_]{2,39}$'),
        name TEXT NOT NULL,
        schema_name TEXT NOT NULL UNIQUE CHECK (schema_name ~ '^tenant_[a-z][a-z0-9_]{2,39}$'),
        status TEXT NOT NULL DEFAULT 'provisioning'
            CHECK (status IN ('provisioning','ready','failed','suspended')),
        schema_version TEXT,
        settings JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        ready_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS public.platform_users (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        email TEXT NOT NULL, password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
        is_platform_admin BOOLEAN NOT NULL DEFAULT FALSE, active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_users_email ON public.platform_users (lower(email))",
    """CREATE TABLE IF NOT EXISTS public.tenant_memberships (
        user_id UUID NOT NULL REFERENCES public.platform_users(id) ON DELETE CASCADE,
        tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
        role TEXT NOT NULL REFERENCES public.global_roles(code),
        scope_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, tenant_id))""",
    "CREATE INDEX IF NOT EXISTS idx_tenant_memberships_tenant ON public.tenant_memberships (tenant_id)",
    """CREATE TABLE IF NOT EXISTS public.tenant_provision_log (
        id BIGSERIAL PRIMARY KEY, slug TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT,
        logged_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """INSERT INTO public.global_roles (code, name, description, tenant_scoped) VALUES
        ('employee','Employee','Passenger: books and tracks own trips',TRUE),
        ('driver','Driver','Runs trips, shifts, inspections, safety checks',TRUE),
        ('employerAdmin','Employer Admin','Administers one employer (company)',TRUE),
        ('fleetManager','Fleet Admin','Operates the shared fleet pool for the tenant',TRUE),
        ('system','System','Automated / platform actions',FALSE)
        ON CONFLICT (code) DO NOTHING""",
]


# Shared migration 0003: tenants get a kind ('fleet' | 'company'); the company registry moves
# into the fleet_manager schema (it must be empty here: nothing is copied or lost).
SHARED_STATEMENTS_0003 = [
    "ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'company'",
    """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tenants_kind') THEN
            ALTER TABLE public.tenants ADD CONSTRAINT ck_tenants_kind CHECK (kind IN ('fleet','company'));
        END IF; END $$""",
    "ALTER TABLE public.tenants DROP CONSTRAINT IF EXISTS tenants_schema_name_check",
    """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tenants_schema_name') THEN
            ALTER TABLE public.tenants ADD CONSTRAINT ck_tenants_schema_name CHECK (
                (kind = 'company' AND schema_name ~ '^tenant_[a-z][a-z0-9_]{2,39}$') OR
                (kind = 'fleet' AND schema_name = 'fleet_manager')); END IF; END $$""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_tenants_single_fleet ON public.tenants (kind) WHERE kind = 'fleet'",
    """DO $$ BEGIN
        IF to_regclass('public.companies') IS NOT NULL THEN
            IF (SELECT count(*) FROM public.companies) > 0 THEN
                RAISE EXCEPTION 'public.companies holds rows; migrate them into fleet_manager.companies first';
            END IF;
            DROP TABLE public.companies;
        END IF; END $$""",
]


# Shared migration 0004: manager role, per-membership active flag (deactivate one workspace login
# without touching the person's other logins), Fleet Admin naming.
SHARED_STATEMENTS_0004 = [
    "ALTER TABLE public.tenant_memberships ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE",
    """INSERT INTO public.global_roles (code, name, description, tenant_scoped) VALUES
        ('manager','Manager','Company manager: creates and manages the drivers of the assigned company',TRUE)
        ON CONFLICT (code) DO NOTHING""",
    "UPDATE public.global_roles SET name='Fleet Admin', "
    "description='Administers the fleet: companies, managers, drivers, vehicles' WHERE code='fleetManager'",
    "UPDATE public.tenants SET name='Fleet Admin' WHERE kind='fleet' AND name='Fleet Manager'",
]

# Shared migration 0005: managers are fleet staff (no company) and sign in as "Fleet Manager".
SHARED_STATEMENTS_0005 = [
    "UPDATE public.global_roles SET name='Fleet Manager', "
    "description='Fleet staff: runs fleet operations and manages drivers; no Fleet Admin privileges' "
    "WHERE code='manager'",
]

# Shared migration 0006: the fleet schema is called fleet_workspace (was fleet_manager).
# ALTER SCHEMA ... RENAME keeps every table, row, index, constraint, sequence, view and grant
# (objects are referenced by OID, so cross-schema FKs from company schemas keep working).
# Never merges: if both names exist it stops and changes nothing.
SHARED_STATEMENTS_0006 = [
    "ALTER TABLE public.tenants DROP CONSTRAINT IF EXISTS ck_tenants_schema_name",
    """DO $$ BEGIN
        IF to_regnamespace('fleet_manager') IS NOT NULL THEN
            IF to_regnamespace('fleet_workspace') IS NOT NULL THEN
                RAISE EXCEPTION 'schemas fleet_manager and fleet_workspace both exist; refusing to merge them';
            END IF;
            ALTER SCHEMA fleet_manager RENAME TO fleet_workspace;
        END IF; END $$""",
    "UPDATE public.tenants SET schema_name = 'fleet_workspace', updated_at = now() "
    "WHERE kind = 'fleet' AND schema_name = 'fleet_manager'",
    """ALTER TABLE public.tenants ADD CONSTRAINT ck_tenants_schema_name CHECK (
        (kind = 'company' AND schema_name ~ '^tenant_[a-z][a-z0-9_]{2,39}$') OR
        (kind = 'fleet' AND schema_name = 'fleet_workspace'))""",
]

# Shared migration 0007: credential version. Sign-in tokens carry it; changing a password increments it,
# so every token issued before the change stops working.
SHARED_STATEMENTS_0007 = [
    "ALTER TABLE public.platform_users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0",
]

# Fleet migration 0003: Fleet Admin accounts. Fleet Admins authenticate against this table
# (PBKDF2 hash only; plain text is rejected by the CHECK). user_id links the account to its
# platform identity (tokens, memberships). Existing Fleet Admins are carried over unchanged.
FLEET_STATEMENTS_0003 = [
    """CREATE TABLE IF NOT EXISTS "admins" (
        "id" TEXT PRIMARY KEY DEFAULT ('FA-' || upper(substr(md5(gen_random_uuid()::text), 1, 8))),
        "user_id" UUID NOT NULL UNIQUE REFERENCES public.platform_users(id) ON DELETE CASCADE,
        "name" TEXT NOT NULL,
        "email" TEXT NOT NULL,
        "password_hash" TEXT NOT NULL CHECK ("password_hash" LIKE 'pbkdf2_sha256$%'),
        "role" TEXT NOT NULL DEFAULT 'fleetManager' REFERENCES public.global_roles(code)
            CHECK ("role" = 'fleetManager'),
        "status" TEXT NOT NULL DEFAULT 'active' CHECK ("status" IN ('active','inactive')),
        "last_login_at" TIMESTAMPTZ,
        "created_at" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updated_at" TIMESTAMPTZ NOT NULL DEFAULT now())""",
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_admins_email" ON "admins" (lower("email"))',
    'CREATE INDEX IF NOT EXISTS "idx_admins_status" ON "admins" ("status")',
    """INSERT INTO "admins" (user_id, name, email, password_hash, status, created_at)
        SELECT u.id, u.display_name, u.email, u.password_hash,
               CASE WHEN m.active AND u.active THEN 'active' ELSE 'inactive' END, m.created_at
        FROM public.tenant_memberships m
        JOIN public.tenants t ON t.id = m.tenant_id AND t.kind = 'fleet'
        JOIN public.platform_users u ON u.id = m.user_id
        WHERE m.role = 'fleetManager'
        ON CONFLICT (user_id) DO NOTHING""",
]

# Fleet migration 0004: fleet rows (inspections, alcohol tests, vehicle events) may reference an incident
# recorded in a company schema, so those references are soft. Only the three constraints are dropped;
# no column or row changes.
FLEET_STATEMENTS_0004 = [
    'ALTER TABLE "inspections" DROP CONSTRAINT IF EXISTS "fk_inspections_defect_incident_id"',
    'ALTER TABLE "alcohol_tests" DROP CONSTRAINT IF EXISTS "fk_alcohol_tests_incident_id"',
    'ALTER TABLE "vehicle_events" DROP CONSTRAINT IF EXISTS "fk_vehicle_events_incident_id"',
]

# Company migration 0004: the company record is protected by the database itself.
#  * exactly one company (employers row) per company schema
#  * never deleted, code never changed (it keys the fleet registry)
#  * company locations are pickup / office points; parking and depots are fleet assets
# Additive (index, trigger, check constraint); existing rows already satisfy every rule.
COMPANY_STATEMENTS_0004 = [
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_employers_single_company" ON "employers" ((true))',
    """CREATE OR REPLACE FUNCTION "employers_guard"() RETURNS trigger LANGUAGE plpgsql AS $fn$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Companies are never deleted; deactivate them instead.'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.code IS DISTINCT FROM OLD.code THEN
                RAISE EXCEPTION 'The company code cannot be changed (it identifies the company).'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $fn$""",
    """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_employers_guard'
                       AND tgrelid = '"employers"'::regclass) THEN
            CREATE TRIGGER "trg_employers_guard" BEFORE UPDATE OR DELETE ON "employers"
                FOR EACH ROW EXECUTE FUNCTION "employers_guard"();
        END IF; END $$""",
    """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_company_locations_type'
                       AND conrelid = '"locations"'::regclass) THEN
            ALTER TABLE "locations" ADD CONSTRAINT "ck_company_locations_type"
                CHECK ("type" NOT IN ('parking', 'depot'));
        END IF; END $$""",
]

# Company migration 0003: managers of this company. Name + email + password live ONCE in
# public.platform_users (identity); this table keeps only what is specific to the manager role here.
COMPANY_STATEMENTS_0003 = [
    """CREATE TABLE IF NOT EXISTS "managers" (
        "id" TEXT PRIMARY KEY,
        "user_id" UUID NOT NULL UNIQUE REFERENCES public.platform_users(id) ON DELETE CASCADE,
        "phone" TEXT NOT NULL DEFAULT '',
        "status" TEXT NOT NULL DEFAULT 'active' CHECK ("status" IN ('active','inactive')),
        "created_by" TEXT,
        "created_at" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updated_at" TIMESTAMPTZ NOT NULL DEFAULT now())""",
    'CREATE INDEX IF NOT EXISTS "idx_managers_status" ON "managers" ("status")',
]
