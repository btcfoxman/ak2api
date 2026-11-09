"""Local agreement preparation never clicks/resumes or submits generation."""
from __future__ import annotations

import asyncio
import json

import pytest

import app.browser_context as browser_context
import app.browser_login as browser_login
import app.service as service_module
from app.akool_client import AkoolUpstreamError
from app.browser_context import (
    AkoolBrowserConsentError, AkoolBrowserError,
    GENERATION_CONSENT_HASH, GENERATION_CONSENT_KEY, GENERATION_CONSENT_ORIGIN,
    GENERATION_RESULT_PATH, _verified_context, ensure_generation_consent,
)
from app.browser_login import LoginError
from test_online_sessions import NativeClient, manager, system, verified_body


def consent_result(**changes):
    return {"ok": True, "origin": GENERATION_CONSENT_ORIGIN, "key": GENERATION_CONSENT_KEY,
            "value": "true", "written": False, **changes}


class ConsentClient(NativeClient):
    def __init__(self, result=None):
        super().__init__()
        self.result = consent_result() if result is None else result

    def evaluate(self, expression):
        if "__akGenerationConsentPreconfirm" in expression:
            self.expressions.append(expression)
            if isinstance(self.result, Exception):
                raise self.result
            return self.result
        return super().evaluate(expression)


def owner(**changes):
    return {"id": 1, "email": "owner@example.com", "user_id": "owner-id", **changes}


@pytest.mark.parametrize("written", [False, True])
def test_preconfirmation_only_returns_a_local_mechanism_receipt(written):
    client = ConsentClient(consent_result(written=written))
    receipt = ensure_generation_consent(client)
    assert receipt["written"] is written
    assert receipt["mechanism"] == "browser_local_storage"
    assert receipt["server_acknowledgement"] is False
    assert receipt["mechanism_hash"].startswith("local-storage:sha256:")
    assert receipt["key"] == "akool-generation-consent"
    script = client.expressions[0]
    assert script.index("if (existing === 'true')") < script.index("localStorage.setItem")
    assert script.count("localStorage.setItem") == 1
    for forbidden in ("fetch(", ".click(", "requestSubmit(", ".resume(", "Network.setCookies"):
        assert forbidden not in script
    assert client.calls == []


@pytest.mark.parametrize("result", [
    {"ok": False, "reason": "storage_unavailable"},
    {"ok": False, "reason": "origin_mismatch"},
    consent_result(value="false", written=True),
    consent_result(origin="https://akool.com.example"),
    consent_result(origin="http://akool.com"),
    consent_result(key="other-marker"),
    consent_result(value=True),
    "true",
    RuntimeError("private synthetic storage diagnostic"),
])
def test_unconfirmed_storage_is_a_typed_non_auth_failure(result):
    client = ConsentClient(result)
    with pytest.raises(AkoolBrowserConsentError) as failure:
        ensure_generation_consent(client)
    assert failure.value.code == "AGREEMENT_REQUIRED"
    assert failure.value.status_code == 503
    assert "private" not in str(failure.value)
    assert client.calls == []


@pytest.mark.parametrize("account,body", [
    (owner(email="different@example.com"), verified_body()),
    (owner(user_id="different-user"), verified_body()),
    (owner(user_id=""), {"data": {"user": {"email": "owner@example.com"}}}),
])
def test_identity_is_validated_before_any_storage_or_cookie_access(system, account, body):
    _, service = system
    client = ConsentClient()
    with pytest.raises(AkoolBrowserError):
        _verified_context(client, account, service.settings, body)
    assert client.expressions == [] and client.calls == []


def test_failed_preparation_never_reads_or_overwrites_cookies(system):
    _, service = system
    client = ConsentClient({"ok": False})
    with pytest.raises(AkoolBrowserConsentError):
        _verified_context(client, owner(), service.settings, verified_body())
    assert client.calls == []


def test_cold_login_does_not_swallow_consent_failure_or_repeat_login(system, monkeypatch):
    _, service = system

    class ColdClient(ConsentClient):
        def evaluate(self, expression):
            if expression == "location.href":
                self.expressions.append(expression)
                return "about:blank"
            return super().evaluate(expression)

        def call(self, method, params=None):
            if method == "Page.navigate":
                self.calls.append((method, params))
                return {}
            return super().call(method, params)

    client = ColdClient({"ok": False})
    monkeypatch.setattr(browser_context, "_open_cdp_with_recovery", lambda *a: client)
    monkeypatch.setattr(browser_context, "_browser_verify", lambda *a: {"body": verified_body()})
    monkeypatch.setattr(browser_context, "_attempt_login", lambda *a: pytest.fail("valid identity must not repeat login because storage is blocked"))
    with pytest.raises(AkoolBrowserConsentError):
        browser_context.refresh_account_context(owner(session_mode="managed_profile"), service.settings)
    assert [method for method, _ in client.calls] == ["Page.navigate"]
    assert client.closed


