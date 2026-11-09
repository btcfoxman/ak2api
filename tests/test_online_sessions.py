from __future__ import annotations

import asyncio
import json
import time

import pytest

import app.browser_context as browser_context
import app.browser_login as browser_login
from app.akool_client import AkoolAuthError, AkoolClient, AkoolSessionConflict, AkoolUpstreamError
from app.browser_context import (
    AkoolBrowserError,
    AkoolBrowserChallengeError,
    AkoolBrowserTransportError,
    browser_binding,
)
from app.browser_login import BrowserLoginManager, LoginError
from app.db import Database
from app.service import AKService
from test_service import settings


def context(
    account, configuration, *, email=None, user_id=None, cookie="fresh-browser"
):
    binding = browser_binding(account, configuration)
    return {
        "email": email or account.get("email") or "online@example.com",
        "user_id": user_id or account.get("user_id") or "user-online",
        "team_id": "team-online",
        "cookie_header": f"session={cookie}",
        "cookie_records": [
            {"name": "session", "value": cookie, "domain": ".akool.com"}
        ],
        "access_token": "fresh-token",
        "user_agent": "actual-browser-agent",
        "sec_ch_ua": '"Chromium";v="151"',
        "sec_ch_ua_platform": '"Linux"',
        "session_mode": "managed_profile",
        "browser_binding": binding,
        "profile_dir": binding["profile"],
        "cdp_port": binding["port"],
    }


@pytest.fixture
def system(tmp_path):
    database = Database(str(tmp_path / "online.db"), default_concurrency=2)
    service = AKService(database, settings(tmp_path))
    try:
        yield database, service
    finally:
        service.stop()


def test_browser_context_compare_and_swap_does_not_restore_stale_session(system):
    database, service = system
    account = database.upsert_account({"name": "one", "enabled": False})
    initial = context(account, service.settings)
    saved = database.apply_browser_context(account["id"], initial, expected_revision=0)
    assert saved["session_revision"] == 1
    assert saved["enabled"] is False
    assert (
        database.apply_browser_context(account["id"], initial, expected_revision=1)[
            "session_revision"
        ]
        == 1
    )
    database.update_account(account["id"], {"proxy_url": "http://127.0.0.1:10809"})
    assert (
        database.apply_browser_context(account["id"], initial, expected_revision=1)
        is None
    )
    latest = database.get_account(account["id"])
    assert latest["proxy_url"] == "http://127.0.0.1:10809"
    assert latest["session_revision"] == 2


@pytest.mark.parametrize(
    "changed", [{"email": "other@example.com"}, {"user_id": "another-user"}]
)
def test_browser_context_rejects_identity_drift_before_persist(system, changed):
    database, service = system
    account = database.upsert_account(
        {"name": "pinned", "email": "pinned@example.com", "user_id": "pinned-user"}
    )
    with pytest.raises(ValueError, match="does not match"):
        database.apply_browser_context(
            account["id"],
            context(account, service.settings, **changed),
            expected_revision=0,
        )
    assert database.get_account(account["id"])["session_mode"] == ""


@pytest.mark.parametrize("status", ["active", "network_error"])
def test_session_conflict_does_not_change_account_login_status(system, monkeypatch, status):
    database, service = system
    account = database.upsert_account({"name": "one", "status": status, "auto_login": True})
    client = AkoolClient(account, service.settings)

    def conflict():
        raise AkoolSessionConflict()

    monkeypatch.setattr(client, "account_state", conflict)
    monkeypatch.setattr(service, "_client", lambda _: client)
    before = database.get_account(account["id"])
    with pytest.raises(AkoolSessionConflict):
        service.check_account(account["id"], recover=False)
    after = database.get_account(account["id"])
    for key in ("status", "enabled", "last_error", "last_checked_at"):
        assert after[key] == before[key]


def test_maintenance_session_conflict_never_schedules_login(system, monkeypatch):
    database, service = system
    account = database.upsert_account({"name": "one", "enabled": True,
                                       "status": "network_error", "auto_login": True})
    logins = []

    def conflict(*args, **kwargs):
        raise AkoolSessionConflict()

    monkeypatch.setattr(service, "check_account", conflict)
    monkeypatch.setattr(service, "schedule_login", lambda account_id: logins.append(account_id))
    service._maintenance_check(account["id"])
    assert logins == []
    after = database.get_account(account["id"])
    assert after["status"] == "network_error"
    assert after["maintenance_lease_until"] == 0


