"""Authenticated console access to the account's existing native CDP browser."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlsplit

from app.browser_context import (
    AkoolBrowserAccountSuspended,
    AkoolBrowserChallengeError,
    AkoolBrowserError,
    _CDP,
    _account_lock,
    _attempt_login,
    _browser_verify,
    _cdp_port,
    _open_cdp_with_recovery,
    _page_state,
    _profile_path,
    _safe_page_url,
    _verified_context,
    _wait_target,
)


@contextmanager
def _connection(
    account: dict[str, Any], settings: Any, *, start: bool = False
) -> Iterator[_CDP]:
    with _account_lock(int(account["id"])):
        port = _cdp_port(account, settings)
        if start:
            client = _open_cdp_with_recovery(
                account, settings, port, _profile_path(account, settings)
            )
        else:
            target = _wait_target(port, 2)
            client = _CDP(target["webSocketDebuggerUrl"], timeout=15)
        try:
            host = urlsplit(str(client.evaluate("location.href") or "")).hostname or ""
            if host != "akool.com" and not host.endswith(".akool.com"):
                raise AkoolBrowserError("Account browser is not on an Akool page")
            yield client
        finally:
            client.close()


def _snapshot(client: _CDP) -> dict[str, Any]:
    page = _page_state(client)
    shot = client.call(
        "Page.captureScreenshot",
        {
            "format": "jpeg",
            "quality": 80,
            "captureBeyondViewport": False,
        },
    )
    return {
        "image": "data:image/jpeg;base64," + shot["data"],
        "url": _safe_page_url(page.get("url")),
        "title": page.get("title") or "Akool",
        "challenge_required": bool(page.get("hasChallenge")),
        "has_login_form": bool(page.get("hasPassword")),
    }


def browser_snapshot(
    account: dict[str, Any], settings: Any, *, start: bool = False
) -> dict[str, Any]:
    with _connection(account, settings, start=start) as client:
        return _snapshot(client)


def browser_action(
    account: dict[str, Any], settings: Any, action: dict[str, Any]
) -> dict[str, Any]:
    with _connection(account, settings) as client:
        kind = action["action"]
        if kind == "login":
            if not account.get("email") or not account.get("password"):
                raise ValueError("此账号未保存账密，请先同步已登录会话")
            result = _attempt_login(
                client, str(account["email"]), str(account["password"])
            )
        else:
            viewport = client.evaluate("({width:innerWidth, height:innerHeight})")
            x = min(
                float(action.get("x", 0.5)) * viewport["width"], viewport["width"] - 1
            )
            y = min(
                float(action.get("y", 0.5)) * viewport["height"], viewport["height"] - 1
            )
            if kind == "click":
                for event in ("mouseMoved", "mousePressed", "mouseReleased"):
                    params = {"type": event, "x": x, "y": y}
                    if event != "mouseMoved":
                        params.update(button="left", clickCount=1)
                    client.call("Input.dispatchMouseEvent", params)
            elif kind == "scroll":
                client.call(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": x,
                        "y": y,
                        "deltaX": 0,
                        "deltaY": action["delta_y"],
                    },
                )
            else:
                raise ValueError("unsupported browser action")
            result = {"phase": kind}
        return {"action_result": result}


def capture_browser_session(account: dict[str, Any], settings: Any) -> dict[str, Any]:
    """Read the completed session without navigating or overwriting its cookies."""
    with _connection(account, settings) as client:
        body = _browser_verify(client).get("body") or {}
        if body.get("code") == 1108:
            raise AkoolBrowserAccountSuspended("Akool account has been suspended")
        if body.get("code") != 1000:
            raise AkoolBrowserChallengeError("会话尚未登录，请完成页面验证和登录后重试")
        return _verified_context(client, account, settings, body)
