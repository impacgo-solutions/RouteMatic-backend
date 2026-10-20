"""Generate ER diagrams (Mermaid + standalone HTML) from the LIVE database.

    python -m scripts.gen_er [--tenant impacgo_solutions]
Writes docs/ER_DIAGRAM.md and docs/er_diagram.html.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from psycopg2 import sql

from app import db

DOCS = Path(__file__).resolve().parents[1] / "docs"
TYPE = {"text": "text", "integer": "int", "numeric": "numeric", "boolean": "bool",
        "timestamp with time zone": "timestamptz", "jsonb": "jsonb", "bigint": "bigint"}


def load(cur, schema: str):
    cur.execute("""SELECT c.relname t, a.attname col, format_type(a.atttypid, a.atttypmod) typ
                   FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
                   JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE n.nspname=%s AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
                   ORDER BY c.relname, a.attnum""", (schema,))
    cols: dict[str, list] = {}
    for r in cur.fetchall():
        cols.setdefault(r["t"], []).append((r["col"], r["typ"]))
    cur.execute("""SELECT c.relname t, k.contype, k.conkey, k.confrelid::regclass::text ref, k.conrelid
                   FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
                   JOIN pg_namespace n ON n.oid=c.relnamespace
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
            fks.append((r["t"], names[0], r["ref"].split(".")[-1].strip('"')))
    return cols, pks, fks


def mermaid(cols, pks, fks, with_cols=True) -> str:
    fkcols = {(t, c) for t, c, _ in fks}
    out = ["erDiagram"]
    for t, cs in cols.items():
        if with_cols:
            out.append(f"  {t} {{")
            for c, typ in cs:
                if c in ("created_by", "updated_by"):
                    continue
                key = "PK" if c in pks.get(t, ()) else ("FK" if (t, c) in fkcols else "")
                ty = TYPE.get(typ, typ.split(" ")[0]).replace(",", "")
                out.append(f"    {ty} {c} {key}".rstrip())
            out.append("  }")
    seen = set()
    for t, c, ref in fks:
        if (t, ref, c) in seen:
            continue
        seen.add((t, ref, c))
        out.append(f'  {ref} ||--o{{ {t} : "{c}"')
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="impacgo_solutions")
    a = ap.parse_args()
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT schema_name FROM public.tenants WHERE slug=%s", (a.tenant,))
        schema = cur.fetchone()["schema_name"]
        cur.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
        cols, pks, fks = load(cur, schema)
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        pub = [r["table_name"] for r in cur.fetchall()]
        pcols, ppks, pfks = load(cur, "public")
    full = mermaid(cols, pks, fks)
    compact = mermaid(cols, pks, fks, with_cols=False)
    shared = mermaid(pcols, ppks, pfks)
    md = (f"# ER diagrams (generated from the live database by `scripts/gen_er.py`)\n\n"
          f"Tenant schema `{schema}`: {len(cols)} tables, {len(fks)} foreign keys. "
          f"`created_by/updated_by` audit columns omitted from the diagram.\n\n"
          f"## Shared `public` schema\n\n```mermaid\n{shared}\n```\n\n"
          f"## Tenant schema - relationships only\n\n```mermaid\n{compact}\n```\n\n"
          f"## Tenant schema - with columns\n\n```mermaid\n{full}\n```\n")
    (DOCS / "ER_DIAGRAM.md").write_text(md, encoding="utf-8")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Routematic ER diagram</title>
<style>body{{font-family:Segoe UI,sans-serif;margin:16px;background:#fafafa}}h2{{margin-top:32px}}
.box{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px;overflow:auto}}</style></head><body>
<h1>Routematic database - ER diagram</h1>
<p>Tenant schema <b>{schema}</b>: {len(cols)} tables, {len(fks)} foreign keys. Scroll / zoom the browser (Ctrl +/-).</p>
<h2>Shared <code>public</code> schema</h2><div class="box"><pre class="mermaid">{shared}</pre></div>
<h2>Tenant schema - with columns</h2><div class="box"><pre class="mermaid">{full}</pre></div>
<script type="module">import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs";
mermaid.initialize({{startOnLoad:true,maxTextSize:1000000,er:{{useMaxWidth:false}},securityLevel:"loose"}});</script>
</body></html>"""
    (DOCS / "er_diagram.html").write_text(html, encoding="utf-8")
    print(f"{len(cols)} tables, {len(fks)} FKs -> docs/ER_DIAGRAM.md, docs/er_diagram.html")


if __name__ == "__main__":
    main()