def test_browser_context_cannot_adopt_an_existing_disabled_identity(system):
    database, service = system
    existing = database.upsert_account(
        {
            "name": "old",
            "email": "old@example.com",
            "user_id": "old-user",
            "enabled": False,
        }
    )
    draft = database.upsert_account({"name": "new-draft", "enabled": False})
    with pytest.raises(ValueError, match="already exists"):
        database.apply_browser_context(
            draft["id"],
            context(
                draft, service.settings, email="old@example.com", user_id="old-user"
            ),
            expected_revision=0,
        )
    assert database.get_account(existing["id"])["enabled"] is False
    assert database.get_account(draft["id"])["email"] == ""


def test_persistent_login_lease_excludes_dispatch_and_hides_secret(system):
    database, _ = system
    account = database.upsert_account(
        {"name": "ready", "status": "active", "last_balance": 100}
    )
    database.claim_browser_login(account["id"], "private-lease", int(time.time()) + 60)
    # Even a concurrent harmless status refresh cannot bypass the lease.
    database.update_account(account["id"], {"status": "active"})
    assert database.acquire_account(account["id"]) is None
    public = database.get_account(account["id"], include_secrets=False)
    assert "login_session_id" not in public
    assert "browser_binding_json" not in public
    assert "private-lease" not in json.dumps(public)
    assert not database.release_browser_login(
        account["id"], "wrong-lease", enabled=True, status="active"
    )
    assert database.release_browser_login(
        account["id"], "private-lease", enabled=True, status="active"
    )
    assert database.acquire_account(account["id"]) is not None


def test_login_refuses_unfinished_upstream_generation_even_after_slot_reset(system):
    database, _ = system
    account = database.upsert_account({"name": "recovery", "status": "active"})
    database.create_task("recover-me", {"prompt": "test", "kind": "video"})
    database.update_task(
        "recover-me", account_id=account["id"], generation_id="upstream-id"
    )
    with pytest.raises(ValueError, match="unfinished tasks"):
        database.claim_browser_login(account["id"], "lease", int(time.time()) + 60)


class NativeClient:
    def __init__(self):
        self.calls = []
        self.expressions = []
        self.closed = False

    def evaluate(self, expression):
        self.expressions.append(expression)
        if expression == "location.href":
            return "https://akool.com/zh-cn/"
        if expression == "navigator.userAgent":
            return "real-browser-agent"
        if "__akGenerationConsentPreconfirm" in expression:
            return {"ok": True, "origin": "https://akool.com", "key": "akool-generation-consent", "value": "true", "written": False}
        if "brands:navigator.userAgentData" in expression:
            return {
                "brands": [{"brand": "Chromium", "version": "151"}],
                "platform": "Linux",
            }
        if "const config=" in expression:
            return {
                "status": 200,
                "body": {"code": 1000, "data": {"id": "paid-operation"}},
                "headers": {},
            }
        raise AssertionError("unexpected browser expression")

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "Network.getAllCookies":
            return {
                "cookies": [
                    {
                        "name": "session",
                        "value": "browser-current",
                        "domain": ".akool.com",
                    }
                ]
            }
        raise AssertionError(
            "warm profile should not be navigated or receive old cookies"
        )

    def close(self):
        self.closed = True


def verified_body(email="owner@example.com", user_id="owner-id"):
    return {
        "code": 1000,
        "data": {"user": {"email": email, "_id": user_id}, "token": "token"},
    }


def test_warm_browser_is_authoritative_over_stale_database_cookies(system, monkeypatch):
    _, service = system
    client = NativeClient()
    monkeypatch.setattr(browser_context, "_open_cdp_with_recovery", lambda *a: client)
    monkeypatch.setattr(
        browser_context, "_browser_verify", lambda *a: {"body": verified_body()}
    )
    captured = browser_context.refresh_account_context(
        {"id": 1, "email": "owner@example.com", "cookie_header": "session=stale"},
        service.settings,
    )
    assert captured["cookie_header"] == "session=browser-current"
    assert captured["user_agent"] == "real-browser-agent"
    assert captured["sec_ch_ua_platform"] == '"Linux"'
    assert not any(
        method in {"Network.setCookies", "Page.navigate"} for method, _ in client.calls
    )


