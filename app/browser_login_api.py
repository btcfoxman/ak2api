"""Administrator-only same-origin noVNC gateway; desktop ports stay private."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from fastapi import Body, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from app.login_desktop import LoginError


PREFIX = "/account-login"
COOKIE = "ak_login_viewer"
NOVNC_ROOT = Path("/usr/share/novnc")
STATIC = Path(__file__).parent / "static"
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN",
           "Content-Security-Policy": "frame-ancestors 'self'"}


def same_origin(request, *, required: bool = False) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return not required and request.headers.get("sec-fetch-site") != "cross-site"
    try:
        source = urlsplit(origin)
    except ValueError:
        return False
    scheme = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme
    scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    return (source.scheme in {"http", "https"} and source.scheme == scheme
            and source.netloc.casefold() == str(request.headers.get("host") or "").casefold())


def failure(status: int, detail: str = "") -> HTTPException:
    return HTTPException(status_code=status, detail=detail, headers=HEADERS)


def asset_path(asset: str) -> Path:
    if "\\" in asset or not Path(asset).parts or any(part.startswith(".") for part in Path(asset).parts):
        raise failure(404)
    root = NOVNC_ROOT.resolve()
    candidate = (root / asset).resolve()
    if (not candidate.is_relative_to(root) or not candidate.is_file()
            or Path(asset).parts[0] not in {"core", "vendor"}):
        raise failure(404)
    return candidate


def register_browser_login(app, manager, admin_auth,
                           admin_token: str | Callable[[], str]) -> None:
    def secret() -> str:
        return str(admin_token() if callable(admin_token) else admin_token)

    def auth(request: Request, _=Depends(admin_auth)) -> None:
        if not secret():
            raise failure(503, "请先配置管理登录密码后再打开账号浏览器")
        if not same_origin(request):
            raise failure(403, "不允许跨站操作账号登录窗口")

    def response(value: dict) -> JSONResponse:
        return JSONResponse(value, headers=HEADERS)

    def viewer(request) -> dict:
        if (not secret() or not same_origin(request)
                or not secrets.compare_digest(request.cookies.get("ak_admin") or "", secret())):
            raise failure(403)
        lease = manager.viewer(request.cookies.get(COOKIE))
        if not lease:
            raise failure(401, "登录窗口已结束或过期")
        return lease

    async def execute(operation):
        try:
            return await operation
        except LoginError as exc:
            raise failure(exc.status, str(exc)) from exc

    @app.get("/api/account-login", dependencies=[Depends(auth)])
    def status() -> JSONResponse:
        return response(manager.status())

    @app.post("/api/accounts/pending", dependencies=[Depends(auth)])
    def create_pending(body: dict = Body(...)) -> JSONResponse:
        try:
            account = manager.create_pending(body)
        except LoginError as exc:
            raise failure(exc.status, str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise failure(422, str(exc)) from exc
        return response(account)

    @app.post("/api/accounts/{account_id}/login/start", dependencies=[Depends(auth)])
    async def start(account_id: int) -> JSONResponse:
        return response(await execute(manager.start(account_id)))

    @app.post("/api/account-login/{session_id}/viewer", dependencies=[Depends(auth)])
    def issue_viewer(session_id: str, request: Request) -> JSONResponse:
        try:
            lease = manager._owned(session_id)
        except LoginError as exc:
            raise failure(exc.status, str(exc)) from exc
        if (lease["phase"] != "login" or not lease["desktop"].password
                or not lease["desktop"].vnc_port):
            raise failure(409, "登录浏览器尚未就绪")
        result = response({"url": PREFIX + "/viewer"})
        scheme = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme
        result.set_cookie(COOKIE, lease["ticket"], httponly=True, secure=scheme == "https",
                          samesite="strict", path=PREFIX,
                          max_age=max(1, int(lease["deadline"] - time.monotonic())))
        return result

    @app.post("/api/account-login/{session_id}/finish", dependencies=[Depends(auth)])
    async def finish(session_id: str) -> JSONResponse:
        result = response(await execute(manager.finish(session_id)))
        result.delete_cookie(COOKIE, path=PREFIX, httponly=True, samesite="strict")
        return result

    @app.post("/api/account-login/{session_id}/cancel", dependencies=[Depends(auth)])
    async def cancel(session_id: str) -> JSONResponse:
        await execute(manager.cancel(session_id))
        result = response({"ok": True})
        result.delete_cookie(COOKIE, path=PREFIX, httponly=True, samesite="strict")
        return result

    @app.get(PREFIX + "/viewer")
    def viewer_page(request: Request) -> FileResponse:
        viewer(request)
        return FileResponse(STATIC / "account-viewer.html", headers=HEADERS)

    @app.get(PREFIX + "/credentials")
    def credentials(request: Request) -> JSONResponse:
        lease = viewer(request)
        if not lease["desktop"].password:
            raise failure(401, "登录窗口已结束或过期")
        return response({"password": lease["desktop"].password})

    @app.get(PREFIX + "/novnc/{asset:path}")
    def assets(asset: str, request: Request) -> FileResponse:
        viewer(request)
        return FileResponse(asset_path(asset), headers=HEADERS)

    connections: set[WebSocket] = set()

    @app.websocket(PREFIX + "/websockify")
    async def gateway(websocket: WebSocket) -> None:
        ticket = websocket.cookies.get(COOKIE)
        lease = manager.viewer(ticket)
        authenticated = bool(secret()) and secrets.compare_digest(websocket.cookies.get("ak_admin") or "", secret())
        if not same_origin(websocket, required=True) or not authenticated or not lease or len(connections) >= 2:
            await websocket.close(code=1008)
            return
        connections.add(websocket)
        writer, tasks = None, []
        try:
            protocols = websocket.scope.get("subprotocols", [])
            await websocket.accept(subprotocol="binary" if "binary" in protocols else None)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", lease["desktop"].vnc_port), 3)

            async def incoming():
                while manager.viewer(ticket) is lease:
                    message = await websocket.receive_bytes()
                    if len(message) > 1024 * 1024 or manager.viewer(ticket) is not lease:
                        return
                    writer.write(message)
                    await writer.drain()

            async def outgoing():
                while manager.viewer(ticket) is lease:
                    data = await reader.read(65536)
                    if not data or manager.viewer(ticket) is not lease:
                        return
                    await websocket.send_bytes(data)

            async def expiry():
                while manager.viewer(ticket) is lease:
                    await asyncio.sleep(.5)

            tasks = [asyncio.create_task(operation()) for operation in (incoming, outgoing, expiry)]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, OSError, asyncio.TimeoutError, RuntimeError, KeyError):
            pass
        finally:
            # Release admission capacity and close the socket even when the ASGI
            # task itself is cancelled during browser/server shutdown.
            connections.discard(websocket)
            if writer:
                writer.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if writer:
                try:
                    await asyncio.wait_for(writer.wait_closed(), 2)
                except (OSError, asyncio.TimeoutError):
                    pass
            try:
                await websocket.close()
            except RuntimeError:
                pass
