"""Offline desktop lifecycle checks: no browser, subprocess, or external network."""
import asyncio
from functools import wraps
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock

import pytest
import requests

from app.login_desktop import LoginDesktop, LoginError, browser_proxy
import app.login_desktop as module


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(requests.Session, "request", lambda *a, **kw: pytest.fail("Real network forbidden"))


class Process:
    def __init__(self):
        self.returncode = None
        self.terminated = self.killed = 0
        self.stdout = type("Output", (), {"readline": AsyncMock(return_value=b"71\n")})()

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1

    async def wait(self):
        self.returncode = 0
        return 0


def warm(tmp_path):
    desktop = LoginDesktop(tmp_path / "account", "socks5://proxy.test:20001", cdp_port=19401)
    desktop.browser_process, desktop.display_process, desktop.window_manager = [Process() for _ in range(3)]
    desktop.processes = [desktop.display_process, desktop.window_manager, desktop.browser_process]
    desktop.profile.mkdir()
    (desktop.profile / "keep-session").write_text("fixture", encoding="utf-8")
    return desktop


@pytest.mark.parametrize("value", ["", "http://proxy.test:10809", "socks5://proxy.test:20001", "https://proxy.test:8443"])
def test_optional_proxy_preserves_canonical_binding(value):
    assert browser_proxy(value) == value


@pytest.mark.parametrize("value", ["ftp://proxy.test:90", "http://proxy.test", "http://proxy.test:no", "http://[broken",
    "http://user:password@proxy.test:10809", "socks5://proxy.test:10809/path", "http://proxy.test:10809?x=1"])
def test_invalid_or_authenticated_proxy_rejected_without_shell(value):
    with pytest.raises(LoginError) as error:
        browser_proxy(value)
    assert error.value.status == 422


@pytest.mark.parametrize("port", [True, 80, 65536, "bad"])
def test_invalid_cdp_port_is_a_validation_error(tmp_path, port):
    with pytest.raises(LoginError) as error:
        LoginDesktop(tmp_path, "", cdp_port=port)
    assert error.value.status == 422


@pytest.mark.parametrize("separate", [False, True])
def test_profile_owner_check_precedes_stale_lock_removal(tmp_path, separate):
    desktop = LoginDesktop(tmp_path / "profile", "")
    process = tmp_path / "proc" / "123"
    process.mkdir(parents=True)
    parts = [b"chromium", b"--user-data-dir", str(desktop.profile).encode()] if separate else [
        b"chromium", ("--user-data-dir=" + str(desktop.profile)).encode()]
    (process / "cmdline").write_bytes(b"\0".join(parts) + b"\0")
    with pytest.raises(LoginError, match="仍在使用"):
        desktop._clear_stale_profile_locks(process.parent)


@run_async
async def test_warm_start_reuses_browser_profile_and_only_reopens_viewer(tmp_path, monkeypatch):
    desktop = warm(tmp_path)
    browser = desktop.browser_process
    open_viewer = AsyncMock()
    monkeypatch.setattr(desktop, "_start_viewer", open_viewer)
    monkeypatch.setattr(desktop, "_spawn", AsyncMock(side_effect=AssertionError("No new browser")))
    await desktop.start()
    open_viewer.assert_awaited_once_with()
    assert desktop.browser_process is browser and desktop.alive()
    assert desktop.cdp_port == 19401 and desktop.proxy == "socks5://proxy.test:20001"