def test_native_json_uses_browser_credentials_and_verifies_before_paid_post(
    system, monkeypatch
):
    _, service = system
    client = NativeClient()
    account = {"id": 1, "email": "owner@example.com", "user_id": "owner-id"}
    monkeypatch.setattr(browser_context, "_open_cdp_with_recovery", lambda *a: client)
    monkeypatch.setattr(
        browser_context, "_browser_verify", lambda *a: {"body": verified_body()}
    )
    events = []
    result, captured = browser_context.browser_json_request(
        account,
        service.settings,
        "POST",
        "https://akool.com/api/create",
        payload={"prompt": "placeholder __METHOD__ stays literal"},
        headers={
            "Cookie": "stale-cookie",
            "User-Agent": "stale-agent",
            "Accept": "application/json",
        },
        on_verified=lambda value: events.append(value["email"]),
    )
    script = next(value for value in client.expressions if "const config=" in value)
    assert "credentials:'include'" in script
    assert "stale-cookie" not in script and "stale-agent" not in script
    assert "placeholder __METHOD__ stays literal" in script
    assert events == ["owner@example.com"]
    assert result["body"]["data"]["id"] == "paid-operation"
    assert captured["cookie_header"] == "session=browser-current"
    assert client.closed


def test_native_json_identity_mismatch_never_dispatches_operation(system, monkeypatch):
    _, service = system
    client = NativeClient()
    monkeypatch.setattr(browser_context, "_open_cdp_with_recovery", lambda *a: client)
    monkeypatch.setattr(
        browser_context,
        "_browser_verify",
        lambda *a: {"body": verified_body("wrong@example.com")},
    )
    with pytest.raises(AkoolBrowserError, match="different account"):
        browser_context.browser_json_request(
            {"id": 1, "email": "owner@example.com"},
            service.settings,
            "POST",
            "https://akool.com/api/create",
        )
    assert not any("const config=" in value for value in client.expressions)


@pytest.mark.parametrize(
    "url",
    [
        "https://outside.example/api",
        "http://akool.com/api",
        "https://akool.com:8443/api",
        "https://user:pass@akool.com/api",
    ],
)
def test_native_json_never_sends_cookie_to_another_origin(system, url):
    _, service = system
    with pytest.raises(AkoolBrowserError, match="HTTPS origin"):
        browser_context.browser_json_request({"id": 1}, service.settings, "GET", url)


def test_managed_profile_proxy_change_is_rejected_before_browser_contact(
    system, monkeypatch
):
    _, service = system
    account = {"id": 1}
    account["browser_binding"] = browser_binding(account, service.settings)
    account["proxy_url"] = "http://127.0.0.1:10809"
    monkeypatch.setattr(
        browser_context,
        "_open_cdp_with_recovery",
        lambda *a: pytest.fail("must not contact changed browser"),
    )
    with pytest.raises(AkoolBrowserError, match="binding changed"):
        browser_context.capture_native_context(account, service.settings)


def test_native_paid_post_stale_capture_aborts_before_business_call(
    system, monkeypatch
):
    _, service = system
    client = AkoolClient({"id": 1, "session_mode": "managed_profile"}, service.settings)
    client.set_session_callbacks(lambda: client.account, lambda context, revision: None)

    def native(*args, on_verified=None, **kwargs):
        on_verified({"cookie_header": "session=new"})
        pytest.fail("stale pre-dispatch session must abort here")

    monkeypatch.setattr(browser_context, "browser_json_request", native)
    with pytest.raises(AkoolSessionConflict) as failure:
        client._request("POST", "/api/create")
    assert failure.value.code == "ACCOUNT_SESSION_CHANGED"


def test_native_paid_post_is_not_retried_on_transport_failure(system, monkeypatch):
    _, service = system
    client = AkoolClient({"id": 1, "session_mode": "managed_profile"}, service.settings)
    calls = []

    def native(*args, **kwargs):
        calls.append(args)
        raise AkoolBrowserTransportError("connection lost")

    monkeypatch.setattr(browser_context, "browser_json_request", native)
    with pytest.raises(AkoolUpstreamError, match="connection lost"):
        client._request("POST", "/api/create")
    assert len(calls) == 1


