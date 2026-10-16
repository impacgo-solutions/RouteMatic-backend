"""Bulk sync endpoints used by the Flutter app's write-through local store.

The app keeps its on-device database as the working copy (business rules run there) and
mirrors every committed transaction here. The server decides WHERE each row lives:

* fleet-owned tables (vehicles, drivers, parking, policies, contracts, ...) -> ``fleet_workspace``
* company-owned tables (employees, trips, bookings, billing, ...)           -> ``tenant_<company>``
* ``locations`` are split by type: parking/depot -> fleet, pickup/office/... -> company
* in the Fleet workspace ``employers`` rows are written to the owning company's schema

``push`` applies a batch atomically in ONE database transaction with deferred FK checks.
Server business rules (``rules.py``: trip state machine, driver + vehicle for assigned trips, cancellation
reason, inactive resources, parking capacity, allocation overlap) apply to synced changes too; set-based
checks run on the batch's final state. Database constraints, tenant isolation, RBAC and row ownership apply.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Literal

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from .. import db, policy, rbac, repo, rules
from ..db import tenant_cursor
from ..deps import TenantCtx, tenant_access
from ..spec import ENTITIES, PARKING_TYPES
from .resources import available, employer_guard, row_scope

router = APIRouter(prefix="/api/v1/t/{tenant}/sync", tags=["sync"])


class Change(BaseModel):
    resource: str
    op: Literal["put", "delete"]
    id: str = Field(min_length=1, max_length=64)
    data: dict[str, Any] | None = None


# Upper bound of rows in one push, per role (the model allows at most 20000 for anyone).
MAX_CHANGES_PER_ROLE = {"fleetManager": 20000, "system": 20000, "employerAdmin": 20000, "manager": 5000,
                        "driver": 2000, "employee": 2000}


class PushIn(BaseModel):
    changes: list[Change] = Field(max_length=20000)
    # Fleet Admin / platform admin only: load existing records (e.g. completed trips) as they are.
    # Every other rule (permissions, ownership, capacity, overlap, references) still applies.
    importRecords: bool = False


def _forbid(role: str, resource: str) -> None:
    if not rbac.can_write(role, resource):
        raise HTTPException(403, f"Role '{role}' may not modify {resource}.")


def _fleet_company_schema(cur, code: str) -> str:
    cur.execute('SELECT t.schema_name FROM "fleet_workspace"."companies" c '
                "JOIN public.tenants t ON t.id = c.tenant_id WHERE c.code = %s AND t.status = 'ready'", (code,))
    row = cur.fetchone()
    if not row:
        raise repo.ApiError(422, f"Company '{code}' is not registered. Create it with POST /companies first.")
    return row["schema_name"]


def _put_location(cur, ctx: TenantCtx, ch: Change, scope) -> None:
    """Locations live in two tables, chosen by type (parking/depot are fleet assets)."""
    data = dict(ch.data or {})
    data["id"] = ch.id
    parking = data.get("type") in PARKING_TYPES
    target, other = (ENTITIES["parking_locations"], ENTITIES["locations"]) if parking else (
        ENTITIES["locations"], ENTITIES["parking_locations"])
    if ctx.kind == "fleet" and not parking:
        raise repo.ApiError(
            422, "Pickup and office locations belong to a company; open the company workspace to edit them.")
    _forbid(ctx.role, "parking_locations" if parking else "locations")
    # a type change moves the row between the two tables (never two rows with one id)
    # (in the Fleet workspace only the parking table is reachable, so there is nothing to move)
    if ctx.kind == "company" and repo._existing(cur, other, ch.id):
        _forbid(ctx.role, "parking_locations" if other is ENTITIES["parking_locations"] else "locations")
        repo.delete(cur, other, ch.id, None, defer=True)
    if repo._existing(cur, target, ch.id):
        repo.update(cur, target, ch.id, data, ctx.actor, partial=False,
                    scope=None if parking else scope, defer=True, trusted=False)
    else:
        repo.create(cur, target, data, ctx.actor, None if parking else scope, defer=True, trusted=False)


def _delete_location(cur, ctx: TenantCtx, ch: Change, scope) -> None:
    for name in (("locations", "parking_locations") if ctx.kind == "company" else ("parking_locations",)):
        e = ENTITIES[name]
        if repo._existing(cur, e, ch.id, lock=False):
            _forbid(ctx.role, name)
            repo.delete(cur, e, ch.id, None if name == "parking_locations" else scope, defer=True)
            return


def _manager_vehicle_after(cur, e, id_: str, before: dict) -> None:
    """A full PUT fills the fields it leaves out with defaults: compare the STORED result, not the body."""
    if not rbac.manager_vehicle_update_allowed(before, repo.get(cur, e, id_), False):
        raise HTTPException(403, "Managers may update a vehicle's operational fields only "
                                 "(status, odometer, battery, driver, inspection).")


def _employers_in_fleet(cur, ctx: TenantCtx, ch: Change) -> None:
    """Fleet workspace: company profile edits are written to the company's own schema."""
    _forbid(ctx.role, "employers")
    if ch.op == "delete":
        raise repo.ApiError(405, "Companies are never deleted; deactivate them instead.")
    schema = _fleet_company_schema(cur, ch.id)
    data = dict(ch.data or {})
    data["id"] = ch.id
    db.pin_search_path(cur, schema, "company")
    try:
        e = ENTITIES["employers"]
        if repo._existing(cur, e, ch.id):
            repo.update(cur, e, ch.id, data, ctx.actor, partial=False, defer=True, trusted=False)
        else:
            repo.create(cur, e, data, ctx.actor, None, defer=True, trusted=False)
    finally:
        db.pin_search_path(cur, db.FLEET_SCHEMA, "fleet")
    # keep the central registry's display fields in step with the company profile
    cur.execute('UPDATE "companies" SET name = coalesce(%s, name), status = %s, updated_at = now() '
                "WHERE code = %s",
                (data.get("name"), "inactive" if data.get("status") == "inactive" else "active", ch.id))


