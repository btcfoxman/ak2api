from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import websocket

from app.config import normalize_proxy_url, rewrite_loopback_proxy
from app.cookies import cookie_header_from_records, cookie_records


AKOOL_LOGIN_PAGE = "https://akool.com/zh-cn/pricing"
AKOOL_VERIFY_PATH = "/interface/user-api/api/v6/verify/user"


class AkoolBrowserError(RuntimeError):
    pass


class AkoolBrowserChallengeError(AkoolBrowserError):
    pass


class _CDP:
    def __init__(self, url: str, timeout: float = 20):
        self.socket = websocket.create_connection(
            url,
            timeout=timeout,
            suppress_origin=True,
        )
        self.command_id = 0
        self.timeout = timeout

    def close(self) -> None:
        try:
            self.socket.close()
        except Exception:
            pass

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.command_id += 1
        command_id = self.command_id
        self.socket.send(
            json.dumps({"id": command_id, "method": method, "params": params or {}})
        )
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                payload = json.loads(self.socket.recv())
            except TimeoutError as exc:
                raise AkoolBrowserError(f"CDP {method} timed out") from exc
            if payload.get("id") != command_id:
                continue
            if payload.get("error"):
                message = payload["error"].get("message") or payload["error"]
                raise AkoolBrowserError(f"CDP {method} failed: {message}")
            return payload.get("result") or {}
        raise AkoolBrowserError(f"CDP {method} timed out")

    def evaluate(self, expression: str) -> Any:
        value = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
        )
        result = value.get("result") or {}
        if result.get("subtype") == "error":
            raise AkoolBrowserError(str(result.get("description") or "browser script failed"))
        return result.get("value")


_managed: dict[int, subprocess.Popen[Any]] = {}
_managed_lock = threading.RLock()