def test_existing_receipt_keeps_its_first_local_observation_time(system):
    _, service = system
    account = owner(tos_hash=GENERATION_CONSENT_HASH, tos_accepted_at="2026-11-09T01:00:00Z")
    captured = _verified_context(ConsentClient(), account, service.settings, verified_body())
    assert captured["tos_hash"] == GENERATION_CONSENT_HASH
    assert captured["tos_accepted_at"] == account["tos_accepted_at"]
    assert captured["generation_consent_receipt"]["server_acknowledgement"] is False


def test_read_only_capture_omits_new_receipt_without_erasing_existing_one(system):
    db, service = system
    account = db.upsert_account({"name": "pinned", "email": "owner@example.com", "user_id": "owner-id"})
    initial = _verified_context(ConsentClient(), account, service.settings, verified_body())
    saved = db.apply_browser_context(account["id"], initial, expected_revision=account["session_revision"])
    read_client = ConsentClient(RuntimeError("storage writes must not occur during result query"))
    captured = _verified_context(read_client, saved, service.settings, verified_body(), initialize_consent=False)
    assert not any("__akGenerationConsentPreconfirm" in x for x in read_client.expressions)
    assert "tos_hash" not in captured and "tos_accepted_at" not in captured
    latest = db.apply_browser_context(account["id"], captured, expected_revision=saved["session_revision"])
    assert latest["tos_hash"] == saved["tos_hash"]
    assert latest["tos_accepted_at"] == saved["tos_accepted_at"]
    assert latest["session_revision"] == saved["session_revision"]


def test_receipt_and_session_share_the_same_compare_and_swap(system):
    db, service = system
    account = db.upsert_account({"name": "pinned", "email": "owner@example.com", "user_id": "owner-id"})
    captured = _verified_context(ConsentClient(), account, service.settings, verified_body())
    db.update_account(account["id"], {"proxy_url": "http://proxy.example:10809"})
    before = db.get_account(account["id"])
    assert db.apply_browser_context(account["id"], captured, expected_revision=account["session_revision"]) is None
    after = db.get_account(account["id"])
    assert after["cookie_header"] == before["cookie_header"]
    assert after["tos_hash"] == before["tos_hash"] == ""
    assert after["tos_accepted_at"] == before["tos_accepted_at"] == ""


def test_receipt_cannot_be_committed_for_a_different_identity(system):
    db, service = system
    account = db.upsert_account({"name": "pinned", "email": "owner@example.com", "user_id": "owner-id"})
    captured = _verified_context(ConsentClient(), account, service.settings, verified_body())
    captured["email"] = "different@example.com"
    with pytest.raises(ValueError, match="identity does not match"):
        db.apply_browser_context(account["id"], captured, expected_revision=account["session_revision"])
    assert db.get_account(account["id"])["tos_hash"] == ""
    assert db.get_account(account["id"])["cookie_header"] == ""


def test_native_submission_does_not_reach_business_fetch_if_consent_is_blocked(system, monkeypatch):
    _, service = system
    client = ConsentClient({"ok": False})
    monkeypatch.setattr(browser_context, "_open_cdp_with_recovery", lambda *a: client)
    monkeypatch.setattr(browser_context, "_browser_verify", lambda *a: {"body": verified_body()})
    with pytest.raises(AkoolBrowserConsentError):
        browser_context.browser_json_request(owner(), service.settings, "POST",
            "https://akool.com/interface/content-api/api/v7/content/image2Video/createBySourcePrompt/batch",
            payload={"prompt": "synthetic"}, on_verified=lambda _: pytest.fail("not prepared"))
    assert not any("const config=" in x for x in client.expressions)
    assert client.calls == [] and client.closed


def test_accepted_result_query_never_needs_consent_storage(system, monkeypatch):
    _, service = system
    client = ConsentClient(RuntimeError("storage is unavailable"))
    monkeypatch.setattr(browser_context, "_open_cdp_with_recovery", lambda *a: client)
    monkeypatch.setattr(browser_context, "_browser_verify", lambda *a: {"body": verified_body()})
    result, context = browser_context.browser_json_request(owner(), service.settings, "GET",
        GENERATION_CONSENT_ORIGIN + GENERATION_RESULT_PATH + "?type=15&size=20")
    assert result["status"] == 200
    assert not any("__akGenerationConsentPreconfirm" in x for x in client.expressions)
    assert "tos_hash" not in context
    scripts = [x for x in client.expressions if "const config=" in x]
    assert len(scripts) == 1 and '"method": "GET"' in scripts[0]
    assert '"hasBody": false' in scripts[0] and client.closed


