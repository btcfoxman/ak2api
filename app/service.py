from __future__ import annotations

import logging
import random
import re
import threading
import time
import uuid
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from app.browser_context import (
    AkoolBrowserChallengeError,
    AkoolBrowserError,
    AkoolBrowserTransportError,
    delete_managed_profile,
    refresh_account_context,
    reset_managed_profile,
    shutdown_managed_browsers,
    stop_managed_browser,
)
from app.config import normalize_proxy_url
from app.db import Database, now_ts
from app.akool_client import (
    AkoolAuthError,
    AkoolClient,
    AkoolRiskBlocked,
    AkoolUpstreamError,
    failure_reason,
    result_urls,
)
from app.model_catalog import (
    MEDIA_LIMITS,
    model_map_json,
    normalize_generation_request,
    public_models,
)


LOGGER = logging.getLogger("ak2api.service")
TERMINAL_STATUSES = {"succeeded", "failed", "expired"}
PUBLIC_FAILURE = "生成失败，积分已返还，请重试~"
PUBLIC_MEDIA_FAILURE = "素材下载失败，请检查~"
PUBLIC_FORMAT_FAILURE = "处理失败，请检查图音视频格式和大小"
PUBLIC_MODERATION_FAILURE = "检测到内容有敏感或违规情况，积分已返还，请重试"


def _is_moderation_failure(message: Any) -> bool:
    value = str(message or "").lower()
    return "content was flagged by our moderation system" in value


def _is_image_constraint_failure(message: Any) -> bool:
    value = str(message or "").lower()
    return any(
        marker in value
        for marker in (
            "height must be between 300px and 6000px",
            "aspect ratio must be between 0.4 and 2.5",
        )
    )


class _DynamicSlots:
    def __init__(self, limit: int):
        self.limit = max(int(limit), 1)
        self.active = 0
        self.condition = threading.Condition()
        self.waiters: deque[object] = deque()

    def set_limit(self, limit: int) -> None:
        with self.condition:
            self.limit = max(int(limit), 1)
            self.condition.notify_all()

    def reserve(self) -> object:
        token = object()
        with self.condition:
            self.waiters.append(token)
            self.condition.notify_all()
        return token

    def cancel(self, token: object) -> None:
        with self.condition:
            try:
                self.waiters.remove(token)
            except ValueError:
                return
            self.condition.notify_all()

    def acquire(self, token: object | None = None) -> None:
        with self.condition:
            if token is None:
                token = object()
                self.waiters.append(token)
            while self.active >= self.limit or self.waiters[0] is not token:
                self.condition.wait(1)
            self.waiters.popleft()
            self.active += 1
            self.condition.notify_all()

    def release(self) -> None:
        with self.condition:
            self.active = max(self.active - 1, 0)
            self.condition.notify_all()