def test_native_paid_post_keeps_acknowledged_result_if_post_capture_is_stale(
    system, monkeypatch
):
    _, service = system
    account = {"id": 1, "session_mode": "managed_profile", "session_revision": 0}
    client = AkoolClient(account, service.settings)
    observations = []

    def save(value, revision):
        observations.append(revision)
        return {**account, "session_revision": 1} if len(observations) == 1 else None

    client.set_session_callbacks(lambda: account, save)
    calls = []

    def native(*args, on_verified=None, **kwargs):
        calls.append(args)
        on_verified({"cookie_header": "session=new"})
        return {"status": 200, "body": {"code": 1000, "data": {"id": "accepted"}}}, {
            "cookie_header": "session=new"
        }

    monkeypatch.setattr(browser_context, "browser_json_request", native)
    body, _ = client._request("POST", "/api/create")
    assert body["data"]["id"] == "accepted"
    assert len(calls) == 1 and observations == [0, 1]


@pytest.mark.parametrize(
    "error_class,message",
    [
        (AkoolBrowserError, "TypeError: Failed to fetch"),
        (AkoolBrowserError, "AbortError: signal is aborted"),
        (AkoolBrowserTransportError, "WebSocket closed during submission"),
    ],
)
def test_service_does_not_recover_or_replay_paid_post_after_verified_dispatch(
    system, monkeypatch, error_class, message
):
    _, service = system
    account = {"id": 1, "session_mode": "managed_profile"}
    client = AkoolClient(account, service.settings)
    submitted, recoveries = [], []

    def native(_account, _settings, method, url, *, on_verified, **kwargs):
        assert method == "POST" and url.endswith("/createBySourcePrompt/batch")
        on_verified({"cookie_header": "session=verified"})
        submitted.append(kwargs["payload"])
        raise error_class(message)

    def recover(_account):
        recoveries.append(_account)
        return client

    monkeypatch.setattr(browser_context, "browser_json_request", native)
    monkeypatch.setattr(service, "_recover_client", recover)
    with pytest.raises(AkoolUpstreamError) as failure:
        service._with_recovery(
            account, client, lambda active: active.generate({"prompt": "fixture"})
        )
    assert failure.value.code == "NETWORK_ERROR"
    assert len(submitted) == 1
    assert recoveries == []


@pytest.mark.parametrize("preflight_failure", ["login", "stale-cas"])
def test_service_recovers_only_auth_failures_before_verified_dispatch(
    system, monkeypatch, preflight_failure
):
    _, service = system
    account = {"id": 1, "session_mode": "managed_profile"}
    first = AkoolClient(account, service.settings)
    recovered = AkoolClient(account, service.settings)
    if preflight_failure == "stale-cas":
        first.set_session_callbacks(lambda: account, lambda *_: None)
    attempts, submitted, recoveries = [], [], []

    def native(_account, _settings, method, url, *, on_verified, **kwargs):
        attempts.append(method)
        if len(attempts) == 1 and preflight_failure == "login":
            raise AkoolBrowserChallengeError("browser session requires login")
        on_verified({"cookie_header": "session=verified"})
        submitted.append(kwargs["payload"])
        return {
            "status": 200,
            "body": {"code": 1000, "data": {"successList": [{"_id": "accepted"}]}},
        }, {}

    def recover(_account):
        recoveries.append(_account)
        return recovered

    monkeypatch.setattr(browser_context, "browser_json_request", native)
    monkeypatch.setattr(service, "_recover_client", recover)
    if preflight_failure == "stale-cas":
        with pytest.raises(AkoolSessionConflict):
            service._with_recovery(
                account, first, lambda item: item.generate({"prompt": "fixture"})
            )
        assert len(attempts) == 1 and submitted == [] and recoveries == []
        return
    result, active = service._with_recovery(
        account, first, lambda item: item.generate({"prompt": "fixture"})
    )
    assert result["generationId"] == "accepted" and active is recovered
    assert len(attempts) == 2 and len(submitted) == 1 and len(recoveries) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_explicit_http_auth_rejection_keeps_existing_recovery(
    system, monkeypatch, status
):
    _, service = system
    account = {"id": 1, "session_mode": "managed_profile"}
    client = AkoolClient(account, service.settings)
    responses, recoveries = [], []

    def native(_account, _settings, method, _url, *, on_verified, **kwargs):
        on_verified({"cookie_header": "session=verified"})
        responses.append(method)
        if len(responses) == 1:
            return {
                "status": status,
                "body": {"message": "unauthorized" if status == 401 else "forbidden"},
            }, {}
        return {
            "status": 200,
            "body": {"code": 1000, "data": {"successList": [{"_id": "accepted"}]}},
        }, {}

    def recover(_account):
        recoveries.append(_account)
        return client

    monkeypatch.setattr(browser_context, "browser_json_request", native)
    monkeypatch.setattr(service, "_recover_client", recover)
    result, _ = service._with_recovery(
        account, client, lambda item: item.generate({"prompt": "fixture"})
    )
    assert result["generationId"] == "accepted"
    assert responses == ["POST", "POST"] and len(recoveries) == 1


