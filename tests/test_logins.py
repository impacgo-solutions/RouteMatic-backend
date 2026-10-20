"""Employer admins give employees a login for the Employee app."""
from app import db

from .conftest import BASE, login, make_user
from .test_api import seed_company, seed_fleet  # noqa: F401  (reuse the same fixtures)


def put_login(client, h, tenant, emp, **body):
    return client.put(f"{BASE}/{tenant}/employees/{emp}/login", json=body, headers=h)


def signin(client, email, tenant, role, password):
    return client.post("/api/v1/auth/signin", json={"email": email, "password": password,
                                                    "tenant": tenant, "role": role})


def test_employer_admin_creates_a_login_and_the_employee_can_sign_in(client, world):
    ea = world["ea_a"]
    if client.get(f"{BASE}/alpha_corp/employees/E1", headers=ea).status_code == 404:
        seed_company(client, world["fm"], "alpha_corp", "ALPHA")
    r = client.post(f"{BASE}/alpha_corp/employees", headers=ea, json={
        "id": "E50", "employeeCode": "E50", "employerId": "ALPHA", "name": "Login Person",
        "pickupLocationId": "ALPHA-PK", "destinationId": "ALPHA-G1"})
    assert r.status_code == 201, r.text
    r = put_login(client, ea, "alpha_corp", "E50", email="login.person@example.com", password="Emp-pass-123")
    assert r.status_code == 200 and r.json()["passwordChanged"] is True and "password" not in r.json() and "created" not in r.json()
    # the employee signs in on the single sign-in screen: company -> Employee -> email + password
    s = signin(client, "login.person@example.com", "alpha_corp", "employee", "Emp-pass-123")
    assert s.status_code == 200, s.text
    assert s.json()["membership"] == {"slug": "alpha_corp", "name": "Alpha Corp", "role": "employee",
                                      "scopeId": "E50"}
    h = {"Authorization": f"Bearer {s.json()['accessToken']}"}
    assert client.get(f"{BASE}/alpha_corp/employees", headers=h).status_code == 200
    assert client.get(f"{BASE}/beta_corp/employees", headers=h).status_code == 403       # only their company
    assert client.get(f"{BASE}/fleet/vehicles", headers=h).status_code == 403
    # the password is hashed, and exists nowhere in the company schema
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT password_hash FROM public.platform_users WHERE email='login.person@example.com'")
        assert cur.fetchone()["password_hash"].startswith("pbkdf2_sha256$")
        cur.execute("SELECT count(*) n FROM tenant_alpha_corp.employees WHERE to_jsonb(employees)::text "
                    "LIKE '%Emp-pass-123%'")
        assert cur.fetchone()["n"] == 0
    # they cannot sign in as another role / company, nor with a wrong password
    assert signin(client, "login.person@example.com", "alpha_corp", "employerAdmin", "Emp-pass-123").status_code == 403
    assert signin(client, "login.person@example.com", "beta_corp", "employee", "Emp-pass-123").status_code == 403
    assert signin(client, "login.person@example.com", "alpha_corp", "employee", "wrong-pass").status_code == 401
    info = client.get(f"{BASE}/alpha_corp/employees/E50/login", headers=ea).json()
    assert info == {"employeeId": "E50", "hasLogin": True, "email": "login.person@example.com"}


def test_password_reset_and_email_change_for_an_owned_login(client, world):
    ea = world["ea_a"]
    r = put_login(client, ea, "alpha_corp", "E50", email="login.person@example.com", password="New-pass-9999")
    assert r.status_code == 200 and r.json() == {"employeeId": "E50", "email": "login.person@example.com",
                                                 "passwordChanged": True}
    assert signin(client, "login.person@example.com", "alpha_corp", "employee", "Emp-pass-123").status_code == 401
    assert signin(client, "login.person@example.com", "alpha_corp", "employee", "New-pass-9999").status_code == 200
    # change the email only (no password) -> old password keeps working under the new email
    r = put_login(client, ea, "alpha_corp", "E50", email="renamed.person@example.com")
    assert r.status_code == 200 and r.json()["passwordChanged"] is False
    assert signin(client, "renamed.person@example.com", "alpha_corp", "employee", "New-pass-9999").status_code == 200
    assert signin(client, "login.person@example.com", "alpha_corp", "employee", "New-pass-9999").status_code == 401
    # one account per employee: a taken email is refused
    assert put_login(client, ea, "alpha_corp", "E50", email="ea.alpha@example.com").status_code == 409


def test_validation_and_authorization(client, world):
    ea, emp, fm, eb = world["ea_a"], world["emp_a"], world["fm"], world["ea_b"]
    client.post(f"{BASE}/alpha_corp/employees", headers=ea, json={
        "id": "E51", "employeeCode": "E51", "employerId": "ALPHA", "name": "No Login Yet",
        "pickupLocationId": "ALPHA-PK", "destinationId": "ALPHA-G1"})
    assert put_login(client, ea, "alpha_corp", "E51", email="not-an-email", password="Emp-pass-123").status_code == 422
    assert put_login(client, ea, "alpha_corp", "E51", email="e51@example.com", password="short").status_code == 422
    assert put_login(client, ea, "alpha_corp", "E51", email="e51@example.com").status_code == 422      # new login needs a password
    assert put_login(client, ea, "alpha_corp", "NOPE", email="x@example.com", password="Emp-pass-123").status_code == 404
    # an employee cannot create logins; another company's admin cannot either; unauthenticated neither
    assert put_login(client, emp, "alpha_corp", "E51", email="x1@example.com", password="Emp-pass-123").status_code == 403
    assert put_login(client, eb, "alpha_corp", "E51", email="x1@example.com", password="Emp-pass-123").status_code == 403
    assert client.put(f"{BASE}/alpha_corp/employees/E51/login",
                      json={"email": "x1@example.com", "password": "Emp-pass-123"}).status_code == 401
    # not available in the fleet workspace
    assert put_login(client, fm, "fleet", "E51", email="x1@example.com", password="Emp-pass-123").status_code == 404
    # a fleet admin of the company may do it too
    assert put_login(client, fm, "alpha_corp", "E51", email="e51@example.com", password="Emp-pass-123").status_code == 200


def test_existing_account_gets_access_without_touching_its_password(client, world):
    ea = world["ea_b"]
    if client.get(f"{BASE}/beta_corp/employees/BE1", headers=ea).status_code == 404:
        assert client.post(f"{BASE}/beta_corp/employees", headers=ea, json={
            "id": "BE1", "employeeCode": "BE1", "employerId": "BETA", "name": "Beta Hire",
            "pickupLocationId": "BETA-PK", "destinationId": "BETA-G1"}).status_code == 201
    # e1@example.com (Alpha employee) is also hired by Beta: gets access, keeps their own password
    r = put_login(client, ea, "beta_corp", "BE1", email="e51@example.com", password="Other-pass-123")
    assert r.status_code == 200 and "existingAccount" not in r.json() and r.json()["passwordChanged"] is False
    assert signin(client, "e51@example.com", "beta_corp", "employee", "Emp-pass-123").status_code == 200
    assert signin(client, "e51@example.com", "beta_corp", "employee", "Other-pass-123").status_code == 401
    # a shared account's password cannot be reset by one company's admin
    r = put_login(client, ea, "beta_corp", "BE1", email="e51@example.com", password="Hijack-pass-1")
    assert r.status_code == 409
    assert signin(client, "e51@example.com", "alpha_corp", "employee", "Emp-pass-123").status_code == 200
