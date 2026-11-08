"""Offline admin/origin/viewer isolation for the private browser login gateway."""
import asyncio
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest
import requests
from starlette.websockets import WebSocketDisconnect

import app.browser_login_api as module
from app.login_desktop import LoginError


class Manager:
    def __init__(self):
        self.desktop = SimpleNamespace(password="rfb-fixture", vnc_port=35901)
        self.active = {"id": "session-fixture", "ticket": "ticket-fixture", "phase": "login",
                       "account_id": 7, "deadline": time.monotonic() + 120,
                       "desktop": self.desktop}
        self.calls = []

    def status(self):
        return {"available": True, "active": None if not self.active else {
            "account_id": 7, "session_id": self.active["id"], "phase": "login", "remaining_seconds": 120}}

    def create_pending(self, body):
        self.calls.append(("pending", body))
        return {"id": 7, "name": body["name"], "enabled": False}

    async def start(self, account_id):
        self.calls.append(("start", account_id))
        return self.status()

    def _owned(self, session_id):
        if not self.active or self.active["id"] != session_id:
            raise LoginError("login window has ended", 404)
        return self.active

    def viewer(self, ticket):
        if not self.active or self.active["ticket"] != ticket or self.active["deadline"] <= time.monotonic():
            return None
        return self.active

    async def finish(self, session_id):
        self._owned(session_id)
        self.calls.append(("finish", session_id))
        self.active = None
        return {"id": 7, "enabled": True}

    async def cancel(self, session_id):
        self._owned(session_id)
        self.calls.append(("cancel", session_id))
        self.active = None


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setattr(requests.Session, "request", lambda *a, **kw: pytest.fail("External HTTP forbidden"))
    manager = Manager()
    app = FastAPI()
    secret = {"value": "admin-fixture"}
    def admin_auth(request: Request):
        if request.cookies.get("ak_admin") != secret["value"]:
            raise HTTPException(401, "admin required")
    module.register_browser_login(app, manager, admin_auth, lambda: secret["value"])
    static = tmp_path / "static"
    static.mkdir()
    (static / "account-viewer.html").write_text("<p>fixture-viewer</p>", encoding="utf-8")
    novnc = tmp_path / "novnc"
    (novnc / "core").mkdir(parents=True)
    (novnc / "core/rfb.js").write_text("export default 1", encoding="utf-8")
    (novnc / "private.txt").write_text("never expose", encoding="utf-8")
    monkeypatch.setattr(module, "STATIC", static)
    monkeypatch.setattr(module, "NOVNC_ROOT", novnc)
    with TestClient(app, base_url="https://console.test") as client:
        yield client, manager, secret


def admin(client):
    client.cookies.set("ak_admin", "admin-fixture")


def issue(client):
    response = client.post("/api/account-login/session-fixture/viewer")
    assert response.status_code == 200
    return response


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/account-login", None),
    ("POST", "/api/accounts/pending", {"name": "fixture"}),
    ("POST", "/api/accounts/7/login/start", {}),
    ("POST", "/api/account-login/session-fixture/viewer", {}),
    ("POST", "/api/account-login/session-fixture/finish", {}),
    ("POST", "/api/account-login/session-fixture/cancel", {}),
])
def test_admin_management_rejects_api_key_only(console, method, path, body):
    client, manager, _ = console
    response = client.request(method, path, json=body, headers={"Authorization": "Bearer public-api-key"})
    assert response.status_code == 401
    assert manager.calls == []


@pytest.mark.parametrize("origin", ["https://evil.test", "http://console.test", "https://console.test:444", "null", "https://[broken"])
def test_cross_origin_mutation_is_rejected_before_manager(console, origin):
    client, manager, _ = console
    admin(client)
    response = client.post("/api/accounts/7/login/start", headers={"Origin": origin})
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert manager.calls == []


