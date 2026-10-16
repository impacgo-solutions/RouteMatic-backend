"""Emit a Graphviz DOT ER diagram (tables with columns, grouped by domain) from the live DB.

    python -m scripts.gen_er_dot out.dot [--tenant impacgo_solutions]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from psycopg2 import sql

from app import db
from scripts.gen_er import load, TYPE

GROUPS = {
    "Organization": ("#DCE9FF", ["employers", "employer_shifts", "employer_vehicle_type_prefs", "locations",
                                 "employees", "employee_travel_preferences", "contracts", "fleet_allocations"]),
    "Fleet & drivers": ("#DDF3E4", ["vehicles", "vehicle_diagnostic_codes", "drivers", "shifts", "inspections",
                                    "inspection_items"]),
    "Routes & trips": ("#FFF1D6", ["routes", "route_stops", "route_stop_passengers", "trips", "trip_passengers",
                                   "trip_stops", "trip_assignment_history", "trip_status_history", "bookings",
                                   "passenger_events", "route_revisions", "route_revision_employees",
                                   "route_revision_stops", "traffic_state"]),
    "Optimization": ("#EBDDFB", ["optimization_configs", "optimization_config_vehicle_types",
                                 "optimization_config_parameters", "optimization_runs",
                                 "optimization_run_employees", "optimization_run_candidates",
                                 "optimization_run_applied_trips", "optimization_run_plans",
                                 "optimization_run_scores"]),
    "Safety & telematics": ("#FFDDDD", ["incidents", "sos_dispatches", "alcohol_tests", "geofences",
                                        "geofence_vehicles", "vehicle_events", "fleet_policy",
                                        "fleet_policy_sos_recipients"]),
    "Billing": ("#D9F2F2", ["billing_records", "invoices", "invoice_trips"]),
    "Platform": ("#EEEEEE", ["notifications", "support_tickets", "audit_events", "user_settings",
                             "id_counters", "schema_migrations"]),
}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--tenant", default="impacgo_solutions")
    a = ap.parse_args()
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT schema_name FROM public.tenants WHERE slug=%s", (a.tenant,))
        schema = cur.fetchone()["schema_name"]
        cur.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
        cols, pks, fks = load(cur, schema)
    fkc = {(t, c) for t, c, _ in fks}
    color = {t: c for _, (c, ts) in GROUPS.items() for t in ts}
    missing = set(cols) - set(color)
    assert not missing, missing
    o = ['digraph ER {', 'graph [rankdir=LR, splines=true, overlap=false, nodesep=0.35, ranksep=1.1, '
         'fontname="Segoe UI", pad=0.4, bgcolor=white, labelloc=t, fontsize=26, '
         f'label="Routematic - tenant schema {schema}  ({len(cols)} tables, {len(fks)} foreign keys)"];',
         'node [shape=plain, fontname="Segoe UI", fontsize=11];',
         'edge [color="#667085", arrowsize=0.7, fontname="Segoe UI", fontsize=8, fontcolor="#667085"];']
    for i, (g, (c, ts)) in enumerate(GROUPS.items()):
        o.append(f'subgraph cluster_{i} {{ label="{g}"; fontsize=18; style="rounded,filled"; '
                 f'color="{c}"; fillcolor="{c}55"; ')
        for t in ts:
            rows = [f'<TR><TD COLSPAN="3" BGCOLOR="{c}" ALIGN="LEFT"><B>{t}</B></TD></TR>']
            for col, typ in cols[t]:
                if col in ("created_by", "updated_by"):
                    continue
                key = "PK" if col in pks.get(t, ()) else ("FK" if (t, col) in fkc else "")
                ty = TYPE.get(typ, typ.split(" ")[0])
                rows.append(f'<TR><TD ALIGN="LEFT" PORT="{col}">{esc(col)}</TD>'
                            f'<TD ALIGN="LEFT"><FONT COLOR="#667085">{esc(ty)}</FONT></TD>'
                            f'<TD><B>{key}</B></TD></TR>')
            o.append(f'"{t}" [label=<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="3" '
                     f'BGCOLOR="white">{"".join(rows)}</TABLE>>];')
        o.append("}")
    for t, c, ref in sorted(set(fks)):
        o.append(f'"{ref}" -> "{t}" [arrowhead=crowodot, arrowtail=tee, dir=both, label="{c}"];')
    o.append("}")
    Path(a.out).write_text("\n".join(o), encoding="utf-8")
    print("wrote", a.out)


if __name__ == "__main__":
    main()
