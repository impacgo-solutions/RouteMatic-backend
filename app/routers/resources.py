"""Tenant-scoped CRUD for every frontend table + dashboard + trip workflow."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

import logging

from .. import policy, rbac, repo, rules
from ..config import get_settings
from ..db import tenant_cursor
from ..deps import TenantCtx, tenant_access
from ..spec import ENTITIES, ENUMS

router = APIRouter(prefix="/api/v1/t/{tenant}", tags=["tenant"])

RESERVED = {"page", "pageSize", "sort", "order", "q"}
log = logging.getLogger("routematic.access")


def available(e, ctx: TenantCtx) -> bool:
    """Is this entity reachable in the caller's scope?

    Fleet scope: fleet-owned + both-sided tables. Company scope: company + both-sided tables,
    plus fleet-owned tables (resolved through the fleet_workspace schema).
    """
    if ctx.kind == "fleet":
        return e.home in ("fleet", "both")
    return True


def row_scope(e, ctx: TenantCtx) -> str | None:
    """Row filter: employer admins see only their company; inside a company scope the
    fleet-owned per-company tables (contracts, allocations) are limited to that company."""
    personal = policy.read_scope(ctx, e)          # drivers / employees: their own personal rows
    if personal:
        return personal
    if ctx.employer_scope:
        return ctx.employer_scope
    if ctx.kind == "company" and e.home == "fleet" and e.table in ("contracts", "fleet_allocations"):
        return ctx.company_code
    return None


def _manager_vehicle_guard(cur, ctx: TenantCtx, resource: str, item_id: str, body: dict, partial: bool,
                           old: dict | None = None) -> None:
    """A Manager may update only the operational fields of a vehicle (never identity/assets)."""
    if ctx.role != "manager" or resource != "vehicles":
        return
    old = old if old is not None else repo.get(cur, ENTITIES["vehicles"], item_id)
    if not rbac.manager_vehicle_update_allowed(old, body, partial):
        raise HTTPException(403, "Managers may update a vehicle's operational fields only "
                                 "(status, odometer, battery, driver, inspection).")


def employer_guard(cur, ctx: TenantCtx, item_id: str | None, body: dict | None, delete: bool) -> None:
    """Company profile rules shared by REST and sync push (company workspaces)."""
    if delete:
        raise repo.ApiError(405, "Companies are never deleted; deactivate them instead.")
    if ctx.kind == "company" and ctx.company_code and (item_id or (body or {}).get("id")) != ctx.company_code:
        raise repo.ApiError(422, f"This workspace holds company '{ctx.company_code}' only.")
    if item_id and body and "code" in body:
        old = repo._existing(cur, ENTITIES["employers"], item_id, lock=False)
        if old is not None and body["code"] != old["code"]:
            raise repo.ApiError(422, "The company code cannot be changed (it identifies the company).")


def location_guard(ctx: TenantCtx, body: dict | None) -> None:
    """Company locations are pickup/office points; parking and depots are fleet assets."""
    from ..spec import PARKING_TYPES
    if body and body.get("type") in PARKING_TYPES:
        raise repo.ApiError(422, "Parking and depots are fleet locations; use parking_locations.")


def _need(ctx: TenantCtx, resource: str, write: bool, delete: bool = False) -> None:
    e = ENTITIES[resource]
    if not available(e, ctx):
        raise HTTPException(404, f"'{resource}' is not part of the Fleet workspace; open the company "
                                 "workspace that owns it.")
    ok = rbac.can_write(ctx.role, resource) if write else rbac.can_read(ctx.role, resource)
    if delete and not policy.may_delete(ctx, resource):
        ok = False
    if not ok:
        raise HTTPException(403, f"Role '{ctx.role}' may not {'modify' if write else 'read'} {resource}.")


def _register(resource: str) -> None:
    e = ENTITIES[resource]

    def list_items(request: Request, ctx: TenantCtx = Depends(tenant_access)) -> dict:
        _need(ctx, resource, False)
        qp = request.query_params
        def num(name: str, default: int) -> int:
            try:
                return int(qp.get(name, default))
            except ValueError:
                raise HTTPException(422, f"{name} must be an integer.")
        filters = {k: v for k, v in qp.items() if k not in RESERVED}
        hidden = policy.private_fields(ctx, e)
        if hidden & set(filters):
            raise HTTPException(403, "Filtering on other people's personal fields is not permitted.")
        # sort accepts the JSON key or the column name (fictionalEmail / fictional_email): refuse both
        if hidden and qp.get("sort") in hidden | {e.col(k).name for k in hidden if e.col(k)}:
            raise HTTPException(403, "Sorting on other people's personal fields is not permitted.")
        if hidden and qp.get("q"):
            raise HTTPException(403, "Search is not available for this list in your role; filter by name instead.")
        with tenant_cursor(ctx.slug) as cur:
            res = repo.list_(cur, e, page=num("page", 1), page_size=num("pageSize", 50),
                             sort=qp.get("sort"), order=qp.get("order", "asc"), search=qp.get("q"),
                             filters=filters, scope=row_scope(e, ctx))
            res["items"] = policy.redact(cur, ctx, e, res["items"])
            return res

    def get_item(item_id: str, ctx: TenantCtx = Depends(tenant_access)) -> dict:
        _need(ctx, resource, False)
        with tenant_cursor(ctx.slug) as cur:
            item = policy.redact(cur, ctx, e, [repo.get(cur, e, item_id, row_scope(e, ctx))])[0]
            if resource in ("drivers", "employees") and not policy.private_fields(ctx, e):
                # full personal record (fleet staff / company admin): who opened which record, no data
                log.info("personal record read: %s %s by %s (%s) in %s", resource, item_id, ctx.actor,
                         ctx.role, ctx.slug)
            return item

    def create_item(body: dict[str, Any] = Body(...), ctx: TenantCtx = Depends(tenant_access)) -> Response:
        _need(ctx, resource, True)
        if ctx.role == "manager" and resource == "vehicles":
            raise HTTPException(403, "Only a Fleet Admin can add vehicles.")
        with tenant_cursor(ctx.slug) as cur:
            if resource == "employers":
                employer_guard(cur, ctx, None, body, False)
            if resource == "locations":
                location_guard(ctx, body)
            batch = policy.Batch(cur, ctx)
            if body.get("id"):
                batch.note(e, body["id"])
            body = dict(body)
            policy.stamp(ctx, resource, body)
            item = repo.create(cur, e, body, ctx.actor, row_scope(e, ctx))
            if not body.get("id"):
                batch.pending.append((e, item["id"], None))
            batch.verify()
            policy.employer_post_check(cur, ctx, e, item["id"], row_scope(e, ctx))
            item = repo.get(cur, e, item["id"])
        return _json(item, 201)

    def replace_item(item_id: str, body: dict[str, Any] = Body(...),
                     ctx: TenantCtx = Depends(tenant_access)) -> dict:
        _need(ctx, resource, True)
        with tenant_cursor(ctx.slug) as cur:
            if resource == "employers":
                employer_guard(cur, ctx, item_id, body, False)
            if resource == "locations":
                location_guard(ctx, body)
            _manager_vehicle_guard(cur, ctx, resource, item_id, body, partial=False)
            batch = policy.Batch(cur, ctx)
            batch.note(e, item_id)
            before = repo.get(cur, e, item_id) if ctx.role == "manager" and resource == "vehicles" else None
            item = repo.update(cur, e, item_id, body, ctx.actor, partial=False, scope=row_scope(e, ctx))
            if before is not None:
                # a full PUT resets the fields it leaves out: judge the stored result, not the body
                _manager_vehicle_guard(cur, ctx, resource, item_id, item, partial=False, old=before)
            batch.verify()
            policy.employer_post_check(cur, ctx, e, item_id, row_scope(e, ctx))
            return item

    def patch_item(item_id: str, body: dict[str, Any] = Body(...),
                   ctx: TenantCtx = Depends(tenant_access)) -> dict:
        _need(ctx, resource, True)
        with tenant_cursor(ctx.slug) as cur:
            if resource == "employers":
                employer_guard(cur, ctx, item_id, body, False)
            if resource == "locations":
                location_guard(ctx, body)
            _manager_vehicle_guard(cur, ctx, resource, item_id, body, partial=True)
            batch = policy.Batch(cur, ctx)
            batch.note(e, item_id)
            item = repo.update(cur, e, item_id, body, ctx.actor, partial=True, scope=row_scope(e, ctx))
            batch.verify()
            policy.employer_post_check(cur, ctx, e, item_id, row_scope(e, ctx))
            return item

    def delete_item(item_id: str, ctx: TenantCtx = Depends(tenant_access)) -> Response:
        _need(ctx, resource, True, delete=True)
        with tenant_cursor(ctx.slug) as cur:
            if resource == "employers":
                employer_guard(cur, ctx, item_id, None, True)
            batch = policy.Batch(cur, ctx)
            batch.note(e, item_id)
            repo.delete(cur, e, item_id, row_scope(e, ctx))
            batch.verify()
        return Response(status_code=204)

    base = f"/{resource}"
    tag = [resource]
    router.add_api_route(base, list_items, methods=["GET"], tags=tag, name=f"list_{resource}")
    router.add_api_route(base, create_item, methods=["POST"], tags=tag, status_code=201,
                         name=f"create_{resource}")
    router.add_api_route(base + "/{item_id}", get_item, methods=["GET"], tags=tag, name=f"get_{resource}")
    router.add_api_route(base + "/{item_id}", replace_item, methods=["PUT"], tags=tag,
                         name=f"replace_{resource}")
    router.add_api_route(base + "/{item_id}", patch_item, methods=["PATCH"], tags=tag,
                         name=f"patch_{resource}")
    router.add_api_route(base + "/{item_id}", delete_item, methods=["DELETE"], tags=tag,
                         status_code=204, name=f"delete_{resource}")


def _json(obj: Any, status: int) -> Response:
    import json
    return Response(json.dumps(obj), status_code=status, media_type="application/json")


for _r in ENTITIES:
    _register(_r)


# --------------------------------------------------------------------------- #
# Trip workflow
# --------------------------------------------------------------------------- #
@router.post("/trips/{trip_id}/transition", tags=["trips"])
def transition_trip(trip_id: str, body: dict[str, Any] = Body(...),
                    ctx: TenantCtx = Depends(tenant_access)) -> dict:
    """Move a trip through the state machine, recording the reason in history."""
    _need(ctx, "trips", True)   # 404 in the fleet workspace
    status = body.get("status")
    if status not in ENUMS["TripStatus"]:
        raise HTTPException(422, f"status must be one of {ENUMS['TripStatus']}")
    patch: dict[str, Any] = {"status": status}
    reason = body.get("reason")
    if status == "cancelled":
        patch["cancellationReason"] = reason
    elif status in ("delayed", "needsAttention") and reason:
        patch["delayReason"] = reason
    if status == "inProgress":
        patch["actualStart"] = repo.out_value(datetime.now(timezone.utc))
    with tenant_cursor(ctx.slug) as cur:
        batch = policy.Batch(cur, ctx)
        batch.note(ENTITIES["trips"], trip_id)
        item = repo.update(cur, ENTITIES["trips"], trip_id, patch, ctx.actor, partial=True,
                           scope=ctx.employer_scope)
        batch.verify()
        if reason:
            cur.execute('UPDATE "trip_status_history" SET reason=%s WHERE id = ('
                        'SELECT max(id) FROM "trip_status_history" WHERE trip_id=%s)', (reason, trip_id))
        return item


@router.get("/trips/{trip_id}/history", tags=["trips"])
def trip_history(trip_id: str, ctx: TenantCtx = Depends(tenant_access)) -> dict:
    _need(ctx, "trips", False)
    with tenant_cursor(ctx.slug) as cur:
        # same row scope as reading the trip itself (a driver / employee: only their own trips)
        repo.get(cur, ENTITIES["trips"], trip_id, row_scope(ENTITIES["trips"], ctx))
        cur.execute('SELECT from_status AS "from", to_status AS "to", reason, changed_by AS "changedBy", '
                    'changed_at AS "changedAt" FROM "trip_status_history" WHERE trip_id=%s '
                    "ORDER BY changed_at, id", (trip_id,))
        rows = [{k: repo.out_value(v) for k, v in r.items()} for r in cur.fetchall()]
    return {"items": rows, "allowedNext": sorted(rules.TRIP_ALLOWED.get(
        rows[-1]["to"], set())) if rows else []}


@router.get("/meta/enums", tags=["meta"])
def enums(ctx: TenantCtx = Depends(tenant_access)) -> dict:
    return ENUMS


# --------------------------------------------------------------------------- #
# Dashboard: live aggregates (no static demo numbers)
# --------------------------------------------------------------------------- #
@router.get("/dashboard", tags=["dashboard"])
def dashboard(ctx: TenantCtx = Depends(tenant_access)) -> dict:
    """Live aggregates for the caller's scope (no static numbers)."""
    if ctx.role not in ("fleetManager", "system", "manager", "employerAdmin"):
        raise HTTPException(403, "The dashboard is available to administrators and managers.")
    return _dashboard_fleet(ctx) if ctx.kind == "fleet" else _dashboard_company(ctx)


