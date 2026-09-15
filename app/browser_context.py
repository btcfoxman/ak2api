from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websocket

from app.config import normalize_proxy_url, rewrite_loopback_proxy
from app.cookies import cookie_header_from_records, cookie_records


AKOOL_LOGIN_PAGE = "https://akool.com/zh-cn/pricing"
AKOOL_VERIFY_PATH = "/interface/user-api/api/v6/verify/user"

# The site handles login in JavaScript. Never allow its fallback GET form to
# put credentials in navigation URLs before React has attached its handler.
_LOGIN_SUBMIT_GUARD = r"""
(() => {
  if (window.__akLoginSubmitGuard) return;
  window.__akLoginSubmitGuard = true;
  window.addEventListener('submit', event => {
    const form = event.target;
    if (form instanceof HTMLFormElement && form.method.toLowerCase() === 'get' &&
        form.querySelector('input[type="password"]')) event.preventDefault();
  });
})()
"""


class AkoolBrowserError(RuntimeError):
    pass


class AkoolBrowserChallengeError(AkoolBrowserError):
    pass


class AkoolBrowserTransportError(AkoolBrowserError):
    pass


class AkoolBrowserAccountSuspended(AkoolBrowserError):
    pass


def _safe_page_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "<unknown>"
    try:
        parsed = urlsplit(raw)
        query = urlencode(
            [
                (
                    key,
                    "REDACTED"
                    if any(
                        marker in key.lower()
                        for marker in ("email", "password", "passwd", "token", "secret")
                    )
                    else item,
                )
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
    except (TypeError, ValueError):
        return raw.split("?", 1)[0]


class _CDP:
    def __init__(self, url: str, timeout: float = 20):
        try:
            self.socket = websocket.create_connection(
                url,
                timeout=timeout,
                suppress_origin=True,
                enable_multithread=False,
            )
        except Exception as exc:
            raise AkoolBrowserTransportError(
                f"CDP WebSocket connection failed: {exc}"
            ) from exc
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
                self.socket.settimeout(max(min(deadline - time.monotonic(), 1), 0.05))
                raw = self.socket.recv()
                if not raw:
                    continue
                payload = json.loads(raw)
            except websocket.WebSocketTimeoutException:
                continue
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise AkoolBrowserTransportError(
                    f"CDP {method} transport failed: {exc}"
                ) from exc
            if payload.get("id") != command_id:
                continue
            if payload.get("error"):
                message = payload["error"].get("message") or payload["error"]
                raise AkoolBrowserTransportError(f"CDP {method} failed: {message}")
            return payload.get("result") or {}
        raise AkoolBrowserTransportError(f"CDP {method} timed out")

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
_account_locks: dict[int, threading.RLock] = {}


def _account_lock(account_id: int) -> threading.RLock:
    with _managed_lock:
        return _account_locks.setdefault(int(account_id), threading.RLock())


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
                    and "akool.com" in str(item.get("url") or "")
                ),
                None,
            )
            if page is None:
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
    raise AkoolBrowserTransportError(
        f"Chrome did not establish CDP on port {port}: {last_error}"
    )


def _endpoint_ready(port: int) -> bool:
    try:
        _cdp_json(int(port), "/json/version", 1)
        return True
    except Exception:
        return False


def _profile_process_running(profile_dir: Path) -> bool:
    if os.name == "nt":
        return False
    expected = profile_dir.resolve()
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return False
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit():
            continue
        try:
            arguments = (process_dir / "cmdline").read_bytes().split(b"\0")
        except (OSError, PermissionError):
            continue
        for argument in arguments:
            if not argument.startswith(b"--user-data-dir="):
                continue
            try:
                candidate = Path(os.fsdecode(argument.split(b"=", 1)[1])).resolve()
            except (OSError, ValueError):
                continue
            if candidate == expected:
                return True
    return False


