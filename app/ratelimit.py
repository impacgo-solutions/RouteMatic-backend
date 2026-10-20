"""Brute-force protection for sign-in.

Three independent limits on FAILED attempts within ``LOGIN_WINDOW_SECONDS`` (successful sign-ins are never
counted and clear the client + email counter):

* one client + one email  (``LOGIN_MAX_FAILURES``)          - guessing one account's password
* one client, any email   (``LOGIN_IP_MAX_FAILURES``)       - password spraying across accounts
* one email, any client   (``LOGIN_ACCOUNT_MAX_FAILURES``)  - distributed guessing of one account

The client is the TCP peer address (``request.client.host``); client-supplied X-Forwarded-For is never
trusted here. Memory is bounded: only failures are stored, expired entries are swept, and the table has a
hard size cap. In-memory, per API process: a deployment with several API processes should move this to a
shared store (for example Redis).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

from .config import get_settings

MAX_KEYS = 50_000          # hard cap per table (oldest keys are dropped first)
_SWEEP_EVERY = 1_000       # failures between sweeps of expired entries

_lock = threading.Lock()
_pair: "OrderedDict[tuple[str, str], list[float]]" = OrderedDict()
_by_ip: "OrderedDict[str, list[float]]" = OrderedDict()
_by_account: "OrderedDict[str, list[float]]" = OrderedDict()
_since_sweep = 0

# kept for compatibility with code that inspects the table size
_failures = _pair


def _email(email: str) -> str:
    return (email or "").strip().lower()


def _client(client: str) -> str:
    return client or "?"


def _recent(table: OrderedDict, key, now: float, window: int) -> list[float]:
    """Live hits for ``key`` (never creates an entry; drops it when everything expired)."""
    hits = table.get(key)
    if not hits:
        return []
    live = [t for t in hits if now - t < window]
    if live:
        table[key] = live
    else:
        table.pop(key, None)
    return live


def _wait(hits: list[float], limit: int, now: float, window: int) -> int:
    if len(hits) < limit:
        return 0
    return max(1, int(window - (now - hits[-limit])))


def retry_after(client: str, email: str) -> int:
    """Seconds until this client may try this email again (0 = allowed now)."""
    s = get_settings()
    now = time.monotonic()
    w = s.login_window_seconds
    with _lock:
        return max(
            _wait(_recent(_pair, (_client(client), _email(email)), now, w), s.login_max_failures, now, w),
            _wait(_recent(_by_ip, _client(client), now, w), s.login_ip_max_failures, now, w),
            _wait(_recent(_by_account, _email(email), now, w), s.login_account_max_failures, now, w),
        )


def _add(table: OrderedDict, key, now: float) -> None:
    table.setdefault(key, []).append(now)
    table.move_to_end(key)
    while len(table) > MAX_KEYS:
        table.popitem(last=False)


def _sweep(now: float, window: int) -> None:
    for table in (_pair, _by_ip, _by_account):
        for key in [k for k, v in table.items() if not v or now - v[-1] >= window]:
            table.pop(key, None)


def failed(client: str, email: str) -> None:
    global _since_sweep
    now = time.monotonic()
    with _lock:
        _add(_pair, (_client(client), _email(email)), now)
        _add(_by_ip, _client(client), now)
        _add(_by_account, _email(email), now)
        _since_sweep += 1
        if _since_sweep >= _SWEEP_EVERY:
            _since_sweep = 0
            _sweep(now, get_settings().login_window_seconds)


def succeeded(client: str, email: str) -> None:
    with _lock:
        _pair.pop((_client(client), _email(email)), None)


def size() -> int:
    with _lock:
        return len(_pair) + len(_by_ip) + len(_by_account)


def reset() -> None:
    with _lock:
        _pair.clear()
        _by_ip.clear()
        _by_account.clear()
