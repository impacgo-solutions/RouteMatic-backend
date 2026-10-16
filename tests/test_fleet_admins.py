"""fleet_workspace schema (renamed from fleet_manager) + Fleet Admin accounts in fleet_workspace.admins."""
from app import admins, db, security
from app.ddl import SHARED_STATEMENTS_0006

from .conftest import BASE, make_user

PW = "Passw0rd!x"


def signin(client, email, password=PW, tenant="fleet", role="fleetManager"):
    return client.post("/api/v1/auth/signin", json={"email": email, "password": password,
                                                    "tenant": tenant, "role": role})


def test_fleet_schema_is_fleet_workspace(test_database):
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT nspname FROM pg_namespace WHERE nspname IN ('fleet_manager','fleet_workspace')")
        assert [r["nspname"] for r in cur.fetchall()] == ["fleet_workspace"]
        cur.execute("SELECT slug, schema_name FROM public.tenants WHERE kind='fleet'")
        assert dict(cur.fetchone()) == {"slug": "fleet", "schema_name": "fleet_workspace"}
        # the company -> fleet foreign keys point into fleet_workspace
        cur.execute("SELECT count(*) n FROM pg_constraint c JOIN pg_class r ON r.oid=c.confrelid "
                    "JOIN pg_namespace n ON n.oid=r.relnamespace WHERE c.contype='f' AND n.nspname='fleet_workspace' "
                    "AND c.connamespace <> n.oid")
        assert cur.fetchone()["n"] > 0
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='fleet_workspace' "
                    "AND table_name='admins' ORDER BY ordinal_position")
        assert [r["column_name"] for r in cur.fetchall()] == [
            "id", "user_id", "name", "email", "password_hash", "role", "status", "last_login_at",
            "created_at", "updated_at"]


def test_fleet_admin_account_lives_in_admins_with_a_hashed_password(client, world):
    with db.transaction(dict_rows=True) as cur:
        a = admins.by_email(cur, "fm@example.com")
    assert a and a["id"].startswith("FA-") and a["role"] == "fleetManager" and a["status"] == "active"
    assert a["password_hash"].startswith("pbkdf2_sha256$") and PW not in a["password_hash"]
    # plain-text passwords are refused by the table itself
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SAVEPOINT s")
        try:
            cur.execute(f"UPDATE {admins.ADMINS} SET password_hash='plain' WHERE email='fm@example.com'")
            raise AssertionError("plain text accepted")
        except Exception as exc:   # CheckViolation
            assert "check" in str(exc).lower()
            cur.execute("ROLLBACK TO SAVEPOINT s")
    r = signin(client, "fm@example.com")
    assert r.status_code == 200 and r.json()["membership"]["role"] == "fleetManager"
    assert signin(client, "fm@example.com", "wrong-pass-1").status_code == 401


def test_fleet_admins_authenticate_against_the_admins_table(client, world):
    make_user("fa2@example.com", "fleetManager", "fleet")
    assert signin(client, "fa2@example.com").status_code == 200
    # the admins hash is what counts: change only the platform copy -> the old password still works there
    with db.transaction(dict_rows=True) as cur:
        cur.execute("UPDATE public.platform_users SET password_hash=%s WHERE email='fa2@example.com'",
                    (security.hash_password("Other-pass-123"),))
    assert signin(client, "fa2@example.com").status_code == 200
    assert signin(client, "fa2@example.com", "Other-pass-123").status_code == 401
    # set_password changes it everywhere (one password per person)
    with db.transaction(dict_rows=True) as cur:
        uid = admins.by_email(cur, "fa2@example.com")["user_id"]
        admins.set_password(cur, uid, security.hash_password("New-pass-1234"))
    assert signin(client, "fa2@example.com", "New-pass-1234").status_code == 200
    assert client.post("/api/v1/auth/login", json={"email": "fa2@example.com", "password": "New-pass-1234"}
                       ).status_code == 200
    assert signin(client, "fa2@example.com").status_code == 401


def test_deactivated_or_unregistered_fleet_admin_is_refused(client, world):
    make_user("fa3@example.com", "fleetManager", "fleet")
    tok = {"Authorization": f"Bearer {signin(client, 'fa3@example.com').json()['accessToken']}"}
    assert client.get(f"{BASE}/fleet/vehicles", headers=tok).status_code == 200
    with db.transaction(dict_rows=True) as cur:
        uid = admins.by_email(cur, "fa3@example.com")["user_id"]
        admins.set_status(cur, uid, False)
    r = signin(client, "fa3@example.com")
    assert r.status_code == 403 and "deactivated" in r.json()["error"]["message"]
    assert client.get(f"{BASE}/fleet/vehicles", headers=tok).status_code == 403     # existing token too
    me = client.post("/api/v1/auth/login", json={"email": "fa3@example.com", "password": PW})
    assert me.json()["memberships"] == []
    with db.transaction(dict_rows=True) as cur:
        admins.set_status(cur, uid, True)
    assert signin(client, "fa3@example.com").status_code == 200
    # a fleetManager membership without an admins account gives no Fleet Admin access
    with db.transaction(dict_rows=True) as cur:
        cur.execute(f"DELETE FROM {admins.ADMINS} WHERE user_id=%s::uuid", (uid,))
    r = signin(client, "fa3@example.com")
    assert r.status_code == 403 and "not a registered Fleet Admin" in r.json()["error"]["message"]
    assert client.get(f"{BASE}/fleet/vehicles", headers=tok).status_code == 403