def _clear_stale_profile_singletons(profile_dir: Path) -> list[str]:
    if os.name == "nt" or _profile_process_running(profile_dir):
        return []
    removed: list[str] = []
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        artifact = profile_dir / name
        try:
            if artifact.is_symlink() or artifact.is_file():
                artifact.unlink()
                removed.append(name)
        except FileNotFoundError:
            continue
    return removed


def _chrome_log_excerpt(path: Path, limit: int = 1200) -> str:
    try:
        content = path.read_bytes()[-16_384:].decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [" ".join(line.split()) for line in content.splitlines() if line.strip()]
    return " | ".join(lines[-4:])[-limit:]


def _launch(
    account: dict[str, Any], settings: Any
) -> tuple[subprocess.Popen[Any] | None, int]:
    account_id = int(account["id"])
    port = _cdp_port(account, settings)
    if _endpoint_ready(port):
        return None, port

    profile = _profile_path(account, settings)
    profile.mkdir(parents=True, exist_ok=True)
    _clear_stale_profile_singletons(profile)
    proxy = rewrite_loopback_proxy(
        normalize_proxy_url(str(account.get("proxy_url") or "")),
        str(settings.proxy_host_override or ""),
    )
    if proxy:
        parsed_proxy = urlsplit(proxy)
        if parsed_proxy.username or parsed_proxy.password:
            raise AkoolBrowserError(
                "authenticated Chrome proxies require a local unauthenticated bridge"
            )
    command = [
        _chrome_executable(str(settings.chrome_executable or "")),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--profile-directory=Default",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
    ]
    if proxy:
        command.append(f"--proxy-server={proxy}")
    if bool(settings.chrome_headless):
        command.extend(["--headless=new", "--window-size=1280,960"])
    if os.name != "nt":
        command.extend(["--no-sandbox", "--disable-dev-shm-usage"])
    command.append(AKOOL_LOGIN_PAGE)
    chrome_log_path = profile / "chrome-launch.log"
    with chrome_log_path.open("ab", buffering=0) as chrome_log:
        chrome_log.write(
            f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] launching Chrome\n".encode()
        )
        process = subprocess.Popen(
            command,
            stdout=chrome_log,
            stderr=subprocess.STDOUT,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt" and bool(settings.chrome_headless)
                else 0
            ),
        )
    with _managed_lock:
        _managed[account_id] = process
    try:
        _wait_target(port, min(int(settings.browser_timeout_seconds), 45))
    except Exception:
        if process.poll() is not None:
            detail = _chrome_log_excerpt(chrome_log_path)
            raise AkoolBrowserTransportError(
                f"Google Chrome exited before CDP became ready ({process.returncode})"
                + (f": {detail}" if detail else "")
            )
        raise
    return process, port


def _safe_cookie_records(account: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    records = cookie_records(account.get("cookie_records") or account.get("cookies_json"))
    if not records and str(account.get("cookie_header") or "").strip():
        parsed = SimpleCookie()
        try:
            parsed.load(str(account.get("cookie_header") or ""))
        except Exception:
            parsed = SimpleCookie()
        records = [
            {"name": name, "value": morsel.value, "domain": ".akool.com", "path": "/"}
            for name, morsel in parsed.items()
        ]
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
        r"""
JSON.stringify((() => {
  const visible = (el) => Boolean(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const text = (document.body && document.body.innerText || '').slice(0, 5000);
  const email = [...document.querySelectorAll('input[type="email"],input[name="email"],input[autocomplete="email"]')].find(visible);
  const password = [...document.querySelectorAll('input[type="password"],input[name="password"],input[autocomplete="current-password"]')].find(visible);
  const challenge = /verify you are human|security checkpoint|checking your browser|please confirm you are human|验证您是人类|驗證您是人類|确认您是人类|確認您是人類|检查您的浏览器|檢查您的瀏覽器/i.test(`${document.title || ''}\n${text}`);
  const invalidCredentials = /incorrect password|invalid (?:email|account|credentials)|account does not exist|wrong password|邮箱或密码|账号或密码/i.test(text);
  return {
    url: location.href,
    ua: navigator.userAgent || '',
    title: document.title || '',
    text,
    hasEmail: Boolean(email),
    hasPassword: Boolean(password),
    hasChallenge: Boolean(challenge),
    invalidCredentials: Boolean(invalidCredentials)
  };
})())
        """
    )
    try:
        state = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    if not state.get("hasChallenge"):
        state["hasChallenge"] = _challenge_frame_visible(client)
    return state


