"""Generic, spec-driven repository (parameterised SQL only).

All table/column identifiers come from :mod:`app.spec` (never from the client) and
are double-quoted; all values are bound parameters.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg2
from psycopg2 import errors
from psycopg2.extras import Json

from .ddl import AUTO_TS, q
from .spec import ENTITIES, ENUMS, Col, Derived, Entity, Ids, Kv, Objs, _UNSET, CLIENT_ID_ENTITIES

def qt(e) -> str:
    """Quoted table reference; entities pinned to a schema (fleet parking) are always qualified."""
    return (q(e.pin) + "." if getattr(e, "pin", "") else "") + q(e.table)


ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")

# Row-level scoping for employer administrators (tables without employer_id).
SCOPE_SQL = {
    "bookings": '"employee_id" IN (SELECT id FROM "employees" WHERE employer_id = %s)',
    "passenger_events": '"trip_id" IN (SELECT id FROM "trips" WHERE employer_id = %s)',
}


class ApiError(Exception):
    def __init__(self, status: int, message: str, details: Any = None):
        self.status, self.message, self.details = status, message, details
        super().__init__(message)


# --------------------------------------------------------------------------- #
# value conversion
# --------------------------------------------------------------------------- #
def out_value(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return v


def parse_ts(v: str) -> datetime:
    s = v.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)  # naive = UTC


# Upper bound for any single text value (abuse / storage protection; real fields are far shorter).
MAX_TEXT = 10_000


def _coerce(c: Col, v: Any, errs: list[str], label: str | None = None) -> Any:
    label = label or c.key
    if v is None:
        if c.null:
            return None
        errs.append(f"{label}: must not be null")
        return None
    ok = True
    if c.kind == "text":
        ok = isinstance(v, str)
    elif c.kind == "int":
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        ok = isinstance(v, int) and not isinstance(v, bool)
    elif c.kind == "num":
        ok = isinstance(v, (int, float)) and not isinstance(v, bool)
        if ok and isinstance(v, float) and not math.isfinite(v):
            errs.append(f"{label}: must be a finite number")
            return None
    elif c.kind == "bool":
        ok = isinstance(v, bool)
    elif c.kind == "ts":
        try:
            if not isinstance(v, str):
                raise ValueError
            v = parse_ts(v)
        except ValueError:
            ok = False
    elif c.kind == "jsonb":
        return Json(v)
    if not ok:
        errs.append(f"{label}: expected {c.kind}")
        return None
    if c.kind == "text" and len(v) > MAX_TEXT:
        errs.append(f"{label}: at most {MAX_TEXT} characters")
        return None
    if c.enum and v not in ENUMS[c.enum]:
        errs.append(f"{label}: must be one of {ENUMS[c.enum]}")
    if c.kind in ("int", "num"):
        if c.min is not None and v < c.min:
            errs.append(f"{label}: must be >= {c.min:g}")
        if c.max is not None and v > c.max:
            errs.append(f"{label}: must be <= {c.max:g}")
    return v


# --------------------------------------------------------------------------- #
# Format rules (server-side mirror of the app's form checks)
# --------------------------------------------------------------------------- #
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9 ()\-]{7,20}$")
# Human-entered business codes (employee / driver / vehicle / company / location / contract).
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,39}$")

EMAIL_KEYS = {"email", "contactEmail", "fictionalEmail"}
PHONE_KEYS = {"fictionalPhone", "fictionalContact"}
CODE_KEYS = {"employeeCode", "driverCode", "vehicleCode", "contractCode"}
CODE_TABLES = {"employers", "locations", "parking_locations"}       # their "code" column
# Maximum lengths of human-entered text (system-generated summaries keep MAX_TEXT).
SHORT_TEXT = {"name": 160, "contactName": 160, "label": 160, "shiftLabel": 160, "scenarioLabel": 160,
              "demoLocationLabel": 160, "title": 200, "subject": 200, "address": 500,
              "reference": 100, "documentRef": 100, "guardReference": 100, "licenceNumber": 64,
              "licenceClass": 64, "fictionalRegistration": 32}
FREE_TEXT_MAX = 2000
FREE_TEXT = {"notes", "note", "description", "issueNote", "handoverNotes", "resolutionNotes",
             "completionNotes", "cancellationReason", "delayReason", "skipReason", "decisionReason",
             "disputeReason", "rejectionReason", "regularizationReason", "reason"}


def _format_error(e: Entity, c: Col, v: Any) -> str | None:
    if not isinstance(v, str) or v == "":
        return None
    label = c.key
    if c.key in EMAIL_KEYS:
        if len(v) > 254 or not EMAIL_RE.match(v.strip()):
            return f"{label}: enter a valid email address (name@company.com)"
    elif c.key in PHONE_KEYS:
        digits = sum(ch.isdigit() for ch in v)
        if not PHONE_RE.match(v.strip()) or not 7 <= digits <= 15:
            return f"{label}: enter a valid phone number (7-15 digits, optional +)"
    elif c.key in CODE_KEYS or (c.key == "code" and e.table in CODE_TABLES):
        if not CODE_RE.match(v):
            return f"{label}: use 1-40 letters, digits, '-', '_' or '.' (no spaces)"
    limit = SHORT_TEXT.get(c.key) or (FREE_TEXT_MAX if c.key in FREE_TEXT else None)
    if limit and len(v) > limit:
        return f"{label}: at most {limit} characters"
    return None


def check_formats(e: Entity, cols: dict, old: dict | None = None) -> None:
    """Reject badly formatted values being WRITTEN. On update only values that actually change are
    checked, so rows stored before these rules existed stay readable and editable (an edit that does
    not touch the old value is not refused because of it)."""
    errs = []
    for c in e.cols:
        if c.name not in cols:
            continue
        v = cols[c.name]
        if old is not None and old.get(c.name) == v:
            continue
        msg = _format_error(e, c, v)
        if msg:
            errs.append(msg)
    if errs:
        raise ApiError(422, "Validation failed.", errs)


def validate(e: Entity, body: dict, *, partial: bool) -> tuple[dict, dict]:
    """Return (snake_case column values, list values). Raises ApiError(422)."""
    if not isinstance(body, dict):
        raise ApiError(422, "Request body must be a JSON object.")
    errs: list[str] = []
    known = {c.key for c in e.cols} | {l.key for l in e.lists} | {"id"}
    for k in body:
        if k not in known:
            errs.append(f"{k}: unknown field")
    cols: dict[str, Any] = {}
    for c in e.cols:
        if c.key in body:
            cols[c.name] = _coerce(c, body[c.key], errs)
        elif not partial:
            if c.key in AUTO_TS and c.kind == "ts":
                continue                      # DB default now()
            if c.required:
                errs.append(f"{c.key}: required")
            elif c.default is not _UNSET:
                cols[c.name] = Json(c.default) if c.kind == "jsonb" else c.default
    lists: dict[str, Any] = {}
    for l in e.lists:
        if isinstance(l, Derived):
            continue
        if l.key not in body:
            continue
        v = body[l.key]
        if isinstance(l, Ids):
            if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
                errs.append(f"{l.key}: expected list of strings")
                continue
            if l.enum:
                bad = [x for x in v if x not in ENUMS[l.enum]]
                if bad:
                    errs.append(f"{l.key}: invalid values {bad}")
            lists[l.key] = list(dict.fromkeys(v))
        elif isinstance(l, Kv):
            if not isinstance(v, dict):
                errs.append(f"{l.key}: expected object")
                continue
            tmp = Col(l.vcol, l.vkind)
            lists[l.key] = {str(k): _coerce(tmp, x, errs, f"{l.key}.{k}") for k, x in v.items()}
        elif isinstance(l, Objs):
            if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
                errs.append(f"{l.key}: expected list of objects")
                continue
            rows = []
            for i, obj in enumerate(v):
                row = {}
                for k in obj:
                    if k not in {c.key for c in l.cols}:
                        errs.append(f"{l.key}[{i}].{k}: unknown field")
                for c in l.cols:
                    if c.key in obj:
                        row[c.name] = _coerce(c, obj[c.key], errs, f"{l.key}[{i}].{c.key}")
                    elif c.required:
                        errs.append(f"{l.key}[{i}].{c.key}: required")
                    elif c.default is not _UNSET:
                        row[c.name] = Json(c.default) if c.kind == "jsonb" else c.default
                rows.append(row)
            lists[l.key] = rows
    if errs:
        raise ApiError(422, "Validation failed.", errs)
    return cols, lists


def _fields(detail: str | None) -> str:
    """'Key (vehicle_code, employer_id)=(C1, X) already exists.' -> 'vehicleCode, employerId' (never values)."""
    m = re.match(r"Key \(([^)]*)\)", detail or "")
    if not m:
        return ""
    def camel(n: str) -> str:
        head, *rest = n.strip().strip('"').split("_")
        return head + "".join(w[:1].upper() + w[1:] for w in rest)
    return ", ".join(camel(n) for n in m.group(1).split(",") if n.strip() and not n.strip().startswith("lower("))


def map_db_error(exc: Exception) -> ApiError:
    """Database errors as safe client messages: field names only, never constraint names, SQL or values."""
    d = getattr(exc, "diag", None)
    detail = (getattr(d, "message_detail", None) or "").strip() or None
    con = getattr(d, "constraint_name", None)
    if isinstance(exc, errors.UniqueViolation) and con == "uq_employers_single_company":
        return ApiError(422, "This workspace already holds its company; a second company record is not allowed.")
    if isinstance(exc, errors.UniqueViolation):
        f = _fields(detail)
        return ApiError(409, f"A record with this {f} already exists." if f else
                        "A record with the same unique value already exists.")
    if isinstance(exc, errors.ForeignKeyViolation):
        f = _fields(detail)
        return ApiError(409, "Referenced record is missing, or this record is still in use"
                             + (f" ({f})." if f else "."))
    if isinstance(exc, errors.CheckViolation) and con == "ck_company_locations_type":
        return ApiError(422, "Parking and depots are fleet locations, not company locations.")
    if isinstance(exc, errors.CheckViolation) and not con and getattr(d, "message_primary", None):
        return ApiError(422, d.message_primary)         # raised by our own guard triggers: user-facing text
    if isinstance(exc, errors.NotNullViolation):
        col = getattr(d, "column_name", None)
        return ApiError(422, f"A required value is missing ({_fields(f'Key ({col})')})." if col
                        else "A required value is missing.")
    if isinstance(exc, errors.CheckViolation):
        return ApiError(422, "A value is outside the allowed range or format.")
    if isinstance(exc, (errors.InvalidTextRepresentation, errors.NumericValueOutOfRange,
                        errors.InvalidDatetimeFormat)) or isinstance(exc, psycopg2.DataError):
        return ApiError(422, "Invalid value: a field has the wrong format or is out of range.")
    if isinstance(exc, (errors.LockNotAvailable, errors.SerializationFailure, errors.DeadlockDetected)):
        return ApiError(409, "The record is being changed by someone else; try again.")
    raise exc


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def _row_json(e: Entity, row: dict) -> dict:
    out = {"id": row["id"]}
    for c in e.cols:
        out[c.key] = out_value(row[c.name])
    return out


def _hydrate(cur, e: Entity, items: list[dict]) -> None:
    if not items:
        return
    ids = [i["id"] for i in items]
    by_id = {i["id"]: i for i in items}
    for l in e.lists:
        for i in items:
            i[l.key] = {} if isinstance(l, Kv) else []
        if isinstance(l, Derived):
            cur.execute(l.sql, (ids,))
            for r in cur.fetchall():
                owner_id, value = list(r.values())[:2]
                by_id[owner_id][l.key].append(value)
        elif isinstance(l, Ids):
            where = f"{q(l.owner)} = ANY(%s)"
            params: list = [ids]
            if l.discr:
                where += f" AND {q(l.discr[0])} = %s"
                params.append(l.discr[1])
            cur.execute(f"SELECT {q(l.owner)} AS o, {q(l.col)} AS v FROM {q(l.table)} "
                        f"WHERE {where} ORDER BY {q(l.owner)}, \"position\"", params)
            for r in cur.fetchall():
                by_id[r["o"]][l.key].append(r["v"])
        elif isinstance(l, Kv):
            cur.execute(f"SELECT {q(l.owner)} AS o, {q(l.kcol)} AS k, {q(l.vcol)} AS v "
                        f"FROM {q(l.table)} WHERE {q(l.owner)} = ANY(%s)", (ids,))
            for r in cur.fetchall():
                by_id[r["o"]][l.key][r["k"]] = out_value(r["v"])
        elif isinstance(l, Objs):
            cur.execute(f"SELECT * FROM {q(l.table)} WHERE {q(l.owner)} = ANY(%s) "
                        f"ORDER BY {q(l.owner)}, \"position\"", (ids,))
            for r in cur.fetchall():
                by_id[r[l.owner]][l.key].append({c.key: out_value(r[c.name]) for c in l.cols})


# A Manager sees/edits only the drivers assigned to its own company: a driver is assigned to a company
# by a ``driver`` membership in that company's tenant (scope_id = the driver's id).
DRIVERS_OF_COMPANY = (
    '"id" IN (SELECT m.scope_id FROM public.tenant_memberships m JOIN public.tenants t ON t.id = m.tenant_id '
    "WHERE t.slug = %s AND m.role = 'driver')"
)


def _scope_clause(e: Entity, scope) -> tuple[str, list]:
    if scope is None:
        return "", []
    if isinstance(scope, tuple):                    # ("drivers-of", company slug) | ("sql", clause, params)
        if scope[0] == "sql":
            return scope[1], list(scope[2])
        if scope[0] == "drivers-of" and e.table == "drivers":
            return DRIVERS_OF_COMPANY, [scope[1]]
        return "", []
    if e.table == "employers":
        return '"id" = %s', [scope]
    if getattr(e, "scoped_by_employer", False):
        return '"employer_id" = %s', [scope]
    if e.table in SCOPE_SQL:
        return SCOPE_SQL[e.table], [scope]
    return "", []


def list_all(cur, e: Entity, scope=None, *, chunk: int = 5000) -> list[dict]:
    """Every row the scope allows, in the default list order, with ONE query per table.

    (Paging with OFFSET + count(*) per page grows quadratically with the table size.)
    Child lists are loaded in chunks so the ``= ANY(ids)`` arrays stay bounded.
    """
    clause, params = _scope_clause(e, scope)
    where = f" WHERE {clause}" if clause else ""
    cur.execute(f"SELECT * FROM {qt(e)}{where} ORDER BY {q(e.sort)} ASC, \"id\"", params)
    items = [_row_json(e, r) for r in cur.fetchall()]
    for i in range(0, len(items), chunk):
        _hydrate(cur, e, items[i:i + chunk])
    return items


def get(cur, e: Entity, id_: str, scope: str | None = None) -> dict:
    clause, params = _scope_clause(e, scope)
    where = '"id" = %s' + (f" AND {clause}" if clause else "")
    cur.execute(f"SELECT * FROM {qt(e)} WHERE {where}", [id_] + params)
    row = cur.fetchone()
    if not row:
        raise ApiError(404, f"{e.table} '{id_}' not found.")
    item = _row_json(e, row)
    _hydrate(cur, e, [item])
    return item


def _convert_filter(c: Col, raw: str) -> Any:
    try:
        if c.kind == "int":
            return int(raw)
        if c.kind == "num":
            return float(raw)
        if c.kind == "bool":
            if raw.lower() not in ("true", "false"):
                raise ValueError
            return raw.lower() == "true"
        if c.kind == "ts":
            return parse_ts(raw)
    except ValueError:
        raise ApiError(422, f"Invalid filter value for '{c.key}'.")
    if c.enum and raw not in ENUMS[c.enum]:
        raise ApiError(422, f"Invalid filter value for '{c.key}': must be one of {ENUMS[c.enum]}")
    return raw


def list_(cur, e: Entity, *, page: int = 1, page_size: int = 50, sort: str | None = None,
          order: str = "asc", search: str | None = None, filters: dict[str, str] | None = None,
          scope: str | None = None) -> dict:
    if page < 1 or not 1 <= page_size <= 500:
        raise ApiError(422, "page must be >= 1 and pageSize between 1 and 500.")
    if order.lower() not in ("asc", "desc"):
        raise ApiError(422, "order must be 'asc' or 'desc'.")
    cols = {c.key: c for c in e.cols}
    sort_col = e.sort
    if sort:
        if sort == "id":
            sort_col = "id"
        elif sort in cols:
            sort_col = cols[sort].name
        elif sort in {c.name for c in e.cols}:
            sort_col = sort
        else:
            raise ApiError(422, f"Cannot sort by '{sort}'.")
    wh: list[str] = []
    params: list = []
    for k, raw in (filters or {}).items():
        if k == "id":
            wh.append('"id" = %s'); params.append(raw); continue
        if k not in cols:
            raise ApiError(422, f"Cannot filter by '{k}'.")
        c = cols[k]
        if raw == "null":
            wh.append(f"{q(c.name)} IS NULL")
        else:
            wh.append(f"{q(c.name)} = %s"); params.append(_convert_filter(c, raw))
    if search:
        like = "%" + search.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        cols_s = e.search or ["id"]
        wh.append("(" + " OR ".join(f"{q(col)}::text ILIKE %s" for col in cols_s) + ")")
        params += [like] * len(cols_s)
    clause, sp = _scope_clause(e, scope)
    if clause:
        wh.append(clause); params += sp
    where = (" WHERE " + " AND ".join(wh)) if wh else ""
    cur.execute(f"SELECT count(*) AS n FROM {qt(e)}{where}", params)
    total = cur.fetchone()["n"]
    cur.execute(f"SELECT * FROM {qt(e)}{where} ORDER BY {q(sort_col)} {order.upper()}, \"id\" "
                f"LIMIT %s OFFSET %s", params + [page_size, (page - 1) * page_size])
    items = [_row_json(e, r) for r in cur.fetchall()]
    _hydrate(cur, e, items)
    return {"items": items, "total": total, "page": page, "pageSize": page_size}


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
def next_id(cur, e: Entity) -> str:
    for _ in range(10000):
        cur.execute('INSERT INTO "id_counters"(prefix, last_value) VALUES (%s, 1) '
                    'ON CONFLICT (prefix) DO UPDATE SET last_value = "id_counters".last_value + 1 '
                    'RETURNING last_value', (e.table + ":" + e.prefix,))
        cand = f"{e.prefix}{cur.fetchone()['last_value']}"
        cur.execute(f"SELECT 1 FROM {qt(e)} WHERE id = %s", (cand,))
        if not cur.fetchone():
            return cand
    raise ApiError(500, "Could not allocate an id.")


def _write_lists(cur, e: Entity, id_: str, lists: dict, *, replace_all: bool) -> None:
    for l in e.lists:
        if isinstance(l, Derived):
            continue
        if l.key not in lists:
            if not replace_all:
                continue
            value: Any = {} if isinstance(l, Kv) else []
        else:
            value = lists[l.key]
        if isinstance(l, Ids):
            where = f"{q(l.owner)} = %s"
            params: list = [id_]
            if l.discr:
                where += f" AND {q(l.discr[0])} = %s"; params.append(l.discr[1])
            cur.execute(f"DELETE FROM {q(l.table)} WHERE {where}", params)
            for pos, v in enumerate(value):
                cols = [l.owner, l.col, "position"]; vals = [id_, v, pos]
                if l.discr:
                    cols.append(l.discr[0]); vals.append(l.discr[1])
                cur.execute(f"INSERT INTO {q(l.table)} ({', '.join(q(c) for c in cols)}) "
                            f"VALUES ({', '.join(['%s'] * len(cols))})", vals)
        elif isinstance(l, Kv):
            cur.execute(f"DELETE FROM {q(l.table)} WHERE {q(l.owner)} = %s", (id_,))
            for k, v in value.items():
                cur.execute(f"INSERT INTO {q(l.table)} ({q(l.owner)}, {q(l.kcol)}, {q(l.vcol)}) "
                            f"VALUES (%s, %s, %s)", (id_, k, v))
        elif isinstance(l, Objs):
            cur.execute(f"DELETE FROM {q(l.table)} WHERE {q(l.owner)} = %s", (id_,))
            for pos, row in enumerate(value):
                cols = [l.owner, "position"] + list(row.keys())
                vals = [id_, pos] + list(row.values())
                cur.execute(f"INSERT INTO {q(l.table)} ({', '.join(q(c) for c in cols)}) "
                            f"VALUES ({', '.join(['%s'] * len(cols))})", vals)


def _existing(cur, e: Entity, id_: str, lock: bool = True) -> dict | None:
    cur.execute(f"SELECT * FROM {qt(e)} WHERE id = %s" + (" FOR UPDATE" if lock else ""), (id_,))
    return cur.fetchone()


def _check_scope_write(e: Entity, cols: dict, scope: str | None) -> None:
    if scope is None or isinstance(scope, tuple):   # tuple scopes are read filters (writes: policy.py)
        return
    if e.table == "employers":
        return
    if getattr(e, "scoped_by_employer", False) and "employer_id" in cols \
            and cols["employer_id"] != scope:
        raise ApiError(403, "Employer administrators may only write their own employer's records.")


def create(cur, e: Entity, body: dict, actor: str, scope: str | None = None, *,
           defer: bool = False, trusted: bool = False) -> dict:
    from . import rules
    cols, lists = validate(e, body, partial=False)
    check_formats(e, cols)
    _check_scope_write(e, cols, scope)
    if isinstance(scope, str) and getattr(e, "scoped_by_employer", False) and "employer_id" not in cols:
        raise ApiError(403, "Employer administrators must set their own employerId.")
    id_ = body.get("id")
    if id_ is None:
        if e.table in CLIENT_ID_ENTITIES:
            raise ApiError(422, "id is required for this resource.")
        id_ = next_id(cur, e)
    elif not isinstance(id_, str) or not ID_RE.match(id_):
        raise ApiError(422, "id must be 1-64 chars of [A-Za-z0-9_.:-].")
    if not trusted:
        rules.before_write(cur, e, None, id_, cols, lists, actor)
    names = ["id", "created_by", "updated_by"] + list(cols)
    vals = [id_, actor, actor] + list(cols.values())
    try:
        cur.execute("SAVEPOINT w")
        cur.execute(f"INSERT INTO {qt(e)} ({', '.join(q(n) for n in names)}) "
                    f"VALUES ({', '.join(['%s'] * len(names))})", vals)
        _write_lists(cur, e, id_, lists, replace_all=True)
        rules.after_write(cur, e, None, id_, cols, actor)
        if not defer:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
    except psycopg2.Error as exc:
        cur.execute("ROLLBACK TO SAVEPOINT w")
        raise map_db_error(exc)
    return get(cur, e, id_)


def update(cur, e: Entity, id_: str, body: dict, actor: str, *, partial: bool,
           scope: str | None = None, defer: bool = False, trusted: bool = False) -> dict:
    from . import rules
    if "id" in body and body["id"] != id_:
        raise ApiError(422, "id in body does not match the URL.")
    old = _existing(cur, e, id_)
    if not old:
        raise ApiError(404, f"{e.table} '{id_}' not found.")
    if scope is not None:
        get(cur, e, id_, scope)   # raises 404 if outside the caller's scope
    cols, lists = validate(e, {k: v for k, v in body.items() if k != "id"}, partial=partial)
    check_formats(e, cols, old)
    _check_scope_write(e, cols, scope)
    if any(c.key == "updatedAt" for c in e.cols) and "updated_at" not in cols:
        cols["updated_at"] = datetime.now(timezone.utc)
    if not trusted:
        rules.before_write(cur, e, old, id_, cols, lists, actor)
    sets = list(cols) + ["updated_by"]
    vals = list(cols.values()) + [actor]
    try:
        cur.execute("SAVEPOINT w")
        cur.execute(f"UPDATE {qt(e)} SET {', '.join(q(n) + ' = %s' for n in sets)} "
                    f"WHERE id = %s", vals + [id_])
        _write_lists(cur, e, id_, lists, replace_all=not partial)
        rules.after_write(cur, e, old, id_, cols, actor)
        if not defer:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
    except psycopg2.Error as exc:
        cur.execute("ROLLBACK TO SAVEPOINT w")
        raise map_db_error(exc)
    return get(cur, e, id_)


def _retire_logins(cur, e: Entity, id_: str) -> None:
    """A deleted employee / driver record takes its logins with it: the memberships pointing at it are
    deactivated, so a later record re-using the same id never inherits the old person's login."""
    if e.table == "employees":
        cur.execute("UPDATE public.tenant_memberships SET active = FALSE WHERE role = 'employee' "
                    "AND scope_id = %s AND tenant_id = (SELECT id FROM public.tenants "
                    "WHERE schema_name = current_schema())", (id_,))
    elif e.table == "drivers":                  # driver ids are fleet-wide: every company's driver login
        cur.execute("UPDATE public.tenant_memberships SET active = FALSE "
                    "WHERE role = 'driver' AND scope_id = %s", (id_,))


def delete(cur, e: Entity, id_: str, scope: str | None = None, *, defer: bool = False) -> None:
    if e.singleton:
        raise ApiError(405, f"{e.table} is a singleton and cannot be deleted.")
    if scope is not None:
        get(cur, e, id_, scope)
    try:
        cur.execute("SAVEPOINT w")
        cur.execute(f"DELETE FROM {qt(e)} WHERE id = %s", (id_,))
        deleted = cur.rowcount
        if deleted:
            _retire_logins(cur, e, id_)
        if not defer:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
    except psycopg2.Error as exc:
        cur.execute("ROLLBACK TO SAVEPOINT w")
        raise map_db_error(exc)
    if deleted == 0:
        raise ApiError(404, f"{e.table} '{id_}' not found.")