def test_new_fleet_admin_members_get_an_admins_account(client, world):
    r = client.post("/api/v1/tenants/fleet/members", headers=world["fm"], json={
        "email": "fa4@example.com", "displayName": "Fourth Admin", "password": PW, "role": "fleetManager"})
    assert r.status_code == 201, r.text
    with db.transaction(dict_rows=True) as cur:
        a = admins.by_email(cur, "fa4@example.com")
    assert (a["name"], a["status"], a["role"]) == ("Fourth Admin", "active", "fleetManager")
    assert signin(client, "fa4@example.com").status_code == 200
    # other roles are unaffected: no admins row for an employer admin, and they cannot sign in as Fleet Admin
    with db.transaction(dict_rows=True) as cur:
        assert admins.by_email(cur, "ea.alpha@example.com") is None
    assert signin(client, "ea.alpha@example.com", tenant="alpha_corp", role="employerAdmin").status_code == 200
    assert signin(client, "ea.alpha@example.com").status_code == 403
    # the platform admin still enters the fleet workspace (as system), as before
    assert signin(client, "root@example.com").json()["membership"]["role"] == "system"


def test_rename_migration_preserves_an_existing_fleet_manager_schema(client, world):
    """Re-run the 0006 rename on a database laid out the old way, inside a rolled-back transaction."""
    count_sql = ("SELECT (SELECT count(*) FROM pg_class c WHERE c.relnamespace = %(ns)s::regnamespace) AS rels, "
                 "(SELECT count(*) FROM pg_constraint WHERE connamespace = %(ns)s::regnamespace) AS cons, "
                 "(SELECT count(*) FROM pg_indexes WHERE schemaname = %(name)s) AS idx, "
                 "(SELECT nspacl::text FROM pg_namespace WHERE nspname = %(name)s) AS acl")
    class Undo(Exception):
        pass

    try:
        with db.transaction(dict_rows=True) as cur:
            cur.execute("SELECT count(*) n FROM fleet_workspace.vehicles"); vehicles = cur.fetchone()["n"]
            cur.execute("SELECT count(*) n FROM fleet_workspace.admins"); n_admins = cur.fetchone()["n"]
            cur.execute(count_sql, {"ns": "fleet_workspace", "name": "fleet_workspace"}); before = dict(cur.fetchone())
            # back to the old layout
            cur.execute("ALTER TABLE public.tenants DROP CONSTRAINT ck_tenants_schema_name")
            cur.execute("ALTER SCHEMA fleet_workspace RENAME TO fleet_manager")
            cur.execute("UPDATE public.tenants SET schema_name='fleet_manager' WHERE kind='fleet'")
            # the migration
            for stmt in SHARED_STATEMENTS_0006:
                cur.execute(stmt)
            cur.execute("SELECT schema_name FROM public.tenants WHERE kind='fleet'")
            assert cur.fetchone()["schema_name"] == "fleet_workspace"
            cur.execute(count_sql, {"ns": "fleet_workspace", "name": "fleet_workspace"})
            assert dict(cur.fetchone()) == before
            cur.execute("SELECT count(*) n FROM fleet_workspace.vehicles"); assert cur.fetchone()["n"] == vehicles
            cur.execute("SELECT count(*) n FROM fleet_workspace.admins"); assert cur.fetchone()["n"] == n_admins
            # idempotent: running it again changes nothing
            for stmt in SHARED_STATEMENTS_0006:
                cur.execute(stmt)
            cur.execute(count_sql, {"ns": "fleet_workspace", "name": "fleet_workspace"})
            assert dict(cur.fetchone()) == before
            # never merges two schemas
            cur.execute("CREATE SCHEMA fleet_manager")
            cur.execute("SAVEPOINT s")
            try:
                cur.execute(SHARED_STATEMENTS_0006[1])
                raise AssertionError("merge was not refused")
            except Exception as exc:
                assert "refusing to merge" in str(exc)
                cur.execute("ROLLBACK TO SAVEPOINT s")
            raise Undo()          # roll everything back: leave the test database as it was
    except Undo:
        pass
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT nspname FROM pg_namespace WHERE nspname IN ('fleet_manager','fleet_workspace')")
        assert [r["nspname"] for r in cur.fetchall()] == ["fleet_workspace"]