class AKService:
    runtime_fields = (
        "task_workers",
        "task_queue_capacity",
        "poll_interval_seconds",
        "task_timeout_seconds",
        "request_timeout_seconds",
        "request_retries",
        "account_maintenance_interval_seconds",
        "account_maintenance_workers",
        "browser_recovery_enabled",
        "browser_timeout_seconds",
        "browser_login_workers",
        "browser_login_stagger_seconds",
        "browser_challenge_grace_seconds",
        "chrome_executable",
        "chrome_user_data_root",
        "chrome_headless",
        "proxy_host_override",
        "proxy_pool_enabled",
        "proxy_pool",
        "low_balance_disable_threshold",
        "excess_media_policy",
        "prompt_media_reference_cleanup_enabled",
        "model_map",
    )

    def __init__(self, db: Database, settings: Any):
        self.db = db
        self.settings = settings
        self._load_runtime_settings()
        self._tasks = ThreadPoolExecutor(max_workers=50, thread_name_prefix="ak-task")
        self._logins = ThreadPoolExecutor(max_workers=10, thread_name_prefix="ak-login")
        self._maintenance = ThreadPoolExecutor(
            max_workers=20,
            thread_name_prefix="ak-maintenance",
        )
        self._slots = _DynamicSlots(int(settings.task_workers))
        self._login_slots = _DynamicSlots(int(settings.browser_login_workers))
        self._maintenance_slots = _DynamicSlots(int(settings.account_maintenance_workers))
        self._futures: dict[str, Future[Any]] = {}
        self._future_lock = threading.RLock()
        self._task_submit_lock = threading.RLock()
        self._account_guard = threading.RLock()
        self._running_logins: set[int] = set()
        self._running_maintenance: set[int] = set()
        self._resetting_profiles: set[int] = set()
        self._stop = threading.Event()
        self._maintenance_wakeup = threading.Event()
        self._maintenance_thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._maintenance_wakeup.clear()
        for task in self.db.recoverable_tasks():
            self._schedule(str(task["id"]))
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name="ak-account-maintenance",
            daemon=True,
        )
        self._maintenance_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._maintenance_wakeup.set()
        if self._maintenance_thread:
            self._maintenance_thread.join(timeout=3)
        self._tasks.shutdown(wait=False, cancel_futures=False)
        self._logins.shutdown(wait=False, cancel_futures=False)
        self._maintenance.shutdown(wait=False, cancel_futures=True)
        shutdown_managed_browsers()

    def _load_runtime_settings(self) -> None:
        for field in self.runtime_fields:
            raw = self.db.get_setting(field, "")
            if not raw:
                continue
            current = getattr(self.settings, field)
            if isinstance(current, bool):
                value: Any = raw.lower() in {"1", "true", "yes", "on"}
            elif isinstance(current, int):
                value = int(raw)
            elif isinstance(current, float):
                value = float(raw)
            else:
                value = raw
            setattr(self.settings, field, value)

    def runtime_settings(self) -> dict[str, Any]:
        value = {field: getattr(self.settings, field) for field in self.runtime_fields}
        value["ignore_excess_media"] = value["excess_media_policy"] == "ignore"
        value["media_limits"] = dict(MEDIA_LIMITS)
        return value

    def update_runtime_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        values = {key: value for key, value in changes.items() if value is not None}
        alias = values.pop("ignore_excess_media", None)
        if alias is not None and "excess_media_policy" not in values:
            values["excess_media_policy"] = "ignore" if alias else "strict"
        if "model_map" in values:
            values["model_map"] = model_map_json(values["model_map"])
        if "excess_media_policy" in values and values["excess_media_policy"] not in {
            "ignore",
            "strict",
        }:
            raise ValueError("excess_media_policy must be ignore or strict")
        persisted: dict[str, Any] = {}
        for field, value in values.items():
            if field not in self.runtime_fields:
                continue
            setattr(self.settings, field, value)
            persisted[field] = value
        self.db.set_settings(persisted)
        self._slots.set_limit(int(self.settings.task_workers))
        self._login_slots.set_limit(int(self.settings.browser_login_workers))
        self._maintenance_slots.set_limit(
            int(self.settings.account_maintenance_workers)
        )
        if {
            "account_maintenance_interval_seconds",
            "account_maintenance_workers",
        } & values.keys():
            self._maintenance_wakeup.set()
        return self.runtime_settings()

    def models(self) -> list[dict[str, Any]]:
        return public_models(self.settings.model_map)

    def _proxy_values(self) -> list[str]:
        raw = str(self.settings.proxy_pool or "")
        values: list[str] = []
        for item in raw.replace(";", "\n").replace(",", "\n").splitlines():
            proxy = normalize_proxy_url(item.strip())
            if proxy and proxy not in values:
                values.append(proxy)
        return values

    def _assign_proxy(self, *, exclude_proxy: str = "") -> str:
        if not bool(self.settings.proxy_pool_enabled):
            return ""
        excluded = normalize_proxy_url(exclude_proxy)
        proxies = [item for item in self._proxy_values() if item != excluded]
        if not proxies:
            return ""
        counts = self.db.proxy_assignment_counts()
        minimum = min(counts.get(proxy, 0) for proxy in proxies)
        candidates = [proxy for proxy in proxies if counts.get(proxy, 0) == minimum]
        return random.choice(candidates)

    @staticmethod
    def _identity_key(payload: dict[str, Any]) -> str:
        return str(payload.get("email") or payload.get("name") or "").strip().lower()

    @staticmethod
    def _looks_like_proxy(value: str) -> bool:
        return bool(
            re.fullmatch(r"(?:\[[^]]+\]|[^\s:/]+):\d+", value)
            or re.match(r"^(?:https?|socks4|socks5h?)://", value, re.IGNORECASE)
        )

    def _parse_batch_text(
        self, text: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        accounts: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for line_number, raw in enumerate(str(text or "").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            for separator in ("\t", "----", "|", ","):
                if separator in line:
                    parts = [part.strip() for part in line.split(separator)]
                    break
            else:
                parts = [line]
            email = parts[0] if parts else ""
            password = parts[1] if len(parts) > 1 else ""
            proxy_url = parts[2] if len(parts) > 2 else ""
            if len(parts) == 2 and self._looks_like_proxy(password):
                proxy_url, password = password, ""
            if not email:
                errors.append({"line": str(line_number), "message": "email is required"})
                continue
            if len(parts) > 3:
                errors.append(
                    {
                        "line": str(line_number),
                        "message": "too many fields; expected email, password, proxy",
                    }
                )
                continue
            accounts.append(
                {
                    "name": email,
                    "email": email,
                    "password": password,
                    "proxy_url": proxy_url,
                    "enabled": True,
                    "auto_login": True,
                    "max_concurrency": int(self.settings.account_default_concurrency),
                }
            )
        return accounts, errors

    @staticmethod
    def _account_payload(payload: dict[str, Any]) -> dict[str, Any]:
        value = dict(payload)
        if "access_token" in value or "token" in value:
            value["access_token"] = str(
                value.get("access_token") or value.get("token") or ""
            )
        if "user_id" in value or "uid" in value:
            value["user_id"] = str(value.get("user_id") or value.get("uid") or "")
        if "team_id" in value or "team" in value:
            value["team_id"] = str(value.get("team_id") or value.get("team") or "")
        records = value.get("cookie_records")
        if records is not None:
            value["cookie_records"] = [
                item.model_dump() if hasattr(item, "model_dump") else dict(item)
                for item in records
            ]
        return value

    def upsert_account(self, payload: dict[str, Any], *, start_login: bool = False) -> dict[str, Any]:
        value = self._account_payload(payload)
        with self._account_guard:
            if not str(value.get("proxy_url") or "").strip() and bool(
                value.pop("use_proxy_pool", True)
            ):
                value["proxy_url"] = self._assign_proxy()
            account = self.db.upsert_account(value)
        if start_login:
            self.schedule_login(int(account["id"]))
        return self.db.get_account(int(account["id"]), include_secrets=False) or account

    def sync_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = self._account_payload(payload)
        value["name"] = str(value.get("name") or value.get("email") or "").strip()
        account = self.upsert_account(value, start_login=False)
        try:
            return self.check_account(int(account["id"]), recover=False)
        except Exception:
            return self.db.get_account(int(account["id"]), include_secrets=False) or account

    def batch_import(
        self,
        source: str | list[dict[str, Any]],
        *,
        start_login: bool,
        use_proxy_pool: bool,
    ) -> dict[str, Any]:
        if isinstance(source, list):
            raw_accounts = [self._account_payload(item) for item in source]
            errors: list[dict[str, str]] = []
        else:
            raw_accounts, errors = self._parse_batch_text(source)
        if not raw_accounts:
            raise ValueError("no accounts were provided")
        if len(raw_accounts) > 1000:
            raise ValueError("at most 1000 accounts can be imported at once")

        values: list[dict[str, Any]] = []
        identity_indexes: dict[str, int] = {}
        for item in raw_accounts:
            identity = self._identity_key(item)
            if not identity:
                errors.append({"line": "", "message": "account name or email is required"})
                continue
            if identity in identity_indexes:
                current = values[identity_indexes[identity]]
                for key, value in item.items():
                    if key in {"name", "email"}:
                        continue
                    if value not in (None, "", [], {}):
                        current[key] = value
                continue
            identity_indexes[identity] = len(values)
            values.append(dict(item))

        pool = self._proxy_values() if use_proxy_pool and self.settings.proxy_pool_enabled else []
        counts = Counter(self.db.proxy_assignment_counts())
        existing_count = 0
        for item in values:
            explicit_proxy = normalize_proxy_url(str(item.get("proxy_url") or ""))
            existing = self.db.find_account_by_identity(item, include_secrets=False)
            if existing:
                existing_count += 1
                item["name"] = existing["name"]
            if explicit_proxy:
                item["proxy_url"] = explicit_proxy
                if not existing:
                    counts[explicit_proxy] += 1
                continue
            if existing:
                continue
            if pool:
                selected = min(pool, key=lambda proxy: (counts[proxy], pool.index(proxy)))
                item["proxy_url"] = selected
                counts[selected] += 1

        accounts: list[dict[str, Any]] = []
        login_started_count = 0
        for index, payload in enumerate(values, 1):
            try:
                account = self.upsert_account(payload, start_login=False)
                accounts.append(account)
                if start_login and self.schedule_login(int(account["id"])):
                    login_started_count += 1
            except Exception as exc:
                errors.append({"line": str(index), "message": str(exc)})
        return {
            "accounts": accounts,
            "count": len(accounts),
            "input_count": len(raw_accounts),
            "duplicate_count": len(raw_accounts) - len(values) + existing_count,
            "existing_count": existing_count,
            "login_started": bool(start_login),
            "login_started_count": login_started_count,
            "errors": errors,
        }

    def update_account(self, account_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        before = self.db.get_account(account_id)
        if not before:
            raise KeyError("account not found")
        value = self._account_payload(changes)
        if value.get("enabled") is False:
            stop_managed_browser(account_id, int(before.get("cdp_port") or 0))
            value.setdefault("status", "disabled")
        elif value.get("enabled") is True and before.get("status") in {
            "disabled",
            "disabled_low_balance",
        }:
            value.setdefault("status", "pending")
            value.setdefault("last_error", "")
        account = self.db.update_account(account_id, value)
        if not account:
            raise KeyError("account not found")
        return self.db.get_account(account_id, include_secrets=False) or account

    def delete_account(self, account_id: int) -> bool:
        account = self.db.get_account(account_id)
        if not account:
            return False
        if account.get("enabled"):
            raise ValueError("account must be disabled before deletion")
        if int(account.get("active_tasks") or 0):
            raise ValueError("account has active tasks")
        with self._account_guard:
            if int(account_id) in self._running_logins:
                raise ValueError("account login is in progress")
            if int(account_id) in self._running_maintenance:
                raise ValueError("account maintenance is in progress")
            if int(account_id) in self._resetting_profiles:
                raise ValueError("account profile reset is in progress")
        delete_managed_profile(account, self.settings)
        return self.db.delete_account(account_id)

    def reset_account_profile(
        self,
        account_id: int,
        *,
        proxy_url: str | None,
        use_proxy_pool: bool,
        start_login: bool,
    ) -> dict[str, Any]:
        account_id = int(account_id)
        with self._account_guard:
            account = self.db.get_account(account_id)
            if not account:
                raise KeyError("account not found")
            if int(account.get("active_tasks") or 0):
                raise ValueError("account has active tasks")
            if account_id in self._running_logins:
                raise ValueError("account login is already in progress")
            if account_id in self._running_maintenance:
                raise ValueError("account maintenance is already in progress")
            if account_id in self._resetting_profiles:
                raise ValueError("account profile reset is already in progress")
            self._resetting_profiles.add(account_id)
        previous_status = str(account.get("status") or "login_required")
        self.db.update_account(account_id, {"status": "profile_resetting", "last_error": ""})
        try:
            current_proxy = normalize_proxy_url(str(account.get("proxy_url") or ""))
            if use_proxy_pool:
                selected_proxy = self._assign_proxy(exclude_proxy=current_proxy)
                if not selected_proxy:
                    raise ValueError("proxy pool is disabled, empty, or has no alternate proxy")
            elif proxy_url is None:
                selected_proxy = current_proxy
            else:
                selected_proxy = normalize_proxy_url(proxy_url)
            profile = reset_managed_profile(account, self.settings)
            updated = self.db.update_account(
                account_id,
                {
                    **profile,
                    "proxy_url": selected_proxy,
                    "access_token": "",
                    "user_id": "",
                    "team_id": "",
                    "cookie_header": "",
                    "cookie_records": [],
                    "user_agent": "",
                    "sec_ch_ua": "",
                    "sec_ch_ua_platform": "",
                    "status": "login_pending" if start_login else "login_required",
                    "last_error": "",
                    "last_login_at": None,
                },
            )
        except Exception as exc:
            self.db.update_account(
                account_id,
                {"status": previous_status, "last_error": f"profile reset failed: {exc}"},
            )
            raise
        finally:
            with self._account_guard:
                self._resetting_profiles.discard(account_id)
        login_started = self.schedule_login(account_id) if start_login else False
        return {
            "account": self.db.get_account(account_id, include_secrets=False) or updated or {},
            "profile_reset": profile,
            "login_started": login_started,
        }

    def schedule_login(self, account_id: int) -> bool:
        account_id = int(account_id)
        with self._account_guard:
            if account_id in self._running_logins:
                return False
            if not self.db.get_account(account_id, include_secrets=False):
                return False
            self._running_logins.add(account_id)
            self.db.update_account(account_id, {"status": "logging_in", "last_error": ""})
            try:
                future = self._logins.submit(self._login_account, account_id)
            except Exception:
                self._running_logins.discard(account_id)
                raise
        future.add_done_callback(lambda _future, value=account_id: self._finish_login(value))
        return True

    def _finish_login(self, account_id: int) -> None:
        with self._account_guard:
            self._running_logins.discard(int(account_id))

    def _login_account(self, account_id: int) -> dict[str, Any]:
        self._login_slots.acquire()
        try:
            stagger = float(self.settings.browser_login_stagger_seconds)
            if stagger > 0:
                time.sleep(random.uniform(0, stagger))
            account = self.db.get_account(account_id)
            if not account:
                raise KeyError("account not found")
            context = refresh_account_context(account, self.settings)
            self.db.update_account(account_id, context)
            return self.check_account(account_id, recover=False)
        except Exception as exc:
            if isinstance(exc, (AkoolBrowserChallengeError, AkoolRiskBlocked)):
                status = "challenge_required"
            elif isinstance(exc, AkoolBrowserTransportError):
                status = "network_error"
            elif isinstance(exc, AkoolAuthError):
                status = "login_failed"
            elif isinstance(exc, AkoolBrowserError):
                status = "login_failed"
            else:
                status = "network_error"
            self.db.update_account(
                account_id,
                {"status": status, "last_error": str(exc), "last_checked_at": now_ts()},
            )
            raise
        finally:
            self._login_slots.release()

    def _recover_client(self, account: dict[str, Any]) -> AkoolClient:
        if not bool(self.settings.browser_recovery_enabled):
            raise AkoolAuthError("Akool session recovery is disabled")
        context = refresh_account_context(account, self.settings)
        updated = self.db.update_account(int(account["id"]), context) or account
        return AkoolClient(updated, self.settings)

    def _with_recovery(
        self,
        account: dict[str, Any],
        client: AkoolClient,
        operation: Callable[[AkoolClient], Any],
    ) -> tuple[Any, AkoolClient]:
        try:
            return operation(client), client
        except (AkoolAuthError, AkoolRiskBlocked):
            recovered = self._recover_client(account)
            return operation(recovered), recovered

    def check_account(self, account_id: int, *, recover: bool = True) -> dict[str, Any]:
        account = self.db.get_account(account_id)
        if not account:
            raise KeyError("account not found")
        client = AkoolClient(account, self.settings)
        try:
            if recover:
                state, client = self._with_recovery(account, client, lambda item: item.account_state())
            else:
                state = client.account_state()
            details = state.get("buckets") or {}
            updated = self.db.update_account(
                account_id,
                {
                    "email": state.get("email") or account.get("email") or "",
                    "user_id": state.get("user_id") or account.get("user_id") or "",
                    "team_id": state.get("team_id") or account.get("team_id") or "",
                    "access_token": state.get("token") or account.get("access_token") or "",
                    "last_balance": state.get("available_balance"),
                    "balance_details": details,
                    "plan": state.get("plan") or "",
                    "status": "active",
                    "last_error": "",
                    "last_checked_at": now_ts(),
                },
            )
            if (
                updated
                and float(updated.get("last_balance") or 0)
                < float(self.settings.low_balance_disable_threshold)
            ):
                self.db.disable_account_for_low_balance_if_idle(
                    account_id, float(self.settings.low_balance_disable_threshold)
                )
            return self.db.get_account(account_id, include_secrets=False) or updated or {}
        except Exception as exc:
            status = "login_required" if isinstance(
                exc, (AkoolAuthError, AkoolRiskBlocked, AkoolBrowserError)
            ) else "network_error"
            self.db.update_account(
                account_id,
                {"status": status, "last_error": str(exc), "last_checked_at": now_ts()},
            )
            raise

    def create_task(self, payload: dict[str, Any], *, caller_request: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._task_submit_lock:
            capacity = max(int(self.settings.task_queue_capacity), 0)
            maximum_active = int(self.settings.task_workers) + capacity
            if self.db.active_task_count() >= maximum_active:
                raise AkoolUpstreamError(
                    "Akool task queue is full",
                    code="TASK_QUEUE_FULL",
                    status_code=429,
                )
            normalized = normalize_generation_request(
                payload,
                self.settings.model_map,
                self.settings.excess_media_policy,
                bool(self.settings.prompt_media_reference_cleanup_enabled),
            )
            normalized["_requested_model"] = str(
                payload.get("model") or "doubao-seedance-2-0-mini-260615"
            )
            normalized["_estimated_cost"] = self.db.estimate_cost(normalized)
            task_id = f"gen_{uuid.uuid4().hex[:16]}"
            task = self.db.create_task(
                task_id,
                normalized,
                caller_request=caller_request or payload,
            )
            self._schedule(task_id)
            return task

    def _schedule(self, task_id: str) -> None:
        with self._future_lock:
            existing = self._futures.get(task_id)
            if existing and not existing.done():
                return
            slot_token = self._slots.reserve()
            try:
                future = self._tasks.submit(self._run_guarded, task_id, slot_token)
            except Exception:
                self._slots.cancel(slot_token)
                raise
            self._futures[task_id] = future

    def _run_guarded(self, task_id: str, slot_token: object) -> None:
        self._slots.acquire(slot_token)
        try:
            self._run_task(task_id)
        except Exception:
            LOGGER.exception("task %s crashed", task_id)
        finally:
            self._slots.release()
            with self._future_lock:
                self._futures.pop(task_id, None)

    def _acquire_task_account(
        self,
        task: dict[str, Any],
        deadline: float,
        exclude_ids: set[int] | None = None,
    ) -> dict[str, Any]:
        payload = task.get("request") or {}
        recovering = bool(task.get("generation_id"))
        preferred = payload.get("account_id")
        if recovering and not preferred:
            preferred = task.get("account_id")
        if preferred and exclude_ids and int(preferred) in exclude_ids:
            preferred = None
        reservation = 0 if recovering else float(task.get("estimated_cost") or 0)
        while time.monotonic() < deadline:
            account = self.db.acquire_account(
                int(preferred) if preferred else None,
                exclude_ids=exclude_ids,
                kind="video",
                minimum_balance=reservation,
                task_id=str(task["id"]),
                recovering=recovering,
                reservation_cost=reservation,
            )
            if account:
                return account
            available = self.db.available_account_count(exclude_ids)
            if available <= 0:
                if exclude_ids:
                    raise AkoolUpstreamError(
                        "no remaining Akool account has enough credit for this task",
                        code="INSUFFICIENT_CREDITS",
                        status_code=409,
                    )
                raise AkoolUpstreamError(
                    "no active Akool account is available",
                    code="NO_AVAILABLE_ACCOUNT",
                    status_code=503,
                )
            if reservation > 0 and self.db.available_account_count(
                exclude_ids,
                minimum_balance=reservation,
            ) <= 0:
                raise AkoolUpstreamError(
                    "no active Akool account has enough available credit for this task",
                    code="INSUFFICIENT_CREDITS",
                    status_code=409,
                )
            time.sleep(1)
        raise TimeoutError("timed out waiting for an available Akool account")

    def _release_insufficient_credit_account(
        self,
        *,
        task_id: str,
        account: dict[str, Any],
        client: AkoolClient,
        error: AkoolUpstreamError,
        phase: str,
        attempts: list[dict[str, Any]],
    ) -> None:
        account_id = int(account["id"])
        attempts.append(
            {
                f"{phase}_error": {
                    "account_id": account_id,
                    "code": error.code,
                    "message": str(error),
                    "response": error.details or {},
                }
            }
        )
        refreshed_balance = False
        try:
            self.db.update_task(
                task_id,
                upstream_response={"attempts": attempts[-30:]},
                status="preparing",
                progress=35,
            )
            try:
                state, _ = self._with_recovery(
                    account, client, lambda current: current.account_state()
                )
                self.db.update_account(
                    account_id,
                    {
                        "last_balance": state.get("available_balance"),
                        "balance_details": state.get("buckets") or {},
                        "plan": state.get("plan") or "",
                        "status": "active",
                        "last_error": str(error),
                        "last_checked_at": now_ts(),
                    },
                )
                refreshed_balance = True
            except Exception as refresh_exc:
                self.db.update_account(
                    account_id,
                    {
                        "last_error": (
                            f"{error}; balance refresh failed: {refresh_exc}"
                        ),
                        "last_checked_at": now_ts(),
                    },
                )
        finally:
            self.db.release_account(account_id, task_id=task_id)
        if refreshed_balance:
            self.db.disable_account_for_low_balance_if_idle(
                account_id,
                float(self.settings.low_balance_disable_threshold),
            )

    def _run_task(self, task_id: str) -> None:
        task = self.db.get_task(task_id)
        if not task or task.get("status") in TERMINAL_STATUSES:
            return
        deadline = time.monotonic() + int(self.settings.task_timeout_seconds)
        account: dict[str, Any] | None = None
        balance_fresh = False
        stored_attempts = (task.get("upstream_response") or {}).get("attempts") or []
        attempts: list[dict[str, Any]] = [
            item for item in stored_attempts if isinstance(item, dict)
        ][-30:]
        try:
            payload = task.get("request") or {}
            generation_id = str(task.get("generation_id") or "")
            actual_cost: float | None = (
                float(task.get("reserved_cost") or task.get("estimated_cost") or 0)
                if generation_id
                else None
            )
            excluded_ids: set[int] = set()
            before_balance: float | None = None
            client: AkoolClient | None = None
            image_constraint_retry = any(
                "image_constraint_retry" in item for item in attempts
            )
            sources = (
                [("image", item) for item in payload.get("_images") or []]
                + [("video", item) for item in payload.get("_videos") or []]
                + [("audio", item) for item in payload.get("_audio") or []]
            )
            uploads = []

            while account is None:
                account = self._acquire_task_account(task, deadline, excluded_ids)
                account_id = int(account["id"])
                client = AkoolClient(account, self.settings)
                before_balance = (
                    float(account["last_balance"])
                    if account.get("last_balance") is not None
                    else None
                )
                if generation_id:
                    break

                if before_balance is None:
                    state, client = self._with_recovery(
                        account, client, lambda current: current.account_state()
                    )
                    before_balance = float(state.get("available_balance") or 0)
                    self.db.update_account(
                        account_id,
                        {
                            "last_balance": before_balance,
                            "balance_details": state.get("buckets") or {},
                            "plan": state.get("plan") or "",
                            "status": "active",
                            "last_error": "",
                            "last_checked_at": now_ts(),
                        },
                    )

                self.db.update_task(
                    task_id, status="preparing", progress=5, channel="akapi"
                )
                for index, (kind, item) in enumerate(sources):
                    upload, client = self._with_recovery(
                        account,
                        client,
                        lambda current, source=item, media_kind=kind: current.upload_media(
                            str(source.get("value") or ""),
                            media_kind,
                            str(source.get("name") or ""),
                        ),
                    )
                    uploads.append(upload)
                    progress = 5 + int(((index + 1) / max(len(sources), 1)) * 25)
                    self.db.update_task(task_id, progress=progress)

                upstream_request = client.build_generation_request(payload, uploads)
                try:
                    fee_result, client = self._with_recovery(
                        account,
                        client,
                        lambda current: current.calculate_fee(
                            payload, uploads, upstream_request
                        ),
                    )
                except AkoolUpstreamError as exc:
                    if str(exc.code) != "INSUFFICIENT_CREDITS":
                        raise
                    self._release_insufficient_credit_account(
                        task_id=task_id,
                        account=account,
                        client=client,
                        error=exc,
                        phase="calculate_fee",
                        attempts=attempts,
                    )
                    excluded_ids.add(account_id)
                    account = None
                    client = None
                    before_balance = None
                    actual_cost = None
                    balance_fresh = False
                    continue
                actual_cost = max(float(fee_result.get("fee") or 0), 0)
                audit_request = {
                    "upload_endpoint": "POST /interface/storagesvc/api/v1/upload/signature -> PUT S3 -> POST /interface/content-api/api/v7/content/profile/create",
                    "uploads": [item.audit_view() for item in uploads],
                    "fee": {
                        "method": "POST",
                        "url": "https://akool.com/interface/content-api/api/v7/content/calculateFee",
                        "body": fee_result.get("request") or {},
                    },
                    "submit": {
                        "method": "POST",
                        "url": "https://akool.com/interface/content-api/api/v7/content/image2Video/createBySourcePrompt/batch",
                        "body": upstream_request,
                    },
                }
                attempts.append(
                    {
                        "calculate_fee": fee_result.get("response") or {},
                        "fee": actual_cost,
                    }
                )
                self.db.update_task(
                    task_id,
                    upstream_request=audit_request,
                    upstream_response={"attempts": attempts},
                    estimated_cost=actual_cost,
                    status="preparing",
                    progress=35,
                )
                if not self.db.reserve_task_balance(task_id, account_id, actual_cost):
                    excluded_ids.add(account_id)
                    self.db.release_account(account_id, task_id=task_id)
                    account = None
                    client = None
                    continue

                try:
                    generated, client = self._with_recovery(
                        account, client, lambda current: current.generate(upstream_request)
                    )
                except AkoolUpstreamError as exc:
                    if str(exc.code) != "INSUFFICIENT_CREDITS":
                        raise
                    self._release_insufficient_credit_account(
                        task_id=task_id,
                        account=account,
                        client=client,
                        error=exc,
                        phase="generate",
                        attempts=attempts,
                    )
                    excluded_ids.add(account_id)
                    account = None
                    client = None
                    before_balance = None
                    actual_cost = None
                    balance_fresh = False
                    continue
                generated["fee"] = actual_cost
                generation_id = str(generated["generationId"])
                attempts.append({"generate": generated})
                self.db.update_task(
                    task_id,
                    generation_id=generation_id,
                    status="submitted",
                    progress=40,
                    upstream_response={"attempts": attempts[-30:]},
                )

            if account is None or client is None:
                raise AkoolUpstreamError("no Akool account was selected")
            account_id = int(account["id"])

            while time.monotonic() < deadline:
                detail, client = self._with_recovery(
                    account, client, lambda current: current.generation_detail(generation_id)
                )
                status = str(detail.get("status") or "PROCESSING").upper()
                provider_progress = float(detail.get("progress") or 0)
                if provider_progress <= 1:
                    provider_progress *= 100
                progress = max(42, min(95, 42 + int(provider_progress * 0.53)))
                attempts.append({"poll": detail})
                self.db.update_task(
                    task_id,
                    status="running",
                    progress=progress,
                    raw_status=detail,
                    upstream_response={"attempts": attempts[-30:]},
                )
                if status == "COMPLETE":
                    urls = result_urls(detail)
                    if not urls:
                        raise AkoolUpstreamError("Akool completed without a result URL")
                    try:
                        state = client.account_state()
                        after_balance = float(state.get("available_balance") or 0)
                        self.db.update_account(
                            account_id,
                            {
                                "last_balance": after_balance,
                                "balance_details": state.get("buckets") or {},
                                "plan": state.get("plan") or "",
                                "status": "active",
                                "last_error": "",
                                "last_checked_at": now_ts(),
                            },
                        )
                        balance_fresh = True
                    except Exception as exc:
                        LOGGER.warning("balance refresh failed after %s: %s", task_id, exc)
                    actual_cost = float(actual_cost or 0)
                    self.db.update_task(
                        task_id,
                        status="succeeded",
                        progress=100,
                        result_urls=urls,
                        thumbnail_url=str(detail.get("thumbnailUrl") or ""),
                        raw_status=detail,
                        upstream_response={"attempts": attempts[-30:]},
                        actual_cost=actual_cost,
                        completed_at=now_ts(),
                        error_code="",
                        error_message="",
                    )
                    self.db.record_model_cost(task_id, payload, actual_cost)
                    self.db.settle_task_balance(
                        task_id,
                        account_id,
                        actual_cost=actual_cost,
                        balance_snapshot_fresh=balance_fresh,
                    )
                    self.db.release_account(account_id, task_id=task_id)
                    account = None
                    if balance_fresh:
                        self.db.disable_account_for_low_balance_if_idle(
                            account_id, float(self.settings.low_balance_disable_threshold)
                        )
                    return
                if status == "FAILED":
                    reason = failure_reason(detail)
                    if (
                        not image_constraint_retry
                        and any(kind == "image" for kind, _ in sources)
                        and _is_image_constraint_failure(reason)
                    ):
                        image_constraint_retry = True
                        attempts.append(
                            {
                                "image_constraint_retry": {
                                    "reason": reason,
                                    "previous_generation_id": generation_id,
                                }
                            }
                        )
                        self.db.update_task(
                            task_id,
                            status="preparing",
                            progress=10,
                            upstream_response={"attempts": attempts[-30:]},
                        )
                        repaired_uploads = []
                        for index, (kind, item) in enumerate(sources):
                            existing = uploads[index] if index < len(uploads) else None
                            if kind != "image" and existing is not None:
                                repaired_uploads.append(existing)
                                continue
                            upload, client = self._with_recovery(
                                account,
                                client,
                                lambda current, source=item, media_kind=kind: current.upload_media(
                                    str(source.get("value") or ""),
                                    media_kind,
                                    str(source.get("name") or ""),
                                    force_image_normalization=media_kind == "image",
                                ),
                            )
                            repaired_uploads.append(upload)
                            repair_progress = 10 + int(
                                ((index + 1) / max(len(sources), 1)) * 20
                            )
                            self.db.update_task(task_id, progress=repair_progress)
                        uploads = repaired_uploads
                        upstream_request = client.build_generation_request(
                            payload, uploads
                        )
                        fee_result, client = self._with_recovery(
                            account,
                            client,
                            lambda current: current.calculate_fee(
                                payload, uploads, upstream_request
                            ),
                        )
                        actual_cost = max(float(fee_result.get("fee") or 0), 0)
                        if not self.db.reserve_task_balance(
                            task_id, account_id, actual_cost
                        ):
                            raise AkoolUpstreamError(
                                "account balance is insufficient for the repaired image retry",
                                code="INSUFFICIENT_CREDITS",
                                status_code=409,
                            )
                        attempts.append(
                            {
                                "image_constraint_retry_uploads": [
                                    item.audit_view() for item in uploads
                                ],
                                "calculate_fee": fee_result.get("response") or {},
                                "fee": actual_cost,
                            }
                        )
                        generated, client = self._with_recovery(
                            account,
                            client,
                            lambda current: current.generate(upstream_request),
                        )
                        generated["fee"] = actual_cost
                        generation_id = str(generated["generationId"])
                        attempts.append({"image_constraint_retry_generate": generated})
                        retry_audit = {
                            "upload_endpoint": "POST /interface/storagesvc/api/v1/upload/signature -> PUT S3 -> POST /interface/content-api/api/v7/content/profile/create",
                            "uploads": [item.audit_view() for item in uploads],
                            "fee": {
                                "method": "POST",
                                "url": "https://akool.com/interface/content-api/api/v7/content/calculateFee",
                                "body": fee_result.get("request") or {},
                            },
                            "submit": {
                                "method": "POST",
                                "url": "https://akool.com/interface/content-api/api/v7/content/image2Video/createBySourcePrompt/batch",
                                "body": upstream_request,
                            },
                        }
                        self.db.update_task(
                            task_id,
                            generation_id=generation_id,
                            status="submitted",
                            progress=40,
                            upstream_request=retry_audit,
                            upstream_response={"attempts": attempts[-30:]},
                            estimated_cost=actual_cost,
                            raw_status={},
                            error_code="",
                            error_message="",
                        )
                        continue
                    raise AkoolUpstreamError(
                        reason,
                        code="GENERATION_FAILED",
                        details=detail,
                    )
                time.sleep(int(self.settings.poll_interval_seconds))
            raise TimeoutError("Akool generation timed out")
        except Exception as exc:
            code = getattr(exc, "code", "TASK_FAILED")
            if isinstance(exc, TimeoutError):
                code = "TASK_TIMEOUT"
            elif _is_moderation_failure(exc):
                code = "CONTENT_MODERATION_FAILED"
            self.db.update_task(
                task_id,
                status="expired" if code == "TASK_TIMEOUT" else "failed",
                progress=100,
                error_code=str(code),
                error_message=str(exc),
                raw_status=getattr(exc, "details", None) or {},
                completed_at=now_ts(),
            )
            if account and isinstance(exc, (AkoolAuthError, AkoolRiskBlocked, AkoolBrowserError)):
                self.db.update_account(
                    int(account["id"]),
                    {"status": "login_required", "last_error": str(exc), "last_checked_at": now_ts()},
                )
            elif account:
                try:
                    state = AkoolClient(account, self.settings).account_state()
                    self.db.update_account(
                        int(account["id"]),
                        {
                            "last_balance": state.get("available_balance"),
                            "balance_details": state.get("buckets") or {},
                            "plan": state.get("plan") or "",
                            "last_checked_at": now_ts(),
                        },
                    )
                except Exception:
                    pass
        finally:
            if account:
                self.db.release_account(int(account["id"]), task_id=task_id)

    def retry_task(self, task_id: str) -> dict[str, Any]:
        task = self.db.get_task(task_id)
        if not task:
            raise KeyError("task not found")
        if task.get("status") not in TERMINAL_STATUSES:
            raise ValueError("task is still active")
        changes: dict[str, Any] = {
            "status": "queued",
            "progress": 0,
            "account_id": None,
            "generation_id": "",
            "error_code": "",
            "error_message": "",
            "completed_at": None,
        }
        self.db.update_task(
            task_id,
            **changes,
        )
        self._schedule(task_id)
        return self.db.get_task(task_id) or {}

    def wait_task(self, task_id: str, timeout: int | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + int(timeout or self.settings.synchronous_timeout_seconds)
        while time.monotonic() < deadline:
            task = self.db.get_task(task_id)
            if not task:
                raise KeyError("task not found")
            if task.get("status") in TERMINAL_STATUSES:
                return task
            time.sleep(1)
        raise TimeoutError("synchronous task wait timed out")

    @staticmethod
    def public_failure_message(task: dict[str, Any]) -> str:
        code = str(task.get("error_code") or "")
        if code == "CONTENT_MODERATION_FAILED" or _is_moderation_failure(
            task.get("error_message")
        ):
            return PUBLIC_MODERATION_FAILURE
        if code == "MEDIA_DOWNLOAD_FAILED":
            return PUBLIC_MEDIA_FAILURE
        if code == "PROVIDER_INVALID_REQUEST":
            return PUBLIC_FORMAT_FAILURE
        return PUBLIC_FAILURE

    def public_task(self, task: dict[str, Any]) -> dict[str, Any]:
        result = {
            "id": task["id"],
            "object": "video.generation",
            "created": task.get("created_at"),
            "updated": task.get("updated_at"),
            "status": task.get("status"),
            "progress": task.get("progress", 0),
            "model": task.get("model"),
            "data": [
                {"url": url}
                for url in task.get("result_urls") or []
                if str(url or "").startswith("http")
            ],
        }
        if task.get("status") in {"failed", "expired"}:
            result["error"] = {
                "code": task.get("error_code") or "generation_failed",
                "message": self.public_failure_message(task),
            }
        return result

    def task_media_source(self, task_id: str, index: int) -> str:
        task = self.db.get_task(task_id)
        if not task:
            raise KeyError("task not found")
        payload = task.get("request") or {}
        items = (
            list(payload.get("_images") or [])
            + list(payload.get("_videos") or [])
            + list(payload.get("_audio") or [])
        )
        if index < 0 or index >= len(items):
            raise IndexError("media not found")
        return str(items[index].get("value") or "")

    def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            interrupted = self._maintenance_wakeup.wait(
                int(self.settings.account_maintenance_interval_seconds)
            )
            self._maintenance_wakeup.clear()
            if self._stop.is_set():
                break
            if interrupted:
                continue
            for account in self.db.list_accounts(include_secrets=False):
                if (
                    account.get("enabled")
                    and account.get("auto_login")
                    and not int(account.get("active_tasks") or 0)
                ):
                    account_id = int(account["id"])
                    with self._account_guard:
                        if (
                            account_id in self._running_logins
                            or account_id in self._running_maintenance
                            or account_id in self._resetting_profiles
                        ):
                            continue
                        self._running_maintenance.add(account_id)
                    future = self._maintenance.submit(self._maintenance_check, account_id)
                    future.add_done_callback(
                        lambda _future, value=account_id: self._finish_maintenance(value)
                    )

    def _maintenance_check(self, account_id: int) -> None:
        self._maintenance_slots.acquire()
        try:
            self.check_account(account_id, recover=False)
        except Exception as exc:
            LOGGER.info("account %s maintenance check failed: %s", account_id, exc)
            account = self.db.get_account(account_id, include_secrets=False) or {}
            if account.get("enabled") and account.get("auto_login") and account.get(
                "status"
            ) in {"login_required", "network_error"}:
                self.schedule_login(account_id)
        finally:
            self._maintenance_slots.release()

    def _finish_maintenance(self, account_id: int) -> None:
        with self._account_guard:
            self._running_maintenance.discard(int(account_id))

    def status(self) -> dict[str, Any]:
        tasks = self.db.list_task_summaries(100)
        return {
            "service": "ak2api",
            "schema_version": self.settings.schema_version,
            "accounts": len(self.db.list_accounts(include_secrets=False)),
            "available_accounts": self.db.available_account_count(),
            "running_tasks": sum(1 for item in tasks if item.get("status") not in TERMINAL_STATUSES),
            "task_workers": int(self.settings.task_workers),
        }