def _chrome_executable(configured: str) -> str:
    candidates = [
        configured,
        shutil.which("google-chrome") or "",
        shutil.which("google-chrome-stable") or "",
        shutil.which("chromium") or "",
        shutil.which("chrome") or "",
        os.path.join(
            os.environ.get("PROGRAMFILES", ""),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
        os.path.join(
            os.environ.get("PROGRAMFILES(X86)", ""),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    raise AkoolBrowserError("Google Chrome executable was not found")


def _profile_path(account: dict[str, Any], settings: Any) -> Path:
    configured = str(account.get("profile_dir") or "").strip()
    if configured:
        return Path(configured).resolve()
    return (
        Path(settings.chrome_user_data_root) / f"account-{int(account['id'])}"
    ).resolve()


def _cdp_port(account: dict[str, Any], settings: Any) -> int:
    port = int(
        account.get("cdp_port")
        or (int(settings.chrome_cdp_base_port) + int(account["id"]))
    )
    if not 1024 <= port <= 65535:
        raise AkoolBrowserError(f"invalid CDP port for account #{account['id']}: {port}")
    return port


def _cdp_json(port: int, path: str, timeout: float = 3) -> Any:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}{path}", timeout=timeout
    ) as response:
        return json.load(response)


def _wait_target(port: int, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            targets = _cdp_json(port, "/json/list")
            page = next(
                (
                    item
                    for item in targets
                    if item.get("type") == "page"
                    and item.get("webSocketDebuggerUrl")
                ),
                None,
            )
            if page:
                return page
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise AkoolBrowserError(f"Chrome did not establish CDP on port {port}: {last_error}")


def _launch(
    account: dict[str, Any], settings: Any
) -> tuple[subprocess.Popen[Any] | None, int]:
    account_id = int(account["id"])
    port = _cdp_port(account, settings)
    try:
        _cdp_json(port, "/json/version", 1)
        return None, port
    except Exception:
        pass

    profile = _profile_path(account, settings)
    profile.mkdir(parents=True, exist_ok=True)
    proxy = rewrite_loopback_proxy(
        normalize_proxy_url(str(account.get("proxy_url") or "")),
        str(settings.proxy_host_override or ""),
    )
    command = [
        _chrome_executable(str(settings.chrome_executable or "")),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--no-sandbox",
    ]
    if proxy:
        command.append(f"--proxy-server={proxy}")
    if bool(settings.chrome_headless):
        command.extend(["--headless=new", "--disable-gpu"])
    command.append(AKOOL_LOGIN_PAGE)
    creationflags = 0
    if os.name == "nt" and bool(settings.chrome_headless):
        creationflags = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    with _managed_lock:
        _managed[account_id] = process
    try:
        _wait_target(port, min(int(settings.browser_timeout_seconds), 45))
    except Exception:
        if process.poll() is not None:
            raise AkoolBrowserError(
                f"Google Chrome exited before CDP became ready ({process.returncode})"
            )
        raise
    return process, port


def _safe_cookie_records(account: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    records = cookie_records(account.get("cookie_records") or account.get("cookies_json"))
    for item in records:
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        if not name or value is None:
            continue
        cookie: dict[str, Any] = {
            "name": name,
            "value": str(value),
            "domain": str(item.get("domain") or ".akool.com"),
            "path": str(item.get("path") or "/"),
            "secure": bool(item.get("secure", True)),
            "httpOnly": bool(item.get("httpOnly", False)),
        }
        expires = item.get("expires")
        if expires not in (None, "", -1, 0):
            try:
                numeric_expires = float(expires)
                if numeric_expires > 0:
                    cookie["expires"] = numeric_expires
            except (TypeError, ValueError):
                pass
        same_site = str(item.get("sameSite") or item.get("same_site") or "")
        if same_site in {"Strict", "Lax", "None"}:
            cookie["sameSite"] = same_site
        result.append(cookie)
    return result


def _page_state(client: _CDP) -> dict[str, Any]:
    raw = client.evaluate(
        "JSON.stringify({url:location.href,ua:navigator.userAgent||'',"
        "title:document.title||'',text:(document.body&&document.body.innerText||'').slice(0,5000)})"
    )
    try:
        return json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}


def _browser_verify(client: _CDP) -> dict[str, Any]:
    expression = f"""
    (async () => {{
      try {{
        const response = await fetch({json.dumps(AKOOL_VERIFY_PATH)}, {{
          method: 'GET', credentials: 'include', headers: {{Accept: 'application/json'}}
        }});
        let body = null;
        try {{ body = await response.json(); }} catch (_) {{}}
        return {{status: response.status, body}};
      }} catch (error) {{ return {{status: 0, error: String(error)}}; }}
    }})()
    """
    value = client.evaluate(expression)
    return value if isinstance(value, dict) else {}


def _attempt_login(client: _CDP, email: str, password: str) -> dict[str, Any]:
    expression = f"""
    (() => {{
      const email = {json.dumps(email)};
      const password = {json.dumps(password)};
      const visible = (el) => !!(el && el.getClientRects().length);
      const clickText = (...needles) => {{
        const values = needles.map(v => v.toLowerCase());
        const el = [...document.querySelectorAll('button,a,[role="button"]')].find(node =>
          visible(node) && values.some(value => (node.innerText || node.textContent || '').trim().toLowerCase().includes(value)));
        if (el) {{ el.click(); return true; }}
        return false;
      }};
      const set = (el, value) => {{
        if (!el) return false;
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        setter.call(el, value);
        el.dispatchEvent(new Event('input', {{bubbles:true}}));
        el.dispatchEvent(new Event('change', {{bubbles:true}}));
        return true;
      }};
      const emailInput = [...document.querySelectorAll('input')].find(el =>
        visible(el) && (el.type === 'email' || /email|邮箱/i.test(`${{el.name}} ${{el.placeholder}}`)));
      const passwordInput = [...document.querySelectorAll('input')].find(el =>
        visible(el) && el.type === 'password');
      if (!emailInput || !passwordInput) {{
        const opened = clickText('log in', 'login', 'sign in', '登录');
        return {{phase:'open-login', opened}};
      }}
      set(emailInput, email);
      set(passwordInput, password);
      const form = passwordInput.closest('form');
      const submit = (form && form.querySelector('button[type="submit"],input[type="submit"]')) ||
        [...document.querySelectorAll('button')].find(el => visible(el) && /log in|login|sign in|登录/i.test(el.innerText || ''));
      if (submit) submit.click(); else if (form) form.requestSubmit();
      return {{phase:'submitted', submitted:!!(submit || form)}};
    }})()
    """
    value = client.evaluate(expression)
    return value if isinstance(value, dict) else {}


def _click_turnstile(client: _CDP) -> bool:
    try:
        targets = client.call("Target.getTargets").get("targetInfos") or []
        iframe = next(
            (
                item
                for item in targets
                if "challenges.cloudflare.com" in str(item.get("url") or "")
            ),
            None,
        )
        if not iframe:
            return False
        # The widget normally handles itself. A real click is only attempted on the
        # visible iframe center; no challenge token is fabricated.
        rect = client.evaluate(
            "(() => {const f=[...document.querySelectorAll('iframe')].find(x=>x.src.includes('challenges.cloudflare.com'));"
            "if(!f)return null;const r=f.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,w:r.width,h:r.height};})()"
        )
        if not isinstance(rect, dict) or not rect.get("w") or not rect.get("h"):
            return False
        params = {"x": float(rect["x"]), "y": float(rect["y"]), "button": "left", "clickCount": 1}
        client.call("Input.dispatchMouseEvent", {**params, "type": "mousePressed"})
        client.call("Input.dispatchMouseEvent", {**params, "type": "mouseReleased"})
        return True
    except Exception:
        return False


def refresh_account_context(account: dict[str, Any], settings: Any) -> dict[str, Any]:
    _, port = _launch(account, settings)
    target = _wait_target(port, int(settings.browser_timeout_seconds))
    client = _CDP(str(target["webSocketDebuggerUrl"]), timeout=30)
    try:
        client.call("Network.enable")
        client.call("Page.enable")
        imported = _safe_cookie_records(account)
        if imported:
            client.call("Network.setCookies", {"cookies": imported})
        client.call("Page.navigate", {"url": AKOOL_LOGIN_PAGE})
        deadline = time.monotonic() + int(settings.browser_timeout_seconds)
        next_login_attempt = 0.0
        clicked_turnstile = False
        last_page: dict[str, Any] = {}
        last_error = ""
        while time.monotonic() < deadline:
            try:
                verified = _browser_verify(client)
                body = verified.get("body") or {}
                if int(body.get("code") or 0) == 1000:
                    data = body.get("data") or {}
                    user = data.get("user") or {}
                    team = data.get("team") or {}
                    latest = list(
                        (client.call("Network.getAllCookies") or {}).get("cookies") or []
                    )
                    page = _page_state(client)
                    return {
                        "cookie_header": cookie_header_from_records(latest),
                        "cookie_records": latest,
                        "cookies_json": latest,
                        "access_token": str(data.get("token") or ""),
                        "user_id": str(user.get("_id") or ""),
                        "team_id": str(team.get("_id") or ""),
                        "email": str(user.get("email") or account.get("email") or ""),
                        "user_agent": str(page.get("ua") or account.get("user_agent") or ""),
                        "profile_dir": str(_profile_path(account, settings)),
                        "cdp_port": port,
                        "status": "pending",
                        "last_error": "",
                        "last_login_at": int(time.time()),
                    }
                last_error = str(verified.get("error") or body.get("msg") or "")
            except Exception as exc:
                last_error = str(exc)

            last_page = _page_state(client)
            page_text = str(last_page.get("text") or "").lower()
            if any(
                marker in page_text
                for marker in (
                    "verify you are human",
                    "security checkpoint",
                    "checking your browser",
                )
            ):
                clicked_turnstile = _click_turnstile(client) or clicked_turnstile
                if bool(settings.chrome_headless):
                    raise AkoolBrowserChallengeError(
                        "Akool browser challenge requires an interactive Chrome; "
                        f"last page={last_page.get('url') or ''}"
                    )

            now = time.monotonic()
            if account.get("email") and account.get("password") and now >= next_login_attempt:
                result = _attempt_login(
                    client,
                    str(account.get("email") or ""),
                    str(account.get("password") or ""),
                )
                next_login_attempt = now + (4 if result.get("phase") == "open-login" else 15)
            time.sleep(1)

        challenge = " (Turnstile was detected)" if clicked_turnstile else ""
        raise AkoolBrowserError(
            "Akool login did not produce a valid session"
            f"{challenge}; last page={last_page.get('url') or ''}; {last_error}"
        )
    finally:
        client.close()


def stop_managed_browser(account_id: int, cdp_port: int) -> bool:
    stopped = False
    if cdp_port:
        try:
            version = _cdp_json(int(cdp_port), "/json/version", 1)
            url = version.get("webSocketDebuggerUrl")
            if url:
                client = _CDP(str(url))
                try:
                    client.call("Browser.close")
                    stopped = True
                finally:
                    client.close()
        except Exception:
            pass
    with _managed_lock:
        process = _managed.pop(int(account_id), None)
    if process and process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
            stopped = True
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    return stopped


def delete_managed_profile(account: dict[str, Any], settings: Any) -> None:
    account_id = int(account["id"])
    stop_managed_browser(account_id, _cdp_port(account, settings))
    profile = _profile_path(account, settings)
    root = Path(settings.chrome_user_data_root).resolve()
    if profile == root or root not in profile.parents:
        if account.get("profile_dir"):
            raise ValueError("refusing to delete a profile outside the managed root")
        return
    if profile.exists():
        shutil.rmtree(profile)


def reset_managed_profile(account: dict[str, Any], settings: Any) -> dict[str, Any]:
    delete_managed_profile(account, settings)
    profile = _profile_path({**account, "profile_dir": ""}, settings)
    profile.mkdir(parents=True, exist_ok=True)
    return {"profile_dir": str(profile), "cdp_port": _cdp_port(account, settings)}


def shutdown_managed_browsers() -> None:
    with _managed_lock:
        items = list(_managed.items())
    for account_id, process in items:
        if process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
        with _managed_lock:
            _managed.pop(account_id, None)