def test_postflight_capture_never_rechecks_or_rewrites_the_consent_marker(system, monkeypatch):
    _, service = system
    client = ConsentClient(consent_result(written=True))
    monkeypatch.setattr(browser_context, "_open_cdp_with_recovery", lambda *a: client)
    monkeypatch.setattr(browser_context, "_browser_verify", lambda *a: {"body": verified_body()})
    result, _ = browser_context.browser_json_request(owner(), service.settings, "POST",
        "https://akool.com/api/create", payload={"prompt": "synthetic"})
    assert result["body"]["data"]["id"] == "paid-operation"
    assert sum("__akGenerationConsentPreconfirm" in x for x in client.expressions) == 1


@pytest.mark.parametrize("wrapped", [False, True])
def test_check_account_consent_failure_is_agreement_required_not_login_error(system, monkeypatch, wrapped):
    db, service = system
    account = db.upsert_account({"name": "ready", "status": "active", "last_balance": 100})
    failure = AkoolUpstreamError("agreement preparation required", code="AGREEMENT_REQUIRED", status_code=503) if wrapped else AkoolBrowserConsentError()

    def fail():
        raise failure

    class Client:
        account_state = staticmethod(fail)

    monkeypatch.setattr(service, "_client", lambda *a: Client())
    with pytest.raises(type(failure)):
        service.check_account(account["id"], recover=False)
    current = db.get_account(account["id"])
    assert current["status"] == "agreement_required"
    assert db.acquire_account(account["id"]) is None
    assert service.public_failure_message({"error_code": "AGREEMENT_REQUIRED"}) == "视频生成提交失败，请稍后重试~"


def test_automatic_login_consent_failure_is_not_a_login_error(system, monkeypatch):
    db, service = system
    account = db.upsert_account({"name": "automatic", "status": "active"})
    monkeypatch.setattr(service_module, "refresh_account_context", lambda *a: (_ for _ in ()).throw(AkoolBrowserConsentError()))
    with pytest.raises(AkoolBrowserConsentError):
        service._login_account(account["id"])
    assert db.get_account(account["id"])["status"] == "agreement_required"


def test_manual_capture_failure_does_not_publish_usable_or_change_cookies(system, monkeypatch):
    db, service = system
    account = db.upsert_account({"name": "manual", "status": "active", "cookie_header": "session=original"})
    monkeypatch.setattr(service_module, "browser_snapshot", lambda *a, **kw: {"image": "synthetic"})
    monkeypatch.setattr(service_module, "capture_browser_session", lambda *a: (_ for _ in ()).throw(AkoolBrowserConsentError()))
    service.open_manual_browser(account["id"])
    try:
        with pytest.raises(AkoolBrowserConsentError):
            service.complete_manual_browser(account["id"])
        current = db.get_account(account["id"])
        assert current["status"] == "agreement_required" and current["cookie_header"] == "session=original"
        assert current["tos_hash"] == "" and db.acquire_account(account["id"]) is None
    finally:
        service.close_manual_browser(account["id"])
    current = db.get_account(account["id"])
    assert current["status"] == "agreement_required" and not current["enabled"]


def test_online_addition_is_ready_only_after_local_marker_and_receipt(manager, monkeypatch):
    async def scenario():
        draft = manager.create_pending({"name": "consent-new", "use_proxy_pool": False})
        started = await manager.start(draft["id"])
        client = ConsentClient({"ok": False})
        monkeypatch.setattr(browser_login, "capture_native_context", lambda account, config:
            _verified_context(client, account, config, verified_body()))
        try:
            with pytest.raises(LoginError) as failure:
                await manager.finish(started["active"]["session_id"])
            assert failure.value.status == 503
            failed = manager.store.get_account(draft["id"])
            assert not failed["enabled"] and failed["online_draft"] == 1
            assert failed["status"] == "agreement_required"
            assert failed["tos_hash"] == "" and failed["cookie_header"] == ""
            assert manager.store.acquire_account(draft["id"]) is None
            client.result = consent_result(written=True)
            saved = await manager.finish(started["active"]["session_id"])
            assert saved["enabled"] and saved["status"] == "active"
            stored = manager.store.get_account(draft["id"])
            assert stored["tos_hash"] == GENERATION_CONSENT_HASH
            assert stored["tos_accepted_at"] and stored["cookie_header"] == "session=browser-current"
        finally:
            await manager.close()

    asyncio.run(scenario())