def _dashboard_fleet(ctx: TenantCtx) -> dict:
    out: dict[str, Any] = {"scope": "fleet"}
    with tenant_cursor(ctx.slug) as cur:
        def one(sql_: str) -> Any:
            cur.execute(sql_)
            return list(cur.fetchone().values())[0]

        def group(sql_: str) -> dict:
            cur.execute(sql_)
            return {r["k"]: r["n"] for r in cur.fetchall()}

        out["companies"] = group("SELECT status k, count(*) n FROM companies GROUP BY status")
        out["vehicles"] = group("SELECT status k, count(*) n FROM vehicles WHERE active GROUP BY status")
        out["drivers"] = group("SELECT status k, count(*) n FROM drivers WHERE active GROUP BY status")
        out["parkingLocations"] = one("SELECT count(*) FROM locations")
        out["evFleetPercent"] = float(one(
            "SELECT coalesce(round(100.0 * count(*) FILTER (WHERE propulsion='ev') / nullif(count(*),0), 1), 0) "
            "FROM vehicles WHERE active"))
        out["openVehicleEvents"] = one("SELECT count(*) FROM vehicle_events WHERE acknowledged_at IS NULL")
        out["blockingAlcoholTests"] = one(
            "SELECT count(*) FROM alcohol_tests WHERE result <> 'pass' AND cleared_at IS NULL")
        out["activeContracts"] = one("SELECT count(*) FROM contracts WHERE status = 'active'")
        out["unreadNotifications"] = one("SELECT count(*) FROM notifications WHERE read_at IS NULL")
    return out


