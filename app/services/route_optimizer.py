"""Employee-to-vehicle assignment and pickup-sequence optimisation.

Deterministic heuristic (NOT a proven global optimum):

1. cheapest feasible insertion, from several deterministic employee orders
   (tightest deadline / longest or shortest trip first): each employee goes
   to the vehicle and pickup position that adds the least cost;
2. local search: relocate / swap pickups inside a route, move or swap
   employees between routes, and retry unassigned employees, accepting only
   strict improvements, until nothing improves or the time budget ends;
3. keep the start that seats the most employees, then the lowest cost.

Route model (home-to-office commute): depot -> pickups (optimised order) ->
drop-offs (nearest-first). Hard constraints: passenger capacity, vehicle
availability, arrival deadline, pickup window, maximum ride time, maximum
vehicle route duration. Objective:
total vehicle travel time + RIDE_WEIGHT x total passenger ride time (the ride
term stops one vehicle chaining every employee when others are free).

Travel times come from a caller-supplied ``travel(i, j)`` over point indices,
so the optimiser never knows (or fakes) where the numbers came from.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

RIDE_WEIGHT = 0.5
INF = math.inf

# travel(i, j) -> (distance_km, duration_min) or None when no route exists.
Travel = Callable[[int, int], "tuple[float, float] | None"]


@dataclass(frozen=True)
class Employee:
    id: str
    pickup: int  # point index
    dropoff: int  # point index
    arrival_deadline_min: float | None = None
    earliest_pickup_min: float | None = None
    latest_pickup_min: float | None = None


@dataclass(frozen=True)
class Vehicle:
    id: str
    depot: int  # point index
    capacity: int  # passenger seats (driver/guard already excluded)
    available_from_min: float | None = None


@dataclass
class Plan:
    vehicle: Vehicle
    order: list[str]  # employee ids in pickup order
    feasible: bool = True
    reason: str = ""
    cost: float = 0.0
    start_min: float = 0.0  # departure from depot (absolute when times are known)
    stops: list[dict] = field(default_factory=list)
    distance_km: float = 0.0
    duration_min: float = 0.0


@dataclass
class Solution:
    plans: dict[str, Plan]
    unassigned: dict[str, str]  # employee id -> reason
    method: str
    iterations: int
    objective: float


class Optimizer:
    def __init__(
        self,
        employees: list[Employee],
        vehicles: list[Vehicle],
        travel: Travel,
        *,
        boarding_min: float = 3.0,
        alighting_min: float = 1.0,
        max_ride_min: float | None = None,
        max_route_min: float | None = None,
        time_budget_s: float = 3.0,
    ):
        self.emps = {e.id: e for e in employees}
        self.vehicles = list(vehicles)
        self.travel = travel
        self.boarding = boarding_min
        self.alighting = alighting_min
        self.max_ride = max_ride_min
        self.max_route = max_route_min
        self.budget = time_budget_s

    # ------------------------------------------------------------ evaluation
    def evaluate(self, vehicle: Vehicle, order: list[str]) -> Plan:
        plan = Plan(vehicle=vehicle, order=list(order))
        if len(order) > vehicle.capacity:
            plan.feasible, plan.reason = False, "capacity"
            return plan
        if not order:
            return plan

        stops, t, dist, here = [], 0.0, 0.0, vehicle.depot
        pickup_t: dict[str, float] = {}
        for eid in order:
            leg = self.travel(here, self.emps[eid].pickup)
            if leg is None:
                plan.feasible, plan.reason = False, "no_route"
                return plan
            dist += leg[0]
            t += leg[1]
            stops.append(("pickup", eid, self.emps[eid].pickup, leg, t))
            pickup_t[eid] = t
            t += self.boarding
            here = self.emps[eid].pickup

        # Drop-offs: nearest next destination first (deterministic).
        pending = sorted(order)
        dropoff_t: dict[str, float] = {}
        while pending:
            best = None
            for eid in pending:
                point = self.emps[eid].dropoff
                leg = (0.0, 0.0) if point == here else self.travel(here, point)
                if leg is None:
                    continue
                key = (leg[1], eid)
                if best is None or key < best[0]:
                    best = (key, eid, point, leg)
            if best is None:
                plan.feasible, plan.reason = False, "no_route"
                return plan
            _, eid, point, leg = best
            dist += leg[0]
            t += leg[1]
            stops.append(("dropoff", eid, point, leg, t))
            dropoff_t[eid] = t
            t += self.alighting
            here = point
            pending.remove(eid)

        # Departure window from every hard time constraint.
        lower = vehicle.available_from_min if vehicle.available_from_min is not None else -INF
        upper, upper_reason = INF, ""
        lower_reason = "vehicle_availability" if vehicle.available_from_min is not None else ""
        for eid in order:
            e = self.emps[eid]
            if e.arrival_deadline_min is not None:
                bound = e.arrival_deadline_min - dropoff_t[eid]
                if bound < upper:
                    upper, upper_reason = bound, "deadline"
            if e.latest_pickup_min is not None:
                bound = e.latest_pickup_min - pickup_t[eid]
                if bound < upper:
                    upper, upper_reason = bound, "pickup_window"
            if e.earliest_pickup_min is not None:
                bound = e.earliest_pickup_min - pickup_t[eid]
                if bound > lower:
                    lower, lower_reason = bound, "pickup_window"
        if lower > upper + 1e-9:
            plan.feasible = False
            plan.reason = (
                "vehicle_availability"
                if lower_reason == "vehicle_availability"
                else upper_reason or lower_reason
            )
            return plan

        if self.max_route is not None and t - self.alighting > self.max_route + 1e-9:
            plan.feasible, plan.reason = False, "route_length"
            return plan

        ride_total = 0.0
        for eid in order:
            ride = dropoff_t[eid] - (pickup_t[eid] + self.boarding)
            if self.max_ride is not None and ride > self.max_ride + 1e-9:
                plan.feasible, plan.reason = False, "ride_time"
                return plan
            ride_total += ride

        # Leave as late as allowed (shortest waits); 0 when no clock is known.
        start = upper if upper < INF else (lower if lower > -INF else 0.0)
        plan.start_min = start
        plan.stops = [
            {
                "type": kind,
                "employee_id": eid,
                "point": point,
                "leg_distance_km": leg[0],
                "travel_time_min": leg[1],
                "eta_offset_min": at,
            }
            for kind, eid, point, leg, at in stops
        ]
        plan.distance_km = dist
        plan.duration_min = t - self.alighting
        plan.cost = plan.duration_min + RIDE_WEIGHT * ride_total
        return plan

    # ------------------------------------------------------------ solving
    def _best_insertion(self, eid: str, plans: dict[str, Plan]):
        best, reasons = None, {}
        for v in self.vehicles:
            current = plans[v.id]
            if len(current.order) >= v.capacity:
                # Full: say "no seat" only if a seat would actually have helped.
                solo = self.evaluate(v, [eid])
                reasons[v.id] = ["capacity" if solo.feasible else solo.reason]
                continue
            for pos in range(len(current.order) + 1):
                order = current.order[:pos] + [eid] + current.order[pos:]
                cand = self.evaluate(v, order)
                if not cand.feasible:
                    reasons.setdefault(v.id, []).append(cand.reason)
                    continue
                delta = cand.cost - current.cost
                key = (delta, v.id, pos)
                if best is None or key < best[0]:
                    best = (key, cand)
        return best, reasons

    def solve(self) -> Solution:
        """Multi-start: several deterministic insertion orders, each improved
        by local search; keep the one that seats the MOST employees (primary
        objective), then the lowest cost."""
        started = time.monotonic()

        def trip_len(e: Employee) -> float:
            leg = self.travel(e.pickup, e.dropoff) if e.pickup != e.dropoff else (0.0, 0.0)
            return leg[1] if leg else INF

        def deadline(e: Employee) -> float:
            return e.arrival_deadline_min if e.arrival_deadline_min is not None else INF

        emps = list(self.emps.values())
        orders = [
            sorted(emps, key=lambda e: (deadline(e), -trip_len(e), e.id)),  # tight, far first
            sorted(emps, key=lambda e: (deadline(e), trip_len(e), e.id)),  # tight, near first
            sorted(emps, key=lambda e: (-trip_len(e), e.id)),  # far first
            sorted(emps, key=lambda e: (trip_len(e), e.id)),  # near first
        ]
        best = None
        for k, ordered in enumerate(orders):
            share = self.budget * (k + 1) / len(orders)
            plans, unassigned, iterations = self._construct_and_improve(
                ordered, started, share
            )
            key = (len(unassigned), round(sum(p.cost for p in plans.values()), 6), k)
            if best is None or key < best[0]:
                best = (key, plans, unassigned, iterations)
            if not unassigned and time.monotonic() - started > self.budget:
                break
        _, plans, unassigned, iterations = best

        for eid in list(unassigned):  # final, specific reason
            _, reasons = self._best_insertion(eid, plans)
            unassigned[eid] = _explain(reasons, bool(self.vehicles))

        return Solution(
            plans=plans,
            unassigned=unassigned,
            method=(
                "heuristic: multi-start cheapest feasible insertion + relocate/"
                "swap local search, maximising employees seated first "
                "(deterministic; not a proven global optimum)"
            ),
            iterations=iterations,
            objective=round(sum(p.cost for p in plans.values()), 3),
        )

    def _construct_and_improve(self, ordered, started: float, budget_s: float):
        plans = {v.id: self.evaluate(v, []) for v in self.vehicles}
        unassigned: dict[str, str] = {}
        for e in ordered:
            best, _ = self._best_insertion(e.id, plans)
            if best is None:
                unassigned[e.id] = ""
            else:
                plans[best[1].vehicle.id] = best[1]

        iterations = 0
        improved = True
        while improved and time.monotonic() - started < budget_s:
            improved = False
            iterations += 1
            improved |= self._intra_route(plans)
            improved |= self._inter_route(plans)
            for eid in sorted(unassigned):
                best, _ = self._best_insertion(eid, plans)
                if best is not None:
                    plans[best[1].vehicle.id] = best[1]
                    del unassigned[eid]
                    improved = True
        return plans, unassigned, iterations

    def _intra_route(self, plans: dict[str, Plan]) -> bool:
        improved = False
        for v in self.vehicles:
            plan = plans[v.id]
            n = len(plan.order)
            for i in range(n):
                for j in range(n):
                    if i == j:
                        continue
                    order = list(plan.order)
                    if i < j:  # swap pair once, relocate both ways
                        swapped = list(order)
                        swapped[i], swapped[j] = swapped[j], swapped[i]
                        cand = self.evaluate(v, swapped)
                        if cand.feasible and cand.cost < plan.cost - 1e-9:
                            plan = plans[v.id] = cand
                            improved = True
                            continue
                    moved = list(plan.order)
                    moved.insert(j, moved.pop(i))
                    cand = self.evaluate(v, moved)
                    if cand.feasible and cand.cost < plan.cost - 1e-9:
                        plan = plans[v.id] = cand
                        improved = True
        return improved

    def _inter_route(self, plans: dict[str, Plan]) -> bool:
        improved = False
        for a in self.vehicles:
            for eid in list(plans[a.id].order):
                if eid not in plans[a.id].order:
                    continue
                pa = plans[a.id]
                without = self.evaluate(a, [x for x in pa.order if x != eid])
                if not without.feasible:
                    continue
                for b in self.vehicles:
                    if b.id == a.id:
                        continue
                    pb = plans[b.id]
                    before = pa.cost + pb.cost
                    # move
                    if len(pb.order) < b.capacity:
                        for pos in range(len(pb.order) + 1):
                            cand = self.evaluate(b, pb.order[:pos] + [eid] + pb.order[pos:])
                            if cand.feasible and without.cost + cand.cost < before - 1e-9:
                                plans[a.id], plans[b.id] = without, cand
                                improved = True
                                break
                        if eid not in plans[a.id].order:
                            break
                    # swap with each employee of b, same positions
                    done = False
                    for k, other in enumerate(pb.order):
                        ia = pa.order.index(eid)
                        new_a = list(pa.order)
                        new_a[ia] = other
                        new_b = list(pb.order)
                        new_b[k] = eid
                        ca, cb = self.evaluate(a, new_a), self.evaluate(b, new_b)
                        if ca.feasible and cb.feasible and ca.cost + cb.cost < before - 1e-9:
                            plans[a.id], plans[b.id] = ca, cb
                            improved = done = True
                            break
                    if done:
                        break
        return improved


_REASON_TEXT = {
    "capacity": "No free passenger seat",
    "deadline": "Would miss the employee's required arrival time",
    "pickup_window": "Cannot pick up inside the employee's pickup window",
    "ride_time": "Would exceed the maximum ride time",
    "route_length": "Route would exceed the maximum vehicle route duration",
    "vehicle_availability": "Vehicle is not available early enough",
    "no_route": "No road route between the vehicle and this employee's locations",
}


def _explain(reasons: dict[str, list[str]], have_vehicles: bool) -> str:
    """One clause per blocking constraint, naming the vehicles it applies to."""
    if not have_vehicles:
        return "No available vehicle"
    if not reasons:
        return "No feasible vehicle"
    priority = list(_REASON_TEXT)
    rank = lambda r: priority.index(r) if r in priority else 99  # noqa: E731
    by_reason: dict[str, list[str]] = {}
    for vehicle_id, codes in sorted(reasons.items()):
        # A vehicle's binding constraint: its most frequent failure.
        code = max(set(codes), key=lambda r: (codes.count(r), -rank(r)))
        by_reason.setdefault(code, []).append(vehicle_id)
    return "; ".join(
        f"{_REASON_TEXT.get(code, 'Infeasible')} ({', '.join(vids)})"
        for code, vids in sorted(by_reason.items(), key=lambda kv: rank(kv[0]))
    )
