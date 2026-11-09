"""Deterministic native-session concurrency checks with no upstream requests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

import pytest

import app.browser_context as browser_context
from app.db import Database
from app.service import AKService
from test_online_sessions import context
from test_service import settings


@pytest.fixture
def native_system(tmp_path):
    database = Database(str(tmp_path / "native-concurrency.db"), default_concurrency=2)
    service = AKService(database, settings(tmp_path))
    try:
        yield database, service
    finally:
        service.stop()


def managed_account(database, service, name="one"):
    account = database.upsert_account({"name": name, "enabled": True})
    verified = context(account, service.settings, email=name + "@example.com",
                       user_id="user-" + name, cookie="initial-" + name)
    return database.apply_browser_context(account["id"], verified, expected_revision=0)


def response(account):
    return {"status": 200, "body": {"code": 1000, "data": {"id": str(account["id"])}}}


def test_waiting_native_request_reads_revision_after_acquiring_account_lock(
    native_system, monkeypatch
):
    database, service = native_system
    account = managed_account(database, service)
    client = service._client(account)
    account_id = account["id"]
    real_lock = browser_context._account_lock(account_id)
    lock_attempted = threading.Event()
    reads, dispatched = [], []

    @contextmanager
    def observed_lock(value):
        assert value == account_id
        lock_attempted.set()
        with real_lock:
            yield

    def read():
        current = database.get_account(account_id)
        reads.append(current["session_revision"])
        return current

    client.set_session_callbacks(
        read,
        lambda value, revision: database.apply_browser_context(
            account_id, value, expected_revision=revision, source="native_request"
        ),
    )

    def native(current, configuration, method, url, *, on_verified, **kwargs):
        with observed_lock(current["id"]):
            verified = context(current, configuration, cookie="request-verified")
            on_verified(verified)
            dispatched.append((method, url))
            return response(current), verified

    monkeypatch.setattr(browser_context, "_account_lock", observed_lock)
    monkeypatch.setattr(browser_context, "browser_json_request", native)
    with ThreadPoolExecutor(max_workers=1) as executor:
        real_lock.acquire()
        try:
            future = executor.submit(client._native_request, "POST",
                                     "https://akool.com/api/create", {}, None)
            assert lock_attempted.wait(timeout=5), "request did not reach the browser lock"
            updated = database.apply_browser_context(
                account_id, context(account, service.settings, cookie="peer-refresh"),
                expected_revision=account["session_revision"], source="native_request",
            )
            assert updated is not None
        finally:
            real_lock.release()
        result = future.result(timeout=5)
    assert result.json()["data"]["id"] == str(account_id)
    assert reads == [updated["session_revision"]]
    assert dispatched == [("POST", "https://akool.com/api/create")]


def test_postflight_session_persistence_remains_under_the_same_account_lock(
    native_system, monkeypatch
):
    database, service = native_system
    account = managed_account(database, service)
    client = service._client(account)
    account_id = account["id"]
    lock = browser_context._account_lock(account_id)
    peer_acquisitions, dispatched = [], []

    def persist(value, revision):
        # A different thread must not enter between the native fetch and the
        # postflight cookie snapshot. RLock ownership must be probed by a peer.
        acquired = []

        def probe():
            entered = lock.acquire(blocking=False)
            acquired.append(entered)
            if entered:
                lock.release()

        peer = threading.Thread(target=probe)
        peer.start()
        peer.join(timeout=5)
        assert not peer.is_alive()
        peer_acquisitions.extend(acquired)
        return database.apply_browser_context(
            account_id, value, expected_revision=revision, source="native_request"
        )

    client.set_session_callbacks(lambda: database.get_account(account_id), persist)

    def native(current, configuration, method, url, *, on_verified, **kwargs):
        with lock:
            on_verified(context(current, configuration, cookie="preflight"))
            dispatched.append((method, url))
            return response(current), context(current, configuration, cookie="postflight")

    monkeypatch.setattr(browser_context, "browser_json_request", native)
    result = client._native_request("POST", "https://akool.com/api/create", {}, None)
    assert result.json()["data"]["id"] == str(account_id)
    assert peer_acquisitions == [False, False]
    assert dispatched == [("POST", "https://akool.com/api/create")]
    assert database.get_account(account_id)["cookie_header"] == "session=postflight"


def test_native_session_lock_does_not_serialize_different_accounts(
    native_system, monkeypatch
):
    database, service = native_system
    accounts = [managed_account(database, service, name) for name in ("one", "two")]
    clients = [service._client(account) for account in accounts]
    both_entered = threading.Barrier(2)
    dispatched = []

    def native(current, configuration, method, url, *, on_verified, **kwargs):
        with browser_context._account_lock(current["id"]):
            # If request locking becomes global, neither request can pass this
            # barrier; different accounts must retain independent browser locks.
            both_entered.wait(timeout=5)
            verified = context(current, configuration, cookie="verified")
            on_verified(verified)
            dispatched.append(current["id"])
            return response(current), verified

    monkeypatch.setattr(browser_context, "browser_json_request", native)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(client._native_request, "POST",
                                   "https://akool.com/api/create", {}, None)
                   for client in clients]
        results = [future.result(timeout=10).json() for future in futures]
    assert sorted(dispatched) == sorted(account["id"] for account in accounts)
    assert {result["data"]["id"] for result in results} == {
        str(account["id"]) for account in accounts
    }
