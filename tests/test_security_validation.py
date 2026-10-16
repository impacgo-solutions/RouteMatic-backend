"""Server-side format validation (mirrors the app's form checks) and logins whose record was deleted.

Every write path (REST and /sync/push) goes through repo.create / repo.update, so both are covered.
"""
from app import db

from .conftest import BASE, make_user

T = "epsilon_corp"
PW = "Passw0rd!x"


def _setup(client, world):
    if getattr(_setup, "done", False):
        return
    r = client.post(f"{BASE}/fleet/companies", headers=world["fm"], json={
        "slug": T, "employer": {"code": "EPSILON", "name": "Epsilon Corp"},
        "admin": {"email": "ea.epsilon@example.com", "displayName": "Epsilon Admin", "password": PW}})
    assert r.status_code == 201, r.text
    for i, typ in (("E-PK", "pickup"), ("E-OF", "office")):
        assert _post(client, world, "locations",
                     {"id": i, "code": i, "name": i, "type": typ, "employerId": "EPSILON"}).status_code == 201
    _setup.done = True


def _post(client, world, res, body):
    return client.post(f"{BASE}/{T}/{res}", headers=world["fm"], json=body)


def _emp(i, **extra):
    return {"id": i, "employeeCode": i, "employerId": "EPSILON", "name": f"Emp {i}",
            "pickupLocationId": "E-PK", "destinationId": "E-OF", **extra}


def _errors(r) -> str:
    return " ".join(r.json()["error"]["details"] or [])


def test_bad_email_phone_and_code_are_refused_with_the_field_named(client, world):
    _setup(client, world)
    r = _post(client, world, "employees", _emp("EV1", fictionalEmail="asha@", fictionalPhone="call me",))
    assert r.status_code == 422, r.text
    assert "fictionalEmail: enter a valid email" in _errors(r)
    assert "fictionalPhone: enter a valid phone number" in _errors(r)
    r = _post(client, world, "employees", {**_emp("EV2"), "employeeCode": "E 2/../x"})
    assert r.status_code == 422 and "employeeCode:" in _errors(r)
    # valid values (and empty optional ones) are accepted
    ok = _post(client, world, "employees", _emp("EV3", fictionalEmail="asha.rao@epsilon.example",
                                                fictionalPhone="+91 98450 12345"))
    assert ok.status_code == 201, ok.text
    assert _post(client, world, "employees", _emp("EV4")).status_code == 201


def test_bad_values_are_refused_through_sync_push_too(client, world):
    _setup(client, world)
    r = client.post(f"{BASE}/{T}/sync/push", headers=world["fm"], json={"changes": [
        {"resource": "employees", "op": "put", "id": "EV5", "data": _emp("EV5", fictionalEmail="not-an-email")}]})
    assert r.status_code == 422 and "fictionalEmail" in r.text


def test_non_finite_numbers_and_oversized_text_are_refused(client, world):
    _setup(client, world)
    # a hostile client can send the non-standard JSON literals NaN / Infinity
    for bad in ("NaN", "Infinity", "-Infinity"):
        r = client.patch(f"{BASE}/{T}/employers/EPSILON", headers={**world["fm"], "content-type": "application/json"},
                         content='{"defaultDistanceRate": %s}' % bad)
        assert r.status_code == 422 and "finite" in r.text, r.text
    r = _post(client, world, "employees", _emp("EV6", notes="x" * 2001))
    assert r.status_code == 422 and "notes: at most 2000" in _errors(r)
    r = _post(client, world, "employees", {**_emp("EV7"), "name": "N" * 161})
    assert r.status_code == 422 and "name: at most 160" in _errors(r)


def test_rows_stored_before_the_rules_stay_editable(client, world):
    """Existing data that would fail the new rules is not locked: only CHANGED values are checked."""
    _setup(client, world)
    assert _post(client, world, "employees", _emp("EV8")).status_code == 201
    with db.transaction() as cur:
        cur.execute(f'UPDATE "tenant_{T}"."employees" SET fictional_email = %s WHERE id = %s', ("legacy@bad", "EV8"))
    row = client.get(f"{BASE}/{T}/employees/EV8", headers=world["fm"])
    assert row.status_code == 200 and row.json()["fictionalEmail"] == "legacy@bad"
    # edit another field (full PUT keeps the legacy value unchanged) → accepted
    body = {k: v for k, v in row.json().items() if k not in ("createdAt", "updatedAt", "createdBy", "updatedBy")}
    r = client.put(f"{BASE}/{T}/employees/EV8", headers=world["fm"], json={**body, "name": "Renamed"})
    assert r.status_code == 200, r.text
    # changing the email to another invalid value is refused; a valid one is accepted
    assert client.patch(f"{BASE}/{T}/employees/EV8", headers=world["fm"],
                        json={"fictionalEmail": "still@bad"}).status_code == 422
    assert client.patch(f"{BASE}/{T}/employees/EV8", headers=world["fm"],
                        json={"fictionalEmail": "fixed@epsilon.example"}).status_code == 200


def test_login_of_a_deleted_employee_record_is_refused(client, world):
    _setup(client, world)
    assert _post(client, world, "employees", _emp("EV9")).status_code == 201
    make_user("ev9@example.com", "employee", T, scope="EV9")
    sign = lambda: client.post("/api/v1/auth/signin", json={  # noqa: E731
        "email": "ev9@example.com", "password": PW, "tenant": T, "role": "employee"})
    s = sign()
    assert s.status_code == 200, s.text
    tok = {"Authorization": f"Bearer {s.json()['accessToken']}"}
    assert client.get(f"{BASE}/{T}/trips", headers=tok).status_code == 200
    # the record is deleted: the old token and a new sign-in are both refused
    assert client.delete(f"{BASE}/{T}/employees/EV9", headers=world["fm"]).status_code in (200, 204)
    assert client.get(f"{BASE}/{T}/trips", headers=tok).status_code == 403
    again = sign()
    assert again.status_code == 403
    assert "deactivated" in again.text or "no longer exists" in again.text
    # re-using the id for a NEW person does not hand that person's data to the old login
    assert _post(client, world, "employees", _emp("EV9", name="New person")).status_code == 201
    assert client.get(f"{BASE}/{T}/trips", headers=tok).status_code == 403
    assert sign().status_code == 403