@router.post("/push")
def push(body: PushIn, ctx: TenantCtx = Depends(tenant_access)) -> dict:
    n = 0
    if len(body.changes) > MAX_CHANGES_PER_ROLE.get(ctx.role, 2000):
        # a field user's app sends a handful of rows per action; bulk loads are an admin job
        raise HTTPException(413, f"Too many changes in one upload for role '{ctx.role}' "
                                 f"(limit {MAX_CHANGES_PER_ROLE.get(ctx.role, 2000)}). Split it into smaller batches.")
    if body.importRecords and ctx.role not in ("fleetManager", "system"):
        raise HTTPException(403, "Only a Fleet Admin can import existing records.")
    with tenant_cursor(ctx.slug) as cur, rules.batch(allow_imports=body.importRecords):
        batch = policy.Batch(cur, ctx)          # drivers / employees: only their own rows
        for i, ch in enumerate(body.changes):
            try:
                if ch.resource == "locations":
                    scope = row_scope(ENTITIES["locations"], ctx)
                    if ch.op == "delete":
                        _delete_location(cur, ctx, ch, scope)
                    else:
                        _put_location(cur, ctx, ch, scope)
                elif ch.resource == "employers" and ctx.kind == "fleet":
                    _employers_in_fleet(cur, ctx, ch)
                else:
                    e = ENTITIES.get(ch.resource)
                    if e is None or ch.resource == "parking_locations":
                        raise repo.ApiError(422, f"unknown resource '{ch.resource}'.")
                    if not available(e, ctx):
                        raise repo.ApiError(
                            422, f"'{ch.resource}' belongs to a company; open that company's workspace "
                                 "(it is not stored in the Fleet workspace).")
                    _forbid(ctx.role, ch.resource)
                    if ch.op == "delete" and not policy.may_delete(ctx, ch.resource):
                        raise HTTPException(403, f"Role '{ctx.role}' may not delete {ch.resource}.")
                    scope = row_scope(e, ctx)
                    batch.note(e, ch.id)
                    if ch.resource == "employers":
                        employer_guard(cur, ctx, ch.id, ch.data, ch.op == "delete")
                    manager_vehicle_before = None
                    if ctx.role == "manager" and ch.resource == "vehicles" and ch.op == "put":
                        existing = repo._existing(cur, e, ch.id, lock=False)
                        if not existing or not rbac.manager_vehicle_update_allowed(
                                repo.get(cur, e, ch.id), dict(ch.data or {}), False):
                            raise HTTPException(403, "Managers may update a vehicle's operational fields only "
                                                     "(status, odometer, battery, driver, inspection).")
                        manager_vehicle_before = repo.get(cur, e, ch.id)
                    if ch.op == "delete":
                        if not e.singleton:
                            try:
                                repo.delete(cur, e, ch.id, scope, defer=True)
                            except repo.ApiError as exc:
                                if exc.status != 404:
                                    raise
                    else:
                        data = dict(ch.data or {})
                        data["id"] = ch.id
                        if repo._existing(cur, e, ch.id):
                            repo.update(cur, e, ch.id, data, ctx.actor, partial=False, scope=scope,
                                        defer=True, trusted=False)
                            if manager_vehicle_before is not None:
                                _manager_vehicle_after(cur, e, ch.id, manager_vehicle_before)
                        else:
                            policy.stamp(ctx, ch.resource, data)
                            repo.create(cur, e, data, ctx.actor, scope, defer=True, trusted=False)
                        policy.employer_post_check(cur, ctx, e, ch.id, scope)
            except repo.ApiError as exc:
                raise repo.ApiError(exc.status, f"changes[{i}] {ch.resource}/{ch.id}: {exc.message}",
                                    exc.details)
            n += 1
        rules.finish(cur)
        batch.verify()
        if body.importRecords:
            # the import is part of the audit trail (who, how many rows, which tables)
            tables = sorted({c.resource for c in body.changes})
            cur.execute('INSERT INTO "audit_events"(id, actor_role, actor_id, action, entity_type, entity_id, '
                        '"timestamp", summary, outcome) VALUES (%s,%s,%s,%s,%s,%s,now(),%s,%s)',
                        ("AU-IMPORT-" + uuid.uuid4().hex[:10].upper(), ctx.role, ctx.actor, "records.import",
                         "dataset", ctx.slug, f"Imported {n} existing record(s): {', '.join(tables)}"[:500],
                         "success"))
        try:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        except psycopg2.Error as exc:
            raise repo.map_db_error(exc)
    return {"applied": n}