def _challenge_frame_visible(client: _CDP) -> bool:
    """Inspect visible CF frames, including closed shadow roots, without interacting."""
    document = client.call("DOM.getDocument", {"depth": -1, "pierce": True})
    pending = [document.get("root") or {}]
    while pending:
        node = pending.pop()
        pending.extend(node.get("children") or [])
        pending.extend(node.get("shadowRoots") or [])
        if node.get("contentDocument"):
            pending.append(node["contentDocument"])
        if node.get("nodeName") != "IFRAME":
            continue
        attributes = node.get("attributes") or []
        attrs = dict(zip(attributes[::2], attributes[1::2]))
        try:
            host = urlsplit(attrs.get("src", "")).hostname
        except ValueError:
            continue
        if host != "challenges.cloudflare.com":
            continue
        try:
            obj = client.call("DOM.resolveNode", {"backendNodeId": node["backendNodeId"]})
            object_id = (obj.get("object") or {}).get("objectId")
            if not object_id:
                continue
            try:
                result = client.call("Runtime.callFunctionOn", {
                    "objectId": object_id,
                    "functionDeclaration": """function() {
                      const r = this.getBoundingClientRect();
                      if (!this.isConnected || r.width < 120 || r.height < 40) return false;
                      if (typeof this.checkVisibility === 'function')
                        return this.checkVisibility({checkOpacity:true, checkVisibilityCSS:true});
                      for (let node = this; node; node = node.parentElement || node.getRootNode().host) {
                        const style = getComputedStyle(node);
                        if (style.display === 'none' || style.visibility === 'hidden' ||
                            style.visibility === 'collapse' || Number(style.opacity) === 0) return false;
                      }
                      return true;
                    }""",
                    "returnByValue": True,
                })
                if (result.get("result") or {}).get("value"):
                    return True
            finally:
                try:
                    client.call("Runtime.releaseObject", {"objectId": object_id})
                except AkoolBrowserTransportError:
                    pass
        except AkoolBrowserTransportError:
            # Turnstile may replace its frame while it retries a challenge.
            continue
    return False


def _challenge_error(page: dict[str, Any]) -> AkoolBrowserChallengeError:
    return AkoolBrowserChallengeError(
        "Akool browser challenge requires manual verification"
        f"; last page={_safe_page_url(page.get('url'))}"
    )


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


def _verified_context(client: _CDP, account: dict[str, Any], settings: Any,
                      body: dict[str, Any]) -> dict[str, Any]:
    data = body.get("data") or {}
    user = data.get("user") or {}
    team = data.get("team") or {}
    email = str(user.get("email") or "")
    if not email or (account.get("email") and email.casefold() != str(account["email"]).casefold()):
        raise AkoolBrowserError("Browser session belongs to a different account")
    if account.get("user_id") and str(user.get("_id") or "") != str(account["user_id"]):
        raise AkoolBrowserError("Browser session user id does not match this account")
    latest = [item for item in client.call("Network.getAllCookies").get("cookies", [])
              if str(item.get("domain") or "").lstrip(".") == "akool.com"
              or str(item.get("domain") or "").endswith(".akool.com")]
    return {
        "cookie_header": cookie_header_from_records(latest),
        "cookie_records": latest, "cookies_json": latest,
        "access_token": str(data.get("token") or ""),
        "user_id": str(user.get("_id") or ""), "team_id": str(team.get("_id") or ""),
        "email": email, "user_agent": str(client.evaluate("navigator.userAgent") or ""),
        "profile_dir": str(_profile_path(account, settings)),
        "cdp_port": _cdp_port(account, settings), "status": "pending",
        "last_error": "", "last_login_at": int(time.time()),
    }


