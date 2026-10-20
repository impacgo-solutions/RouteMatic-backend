"""Grouped Mermaid flowchart ER diagram (tables + columns, by domain) from the LIVE database.

    python -m scripts.gen_er_flow out.mmd --scope fleet            # the fleet_workspace schema
    python -m scripts.gen_er_flow out.mmd --scope <company-slug>   # one company schema
    python -m scripts.gen_er_flow out.mmd --scope public           # logins + tenant registry

Cross-schema foreign keys (company -> fleet_workspace) are drawn to dashed ``fleet_workspace.<table>`` boxes.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from psycopg2 import sql

from app import db

TYPE = {"text": "text", "integer": "int", "numeric": "numeric", "boolean": "bool",
        "timestamp with time zone": "timestamptz", "jsonb": "jsonb", "bigint": "bigint", "uuid": "uuid"}

GROUPS = {
    "Fleet registry & users": ("#E3E0FF", ["companies", "fleet_users", "admins", "managers"]),
    "Fleet assets": ("#DDF3E4", ["vehicles", "vehicle_diagnostic_codes", "drivers", "locations", "shifts",
                                  "inspections", "inspection_items", "geofences", "geofence_vehicles"]),
    "Fleet configuration & contracts": ("#FFF1D6", ["fleet_policy", "fleet_policy_sos_recipients", "traffic_state",
                                                    "optimization_configs", "optimization_config_vehicle_types",
                                                    "optimization_config_parameters", "contracts",
                                                    "fleet_allocations"]),
    "Safety & telematics": ("#FFDDDD", ["vehicle_events", "alcohol_tests", "incidents", "sos_dispatches"]),
    "Company profile & people": ("#DCE9FF", ["employers", "employer_shifts", "employer_vehicle_type_prefs",
                                              "employees", "employee_travel_preferences"]),
    "Trips & routes": ("#FFF1D6", ["routes", "route_stops", "route_stop_passengers", "trips", "trip_passengers",
                                   "trip_stops", "trip_assignment_history", "trip_status_history", "bookings",
                                   "passenger_events", "route_revisions", "route_revision_employees",
                                   "route_revision_stops"]),
    "Optimization runs": ("#EBDDFB", ["optimization_runs", "optimization_run_employees",
                                      "optimization_run_candidates", "optimization_run_applied_trips",
                                      "optimization_run_plans", "optimization_run_scores"]),
    "Billing": ("#D9F2F2", ["billing_records", "invoices", "invoice_trips"]),
    "Platform (per schema)": ("#EEEEEE", ["notifications", "support_tickets", "audit_events", "user_settings",
                                          "id_counters", "schema_migrations"]),
    "Identity & registry (public)": ("#E3E0FF", ["platform_users", "tenant_memberships", "tenants",
                                                 "global_roles", "tenant_provision_log"]),
}


def load(cur, schema: str):
    cur.execute("""SELECT c.relname t, a.attname col, format_type(a.atttypid, a.atttypmod) typ
                   FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
                   JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE n.nspname=%s AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
                   ORDER BY c.relname, a.attnum""", (schema,))
    cols: dict[str, list] = {}
    for r in cur.fetchall():
        cols.setdefault(r["t"], []).append((r["col"], r["typ"]))
    cur.execute("""SELECT c.relname t, k.contype, k.conkey, k.conrelid,
                          rn.nspname AS ref_schema, rc.relname AS ref_table
                   FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
                   JOIN pg_namespace n ON n.oid=c.relnamespace
                   LEFT JOIN pg_class rc ON rc.oid=k.confrelid
                   LEFT JOIN pg_namespace rn ON rn.oid=rc.relnamespace
                   WHERE n.nspname=%s AND k.contype IN ('p','f')""", (schema,))
    pks: dict[str, set] = {}
    fks: list[tuple] = []
    for r in cur.fetchall():
        cur.execute("SELECT attname FROM pg_attribute WHERE attrelid=%s AND attnum = ANY(%s)",
                    (r["conrelid"], r["conkey"]))
        names = [x["attname"] for x in cur.fetchall()]
        if r["contype"] == "p":
            pks[r["t"]] = set(names)
        else:
            fks.append((r["t"], names[0], r["ref_schema"], r["ref_table"]))
    return cols, pks, fks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--scope", default="fleet", help="fleet | public | <company slug>")
    a = ap.parse_args()
    with db.transaction(dict_rows=True) as cur:
        if a.scope == "public":
            schema, title = "public", "public (logins + tenant registry)"
        else:
            cur.execute("SELECT schema_name, kind FROM public.tenants WHERE slug=%s", (a.scope,))
            row = cur.fetchone()
            schema, title = row["schema_name"], f"{row['schema_name']} ({row['kind']})"
        cols, pks, fks = load(cur, schema)
    fkc = {(t, c) for t, c, _, _ in fks}
    grouped = {t for _, (_, ts) in GROUPS.items() for t in ts}
    leftover = sorted(set(cols) - grouped)
    groups = dict(GROUPS)
    if leftover:
        groups["Other"] = ("#EEEEEE", leftover)
    o = ["%%{init: {'flowchart': {'htmlLabels': true, 'nodeSpacing': 25, 'rankSpacing': 70, 'curve': 'basis'}}}%%",
         "flowchart LR"]
    styles: list[str] = []
    gi = 0
    for g, (color, ts) in groups.items():
        present = [t for t in ts if t in cols]
        if not present:
            continue
        o.append(f'  subgraph G{gi}["{g}"]')
        o.append("    direction TB")
        for t in present:
            lines = [f"<b>{t}</b>"]
            for col, typ in cols[t]:
                if col in ("created_by", "updated_by") and t not in ("companies",):
                    continue
                tag = "🔑 " if col in pks.get(t, ()) else ("🔗 " if (t, col) in fkc else "")
                lines.append(f"{tag}{col}")
            o.append(f'    {t}["' + "<br/>".join(lines) + '"]')
            styles.append(f"  style {t} fill:{color},stroke:#667085,text-align:left")
        o.append("  end")
        styles.append(f"  style G{gi} fill:{color}33,stroke:{color},stroke-width:2px")
        gi += 1
    externals: dict[str, str] = {}
    edges: set[tuple[str, str]] = set()
    for t, c, rs, rt in fks:
        if rs == schema:
            edges.add((rt, t))
        else:                                   # reference into another schema
            ext = f"x_{rs}_{rt}"
            externals[ext] = f"{rs}.{rt}"
            edges.add((ext, t))
    if externals:
        o.append('  subgraph GX["Other schemas (referenced, not copied)"]')
        for ext, label in externals.items():
            o.append(f'    {ext}["{label}"]')
            styles.append(f"  style {ext} fill:#ffffff,stroke:#667085,stroke-dasharray:5 3")
        o.append("  end")
        styles.append("  style GX fill:#ffffff,stroke:#98a2b3,stroke-dasharray:5 3")
    o += styles
    for src, dst in sorted(edges):
        o.append(f"  {src} --> {dst}")
    Path(a.out).write_text(chr(10).join(o), encoding="utf-8")
    print(f"{title}: {len(cols)} tables, {len(fks)} foreign keys -> {a.out}")


if __name__ == "__main__":
    main()
