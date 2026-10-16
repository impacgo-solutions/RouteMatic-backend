"""Per-role upload cap on /sync/push: a field user cannot post a bulk batch."""
from .conftest import BASE


def _changes(n):
    return [{"resource": "notifications", "op": "delete", "id": f"N-CAP-{i}"} for i in range(n)]


def test_employee_bulk_push_is_refused_before_any_work(client, world):
    r = client.post(f"{BASE}/alpha_corp/sync/push", json={"changes": _changes(2001)}, headers=world["emp_a"])
    assert r.status_code == 413, r.text
    assert "limit 2000" in r.json()["error"]["message"]


def test_admin_may_send_a_large_batch(client, world):
    # 2,001 deletes of rows that do not exist: accepted (deletes of missing rows are ignored)
    r = client.post(f"{BASE}/alpha_corp/sync/push", json={"changes": _changes(2001)}, headers=world["ea_a"])
    assert r.status_code != 413, r.text
