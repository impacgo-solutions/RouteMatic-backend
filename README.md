# Routematic backend (FastAPI + PostgreSQL: Fleet Admin schema + one schema per company)

Backend and database for the Executive Fleet Mobility Flutter app in the parent folder.
One PostgreSQL database (`Routematic`): a tiny `public` schema (logins + tenant registry), **one
`fleet_workspace` schema** (fleet-level data + the company registry) and **one `tenant_<company>` schema
per registered company**. The existing `impaco_test_dev` database is never touched
(see *Safety*). Design details: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md);
frontend analysis: [`docs/FRONTEND_INVENTORY.md`](docs/FRONTEND_INVENTORY.md).

## Setup (Windows PowerShell, from `backend/`)

```powershell
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # then put your real PostgreSQL password in .env (git-ignored)
```

## Initialise the database (idempotent, safe to re-run)

```powershell
python -m scripts.init_db                          # create DB if missing, shared schema, the fleet_workspace schema, upgrade companies
$env:ROUTEMATIC_ADMIN_PASSWORD = "choose-a-strong-one"
python -m scripts.create_admin --email you@example.com           # platform administrator (can enter every workspace)
$env:ROUTEMATIC_FM_PASSWORD = "choose-a-strong-one"
python -m scripts.create_fleet_manager --email fm@example.com    # a Fleet Admin login (fleet workspace)
python -m scripts.verify_protected_db                            # confirms impaco_test_dev is not touched
```

`init_db` connects to the maintenance DB `postgres` only to check for / create `Routematic`,
then verifies `SELECT current_database()` on every connection before any change.

## Run

```powershell
python -m uvicorn app.main:app --reload --port 8765      # Swagger UI: http://localhost:8765/docs
```

(Port 8000 is used by another service on the machine this was built on; any free port works.)

## Test (uses a throw-away `routematic_test_<random>` database, dropped afterwards)

```powershell
python -m pytest tests -q
```

## Demo data (three synthetic demo companies + Fleet Workspace demo data)

`scripts/seed_demo.py` creates Northstar Technologies (Demo), BluePeak Consulting (Demo) and Greenfield Digital
Services (Demo) through the application's own API (company registration, sync push with all business rules,
login endpoints), plus shared fleet data (parking, vehicles, drivers, contracts, allocations, optimisation
profiles, policy, simulated traffic / geofences). It writes only to a development/demo database named
`routematic_test_<name>`; any other database (the real `Routematic` included) is refused unless the owner
approved it and `--confirm-database <name>` is given. It never deletes or overwrites: re-running reports every
record as "existing". All ids start with `DEMO-`, accounts use `@*.demo.routematic.test`.

```powershell
# the demo password lives only in backend/.env.demo (git-ignored): DEMO_SEED_PASSWORD=...
Get-Content .env.demo | Where-Object { $_ -match '^DEMO_SEED_PASSWORD=' } | ForEach-Object { $env:DEMO_SEED_PASSWORD = $_.Split('=',2)[1] }
python -m scripts.seed_demo --database routematic_test_demo --dry-run          # preview, writes nothing
python -m scripts.seed_demo --database routematic_test_demo --init --yes-demo-data
python -m scripts.seed_demo --database routematic_test_demo --verify           # counts + consistency checks
# run the API on the demo database
$env:DB_NAME = "routematic_test_demo"; python -m uvicorn app.main:app --port 8765
```

Options: `--anchor-date YYYY-MM-DD` ("today" of the demo timeline, default today), `--utc-offset +05:30`
(business time zone of the demo companies). Demo sign-ins are listed in `backend/DEMO_ACCOUNTS.local.md`
(git-ignored).

## API overview (all JSON; camelCase keys identical to the Dart `toJson` shapes)