def _dashboard_company(ctx: TenantCtx) -> dict:
    scope = ctx.employer_scope
    emp = " WHERE employer_id = %(e)s" if scope else ""
    emp_and = " AND employer_id = %(e)s" if scope else ""
    p = {"e": scope, "tz": get_settings().business_timezone}
    out: dict[str, Any] = {"scope": "company", "company": ctx.company_code}
    with tenant_cursor(ctx.slug) as cur:
        def rows(sql_: str) -> list[dict]:
            cur.execute(sql_, p)
            return cur.fetchall()

        def one(sql_: str) -> Any:
            cur.execute(sql_, p)
            return list(cur.fetchone().values())[0]

        out["employees"] = {r["status"]: r["n"] for r in rows(
            f"SELECT status, count(*) n FROM employees{emp} GROUP BY status")}
        out["tripsByStatus"] = {r["status"]: r["n"] for r in rows(
            f"SELECT status, count(*) n FROM trips{emp} GROUP BY status")}
        out["tripsToday"] = one(
            # the business day, not the UTC day (a trip on 14 Oct in India starts 13 Oct 18:30 UTC)
            "SELECT count(*) FROM trips WHERE (service_date AT TIME ZONE %(tz)s)::date = "
            f"(now() AT TIME ZONE %(tz)s)::date{emp_and}")
        out["openIncidents"] = one(
            "SELECT count(*) FROM incidents WHERE status NOT IN ('resolvedDemo') AND NOT is_sos"
            + (" AND (trip_id IN (SELECT id FROM trips WHERE employer_id = %(e)s))" if scope else ""))
        out["openSos"] = one(
            "SELECT count(*) FROM incidents WHERE is_sos AND status <> 'resolvedDemo'"
            + (" AND (trip_id IN (SELECT id FROM trips WHERE employer_id = %(e)s))" if scope else ""))
        out["pendingBookings"] = one(
            "SELECT count(*) FROM bookings WHERE status IN ('requested','waitlisted')"
            + (" AND employee_id IN (SELECT id FROM employees WHERE employer_id = %(e)s)" if scope else ""))
        out["billing"] = {r["status"]: {"records": r["n"], "total": float(r["t"])} for r in rows(
            f"SELECT status, count(*) n, coalesce(sum(total_amount),0) t FROM billing_records{emp} "
            "GROUP BY status")}
        out["invoicedTotal"] = float(one(f"SELECT coalesce(sum(total),0) FROM invoices{emp}"))
        out["unreadNotifications"] = one("SELECT count(*) FROM notifications WHERE read_at IS NULL")
        out["distanceKmCompleted"] = float(one(
            f"SELECT coalesce(sum(distance_km),0) FROM trips WHERE status='completed'{emp_and}"))
        # fleet-level reference data is read from the fleet schema (never copied)
        out["fleetVehicles"] = one("SELECT count(*) FROM vehicles WHERE active")
    return out
