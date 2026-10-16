# Database architecture (Fleet Admin + one schema per company)

One PostgreSQL database, `Routematic`, three kinds of schema:

```
Routematic
├── public                    identities + the tenant registry (shared, tiny)
│   ├── platform_users        logins (email + password hash)
│   ├── tenant_memberships    user ↔ tenant ↔ role (+ scope_id: company/employee/driver id)
│   ├── tenants               every workspace: slug, schema_name, kind ('fleet'|'company'), status
│   ├── global_roles, schema_migrations, tenant_provision_log
│
├── fleet_workspace             ONE schema: everything the fleet operator owns
│   ├── companies             ★ central mapping: company code/name → tenants.id (→ schema)
│   ├── company_tenants       view: companies ⨝ tenants (slug, schema_name, status, version)
│   ├── fleet_users           fleet admin / driver staff
│   ├── vehicles, drivers, locations (parking & depots), shifts, inspections,
│   │   vehicle_events, alcohol_tests, geofences, fleet_policy, traffic_state,
│   │   optimization_configs, contracts, fleet_allocations   (+ their child tables)
│   └── notifications, support_tickets, audit_events, user_settings, incidents, sos_dispatches
│
└── tenant_<company>          ONE schema PER company, created when the company is registered
    ├── employers (the company's own profile + policies), employees, locations (pickup/office)
    ├── bookings, trips, routes, route_stops, passenger_events, route_revisions
    ├── optimization_runs, billing_records, invoices
    └── notifications, support_tickets, audit_events, user_settings, incidents, sos_dispatches
```

## Where does each thing live? (no duplication)

| Kind of data | Home | How other schemas use it |
| --- | --- | --- |
| Fleet assets & configuration: vehicles, drivers, parking/depots, policies, geofences, contracts, allocations | `fleet_workspace` | company tables hold a **cross-schema foreign key** (e.g. `trips.assigned_vehicle_id → fleet_workspace.vehicles`) — nothing is copied |
| Company master data & operations: profile, employees, pickup/office points, bookings, trips, billing | `tenant_<company>` | the fleet schema keeps only the registry row (`companies`) |
| Notifications, tickets, audit, settings, incidents | a table in **each** schema | each workspace sees its own rows |
| Logins | `public.platform_users` | memberships say which tenants a login may enter, with which role |

Cross-schema foreign keys are `NO ACTION`: a fleet vehicle/driver that a company trip uses cannot be deleted
(deactivate it); a fleet delete can never rewrite company records. References from the fleet schema to company
rows (e.g. `alcohol_tests.trip_id`) are *soft* (validated by the API, no FK) because the target schema varies.

## Registering a company (automatic schema creation)

`POST /api/v1/t/fleet/companies` (Fleet Admin of the fleet workspace, or platform admin) — in **one
transaction**:

1. validate the company profile + admin login;
2. insert the tenant row (`kind='company'`, status `provisioning`), `CREATE SCHEMA tenant_<slug>`, `REVOKE ALL … FROM PUBLIC`;
3. create every company-level table, FK, index and constraint (all **empty**);
4. insert the company's own profile row into that schema's `employers`;
5. insert the registry row into `fleet_workspace.companies` (the mapping);
6. create the company admin login (`employerAdmin`, scope = company code) and give the creator `fleetManager`;
7. mark the tenant `ready`.

Any failure rolls everything back (no schema, no registry row, no user). A tenant is invisible to the API until `ready`.

## Fleet schema

`python -m scripts.init_db` creates `fleet_workspace` once (idempotent) and upgrades every company schema.
`python -m scripts.create_fleet_manager --email …` creates a fleet admin login (user + fleet membership + `fleet_users` row).
Migrations are versioned per kind (`fleet` 0001, `company` 0001, shared 0001-0003) with checksums.

## Request routing, identification and authorization

1. `Authorization: Bearer <JWT>` → active user.
2. URL slug (`fleet` or a company slug) → `public.tenants` (must be `ready`) → the caller's membership (platform admins may enter any scope). Unknown and forbidden tenants return the same 403.
3. `SET LOCAL search_path` for the scope (transaction-scoped, nothing leaks through the pool):
   `fleet` → `fleet_workspace, public`;  company → `tenant_<slug>, fleet_workspace, public`.
   A company scope therefore resolves its own tables first and *reads/writes fleet-owned tables in `fleet_workspace`*; the fleet scope cannot see company tables at all (404).
4. RBAC (`rbac.py`) per role/resource. Employer admins are row-scoped to their company; each company scope only shows its own `contracts` / `fleet_allocations`.
   Fleet-owned tables may be written only by fleet admins (and the driver tables drivers own).
5. All values are bound parameters; sort/filter names are validated against the spec.

| Workspace the app is signed into | Reads | Writes |
| --- | --- | --- |
| **Fleet** | fleet-owned tables + the company directory (each company's profile) | fleet-owned tables; company profile edits are routed to the company's own schema |
| **Company** | its own tables + fleet-owned reference tables (vehicles, drivers, parking, policies; its own contracts/allocations) | its own tables; fleet-owned tables only for fleet admins |

## Sync (Flutter) — rows are routed to their home schema

`POST …/sync/push` applies a batch atomically and routes each row: fleet-owned → `fleet_workspace`; company-owned →
the company schema; `locations` by type (parking/depot → fleet, pickup/office → company, moved on a type change);
`employers` pushed from the Fleet workspace → the owning company's schema (+ the registry's name/status).
`GET …/sync/pull` composes the right tables for the scope. See `app/routers/sync.py`.

## Known limits

* The Fleet workspace has no company-level data (employees, trips, billing): open the company workspace to work with it. There is no cross-company merged trip view yet.
* Ids are unique **per schema**; a company workspace shows its own rows plus fleet rows, so fleet/company ids of the same table are kept distinct by the API (`locations` moves rather than duplicates).
* Row-level security is not enabled (one application DB role); isolation relies on the layers above.
* No refresh tokens or login rate limiting yet.


## Fleet Admin accounts

* `fleet_workspace.admins(id, user_id, name, email, password_hash, role, status, last_login_at, created_at,
  updated_at)` (fleet migration 0003) is where Fleet Admin accounts are stored and authenticated
  (`app/admins.py`). The hash must be PBKDF2 (CHECK constraint); `admins.set_password` keeps the platform copy
  identical so a person has one password.
* `access.blocked_reason` refuses a `fleetManager` membership whose admins row is missing or inactive.
* The schema was called `fleet_manager` until shared migration 0006 renamed it (non-destructive rename).

## Managers and drivers (roles inside the tenant model)

* `public.tenant_memberships(user, tenant, role, scope_id, active)` is the only place a login gets access to a
  workspace. Roles: `fleetManager` (Fleet Admin), `manager` (Fleet Manager), `employerAdmin`, `driver`, `employee`.
* A Fleet Manager is fleet staff: a `manager` membership of the fleet tenant (no scope) plus
  `fleet_workspace.managers(id, user_id, phone, status)` (fleet migration 0002): role-specific data only; name, email
  and password live once in `public.platform_users`. It signs in at the Fleet workspace and uses the same console as
  the Fleet Admin; permissions come from `rbac.py` (role `manager`). The older `<company>.managers` tables (company
  migration 0003) remain empty and unused.
* A **driver** is a fleet-owned row (`fleet_workspace.drivers`, no duplicate in company schemas). It is assigned to a
  company by a `driver` membership (scope = driver id). In the fleet workspace the Fleet Admin and Fleet Manager
  choose that company when they set the driver's login.
* `access.blocked_reason` refuses deactivated memberships and deactivated drivers at sign-in and on every request.