class Desktop:
    @staticmethod
    def available(*args):
        return True

    def __init__(self, profile, proxy, *, cdp_port=None, **kwargs):
        self.profile, self.proxy, self.cdp_port = profile, proxy, cdp_port
        self.password, self.vnc_port = None, None
        self.running = False
        self.starts = 0

    async def start(self):
        self.running = True
        self.starts += 1
        self.password, self.vnc_port = "viewer-password", 49999

    async def revoke_viewer(self):
        self.password, self.vnc_port = None, None

    async def close(self):
        self.running = False
        await self.revoke_viewer()

    def alive(self):
        return self.running


@pytest.fixture
def manager(system, monkeypatch):
    database, service = system
    monkeypatch.setattr(browser_login, "LoginDesktop", Desktop)
    monkeypatch.setattr(browser_login, "stop_idle_account_browser", lambda *a: False)
    monkeypatch.setattr(
        browser_login,
        "capture_native_context",
        lambda account, config: context(account, config),
    )

    def checked(account_id, **kwargs):
        database.update_account(account_id, {"last_balance": 100, "status": "active"})
        return database.get_account(account_id, include_secrets=False)

    monkeypatch.setattr(service, "check_account", checked)
    return BrowserLoginManager(database, service)


def test_online_draft_is_disabled_until_identity_and_balance_are_verified(manager):
    async def scenario():
        draft = manager.create_pending({"name": "new-online", "use_proxy_pool": False})
        assert not draft["enabled"] and draft["status"] == "login_required"
        started = await manager.start(draft["id"])
        sid = started["active"]["session_id"]
        assert manager.service.status()["interactive_browser_count"] == 1
        assert manager.service.schedule_login(draft["id"]) is False
        result = await manager.finish(sid)
        assert result["id"] == draft["id"] and result["enabled"]
        assert result["session_mode"] == "managed_profile"
        assert "login_session_id" not in result
        desktop = manager.desktops[draft["id"]]
        assert desktop.alive() and desktop.vnc_port is None
        second = await manager.start(draft["id"])
        assert manager.desktops[draft["id"]] is desktop and desktop.starts == 2
        await manager.cancel(second["active"]["session_id"])
        await manager.close()

    asyncio.run(scenario())


def test_finish_revokes_interactive_control_before_releasing_account_lease(manager):
    async def scenario():
        account = manager.store.upsert_account(
            {"name": "finish-ready", "enabled": True, "status": "active"}
        )
        info = await manager.start(account["id"])
        sid = info["active"]["session_id"]
        lease = manager.active
        ticket = lease["ticket"]
        entered, release = asyncio.Event(), asyncio.Event()
        original = lease["desktop"].revoke_viewer

        async def delayed_revoke():
            entered.set()
            await release.wait()
            await original()

        lease["desktop"].revoke_viewer = delayed_revoke
        finishing = asyncio.create_task(manager.finish(sid))
        try:
            await entered.wait()
            assert manager.viewer(ticket) is None
            assert manager.store.get_account(account["id"])["login_lease_until"] > int(time.time())
            assert manager.store.acquire_account(account["id"]) is None
            assert account["id"] in manager.service._online_browser_accounts
            assert manager.service.status()["interactive_browser_count"] == 1
        finally:
            release.set()
            await finishing
            manager.store.release_account(account["id"])
            await manager.close()
        assert manager.store.get_account(account["id"])["login_lease_until"] == 0
        assert manager.active is None

    asyncio.run(scenario())