| Method & path | Purpose |
| --- | --- |
| `POST /api/v1/auth/login`, `GET /api/v1/auth/me` | JWT login; memberships (tenant + role) |
| `GET /api/v1/auth/organizations` | public list for the sign-in company selector (slug, name, kind) |
| `POST /api/v1/auth/signin` | one-step sign-in: credentials + selected company + selected role |
| `GET /api/v1/tenants` | your workspaces (platform admin: all) — `fleet` plus companies |
| `POST /api/v1/tenants/{tenant}/members` | add a user to a workspace (platform admin or its fleet admin) |
| `GET/POST /api/v1/t/fleet/staff/managers`, `GET/PATCH …/{id}`, `POST …/{id}/activate|deactivate` | **Manager Management** (Fleet Admin only): create / view / edit / activate / deactivate managers |
| `PUT/GET /api/v1/t/{tenant}/drivers/{id}/login` | give a driver a login and assign them to a company (Fleet Admin / Fleet Manager in the fleet workspace: `company` in the body) |
| `PUT/GET /api/v1/t/{company}/employees/{id}/login` | give an employee a login (Employer Admin / Fleet Admin) |
| `POST/GET /api/v1/t/fleet/companies` | **register a company (creates its schema)** / list the registry |
| `GET/POST /api/v1/t/{tenant}/{resource}` | list (paged) / create |
| `GET/PUT/PATCH/DELETE /api/v1/t/{tenant}/{resource}/{id}` | read / replace / merge / delete |
| `POST /api/v1/t/{tenant}/trips/{id}/transition`, `GET …/history` | trip state machine + status history |
| `GET /api/v1/t/{tenant}/dashboard` | live aggregates (counts, billing, EV %, open SOS …) |
| `GET /api/v1/t/{tenant}/meta/enums` | all enum values |

`{tenant}` is `fleet` (the Fleet workspace) or a company slug. Resources by home:

* **fleet** (`/t/fleet/...`, also readable/writable from a company workspace by a fleet admin):
  `vehicles drivers parking_locations shifts inspections vehicle_events alcohol_tests geofences fleet_policy
  traffic_state optimization_configs contracts fleet_allocations`
* **company** (`/t/<company>/...`): `employers locations employees bookings trips routes route_stops
  passenger_events route_revisions optimization_runs billing_records invoices`
* **both** (each workspace sees its own rows): `notifications support_tickets audit_events user_settings
  incidents sos_dispatches`

A company-level resource requested in the Fleet workspace returns 404 (open the company workspace).

List query params: `page`, `pageSize` (≤500), `sort`, `order=asc|desc`, `q` (text search), and any
column as an equality filter (`?employerId=C1&status=active`, `?field=null`).
Errors always look like `{"error": {"status", "message", "details"}}`; lists return
`{"items", "total", "page", "pageSize"}`; single items are the bare object.

## Safety

* `app/db.py` refuses any database other than `Routematic` (or `routematic_test_*` in tests) and
  hard-refuses `impaco_test_dev`; it re-checks `current_database()` on each transaction.
* No destructive init path (no DROP/TRUNCATE of schemas or data); DDL is `IF NOT EXISTS` / guarded.
* Tenant is resolved from the URL slug → `public.tenants` (status `ready`) → membership check;
  the schema name never comes from the client; `SET LOCAL search_path` is transaction-scoped.
* Credentials live only in `backend/.env` (git-ignored; `.env.example` has placeholders).
  Rotate the dev `JWT_SECRET` and the dev admin password before any shared deployment.

## Registering a company = creating its schema

`POST /api/v1/t/fleet/companies` (Fleet Admin of the fleet workspace, or platform admin):

```json
{ "employer": {"code": "ACME", "name": "Acme Logistics", "billingCycle": "monthly", "...": "Employer fields"},
  "admin":    {"email": "admin@acme.example", "displayName": "Acme Admin", "password": "min-8-chars"},
  "slug": "optional_identifier" }
```

In ONE transaction it: validates → `CREATE SCHEMA tenant_<slug>` with every **company-level** table / FK / index /
constraint (all empty) → inserts the company's own `employers` row → records the company in the central registry
**`fleet_workspace.companies`** (view `fleet_workspace.company_tenants` shows code → tenant slug → schema) → creates the
admin login (`employerAdmin`, scoped to the company) and gives the creator `fleetManager` access → marks the tenant
`ready`. Any failure rolls everything back (no schema, no registry row, no users).
Fleet-level data (vehicles, drivers, parking, policies, contracts) is **not** copied: it stays in `fleet_workspace`
and company tables reference it through cross-schema foreign keys. See `docs/ARCHITECTURE.md`.

Isolation: a user sees only the workspaces in `public.tenant_memberships`; each request is authenticated (JWT), the
slug is checked against the registry + membership, and the schema is pinned with `SET LOCAL search_path`.
A company admin cannot read the fleet workspace or another company, nor register companies.

**Upgrading a database built with the old layout** (an "operator" tenant + full-structure company schemas):
`python -m scripts.migrate_legacy_layout` (dry run), then `--apply` (one transaction; refuses anything it cannot
place; take a `pg_dump` first).

## Connecting the Flutter app