@run_async
async def test_revoke_closes_only_vnc_and_close_preserves_profile(tmp_path):
    desktop = warm(tmp_path)
    desktop.folder = tempfile.TemporaryDirectory(dir=tmp_path)
    desktop.password, desktop.vnc_port = "fixture", 33333
    password_file = Path(desktop.folder.name) / "vnc-password"
    password_file.write_text("fixture", encoding="utf-8")
    desktop.viewer_process = viewer = Process()
    desktop.processes.append(viewer)
    browser, display, wm = desktop.browser_process, desktop.display_process, desktop.window_manager
    unrelated = Process()
    await desktop.revoke_viewer()
    assert viewer.terminated == 1 and desktop.viewer_process is None
    assert desktop.password is None and desktop.vnc_port is None and not password_file.exists()
    assert desktop.alive() and not desktop.viewer_alive()
    assert browser.terminated == display.terminated == wm.terminated == unrelated.terminated == 0
    await desktop.close()
    assert browser.terminated == display.terminated == wm.terminated == 1
    assert unrelated.terminated == 0
    assert (desktop.profile / "keep-session").read_text(encoding="utf-8") == "fixture"
    assert not desktop.alive() and desktop.processes == []


@run_async
async def test_cold_launch_has_fixed_profile_proxy_ua_and_loopback_cdp(tmp_path, monkeypatch):
    desktop = LoginDesktop(tmp_path / "profile", "http://proxy.test:10809", cdp_port=19402,
                           chrome_executable="/configured/chromium", user_agent="Fixture-UA")
    calls = []
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(desktop, "available", lambda *args: True)
    monkeypatch.setattr(desktop, "_clear_stale_profile_locks", lambda: None)
    monkeypatch.setattr(desktop, "_assert_cdp_port_available", lambda: None)
    monkeypatch.setattr(desktop, "_cdp_ready", lambda: True)
    monkeypatch.setattr(desktop, "_start_viewer", AsyncMock())
    await desktop.start()
    arguments, options = next((args, kwargs) for args, kwargs in calls if args[0] == "/configured/chromium")
    assert "--remote-debugging-address=127.0.0.1" in arguments
    assert "--remote-debugging-port=19402" in arguments
    assert "--proxy-server=http://proxy.test:10809" in arguments
    assert "--user-agent=Fixture-UA" in arguments
    assert "--user-data-dir=" + str(desktop.profile) in arguments
    assert arguments[-1] == "https://akool.com/zh-cn/"
    assert "shell" not in options
    await desktop.close()


@run_async
async def test_cancelled_spawn_is_owned_and_viewer_can_be_revoked(tmp_path, monkeypatch):
    desktop = warm(tmp_path)
    gate = asyncio.Event()
    process = Process()
    async def spawn(*args, **kwargs):
        await gate.wait()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(desktop._spawn("x11vnc", role="viewer_process"))
    await asyncio.sleep(0)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert desktop.viewer_process is process and process in desktop.processes
    await desktop.revoke_viewer()
    assert process.terminated == 1 and process not in desktop.processes
    assert desktop.alive()


@run_async
async def test_failed_cold_launch_cleans_owned_children_without_deleting_profile(tmp_path, monkeypatch):
    desktop = LoginDesktop(tmp_path / "profile", "", cdp_port=19403)
    owned = Process()
    owned.stdout.readline = AsyncMock(return_value=b"not-a-display\n")
    async def spawn(*args, **kwargs):
        return owned
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(desktop, "available", lambda *args: True)
    monkeypatch.setattr(desktop, "_clear_stale_profile_locks", lambda: None)
    monkeypatch.setattr(desktop, "_assert_cdp_port_available", lambda: None)
    with pytest.raises(LoginError, match="桌面启动失败"):
        await desktop.start()
    assert owned.terminated == 1 and desktop.profile.is_dir()
    assert desktop.processes == [] and desktop.folder is None


def test_existing_cdp_listener_is_not_adopted_or_stopped(tmp_path, monkeypatch):
    desktop = LoginDesktop(tmp_path / "profile", "", cdp_port=19405)
    listener = type("Listener", (), {"__enter__": lambda self: self,
        "__exit__": lambda *args: None,
        "bind": lambda *args: (_ for _ in ()).throw(OSError("already bound"))})()
    monkeypatch.setattr(module.socket, "socket", lambda: listener)
    with pytest.raises(LoginError, match="CDP 端口仍被使用") as error:
        desktop._assert_cdp_port_available()
    assert error.value.status == 409 and desktop.processes == []