def test_finish_viewer_revoke_failure_retains_lease_and_verified_session(manager):
    async def scenario():
        draft = manager.create_pending({"name": "finish-retry", "use_proxy_pool": False})
        started = await manager.start(draft["id"])
        sid = started["active"]["session_id"]
        lease = manager.active
        original = lease["desktop"].revoke_viewer

        async def failed_revoke():
            raise RuntimeError("synthetic viewer shutdown failed")

        lease["desktop"].revoke_viewer = failed_revoke
        try:
            with pytest.raises(LoginError, match="viewer.*close") as failure:
                await manager.finish(sid)
            assert failure.value.status == 503
            current = manager.store.get_account(draft["id"])
            assert current["login_lease_until"] > int(time.time())
            assert current["online_draft"] == 1 and not current["enabled"]
            assert current["status"] != "login_required" and current["last_error"] == ""
            assert lease["last_identity_ok"] and lease["phase"] == "login"
            assert draft["id"] in manager.service._online_browser_accounts
            assert manager.store.acquire_account(draft["id"]) is None
            lease["desktop"].revoke_viewer = original
            saved = await manager.finish(sid)
            assert saved["enabled"] and saved["online_draft"] == 0
            assert manager.active is None
        finally:
            lease["desktop"].revoke_viewer = original
            await manager.close()

    asyncio.run(scenario())


def test_cancelled_finish_keeps_account_leased_until_viewer_can_close(manager):
    async def scenario():
        account = manager.store.upsert_account(
            {"name": "finish-cancelled", "enabled": True, "status": "active"}
        )
        started = await manager.start(account["id"])
        sid = started["active"]["session_id"]
        lease = manager.active
        original = lease["desktop"].revoke_viewer
        entered = asyncio.Event()

        async def interrupted_revoke():
            entered.set()
            await asyncio.Event().wait()

        lease["desktop"].revoke_viewer = interrupted_revoke
        finishing = asyncio.create_task(manager.finish(sid))
        try:
            await entered.wait()
            finishing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await finishing
            current = manager.store.get_account(account["id"])
            assert current["login_lease_until"] > int(time.time())
            assert current["last_error"] == "" and lease["last_identity_ok"]
            assert lease["phase"] == "login"
            assert account["id"] in manager.service._online_browser_accounts
            assert manager.store.acquire_account(account["id"]) is None
            lease["desktop"].revoke_viewer = original
            await manager.finish(sid)
            assert manager.active is None
            assert manager.store.get_account(account["id"])["login_lease_until"] == 0
        finally:
            lease["desktop"].revoke_viewer = original
            await manager.close()

    asyncio.run(scenario())


def test_cancel_revokes_interactive_control_before_releasing_account_lease(manager):
    async def scenario():
        account = manager.store.upsert_account(
            {"name": "closing-ready", "enabled": True, "status": "active"}
        )
        info = await manager.start(account["id"])
        sid = info["active"]["session_id"]
        lease = manager.active
        ticket = lease["ticket"]
        entered, release = asyncio.Event(), asyncio.Event()
        original = lease["desktop"].revoke_viewer

        async def delayed_revoke():
            entered.set()
            await release.wait()
            await original()

        lease["desktop"].revoke_viewer = delayed_revoke
        closing = asyncio.create_task(manager.cancel(sid))
        await entered.wait()
        assert manager.viewer(ticket) is None
        assert manager.store.get_account(account["id"])["login_lease_until"] > int(
            time.time()
        )
        assert manager.service.status()["interactive_browser_count"] == 1
        release.set()
        await closing
        assert manager.store.get_account(account["id"])["login_lease_until"] == 0
        assert manager.active is None
        await manager.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["finish", "cancel"])
def test_old_low_balance_account_stays_disabled_after_online_login(manager, action):
    async def scenario():
        account = manager.store.upsert_account(
            {
                "name": "disabled-old",
                "enabled": False,
                "status": "disabled_low_balance",
                "last_balance": 0,
            }
        )
        started = await manager.start(account["id"])
        await getattr(manager, action)(started["active"]["session_id"])
        latest = manager.store.get_account(account["id"])
        assert not latest["enabled"] and latest["status"] == "disabled_low_balance"
        await manager.close()

    asyncio.run(scenario())