def _attempt_login(client: _CDP, email: str, password: str) -> dict[str, Any]:
    client.evaluate(_LOGIN_SUBMIT_GUARD)
    expression = f"""
    (async () => {{
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
      const form = passwordInput.closest('form');
      const props = form && Object.keys(form).find(key => key.startsWith('__reactProps'));
      if (!props || typeof form[props].onSubmit !== 'function')
        return {{phase:'waiting-for-form', submitted:false}};
      set(emailInput, email);
      set(passwordInput, password);
      await new Promise(resolve => setTimeout(resolve, 0));
      if (!form.isConnected) return {{phase:'waiting-for-form', submitted:false}};
      const submit = (form && form.querySelector('button[type="submit"],input[type="submit"]')) ||
        [...document.querySelectorAll('button')].find(el => visible(el) && /log in|login|sign in|登录/i.test(el.innerText || ''));
      if (submit && submit.disabled) return {{phase:'waiting-for-form', submitted:false}};
      if (submit) submit.click(); else if (form) form.requestSubmit();
      return {{phase:'submitted', submitted:!!(submit || form)}};
    }})()
    """
    value = client.evaluate(expression)
    return value if isinstance(value, dict) else {}


def refresh_account_context(account: dict[str, Any], settings: Any) -> dict[str, Any]:
    account_id = int(account.get("id") or 0)
    if not account_id:
        raise AkoolBrowserError("CDP login requires a persisted account id")
    with _account_lock(account_id):
        port = _cdp_port(account, settings)
        profile = _profile_path(account, settings)
        client = _open_cdp_with_recovery(account, settings, port, profile)
        try:
            imported = _safe_cookie_records(account)
            if imported:
                client.call("Network.setCookies", {"cookies": imported})
            client.call("Page.navigate", {"url": AKOOL_LOGIN_PAGE})
            deadline = time.monotonic() + int(settings.browser_timeout_seconds)
            last_submit_at = 0.0
            last_open_at = 0.0
            challenge_started_at: float | None = None
            submitted = False
            last_form_signature: tuple[bool, bool] | None = None
            last_page: dict[str, Any] = {}
            last_error = ""
            while time.monotonic() < deadline:
                try:
                    verified = _browser_verify(client)
                    body = verified.get("body") or {}
                    if int(body.get("code") or 0) == 1000:
                        return _verified_context(client, account, settings, body)
                    last_error = str(
                        verified.get("error")
                        or body.get("msg")
                        or body.get("message")
                        or ""
                    )
                    if int(body.get("code") or 0) == 1108 or (
                        "account has been suspended" in last_error.lower()
                    ):
                        raise AkoolBrowserAccountSuspended(
                            "Akool account has been suspended"
                        )
                except AkoolBrowserTransportError:
                    raise
                except AkoolBrowserAccountSuspended:
                    raise
                except Exception as exc:
                    last_error = str(exc)

                last_page = _page_state(client)
                now = time.monotonic()
                if last_page.get("hasChallenge"):
                    if challenge_started_at is None:
                        challenge_started_at = now
                    if now - challenge_started_at >= int(
                        settings.browser_challenge_grace_seconds
                    ):
                        raise _challenge_error(last_page)
                    time.sleep(1)
                    continue
                challenge_started_at = None
                if last_page.get("invalidCredentials") and submitted:
                    raise AkoolBrowserError("Akool rejected the email or password")

                signature = (
                    bool(last_page.get("hasEmail")),
                    bool(last_page.get("hasPassword")),
                )
                if signature != last_form_signature:
                    submitted = False
                    last_form_signature = signature
                if account.get("email") and account.get("password"):
                    if any(signature) and (not submitted or now - last_submit_at >= 30):
                        result = _attempt_login(
                            client,
                            str(account.get("email") or ""),
                            str(account.get("password") or ""),
                        )
                        submitted = bool(result.get("submitted"))
                        last_submit_at = now
                    elif not any(signature) and now - last_open_at >= 5:
                        _attempt_login(
                            client,
                            str(account.get("email") or ""),
                            str(account.get("password") or ""),
                        )
                        last_open_at = now
                time.sleep(1)

            if last_page.get("hasChallenge"):
                raise _challenge_error(last_page)
            raise AkoolBrowserError(
                "Akool login did not produce a valid session"
                f"; last page={_safe_page_url(last_page.get('url'))}"
                + (f"; {last_error}" if last_error else "")
            )
        finally:
            client.close()


