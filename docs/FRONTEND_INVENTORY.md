# Frontend analysis & feature inventory

The "frontend" is the Flutter app in `lib/` (not web/TypeScript): four workspaces sharing one
on-device store (`lib/core/persistence/local_database.dart`, 30 typed tables, schema v3). The
database was derived from `lib/domain/models/*.dart` (`toJson` shapes), `lib/domain/enums/enums.dart`,
`lib/domain/rules/*`, the services in `lib/domain/services/*` and the docs `V2_DOMAIN_API.md`,
`README.md`. No pre-existing SQL, migrations or API clients exist in the repo.

## Workspaces and the data they use

| Workspace (`lib/features/…`) | Screens (files) | Tables used |
| --- | --- | --- |
| Employee (`employee/`) | home, book, booking detail, trips, trip detail, activity, pass (QR/T-PIN), roster, notifications, profile, travel preferences, safety/SOS, support | employees, bookings, trips, route_stops, passenger_events, notifications, support_tickets, incidents, sos_dispatches, user_settings, locations |
| Driver (`driver/screens`) | shift, trip run, inspection, incidents/SOS, alcohol test, notifications | shifts, trips, route_stops, passenger_events, inspections, incidents, alcohol_tests, vehicles, drivers, notifications |
| Employer Admin (`employer_admin/`) | overview, employees(+detail, roster import), assignments, locations, policies, settings, billing(+cycles), traffic, analytics, reports, SOS | employers, employees, locations, trips, bookings, billing_records, invoices, contracts, fleet_allocations, fleet_policy, traffic_state, route_revisions, audit_events |
| Fleet Manager (`fleet_admin/pages`) | overview, dispatch(+wizard), run detail, trip detail, vehicles, drivers, parking, pooling, optimization, parameters, monitoring, registry, v2 (contracts, allocations, audit, traffic), v3 (telematics, geofences, safety/SOS), settings, reports | all tables |

## Entity → table mapping (all 30 Flutter tables are covered)

| Dart table | PostgreSQL table(s) in tenant schema |
| --- | --- |
| employers | `employers`, `employer_vehicle_type_prefs`, `employer_shifts` |
| employees | `employees`, `employee_travel_preferences` |
| drivers / vehicles / locations | `drivers`, `vehicles` (+`vehicle_diagnostic_codes`), `locations` |
| trips | `trips`, `trip_passengers` (assigned/boarded/alighted/noShow/absent), `trip_stops`, `trip_assignment_history`, **`trip_status_history`** (new) |
| routes / route_stops | `routes`, `route_stops`, `route_stop_passengers` |
| bookings / passenger_events | `bookings`, `passenger_events` |
| shifts / inspections | `shifts`, `inspections`, `inspection_items` |
| incidents / sos_dispatches | `incidents`, `sos_dispatches` |
| billing / invoices | `billing_records`, `invoices`, `invoice_trips` (`billingRecordIds` is derived from `billing_records.invoice_id`) |
| optimization_configs / runs | `optimization_configs` (+`_vehicle_types`, `_parameters`), `optimization_runs` (+`_employees`, `_candidates`, `_applied_trips`, `_plans`, `_scores`) |
| notifications / tickets / audit / user_settings | `notifications`, `support_tickets`, `audit_events`, `user_settings` |
| contracts / fleet_allocations | `contracts`, `fleet_allocations` |
| route_revisions / traffic_state | `route_revisions` (+`_employees`, `_stops`), `traffic_state` |
| fleet_policy / alcohol_tests / geofences / vehicle_events | `fleet_policy` (+`_sos_recipients`), `alcohol_tests`, `geofences` (+`geofence_vehicles`), `vehicle_events` |

## Business rules found and where they are enforced now

| Rule (source) | Enforced server-side? |
| --- | --- |
| Trip status transitions (`trip_state_machine.dart`) | Yes – `rules.py`, 409 on illegal move; history recorded |
| Assigned/ready/in-progress trips need driver + vehicle; cancel needs reason | Yes |
| Fleet allocation windows may not overlap across companies (incl. overnight) | Yes |
| Parking capacity ≥ parked vehicles; home parking must be parking/depot | Yes |
| Unique codes, registration / RC / engine / licence numbers | Yes – unique indexes |
| Enum values, numeric ranges, period/validity ordering | Yes – CHECK constraints + request validation |
| Contract hard vehicle limit, licence validity, alcohol-test start blocking, billing-cycle runs, optimizer/matching algorithms, geofence/telematics simulation, tolerance windows | **No** – remain Flutter service logic (documented gap) |

## Assumptions

* Timestamps are `TIMESTAMPTZ`, stored/returned in UTC (`…Z`). Dart `toIso8601String()` on local
  times emits no zone; clients must send UTC (naive values are interpreted as UTC).
* `routes.stopIds` and `invoices.billingRecordIds` are derived (read-only) to avoid duplicated state.
* Variable explanatory lists in optimization runs (warnings, remedies …), `activeParameters`,
  `audit_events.related`, plan `stops` and revision stop `employeeIds` are JSONB (schemaless text/arrays).
* `fleet_allocations.resourceId` and `optimization_run_candidates.resource_id` are polymorphic
  (vehicle|driver): integrity is enforced in the API, not by FK.
* ids stay text (`E1`, `TR-1007`) to match the app; the server generates `<prefix><n>` when omitted.
* Users live in `public` (one person can belong to several tenants); roles = the app's `UserRole`.
