"""Owned headed Chromium desktop for one Akool account's persistent profile."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import secrets
import shutil
import socket
import tempfile
from urllib.parse import urlsplit

import requests


AKOOL_HOME = "https://akool.com/zh-cn/"
NOVNC_ROOT = Path("/usr/share/novnc")


class LoginError(Exception):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def browser_proxy(value: str) -> str:
    """Validate the already-normalized account proxy; direct access is allowed."""
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise LoginError("浏览器代理端口无效", 422) from exc
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname or not port:
        raise LoginError("浏览器代理必须是 HTTP 或 SOCKS5 host:port 地址", 422)
    if parsed.username or parsed.password:
        raise LoginError("浏览器不支持带认证的代理，请使用本地免认证代理桥接", 422)
    if parsed.path or parsed.query or parsed.fragment:
        raise LoginError("浏览器代理不能包含路径、查询参数或片段", 422)
    # Preserve the canonical account binding rather than silently changing its URL.
    return value


class LoginDesktop:
    def __init__(self, profile: Path, proxy: str, *, cdp_port: int | None = None,
                 chrome_executable: str = "chromium", user_agent: str | None = None) -> None:
        self.profile = Path(profile)
        self.proxy = browser_proxy(proxy)
        self.chrome_executable = str(chrome_executable or "chromium")
        self.user_agent = str(user_agent or "").strip()
        if any(character in self.user_agent for character in ("\r", "\n", "\0")):
            raise LoginError("浏览器 User-Agent 无效", 422)
        try:
            if cdp_port is not None and (isinstance(cdp_port, bool) or not 1024 <= int(cdp_port) <= 65535):
                raise ValueError
            self.cdp_port = int(cdp_port) if cdp_port is not None else None
        except (TypeError, ValueError) as exc:
            raise LoginError("浏览器 CDP 端口无效", 422) from exc
        self.folder: tempfile.TemporaryDirectory | None = None
        self.processes: list[asyncio.subprocess.Process] = []
        self.browser_process: asyncio.subprocess.Process | None = None
        self.viewer_process: asyncio.subprocess.Process | None = None
        self.display_process: asyncio.subprocess.Process | None = None
        self.window_manager: asyncio.subprocess.Process | None = None
        self.display: str | None = None
        self.vnc_port: int | None = None
        self.password: str | None = None
        self._environment: dict[str, str] | None = None
        self._lifecycle_lock = asyncio.Lock()

    @staticmethod
    def available(chrome_executable: str = "chromium") -> bool:
        return (os.name == "posix"
                and all(shutil.which(command) for command in
                        ("Xvfb", "x11vnc", "fluxbox", chrome_executable))
                and (NOVNC_ROOT / "core/rfb.js").is_file())

    @staticmethod
    def _port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    async def _spawn(self, *args: str, role: str | None = None, **kwargs) -> asyncio.subprocess.Process:
        if role not in {None, "viewer_process", "browser_process", "display_process", "window_manager"}:
            raise ValueError("unsupported subprocess role")

        def owned(process):
            self.processes.append(process)
            if role:
                setattr(self, role, process)
            return process

        task = asyncio.create_task(asyncio.create_subprocess_exec(
            *args, stderr=asyncio.subprocess.DEVNULL, **kwargs))
        try:
            process = await asyncio.shield(task)
        except asyncio.CancelledError:
            owned(await task)
            raise
        return owned(process)

    def _clear_stale_profile_locks(self, proc_root: Path = Path("/proc")) -> None:
        expected = self.profile.resolve()
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                arguments = (entry / "cmdline").read_bytes().split(b"\0")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            for index, argument in enumerate(arguments):
                candidate = None
                if argument.startswith(b"--user-data-dir="):
                    candidate = os.fsdecode(argument.split(b"=", 1)[1])
                elif argument == b"--user-data-dir" and index + 1 < len(arguments):
                    candidate = os.fsdecode(arguments[index + 1])
                if candidate and Path(candidate).resolve() == expected:
                    raise LoginError("该账号浏览器仍在使用中，请稍后重试", 409)
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = self.profile / name
            if path.is_symlink():
                path.unlink()

    def _assert_cdp_port_available(self) -> None:
        if not self.cdp_port:
            return
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", self.cdp_port))
        except OSError as exc:
            raise LoginError("账号 CDP 端口仍被使用，请关闭原浏览器后重试", 409) from exc

    def alive(self) -> bool:
        """The reusable browser remains alive even after its viewer is revoked."""
        return all(process is not None and process.returncode is None for process in
                   (self.browser_process, self.display_process, self.window_manager))

    def viewer_alive(self) -> bool:
        return (self.alive() and self.viewer_process is not None
                and self.viewer_process.returncode is None
                and bool(self.password) and bool(self.vnc_port))

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.alive():
                await self._start_viewer()
                return
            if self.processes:
                await self._close()
            if not self.available(self.chrome_executable):
                raise LoginError("登录桌面组件不可用，需要 Chromium、Xvfb、x11vnc、Fluxbox 和 noVNC", 503)
            self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.profile.chmod(0o700)
            self._clear_stale_profile_locks()
            self._assert_cdp_port_available()
            self.folder = tempfile.TemporaryDirectory(prefix="ak2api-login-")
            try:
                self.display_process = await self._spawn(
                    "Xvfb", "-displayfd", "1", "-screen", "0", "1920x900x24",
                    "-nolisten", "tcp", "-ac", stdout=asyncio.subprocess.PIPE)
                line = await asyncio.wait_for(self.display_process.stdout.readline(), 10)
                number = line.decode().strip()
                if not number.isdigit() or self.display_process.returncode is not None:
                    raise LoginError("登录桌面启动失败，请稍后重试", 503)
                self.display = ":" + number
                home = self.profile.with_name(self.profile.name + "-home")
                config, cache = home / ".config", home / ".cache"
                for directory in (home, config, cache):
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    directory.chmod(0o700)
                self._environment = {**os.environ, "DISPLAY": self.display, "HOME": str(home),
                                     "XDG_CONFIG_HOME": str(config), "XDG_CACHE_HOME": str(cache)}
                self.window_manager = await self._spawn("fluxbox", env=self._environment,
                                                       stdout=asyncio.subprocess.DEVNULL)
                self.cdp_port = self.cdp_port or self._port()
                arguments = [self.chrome_executable, f"--user-data-dir={self.profile}",
                    "--profile-directory=Default", "--no-sandbox", "--disable-dev-shm-usage",
                    "--password-store=basic", "--no-first-run", "--no-default-browser-check",
                    "--start-maximized", "--window-size=1920,900", "--window-position=0,0",
                    "--remote-debugging-address=127.0.0.1", "--remote-allow-origins=*",
                    f"--remote-debugging-port={self.cdp_port}"]
                if self.proxy:
                    arguments.append("--proxy-server=" + self.proxy)
                if self.user_agent:
                    arguments.append("--user-agent=" + self.user_agent)
                arguments.append(AKOOL_HOME)
                self.browser_process = await self._spawn(*arguments, env=self._environment,
                                                        stdout=asyncio.subprocess.DEVNULL)
                for _ in range(100):
                    if self.browser_process.returncode is not None:
                        break
                    try:
                        if await asyncio.to_thread(self._cdp_ready):
                            await self._start_viewer()
                            return
                    except requests.RequestException:
                        pass
                    await asyncio.sleep(.1)
                raise LoginError("登录浏览器启动失败，请稍后重试", 503)
            except BaseException:
                await self._close()
                raise

    def _cdp_ready(self) -> bool:
        with requests.Session() as client:
            client.trust_env = False
            response = client.get(f"http://127.0.0.1:{self.cdp_port}/json/version", timeout=1)
            return response.ok

    async def start_viewer(self) -> None:
        async with self._lifecycle_lock:
            if not self.alive():
                raise LoginError("账号浏览器已关闭，请重新打开", 409)
            await self._start_viewer()

    async def _start_viewer(self) -> None:
        if self.viewer_alive():
            return
        await self._revoke_viewer()
        if not self.folder:
            raise LoginError("登录桌面已关闭，请重新打开", 409)
        self.password = secrets.token_urlsafe(6)
        password_file = Path(self.folder.name) / "vnc-password"
        password_file.write_text(self.password + "\n", encoding="ascii")
        password_file.chmod(0o600)
        self.vnc_port = self._port()
        try:
            self.viewer_process = await self._spawn("x11vnc", "-display", self.display,
                "-rfbport", str(self.vnc_port), "-listen", "127.0.0.1", "-localhost", "-no6",
                "-forever", "-shared", "-passwdfile", str(password_file), "-norc", "-noremote",
                "-nocmds", "-novncconnect", "-quiet", env=self._environment,
                stdout=asyncio.subprocess.DEVNULL, role="viewer_process")
            for _ in range(50):
                writer = None
                try:
                    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.vnc_port), 1)
                    banner = await asyncio.wait_for(reader.readexactly(12), 1)
                    if banner.startswith(b"RFB "):
                        return
                except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                    pass
                finally:
                    if writer:
                        writer.close()
                        try:
                            await asyncio.wait_for(writer.wait_closed(), 1)
                        except (OSError, asyncio.TimeoutError):
                            pass
                if self.viewer_process.returncode is not None:
                    break
                await asyncio.sleep(.1)
            raise LoginError("远程登录画面启动失败，请稍后重试", 503)
        except BaseException:
            await self._revoke_viewer()
            raise

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        else:
            await process.wait()

    async def _revoke_viewer(self) -> None:
        process, self.viewer_process = self.viewer_process, None
        if process:
            await self._stop(process)
            if process in self.processes:
                self.processes.remove(process)
        if self.folder:
            (Path(self.folder.name) / "vnc-password").unlink(missing_ok=True)
        self.password = None
        self.vnc_port = None

    async def revoke_viewer(self) -> None:
        async with self._lifecycle_lock:
            await self._revoke_viewer()

    async def _close(self) -> None:
        # Only direct subprocess handles created by this object are terminated.
        for process in reversed(self.processes):
            await self._stop(process)
        self.processes.clear()
        self.browser_process = self.viewer_process = self.display_process = self.window_manager = None
        if self.folder:
            self.folder.cleanup()
            self.folder = None
        self.password = self.vnc_port = self.display = None
        self._environment = None

    async def close(self) -> None:
        async with self._lifecycle_lock:
            await self._close()