def _list_all(cur, e, scope) -> list:
    return repo.list_all(cur, e, scope)


@router.get("/pull")
def pull(request: Request, ctx: TenantCtx = Depends(tenant_access)) -> Response:
    """Everything the caller may read in this workspace, in the app's own table names.

    Fleet workspace: fleet-owned tables + the company directory (each company's profile).
    Company workspace: that company's tables + the fleet-owned reference tables it uses.

    The response carries an ``ETag`` (hash of the body). The app polls this endpoint; when it sends
    ``If-None-Match`` and nothing changed, the answer is ``304`` with no body, so an unchanged
    workspace costs the app no download and no decoding.
    """
    body = json.dumps(jsonable_encoder(_pull_data(ctx)), separators=(",", ":")).encode()
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if etag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


def _pull_data(ctx: TenantCtx) -> dict:
    out: dict[str, list] = {}
    with tenant_cursor(ctx.slug) as cur:
        for name, e in ENTITIES.items():
            if name in ("parking_locations", "locations") or not rbac.can_read(ctx.role, name):
                continue
            if not available(e, ctx):
                continue
            out[name] = policy.redact(cur, ctx, e, _list_all(cur, e, row_scope(e, ctx)))

        # locations: company pickup/office points + fleet parking, one list for the app
        locs: list = []
        if ctx.kind == "company" and rbac.can_read(ctx.role, "locations"):
            locs += _list_all(cur, ENTITIES["locations"], row_scope(ENTITIES["locations"], ctx))
        if rbac.can_read(ctx.role, "parking_locations"):
            locs += _list_all(cur, ENTITIES["parking_locations"], None)
        if locs or rbac.can_read(ctx.role, "locations"):
            out["locations"] = locs

        if ctx.kind == "fleet" and rbac.can_read(ctx.role, "employers"):
            # company directory: one profile row per registered company, read from each schema
            cur.execute('SELECT c.code, t.schema_name FROM "companies" c '
                        "JOIN public.tenants t ON t.id = c.tenant_id WHERE t.status = 'ready' ORDER BY c.code")
            companies = cur.fetchall()
            employers: list = []
            for c in companies:
                db.pin_search_path(cur, c["schema_name"], "company")
                employers += _list_all(cur, ENTITIES["employers"], None)
            db.pin_search_path(cur, db.FLEET_SCHEMA, "fleet")
            out["employers"] = employers
    return out
