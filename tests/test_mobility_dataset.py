"""End-to-end optimisation tests on the synthetic Visakhapatnam dataset
(tests/fixtures/vizag_mobility.py). Google Routes is mocked: travel times are a
fixed straight-line x 1.3 at 30 km/h, NOT real road or traffic data."""
from __future__ import annotations

import copy

import pytest

from app.services import google_routes
from app.services.google_routes import GoogleRoutesError
from app.services.simulation_engine import distance_km, simulate_routes
from tests.fixtures import vizag_mobility as data


@pytest.fixture(autouse=True)
def mocked_google(monkeypatch):
    google_routes.clear_cache()

    def matrix(points):
        return {
            (i, j): {"distance_km": distance_km(a, b) * 1.3,
                     "duration_min": distance_km(a, b) * 1.3 / 30 * 60,
                     "static_duration_min": distance_km(a, b) * 1.3 / 35 * 60}
            for i, a in enumerate(points) for j, b in enumerate(points) if i != j
        }

    def legs(waypoints):
        return [{"distance_km": 0.0, "duration_min": 0.0, "encoded_polyline": "mock"}
                for _ in waypoints[1:]]

    monkeypatch.setattr(google_routes, "compute_route_matrix", matrix)
    monkeypatch.setattr(google_routes, "compute_route_legs", legs)


def _run(name):
    return simulate_routes(**data.simulation_input(name))


def _clock(text):
    h, m = map(int, text.split(":"))
    return h * 60 + m


# ---------------------------------------------------------------- dataset sanity
def test_dataset_is_synthetic_labelled_and_sized():
    s = data.scenario("constrained")
    feasible = data.scenario("all_feasible")
    assert 20 <= len(feasible["employees"]) <= 30
    assert 4 <= len(feasible["vehicles"]) <= 6
    assert len({v["capacity"] for v in feasible["vehicles"]}) >= 3, "varied capacities"
    assert len({v["depot_id"] for v in feasible["vehicles"]}) >= 3, "varied start points"
    for record in s["employees"] + s["vehicles"] + data.DEPOTS + [data.OFFICE]:
        assert record["id"].startswith("TEST-VZG-")
    for record in s["employees"] + s["vehicles"]:
        assert data.MARK in record["notes"]
    assert all(e["email"].endswith(".routematic.test") for e in feasible["employees"])
    # every coordinate (when present) lies in the Visakhapatnam area
    for e in s["employees"]:
        if e["pickup"]:
            assert distance_km(e["pickup"], data.OFFICE["point"]) < 50


def test_scenarios_are_deterministic_copies():
    a, b = data.scenario("all_feasible"), data.scenario("all_feasible")
    assert a == b and a is not b
    a["employees"][0]["pickup"]["lat"] = 0
    assert data.scenario("all_feasible")["employees"][0]["pickup"]["lat"] != 0


# ---------------------------------------------------------------- invariants (both scenarios)
@pytest.mark.parametrize("name", list(data.SCENARIOS))
def test_capacity_uniqueness_windows_and_totals(name):
    s = data.scenario(name)
    result = _run(name)
    emp = {e["id"]: e for e in s["employees"]}
    veh = {v["id"]: v for v in s["vehicles"]}

    seated = [eid for r in result["routes"] for eid in r["employee_ids"]]
    assert len(seated) == len(set(seated)), "employee assigned twice"
    assert set(seated) | {u["employee_id"] for u in result["unassigned"]} == set(emp)
    assert not set(seated) & {u["employee_id"] for u in result["unassigned"]}

    for route in result["routes"]:
        v = veh[route["vehicle_id"]]
        assert route["assigned_count"] <= v["capacity"]
        if not v.get("available", True):
            assert route["assigned_count"] == 0
        if not route["employee_ids"]:
            continue
        assert _clock(route["departure_clock"]) >= v["available_from_min"]
        stops = route["stops"]
        assert sum(st["leg_distance_km"] for st in stops) == pytest.approx(route["distance_km"], abs=0.05)
        assert [st["eta_min"] for st in stops] == sorted(st["eta_min"] for st in stops)
        for st in stops:
            e = emp[st["employee_id"]]
            at = _clock(st["eta_clock"])
            if st["type"] == "pickup":
                if e["earliest_pickup_min"] is not None:
                    assert at >= e["earliest_pickup_min"], st
                if e["latest_pickup_min"] is not None:
                    assert at <= e["latest_pickup_min"], st
            elif e["arrival_deadline_min"] is not None:
                assert at <= e["arrival_deadline_min"], st

    s_total = sum(r["distance_km"] for r in result["routes"])
    assert result["summary"]["distance_km"] == pytest.approx(s_total, abs=0.05)
    assert result["summary"]["employees_assigned"] == len(seated)


@pytest.mark.parametrize("name", list(data.SCENARIOS))
def test_identical_inputs_give_identical_results(name):
    inputs = data.simulation_input(name)
    snapshot = copy.deepcopy(inputs)
    first, second = simulate_routes(**inputs), simulate_routes(**inputs)
    assert first == second
    assert inputs == snapshot, "the simulation mutated its inputs"


# ---------------------------------------------------------------- scenario outcomes
def test_all_feasible_scenario_seats_everyone():
    result = _run("all_feasible")
    assert result["summary"]["employees_assigned"] == 24
    assert result["unassigned"] == []


def test_constrained_scenario_fills_every_seat_then_explains_the_rest():
    s = data.scenario("constrained")
    result = _run("constrained")
    seats = sum(v["capacity"] for v in s["vehicles"] if v.get("available", True))
    # Seating as many employees as possible comes before route cost:
    # every available seat is used even though some routes get longer.
    assert seats == 13
    assert result["summary"]["employees_assigned"] == s["expect"]["assigned"] == seats

    reasons = {u["employee_id"]: u["reason"] for u in result["unassigned"]}
    for eid, expected in s["expect"]["unassigned"].items():
        options = expected if isinstance(expected, tuple) else (expected,)
        assert any(o in reasons[eid] for o in options), (eid, reasons[eid])
    regular = [eid for eid in reasons if eid.startswith("TEST-VZG-E")]
    assert regular and all("No free passenger seat" in reasons[e] for e in regular)
    assert result["vehicles_excluded"] == [
        {"vehicle_id": "TEST-VZG-V3", "reason": "In maintenance (test)"}
    ]


def test_mocked_results_are_not_presented_as_estimates_or_live_traffic():
    result = _run("all_feasible")
    # Mock stands in for Google here; the response must not call it an estimate.
    assert result["routing"]["estimate"] is False
    assert {s["distance_source"] for r in result["routes"] for s in r["stops"]} == {"google_route_matrix"}


def test_real_google_failure_is_not_hidden(monkeypatch):
    def blocked(points):
        raise GoogleRoutesError("Google Routes API returned HTTP 403",
                                kind="permission_or_billing", status=403)

    monkeypatch.setattr(google_routes, "compute_route_matrix", blocked)
    with pytest.raises(GoogleRoutesError):
        _run("all_feasible")  # default: fail, never silently estimate
    estimate = simulate_routes(**data.simulation_input("all_feasible"), allow_estimate=True)
    assert estimate["routing"]["status"] == "fallback_estimate"
    assert estimate["routing"]["error"]["status"] == 403
    assert all(s["distance_source"] == "estimate_straight_line"
               for r in estimate["routes"] for s in r["stops"])
