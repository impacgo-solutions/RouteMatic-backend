# ER diagrams (generated from a live database by `scripts/gen_er_flow.py`)

| Diagram | Image | Mermaid source |
| --- | --- | --- |
| `fleet_workspace` schema (fleet assets, configuration, company registry) | `er_fleet_workspace.png` | `er_fleet_workspace.mmd` |
| One company schema `tenant_<company>` (company data; dashed boxes = fleet tables it references, not copies) | `er_company.png` | `er_company.mmd` |
| `public` schema (logins + tenant registry) | `er_public.png` | `er_public.mmd` |

Regenerate after a schema change: `python -m scripts.gen_er_flow docs/er_company.mmd --scope <company-slug>`
(or `--scope fleet` / `--scope public`), then render with Mermaid CLI (`mmdc`). The `.mmd` files also render
directly in GitHub / VS Code Markdown preview.