The app keeps its on-device store as the working copy (all domain rules still run there, so it
works offline) and **mirrors every committed transaction to this API** (`lib/core/persistence/remote_sync.dart`).

Run it against the backend (no credentials in the build; users sign in on the first screen):

```powershell
flutter run -d chrome --dart-define=API_BASE_URL=http://localhost:8765
```

Flow: **one sign-in screen** (the launcher). Pick the **company** (the list comes from the public
`GET /api/v1/auth/organizations`: slug + name + kind only), pick the **role**, enter **email + password**, press
Sign in. The server validates the three together (`POST /api/v1/auth/signin`: credentials, that this login is a
member of that company, and that its role there matches the one chosen), then the company's data opens straight in
that role's console (employees/drivers become the person the account is scoped to). Errors are shown on the same
screen (401 for bad credentials, 403 "This account is not an Employer Admin of Acme Ltd." for a wrong company/role).
The company selection is remembered for next time; **Sign out** (any existing "Sign out" button) returns to the
same screen. The local cache is namespaced per company (`org_<slug>/…`). **Fleet Admin → Companies → Register**
creates the company's tenant on the server before saving the directory entry and offers to open it.

Bootstrapping: `python -m scripts.init_db` (creates the fleet schema), then
`python -m scripts.create_fleet_manager --email ...` and sign in; the Fleet workspace is where companies are
registered (Fleet Admin → Companies → Register). Open a company's workspace to work with its employees, trips
and billing; the Fleet workspace holds fleet assets (vehicles, drivers, parking, policies, contracts).

Dev/CI shortcut (skips the sign-in screen): add `--dart-define=API_TENANT=<slug> --dart-define=API_EMAIL=...
--dart-define=API_PASSWORD=...`.

Behaviour: the server's data (even if empty) replaces the local copy at start. **No demo/seed data is
inserted or uploaded** when a backend is configured. Later edits are queued, pushed in order as atomic batches
(`POST /sync/push`), survive restarts/offline periods, and the app re-pulls every 20 s while idle
(`GET /sync/pull`). Rejected rows (4xx) are dropped and reported under Demo tools → Server sync. An expired
token returns the user to the sign-in screen. Synced writes are *trusted*: server business rules are skipped
because the app already enforced its own; database constraints, tenant isolation and RBAC still apply.
"Reset demo data" is disabled while connected. Tests: `flutter test test/unit test/widget/org_login_flow_test.dart`.

Limits: the Fleet Admin's former cross-company shared pool does not exist across tenants (each company
has its own vehicles/drivers); the session token is stored in the browser/device storage (use HTTPS in
production); no refresh tokens or login rate limiting yet.


## Fleet Admin accounts (`fleet_workspace.admins`)

Fleet Admin accounts live in `fleet_workspace.admins` (id `FA-…`, name, email, PBKDF2 password hash, role
`fleetManager`, status `active|inactive`, last login, timestamps). Sign-in for a Fleet Admin is verified against
this table; `user_id` links the account to its platform identity (tokens, memberships). An inactive or missing
admins row refuses Fleet Admin access at sign-in and on every request. Create / reset one with
`python -m scripts.create_fleet_manager --email … --name …` (password from `ROUTEMATIC_FM_PASSWORD` or a prompt).

The fleet schema was renamed from `fleet_manager` to `fleet_workspace` by shared migration 0006
(`ALTER SCHEMA … RENAME`: tables, rows, indexes, constraints, sequences, views and grants are kept; it never
merges two schemas). Fleet migration 0003 created `admins` and carried the existing Fleet Admins over.

## Roles (who can do what)

