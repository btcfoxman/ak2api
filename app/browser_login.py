"""Interactive login leases backed by the same account profiles used for generation."""

from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from pathlib import Path

from app.browser_context import (
    AkoolBrowserConsentError,
    browser_binding,
    capture_native_context,
    stop_idle_account_browser,
)
from app.config import normalize_proxy_url, rewrite_loopback_proxy
from app.login_desktop import LoginDesktop, LoginError, browser_proxy


class BrowserLoginManager:
    LEASE_SECONDS = 20 * 60
    SYNC_SECONDS = 5

    def __init__(self, store, service):
        self.store, self.service = store, service
        self.active = None
        self.desktops = {}
        self.lock = asyncio.Lock()
        self.maintenance = None
        self.closed = False

    def status(self):
        current = self.active
        return {
            "available": LoginDesktop.available(
                self.service.settings.chrome_executable or "chromium"
            ),
            "active": (
                {
                    "account_id": current["account_id"],
                    "session_id": current["id"],
                    "phase": current["phase"],
                    "remaining_seconds": max(
                        0, int(current["deadline"] - time.monotonic())
                    ),
                }
                if current
                else None
            ),
        }

    def create_pending(self, body):
        name = str(body.get("name") or "").strip() or "Online-" + uuid.uuid4().hex[:10]
        if len(name) > 120:
            raise LoginError("account name is too long", 422)
        if self.store.find_account_by_identity({"name": name}):
            raise LoginError("account name already exists", 409)
        proxy = browser_proxy(normalize_proxy_url(str(body.get("proxy_url") or "")))
        try:
            concurrency = int(
                body.get("max_concurrency")
                or self.service.settings.account_default_concurrency
            )
        except (ValueError, TypeError) as exc:
            raise LoginError("invalid account concurrency", 422) from exc
        if not 1 <= concurrency <= 100:
            raise LoginError("invalid account concurrency", 422)
        account = self.service.upsert_account(
            {
                "name": name,
                "proxy_url": proxy,
                "max_concurrency": concurrency,
                "enabled": False,
                "auto_login": False,
                "status": "login_required",
                "use_proxy_pool": bool(body.get("use_proxy_pool", True)),
            },
            start_login=False,
        )
        self.store.update_account(
            int(account["id"]),
            {
                "session_source": "online_draft",
                "online_draft": 1,
                "status": "login_required",
            },
        )
        return self.store.get_account(int(account["id"]), include_secrets=False)

    def viewer(self, ticket):
        current = self.active
        if (
            not current
            or current["phase"] != "login"
            or not ticket
            or not secrets.compare_digest(str(current["ticket"]), str(ticket))
            or time.monotonic() >= current["deadline"]
            or not current["desktop"].alive()
        ):
            return None
        return current

    def _owned(self, session_id):
        current = self.active
        if not current or not secrets.compare_digest(
            str(current["id"]), str(session_id)
        ):
            raise LoginError("login window has ended", 404)
        if time.monotonic() >= current["deadline"]:
            raise LoginError("login window has expired", 409)
        return current

    def start_maintenance(self):
        if not self.maintenance:
            self.maintenance = asyncio.create_task(
                self._maintain(), name="ak-online-session-sync"
            )

    async def start(self, account_id):
        async with self.lock:
            if self.closed or not LoginDesktop.available(
                self.service.settings.chrome_executable or "chromium"
            ):
                raise LoginError("online login desktop is unavailable", 503)
            if self.active:
                raise LoginError("another login window is already open", 409)
            account_id = int(account_id)
            account = self.store.get_account(account_id)
            if not account:
                raise LoginError("account not found", 404)
            proxy = browser_proxy(
                rewrite_loopback_proxy(
                    str(account.get("proxy_url") or ""),
                    str(self.service.settings.proxy_host_override or ""),
                )
            )
            session_id = secrets.token_urlsafe(24)
            try:
                previous = self.service.begin_online_login(
                    account_id, session_id, int(time.time()) + self.LEASE_SECONDS
                )
            except (ValueError, KeyError) as exc:
                raise LoginError(str(exc), 409) from exc
            desktop = self.desktops.get(account_id)
            lease = {
                "id": session_id,
                "account_id": account_id,
                "ticket": secrets.token_urlsafe(32),
                "deadline": time.monotonic() + self.LEASE_SECONDS,
                "phase": "starting",
                "desktop": desktop,
                "previous": previous,
                "last_identity_ok": False,
                "captured": False,
            }
            self.active = lease
            try:
                expected = browser_binding(account, self.service.settings)
                if desktop and (
                    str(desktop.profile.resolve()) != expected["profile"]
                    or desktop.proxy != proxy
                    or desktop.cdp_port != expected["port"]
                    or str(getattr(desktop, "user_agent", "") or "")
                    != str(account.get("user_agent") or "")
                ):
                    await desktop.close()
                    self.desktops.pop(account_id, None)
                    desktop = None
                if not desktop:
                    # The DB lease already prevents a generation/login race.
                    # Stop only this idle account's existing profile browser.
                    await asyncio.to_thread(
                        stop_idle_account_browser, account, self.service.settings
                    )
                    desktop = LoginDesktop(
                        Path(expected["profile"]),
                        proxy,
                        cdp_port=expected["port"],
                        chrome_executable=self.service.settings.chrome_executable
                        or "chromium",
                        user_agent=account.get("user_agent") or None,
                    )
                    self.desktops[account_id] = desktop
                    lease["desktop"] = desktop
                await desktop.start()
                lease["phase"] = "login"
                return self.status()
            except BaseException as exc:
                self.service.end_online_login(
                    account_id,
                    session_id,
                    enabled=False,
                    status=(
                        previous["status"]
                        if not previous.get("enabled")
                        else "login_required"
                    ),
                    error="online login desktop could not start",
                )
                self.active = None
                if desktop:
                    await desktop.close()
                    self.desktops.pop(account_id, None)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise LoginError(
                    "login desktop did not start; please retry", 503
                ) from exc

    def _capture(self, lease):
        account = self.store.get_account(lease["account_id"])
        if not account:
            raise LoginError("account not found", 404)
        try:
            context = capture_native_context(account, self.service.settings)
        except AkoolBrowserConsentError as exc:
            self.store.update_account(lease["account_id"], {
                "status": "agreement_required", "last_error": str(exc),
                "last_checked_at": int(time.time()),
            })
            raise
        updated = self.store.apply_browser_context(
            lease["account_id"],
            context,
            expected_revision=int(account.get("session_revision") or 0),
            source="online_login",
        )
        if updated is None:
            raise LoginError(
                "account configuration changed; reopen its login window", 409
            )
        lease["captured"] = True
        lease["last_identity_ok"] = True
        # The first launch may use Chromium's default UA. Record its observed
        # value so a warm desktop is not needlessly recreated after capture.
        lease["desktop"].user_agent = str(updated.get("user_agent") or "")
        return updated

    async def finish(self, session_id):
        async with self.lock:
            lease = self._owned(session_id)
            if lease["phase"] != "login":
                raise LoginError("login browser is not ready", 409)
            lease["phase"] = "syncing"
            try:
                await asyncio.to_thread(self._capture, lease)
                account = await asyncio.to_thread(
                    self.service.check_account,
                    lease["account_id"],
                    recover=False,
                    enforce_low_balance=False,
                )
            except Exception as exc:
                lease["phase"] = "login"
                lease["last_identity_ok"] = False
                status = 503 if isinstance(exc, AkoolBrowserConsentError) or getattr(exc, "code", None) == "AGREEMENT_REQUIRED" else 409
                raise LoginError(str(exc), status) from exc
            previous = lease["previous"]
            current = self.store.get_account(lease["account_id"]) or {}
            is_draft = bool(previous.get("online_draft"))
            should_enable = is_draft or bool(
                previous.get("enabled") and current.get("enabled")
            )
            low = float(account.get("last_balance") or 0) < float(
                self.service.settings.low_balance_disable_threshold
            )
            enabled = should_enable and not low
            status = (
                "active"
                if enabled
                else (
                    "disabled_low_balance"
                    if low
                    else (
                        previous["status"]
                        if not previous.get("enabled") and not is_draft
                        else "disabled"
                    )
                )
            )
            # Keep the scheduler's lease until interactive control is revoked.
            # A viewer shutdown failure does not invalidate the verified login.
            try:
                await lease["desktop"].revoke_viewer()
            except asyncio.CancelledError:
                lease["phase"] = "login"
                raise
            except Exception as exc:
                lease["phase"] = "login"
                raise LoginError("login viewer could not close; please retry", 503) from exc
            self.service.end_online_login(
                lease["account_id"], lease["id"], enabled=enabled, status=status
            )
            self.store.update_account(lease["account_id"], {"online_draft": 0})
            self.active = None
            return self.store.get_account(lease["account_id"], include_secrets=False)

    async def _cancel(self, lease):
        lease['phase'] = 'closing'
        # Revoke interactive control before releasing the scheduler's DB lease.
        # The warm browser remains available for the final identity capture.
        if lease.get('desktop'):
            await lease['desktop'].revoke_viewer()
        verified = False
        agreement_error = None
        try:
            if lease.get("desktop") and lease["desktop"].alive():
                await asyncio.to_thread(self._capture, lease)
                verified = True
        except AkoolBrowserConsentError as exc:
            agreement_error = exc
        except Exception:
            pass
        previous = lease["previous"]
        current = self.store.get_account(lease["account_id"]) or {}
        enabled = verified and bool(previous.get("enabled") and current.get("enabled"))
        status = (
            "agreement_required" if agreement_error else "active"
            if enabled
            else (
                previous["status"] if not previous.get("enabled") else "login_required"
            )
        )
        self.service.end_online_login(
            lease["account_id"],
            lease["id"],
            enabled=enabled,
            status=status,
            error=(
                str(agreement_error) if agreement_error else ""
                if verified
                else "login window closed without a verified matching session"
            ),
        )
        self.active = None

    async def cancel(self, session_id):
        async with self.lock:
            await self._cancel(self._owned(session_id))

    async def _maintain(self):
        try:
            while not self.closed:
                await asyncio.sleep(self.SYNC_SECONDS)
                async with self.lock:
                    lease = self.active
                    if not lease:
                        continue
                    if (
                        time.monotonic() >= lease["deadline"]
                        or not lease["desktop"].alive()
                    ):
                        await self._cancel(lease)
                        continue
                    if lease["phase"] == "login":
                        try:
                            await asyncio.to_thread(self._capture, lease)
                        except Exception:
                            lease["last_identity_ok"] = False
        except asyncio.CancelledError:
            return

    async def close(self):
        self.closed = True
        if self.maintenance:
            self.maintenance.cancel()
            await asyncio.gather(self.maintenance, return_exceptions=True)
        async with self.lock:
            if self.active:
                await self._cancel(self.active)
            for desktop in self.desktops.values():
                await desktop.close()
            self.desktops.clear()
