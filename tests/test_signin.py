"""One-screen sign-in: company + role + credentials are validated together."""
from .conftest import BASE

PW = "Passw0rd!x"


def signin(client, email, tenant, role, password=PW):
    return client.post("/api/v1/auth/signin", json={"email": email, "password": password,
                                                    "tenant": tenant, "role": role})


def test_public_company_list_exposes_names_only(client, world):
    r = client.get("/api/v1/auth/organizations")       # no login needed: feeds the company selector
    assert r.status_code == 200
    items = r.json()["items"]
    assert items[0]["kind"] == "fleet" and items[0]["slug"] == "fleet"
    assert {"alpha_corp", "beta_corp"} <= {i["slug"] for i in items}
    assert all(set(i) == {"slug", "name", "kind"} for i in items)        # nothing else is disclosed


def test_valid_company_role_and_credentials(client, world):
    r = signin(client, "ea.alpha@example.com", "alpha_corp", "employerAdmin")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["membership"] == {"slug": "alpha_corp", "name": "Alpha Corp", "role": "employerAdmin",
                               "scopeId": "ALPHA"}
    # the issued token works in exactly that company
    h = {"Authorization": f"Bearer {j['accessToken']}"}
    assert client.get(f"{BASE}/alpha_corp/employers", headers=h).status_code == 200
    assert client.get(f"{BASE}/beta_corp/employers", headers=h).status_code == 403
    assert client.get(f"{BASE}/fleet/vehicles", headers=h).status_code == 403

    e = signin(client, "emp.alpha@example.com", "alpha_corp", "employee").json()
    assert e["membership"]["role"] == "employee" and e["membership"]["scopeId"] == "E1"
    f = signin(client, "fm@example.com", "fleet", "fleetManager").json()
    assert f["membership"]["role"] == "fleetManager" and f["membership"]["slug"] == "fleet"
    # a fleet admin can also sign in to a company it registered
    assert signin(client, "fm@example.com", "alpha_corp", "fleetManager").status_code == 200


def test_wrong_credentials_are_indistinguishable(client, world):
    bad_pw = signin(client, "ea.alpha@example.com", "alpha_corp", "employerAdmin", "wrong-password")
    no_user = signin(client, "nobody@example.com", "alpha_corp", "employerAdmin", "whatever-123")
    assert bad_pw.status_code == no_user.status_code == 401
    assert bad_pw.json()["error"]["message"] == no_user.json()["error"]["message"]


def test_wrong_company_or_role_is_refused(client, world):
    # right login, wrong company
    r = signin(client, "ea.alpha@example.com", "beta_corp", "employerAdmin")
    assert r.status_code == 403 and "Beta Corp" in r.json()["error"]["message"]
    # right login + company, wrong role
    r = signin(client, "ea.alpha@example.com", "alpha_corp", "fleetManager")
    assert r.status_code == 403 and "Fleet Admin" in r.json()["error"]["message"]
    assert signin(client, "ea.alpha@example.com", "alpha_corp", "employee").status_code == 403
    assert signin(client, "ea.alpha@example.com", "fleet", "employerAdmin").status_code == 403
    assert signin(client, "emp.alpha@example.com", "beta_corp", "employee").status_code == 403
    assert signin(client, "fm@example.com", "fleet", "employee").status_code == 403
    # unknown company / invalid role
    assert signin(client, "fm@example.com", "no_such_co", "fleetManager").status_code == 403
    assert signin(client, "fm@example.com", "fleet", "superuser").status_code == 422


def test_platform_admin_signs_in_as_fleet_or_employer_only(client, world):
    ok = signin(client, "root@example.com", "fleet", "fleetManager")
    assert ok.status_code == 200 and ok.json()["membership"]["role"] == "system"
    assert signin(client, "root@example.com", "alpha_corp", "employerAdmin").status_code == 200
    assert signin(client, "root@example.com", "alpha_corp", "employee").status_code == 403
    assert signin(client, "root@example.com", "alpha_corp", "driver").status_code == 403