def test_pending_and_start_delegate_once_with_existing_account_fields(console):
    client, manager, _ = console
    admin(client)
    body = {"name": "online fixture", "proxy_url": "", "max_concurrency": 1}
    response = client.post("/api/accounts/pending", json=body)
    assert response.status_code == 200 and response.json()["enabled"] is False
    response = client.post("/api/accounts/7/login/start")
    assert response.json()["active"]["session_id"] == "session-fixture"
    assert manager.calls == [("pending", body), ("start", 7)]
    assert response.headers["cache-control"] == "no-store"


def test_viewer_ticket_is_cookie_only_secure_and_admin_bound(console):
    client, manager, _ = console
    admin(client)
    response = issue(client)
    assert response.json() == {"url": "/account-login/viewer"}
    assert "ticket-fixture" not in response.text and "rfb-fixture" not in response.text
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header and "secure" in header and "samesite=strict" in header
    assert "path=/account-login" in header
    assert client.get("/account-login/viewer").status_code == 200
    response = client.get("/account-login/credentials")
    assert response.json() == {"password": "rfb-fixture"}
    for name, value in module.HEADERS.items():
        assert response.headers[name] == value
    client.cookies.delete("ak_admin")
    assert client.get("/account-login/credentials").status_code == 403


def test_forged_or_expired_ticket_does_not_reveal_credentials(console):
    client, manager, _ = console
    admin(client)
    client.cookies.set(module.COOKIE, "forged")
    response = client.get("/account-login/credentials")
    assert response.status_code == 401 and "rfb-fixture" not in response.text
    client.cookies.clear()
    admin(client)
    issue(client)
    manager.active["deadline"] = time.monotonic() - 1
    assert client.get("/account-login/credentials").status_code == 401


@pytest.mark.parametrize("action", ["finish", "cancel"])
def test_ending_session_invalidates_ticket_and_clears_cookie(console, action):
    client, manager, _ = console
    admin(client)
    issue(client)
    response = client.post("/api/account-login/session-fixture/" + action)
    assert response.status_code == 200 and manager.active is None
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert client.get("/account-login/credentials").status_code == 401


def test_assets_require_lease_and_traversal_or_non_novnc_assets_fail(console):
    client, manager, _ = console
    assert client.get("/account-login/novnc/core/rfb.js").status_code == 403
    admin(client)
    issue(client)
    assert client.get("/account-login/novnc/core/rfb.js").status_code == 200
    for path in ("../private.txt", "core/../../private.txt", "private.txt", "core\\rfb.js", ""):
        with pytest.raises(HTTPException) as error:
            module.asset_path(path)
        assert error.value.status_code == 404


@pytest.mark.parametrize("origin", [None, "https://evil.test", "http://console.test"])
def test_websocket_requires_exact_origin_and_admin_and_viewer(console, origin, monkeypatch):
    client, _, _ = console
    admin(client)
    issue(client)
    connection = AsyncMock(side_effect=AssertionError("Must not connect to VNC"))
    monkeypatch.setattr(module.asyncio, "open_connection", connection)
    headers = {"Origin": origin} if origin else {}
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("wss://console.test/account-login/websockify", headers=headers):
            pass
    assert error.value.code == 1008
    connection.assert_not_called()


def test_websocket_api_token_cannot_replace_admin_cookie(console, monkeypatch):
    client, _, _ = console
    client.cookies.set(module.COOKIE, "ticket-fixture")
    connection = AsyncMock(side_effect=AssertionError("Must not connect to VNC"))
    monkeypatch.setattr(module.asyncio, "open_connection", connection)
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("wss://console.test/account-login/websockify", headers={"Origin": "https://console.test", "Authorization": "Bearer public-api-key"}):
            pass
    assert error.value.code == 1008
    connection.assert_not_called()


def test_manager_login_error_status_is_preserved(console):
    client, manager, _ = console
    admin(client)
    async def blocked(account_id):
        raise LoginError("account has active tasks", 409)
    manager.start = blocked
    response = client.post("/api/accounts/7/login/start")
    assert response.status_code == 409 and response.json()["detail"] == "account has active tasks"


