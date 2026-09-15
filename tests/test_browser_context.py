from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import app.browser_context as browser_context
from app.browser_context import (
    AkoolBrowserChallengeError,
    AkoolBrowserError,
    AkoolBrowserTransportError,
    _page_state,
    _safe_cookie_records,
    _safe_page_url,
    _verified_context,
)


def test_cookie_header_is_converted_to_valid_cdp_records() -> None:
    records = _safe_cookie_records(
        {
            "cookie_header": "token=abc; refresh_token=def==; session_id=session",
            "cookie_records": [],
        }
    )

    assert {item["name"] for item in records} == {
        "token",
        "refresh_token",
        "session_id",
    }
    assert all(item["domain"] == ".akool.com" for item in records)
    assert all(item["value"] is not None for item in records)


def test_page_url_redacts_login_secrets() -> None:
    value = _safe_page_url(
        "https://akool.com/zh-cn/login?email=user%40example.com&password=private&next=%2Fapp#fragment"
    )

    assert "private" not in value
    assert "password=REDACTED" in value
    assert "email=REDACTED" in value
    assert "#fragment" not in value


@pytest.mark.parametrize("visible", [True, False])
def test_login_detects_visible_turnstile_inside_closed_shadow_root(visible):
    class Client:
        def evaluate(self, expression):
            return json.dumps(
                {"hasEmail": True, "hasPassword": True, "hasChallenge": False}
            )

        def call(self, method, params=None):
            if method == "DOM.getDocument":
                assert params == {"depth": -1, "pierce": True}
                return {
                    "root": {
                        "children": [
                            {
                                "shadowRoots": [
                                    {
                                        "shadowRootType": "closed",
                                        "children": [
                                            {
                                                "nodeName": "IFRAME",
                                                "backendNodeId": 7,
                                                "attributes": [
                                                    "src",
                                                    "https://challenges.cloudflare.com/widget",
                                                ],
                                            }
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                }
            if method == "DOM.resolveNode":
                return {"object": {"objectId": "challenge-frame"}}
            if method == "Runtime.callFunctionOn":
                assert params["objectId"] == "challenge-frame"
                return {"result": {"value": visible}}
            assert method == "Runtime.releaseObject"
            return {}

    assert _page_state(Client())["hasChallenge"] is visible


def test_challenge_scan_survives_replaced_frames_and_reads_nested_documents():
    class Client:
        def evaluate(self, expression):
            return "{}"

        def call(self, method, params=None):
            if method == "DOM.getDocument":

                def frame(node_id):
                    return {
                        "nodeName": "IFRAME",
                        "backendNodeId": node_id,
                        "attributes": [
                            "src",
                            "https://challenges.cloudflare.com/widget",
                        ],
                    }

                return {
                    "root": {
                        "children": [
                            {
                                "nodeName": "IFRAME",
                                "contentDocument": {"children": [frame(1), frame(2)]},
                            },
                        ]
                    }
                }
            if method == "DOM.resolveNode":
                if params["backendNodeId"] == 2:
                    raise AkoolBrowserTransportError("Node was detached")
                return {"object": {"objectId": "remaining-frame"}}
            if method == "Runtime.callFunctionOn":
                return {"result": {"value": True}}
            assert method == "Runtime.releaseObject"
            raise AkoolBrowserTransportError("Execution context was destroyed")

    assert _page_state(Client())["hasChallenge"] is True


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/challenge",
        "https://challenges.cloudflare.com.example.com/widget",
        "https://[invalid",
    ],
)
def test_challenge_scan_ignores_unrelated_or_malformed_frame_urls(url):
    class Client:
        def evaluate(self, expression):
            return "{}"

        def call(self, method, params=None):
            assert method == "DOM.getDocument"
            return {
                "root": {
                    "children": [
                        {
                            "nodeName": "IFRAME",
                            "attributes": ["src", url],
                        }
                    ]
                }
            }

    assert _page_state(Client())["hasChallenge"] is False


@pytest.mark.parametrize("grace,timeout", [(3, 30), (12, 2)])
def test_visible_challenge_stops_login_for_manual_verification(
    monkeypatch, tmp_path, grace, timeout
):
    clock = SimpleNamespace(now=0.0)
    client = SimpleNamespace(call=lambda *args: {}, close=lambda: None)
    monkeypatch.setattr(browser_context.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(
        browser_context.time,
        "sleep",
        lambda seconds: setattr(clock, "now", clock.now + seconds),
    )
    monkeypatch.setattr(
        browser_context, "_open_cdp_with_recovery", lambda *args: client
    )
    monkeypatch.setattr(
        browser_context, "_browser_verify", lambda *args: {"body": {"code": 1102}}
    )
    monkeypatch.setattr(
        browser_context,
        "_page_state",
        lambda *args: {
            "hasChallenge": True,
            "hasEmail": True,
            "hasPassword": True,
            "url": "https://akool.com/login?email=private%40example.com&password=private-password",
            "text": "private-page-text",
        },
    )

    def unexpected_login(*args):
        pytest.fail("Must not submit credentials while a visible challenge is pending")

    monkeypatch.setattr(browser_context, "_attempt_login", unexpected_login)
    config = SimpleNamespace(
        chrome_cdp_base_port=19800,
        chrome_user_data_root=tmp_path,
        browser_timeout_seconds=timeout,
        browser_challenge_grace_seconds=grace,
    )
    with pytest.raises(
        AkoolBrowserChallengeError, match="requires manual verification"
    ) as error:
        browser_context.refresh_account_context(
            {"id": 1, "email": "user", "password": "secret"}, config
        )
    assert clock.now == min(grace, timeout)
    assert "private" not in str(error.value)


def test_verified_session_rejects_a_different_account_before_reading_cookies():
    with pytest.raises(AkoolBrowserError, match="different account"):
        _verified_context(
            None,
            {"email": "expected@example.com"},
            None,
            {"data": {"user": {"email": "other@example.com"}}},
        )


def test_verified_session_only_captures_akool_domain_cookies(tmp_path):
    class Client:
        def evaluate(self, expression):
            return "test-browser"

        def call(self, method):
            assert method == "Network.getAllCookies"
            return {
                "cookies": [
                    {"name": "session", "value": "one", "domain": ".akool.com"},
                    {"name": "session2", "value": "two", "domain": "app.akool.com"},
                    {"name": "other", "value": "three", "domain": "notakool.com"},
                ]
            }

    result = _verified_context(
        Client(),
        {"id": 1, "email": "same@example.com"},
        SimpleNamespace(chrome_cdp_base_port=19800, chrome_user_data_root=tmp_path),
        {
            "data": {
                "token": "token",
                "user": {"email": "same@example.com", "_id": "user"},
            }
        },
    )
    assert [item["name"] for item in result["cookie_records"]] == [
        "session",
        "session2",
    ]
    assert result["cdp_port"] == 19801