def test_online_cancel_with_changed_identity_disables_previous_active_account(
    manager, monkeypatch
):
    async def scenario():
        account = manager.store.upsert_account(
            {
                "name": "previously-ready",
                "email": "correct@example.com",
                "enabled": True,
                "status": "active",
            }
        )
        started = await manager.start(account["id"])
        monkeypatch.setattr(
            browser_login,
            "capture_native_context",
            lambda account, config: context(
                account, config, email="different@example.com"
            ),
        )
        await manager.cancel(started["active"]["session_id"])
        latest = manager.store.get_account(account["id"])
        assert not latest["enabled"] and latest["status"] == "login_required"
        assert latest["email"] == "correct@example.com"
        assert not manager.service._online_browser_accounts
        await manager.close()

    asyncio.run(scenario())


def test_draft_can_be_finished_after_cancel_and_reopen(manager):
    async def scenario():
        draft = manager.create_pending(
            {"name": "try-login-twice", "use_proxy_pool": False}
        )
        first = await manager.start(draft["id"])
        await manager.cancel(first["active"]["session_id"])
        assert manager.store.get_account(draft["id"])["online_draft"] == 1
        second = await manager.start(draft["id"])
        result = await manager.finish(second["active"]["session_id"])
        assert result["enabled"] and result["online_draft"] == 0
        await manager.close()

    asyncio.run(scenario())


def test_online_login_rejects_active_task_and_binding_changes(manager):
    async def scenario():
        account = manager.store.upsert_account(
            {"name": "in-use", "status": "active", "last_balance": 100}
        )
        assert manager.store.acquire_account(account["id"])
        with pytest.raises(LoginError, match="active tasks"):
            await manager.start(account["id"])
        manager.store.release_account(account["id"])
        started = await manager.start(account["id"])
        with pytest.raises(ValueError, match="busy"):
            manager.service.update_account(
                account["id"], {"proxy_url": "http://127.0.0.1:10809"}
            )
        with pytest.raises(ValueError):
            manager.service.reset_account_profile(
                account["id"], proxy_url=None, use_proxy_pool=False, start_login=False
            )
        with pytest.raises(LoginError, match="already open"):
            await manager.start(account["id"])
        await manager.cancel(started["active"]["session_id"])
        await manager.close()

    asyncio.run(scenario())


def test_old_manual_browser_lease_is_visible_for_idle_deployment(system, monkeypatch):
    database, service = system
    import app.service as service_module

    monkeypatch.setattr(
        service_module, "browser_snapshot", lambda *a, **k: {"image": "snapshot"}
    )
    monkeypatch.setattr(
        service_module,
        "capture_browser_session",
        lambda *a: (_ for _ in ()).throw(AkoolBrowserError("not logged in")),
    )
    account = database.upsert_account({"name": "manual", "enabled": False})
    service.open_manual_browser(account["id"])
    assert database.get_account(account["id"])["login_lease_until"] > time.time()
    assert service.status()["interactive_browser_count"] == 1
    service.close_manual_browser(account["id"])
    assert database.get_account(account["id"])["login_lease_until"] == 0
    assert not database.get_account(account["id"])["enabled"]


def test_maintenance_is_persistently_leased_before_browser_or_network_work(
    system, monkeypatch
):
    database, service = system
    account = database.upsert_account(
        {
            "name": "maintenance",
            "enabled": True,
            "status": "active",
            "last_balance": 100,
        }
    )
    service.settings.daily_checkin_enabled = False

    def check(account_id, **kwargs):
        assert database.get_account(account_id)["maintenance_lease_until"] > time.time()
        assert database.acquire_account(account_id) is None
        with pytest.raises(ValueError, match="maintenance"):
            database.claim_browser_login(
                account_id, "login-too-early", int(time.time()) + 60
            )
        return database.get_account(account_id, include_secrets=False)

    monkeypatch.setattr(service, "check_account", check)
    service._maintenance_check(account["id"])
    assert database.get_account(account["id"])["maintenance_lease_until"] == 0