def test_websocket_revocation_closes_only_its_loopback_connection(console, monkeypatch):
    client, manager, _ = console
    admin(client)
    issue(client)
    class Reader:
        async def read(self, count):
            await asyncio.sleep(60)
            return b""
    class Writer:
        def __init__(self):
            self.data = []
            self.closed = False
        def write(self, data): self.data.append(data)
        async def drain(self): pass
        def close(self): self.closed = True
        async def wait_closed(self): pass
    writer = Writer()
    connection = AsyncMock(return_value=(Reader(), writer))
    monkeypatch.setattr(module.asyncio, "open_connection", connection)
    with client.websocket_connect("wss://console.test/account-login/websockify", headers={"Origin": "https://console.test"}, subprotocols=["binary"]) as socket:
        manager.active = None
        socket.send_bytes(b"ignored-after-revoke")
        event = socket.receive()
        assert event["type"] == "websocket.close"
    connection.assert_awaited_once_with("127.0.0.1", 35901)
    assert writer.data == [] and writer.closed


def test_oversized_rfb_input_is_not_forwarded(console, monkeypatch):
    client, _, _ = console
    admin(client)
    issue(client)
    class Reader:
        async def read(self, count):
            await asyncio.sleep(60)
            return b""
    class Writer:
        def __init__(self): self.data = []
        def write(self, data): self.data.append(data)
        async def drain(self): pass
        def close(self): pass
        async def wait_closed(self): pass
    writer = Writer()
    monkeypatch.setattr(module.asyncio, "open_connection", AsyncMock(return_value=(Reader(), writer)))
    with client.websocket_connect("wss://console.test/account-login/websockify", headers={"Origin": "https://console.test"}) as socket:
        socket.send_bytes(b"x" * (1024 * 1024 + 1))
        assert socket.receive()["type"] == "websocket.close"
    assert writer.data == []


def test_gateway_limits_active_viewers_and_releases_capacity(console, monkeypatch):
    client, _, _ = console
    admin(client)
    issue(client)
    class Reader:
        async def read(self, count):
            await asyncio.sleep(60)
            return b""
    class Writer:
        def write(self, data): pass
        async def drain(self): pass
        def close(self): pass
        async def wait_closed(self): pass
    connection = AsyncMock(side_effect=lambda *a, **kw: (Reader(), Writer()))
    monkeypatch.setattr(module.asyncio, "open_connection", connection)
    path, headers = "wss://console.test/account-login/websockify", {"Origin": "https://console.test"}
    with client.websocket_connect(path, headers=headers) as first:
        with client.websocket_connect(path, headers=headers) as second:
            with pytest.raises(WebSocketDisconnect) as error:
                with client.websocket_connect(path, headers=headers):
                    pass
            assert error.value.code == 1008
            second.send_bytes(b'x' * (1024 * 1024 + 1))
            assert second.receive()['type'] == 'websocket.close'
        first.send_bytes(b'x' * (1024 * 1024 + 1))
        assert first.receive()['type'] == 'websocket.close'
    assert connection.await_count == 2
    with client.websocket_connect(path, headers=headers) as third:
        third.send_bytes(b'x' * (1024 * 1024 + 1))
        assert third.receive()['type'] == 'websocket.close'
    assert connection.await_count == 3


def test_empty_admin_secret_never_enables_anonymous_browser(console):
    manager = Manager()
    app = FastAPI()
    module.register_browser_login(app, manager, lambda: None, "")
    with TestClient(app, base_url="https://console.test") as client:
        assert client.get("/api/account-login").status_code == 503
        assert client.post("/api/accounts/7/login/start").status_code == 503
        client.cookies.set(module.COOKIE, "ticket-fixture")
        assert client.get("/account-login/credentials").status_code == 403
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("wss://console.test/account-login/websockify", headers={"Origin": "https://console.test"}):
                pass
        assert error.value.code == 1008
    assert manager.calls == []