| Role (UI name) | Workspace | Can |
| --- | --- | --- |
| **Fleet Admin** (`fleetManager`) | fleet + every company (workspace switcher) | everything: companies, managers, drivers (any company), vehicles, policies, contracts |
| **Fleet Manager** (`manager`) | fleet + every company (workspace switcher), no company of its own | the **same application as the Fleet Admin**: signs in at the Fleet workspace with the role *Fleet Manager* and lands on the same dashboard; reads everything the console shows; creates / edits drivers and assigns their logins to companies; runs driver/safety operations; updates a vehicle's operational fields only. No Fleet Admin privileges: companies, managers, vehicle assets, parking, policies, geofences, contracts, allocations, deleting records |
| **Employer Admin** (`employerAdmin`) | one company | company profile, employees (+ their logins), locations, billing (incl. the trip's distance / billed status), the company's optimisation profile |
| **Driver** (`driver`) | the company it is assigned to | Driver app: **its own** trips (route, stops, bookings, billing line), shifts, inspections, alcohol tests, the operational fields of the vehicle it drives |
| **Employee** (`employee`) | its company | Employee app: **its own** profile, bookings, settings and the trips it rides in (absence / auto-match; never re-assigning drivers or vehicles) |

**Row ownership** (`app/policy.py`): role permissions say which tables a role may write; for drivers and employees
the server also checks *which rows*, against what was theirs before the change and the final state after it (the
same rules for REST and `sync/push`). Drivers / employees read only their own bookings, passenger events, settings,
shifts, inspections and alcohol tests. Deletes through `sync/push` follow the same rule as REST (`DELETE_ROLES`;
employees / managers may only replace a trip's route and stops). Tenant admins can never attach to, or reset the
password of, a platform admin, a Fleet Admin or an account without any role. Company profiles are never deleted
and their code cannot change.

**Fleet staff in company workspaces**: an active Fleet Admin / Fleet Manager has the same role in every company
workspace (dispatch, billing, optimisation for that company), listed in `/auth/me` memberships; the app's Fleet
console switches workspace from the label under the workspace name.

**Refused changes are visible**: when the server refuses a mirrored change the app shows "Not saved on the
server: …" and reloads the server's state (no silent divergence). While connected, new row IDs carry a
per-session tag (e.g. `TR-1007-K3X9`) so devices that see only part of a table never reuse an existing ID.

**Sign-in protection**: 10 failed sign-ins for one email from one client within 15 minutes lock that pair out
(HTTP 429 with `Retry-After`); a successful sign-in clears the counter (`LOGIN_MAX_FAILURES`,
`LOGIN_WINDOW_SECONDS`). The counter is in memory per API process — use a shared store such as Redis when running
several API processes.

**Server audit trail**: company registration, Fleet Manager create / update / activate / deactivate, employee and
driver login set-up, driver transfers, member grants and record imports are written to the workspace's
`audit_events` (who, what, which record — never passwords, hashes or tokens). Opening a full driver / employee
record as fleet staff is logged on the server (`routematic.access`).

**Start-up checks**: the API refuses to start without a JWT secret of at least 16 characters or with a protected
database name (`impaco_test_dev`, `impac_test_dev`). `CORS_ALLOW_LOCALHOST=false` in production.

**Business rules on synced changes**: the server rules (`app/rules.py`: trip state machine, driver + vehicle for
assigned trips, cancellation reason, inactive resources, parking capacity, allocation overlap) apply to
`sync/push` as well as REST. Capacity / overlap are checked on the batch's final state (moving several vehicles
in one save is fine). A Fleet Admin may create (import) trips in any status.

**Sessions**: tokens carry the account's credential version (`platform_users.token_version`, shared migration
0007); any password change increments it, so older sessions stop working immediately.

**Privacy** (company workspaces): employees / drivers do not see other employees' phone, email, notes or gender (a
driver keeps its own passengers' phone numbers); employees, other drivers and company admins see a driver's licence
number masked and no address / email. Filtering or searching on hidden fields is refused. Company admins see
fleet shift / inspection records only of drivers serving their company.

**Driver moves**: assigning a driver to another company switches off its login at the previous company (kept, not
deleted); moving back restores it. Login set-up responses no longer state whether the email already had an account
(`passwordChanged` still tells the admin whether the password they typed was applied).

**Fleet Managers** see Fleet Admin-only actions locked ("Fleet Admin only"); the server refuses them as well.

Fleet migration 0004 made three references soft (`inspections.defect_incident_id`, `alcohol_tests.incident_id`,
`vehicle_events.incident_id`): the incident may be recorded in a company schema, so a database FK into the fleet
schema rejected every driver inspection / alcohol test that raised an incident.

Fleet Managers are created by the Fleet Admin (name, email, phone, password; no company). Identity (name, email,
password hash) is stored once in `public.platform_users`; the manager's access is a `manager` membership of the
fleet workspace (`public.tenant_memberships`, scope empty, with an `active` flag) and its role-specific data
(phone, status) is in `fleet_workspace.managers` (fleet migration 0002). Drivers are fleet-owned rows
(`fleet_workspace.drivers`); a driver belongs to a company through a `driver` membership of that company.
Deactivating a manager (or a driver) blocks sign-in and every request immediately, even with an older token.
(The per-company `managers` tables from company migration 0003 are kept but no longer used.)
