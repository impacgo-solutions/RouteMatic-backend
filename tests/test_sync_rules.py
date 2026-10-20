"""Server business rules also apply to synced changes (sync/push); set-based checks use the batch's final state."""
from .conftest import BASE


def push(client, h, changes, tenant="fleet", **extra):
    return client.post(f"{BASE}/{tenant}/sync/push", headers=h, json={"changes": changes, **extra})


def put(resource, id_, data):
    return {"resource": resource, "op": "put", "id": id_, "data": data}


def test_parking_capacity_is_checked_on_the_final_state_of_a_batch(client, world):
    fm = world["fm"]
    for p in ("PQA", "PQB"):
        assert client.post(f"{BASE}/fleet/parking_locations", headers=fm, json={
            "id": p, "code": p, "name": p, "type": "parking", "capacity": 1}).status_code == 201
    va = {"vehicleCode": "VQA", "fictionalRegistration": "KA-QA", "homeParkingId": "PQA"}
    vb = {"vehicleCode": "VQB", "fictionalRegistration": "KA-QB", "homeParkingId": "PQB"}
    assert push(client, fm, [put("vehicles", "VQA", va), put("vehicles", "VQB", vb)]).status_code == 200
    # swap the two vehicles in ONE save: each spot is momentarily "double booked" mid-batch, fine at the end
    r = push(client, fm, [put("vehicles", "VQA", {**va, "homeParkingId": "PQB"}),
                          put("vehicles", "VQB", {**vb, "homeParkingId": "PQA"})])
    assert r.status_code == 200, r.text
    # both into one spot of capacity 1: refused, nothing changed
    r = push(client, fm, [put("vehicles", "VQA", {**va, "homeParkingId": "PQA"}),
                          put("vehicles", "VQB", {**vb, "homeParkingId": "PQA"})])
    assert r.status_code == 409 and "full" in r.text
    assert client.get(f"{BASE}/fleet/vehicles/VQB", headers=fm).json()["homeParkingId"] == "PQA"
    assert client.get(f"{BASE}/fleet/vehicles/VQA", headers=fm).json()["homeParkingId"] == "PQB"


def test_trip_rules_apply_to_synced_trips(client, world):
    fm, ea = world["fm"], world["ea_a"]
    t = "alpha_corp"
    for i, typ in (("RQ-PK", "pickup"), ("RQ-OF", "office")):
        client.post(f"{BASE}/{t}/locations", headers=fm, json={"id": i, "code": i, "name": i, "type": typ,
                                                               "employerId": "ALPHA"})
    trip = {"tripCode": "TRQ1", "employerId": "ALPHA", "serviceDate": "2026-10-20T00:00:00Z",
            "scheduledStart": "2026-10-20T07:30:00Z", "originLocationId": "RQ-PK", "destinationLocationId": "RQ-OF"}
    # a company admin cannot create a trip that is already completed / without driver + vehicle when assigned
    assert push(client, ea, [put("trips", "TRQ1", {**trip, "status": "completed"})], t).status_code == 422
    assert push(client, ea, [put("trips", "TRQ1", {**trip, "status": "assigned"})], t).status_code == 422
    assert push(client, ea, [put("trips", "TRQ1", {**trip, "status": "planned"})], t).status_code == 200
    # cancelling needs a reason
    assert push(client, ea, [put("trips", "TRQ1", {**trip, "status": "cancelled"})], t).status_code == 422
    # the Fleet Admin may import an existing (completed) trip record — only as an explicit import
    done = put("trips", "TRQ2", {**trip, "tripCode": "TRQ2", "status": "completed"})
    assert push(client, fm, [done], t).status_code == 422
    assert push(client, ea, [done], t, importRecords=True).status_code == 403      # admins only
    assert push(client, fm, [done], t, importRecords=True).status_code == 200