def _open_cdp_with_recovery(
    account: dict[str, Any], settings: Any, port: int, profile: Path
) -> _CDP:
    if not _endpoint_ready(port):
        _launch(account, settings)
    try:
        target = _wait_target(port, min(int(settings.browser_timeout_seconds), 60))
        client = _CDP(str(target["webSocketDebuggerUrl"]), timeout=30)
        client.call("Network.enable")
        client.call("Page.enable")
        client.call("Page.addScriptToEvaluateOnNewDocument", {"source": _LOGIN_SUBMIT_GUARD})
        client.evaluate(_LOGIN_SUBMIT_GUARD)
        return client
    except AkoolBrowserTransportError:
        _stop_managed_browser_unlocked(int(account["id"]), port)
        _wait_for_endpoint_closed(port)
        _launch(account, settings)
        try:
            target = _wait_target(port, min(int(settings.browser_timeout_seconds), 60))
            client = _CDP(str(target["webSocketDebuggerUrl"]), timeout=30)
            client.call("Network.enable")
            client.call("Page.enable")
            client.call("Page.addScriptToEvaluateOnNewDocument", {"source": _LOGIN_SUBMIT_GUARD})
            client.evaluate(_LOGIN_SUBMIT_GUARD)
            return client
        except Exception as exc:
            raise AkoolBrowserTransportError(
                f"CDP recovery failed after browser restart: {exc}"
            ) from exc


def _stop_managed_browser_unlocked(account_id: int, cdp_port: int) -> bool:
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


def stop_managed_browser(account_id: int, cdp_port: int) -> bool:
    with _account_lock(int(account_id)):
        return _stop_managed_browser_unlocked(int(account_id), int(cdp_port or 0))


def _wait_for_endpoint_closed(cdp_port: int, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while _endpoint_ready(cdp_port) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _endpoint_ready(cdp_port):
        raise AkoolBrowserTransportError(
            "Google Chrome did not stop before profile reset"
        )


def _managed_profile_child(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _remove_profile(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def delete_managed_profile(account: dict[str, Any], settings: Any) -> dict[str, Any]:
    account_id = int(account["id"])
    with _account_lock(account_id):
        port = _cdp_port(account, settings)
        stopped = _stop_managed_browser_unlocked(account_id, port)
        _wait_for_endpoint_closed(port)
        root = Path(settings.chrome_user_data_root).resolve()
        target = (root / f"account-{account_id}").resolve()
        current = _profile_path(account, settings)
        removed: list[str] = []
        for profile in dict.fromkeys((current, target)):
            if not _managed_profile_child(profile, root) or not profile.exists():
                continue
            _remove_profile(profile)
            removed.append(str(profile))
        return {
            "browser_stopped": stopped,
            "removed_profiles": removed,
            "external_profile_preserved": (
                str(current)
                if current.exists() and not _managed_profile_child(current, root)
                else ""
            ),
        }


def reset_managed_profile(account: dict[str, Any], settings: Any) -> dict[str, Any]:
    account_id = int(account["id"])
    with _account_lock(account_id):
        deletion = delete_managed_profile(account, settings)
        profile = _profile_path({**account, "profile_dir": ""}, settings)
        if profile.exists():
            _remove_profile(profile)
        profile.mkdir(parents=True, exist_ok=False)
        return {
            "profile_dir": str(profile),
            "cdp_port": _cdp_port(account, settings),
            **deletion,
        }


def shutdown_managed_browsers() -> None:
    with _managed_lock:
        account_ids = list(_managed)
    for account_id in account_ids:
        with _account_lock(account_id):
            with _managed_lock:
                process = _managed.pop(account_id, None)
            if process and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
