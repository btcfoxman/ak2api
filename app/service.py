from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from app.browser_context import (
    AkoolBrowserChallengeError,
    AkoolBrowserError,
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


class _DynamicSlots:
    def __init__(self, limit: int):
        self.limit = max(int(limit), 1)
        self.active = 0
        self.condition = threading.Condition()

    def set_limit(self, limit: int) -> None:
        with self.condition:
            self.limit = max(int(limit), 1)
            self.condition.notify_all()

    def acquire(self) -> None:
        with self.condition:
            while self.active >= self.limit:
                self.condition.wait(1)
            self.active += 1

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
        "browser_recovery_enabled",
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
        self._logins = ThreadPoolExecutor(
            max_workers=int(settings.browser_login_workers),
            thread_name_prefix="ak-login",
        )
        self._maintenance = ThreadPoolExecutor(
            max_workers=int(settings.account_maintenance_workers),
            thread_name_prefix="ak-maintenance",
        )
        self._slots = _DynamicSlots(int(settings.task_workers))
        self._futures: dict[str, Future[Any]] = {}
        self._future_lock = threading.RLock()
        self._stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None

    def start(self) -> None:
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
        return self.runtime_settings()

    def models(self) -> list[dict[str, Any]]:
        return public_models(self.settings.model_map)

    def _proxy_values(self) -> list[str]:
        raw = str(self.settings.proxy_pool or "")
        return [
            normalize_proxy_url(item.strip())
            for item in raw.replace(";", "\n").replace(",", "\n").splitlines()
            if item.strip()
        ]

    def _assign_proxy(self) -> str:
        if not bool(self.settings.proxy_pool_enabled):
            return ""
        proxies = self._proxy_values()
        if not proxies:
            return ""
        counts = self.db.proxy_assignment_counts()
        minimum = min(counts.get(proxy, 0) for proxy in proxies)
        candidates = [proxy for proxy in proxies if counts.get(proxy, 0) == minimum]
        return random.choice(candidates)

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
        if not str(value.get("proxy_url") or "").strip() and bool(value.pop("use_proxy_pool", True)):
            value["proxy_url"] = self._assign_proxy()
        account = self.db.upsert_account(value)
        if start_login or (account.get("auto_login") and not account.get("cookie_header")):
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

    def batch_import(self, text: str, *, start_login: bool, use_proxy_pool: bool) -> dict[str, Any]:
        accounts: list[dict[str, Any]] = []
        duplicate_count = 0
        errors: list[dict[str, str]] = []
        for line_number, raw in enumerate(str(text or "").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            separator = "----" if "----" in line else "|"
            parts = [part.strip() for part in line.split(separator)]
            email = parts[0] if parts else ""
            if not email:
                continue
            payload = {
                "name": email,
                "email": email,
                "password": parts[1] if len(parts) > 1 else "",
                "proxy_url": parts[2] if len(parts) > 2 else "",
                "use_proxy_pool": use_proxy_pool,
                "enabled": True,
                "auto_login": True,
            }
            try:
                existing = self.db.find_account_by_identity(payload)
                account = self.upsert_account(payload, start_login=start_login)
                duplicate_count += 1 if existing else 0
                accounts.append(account)
            except Exception as exc:
                errors.append({"line": str(line_number), "message": str(exc)})
        return {
            "accounts": accounts,
            "count": len(accounts),
            "duplicate_count": duplicate_count,
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
        if int(account.get("active_tasks") or 0):
            raise ValueError("account has active tasks")
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
        account = self.db.get_account(account_id)
        if not account:
            raise KeyError("account not found")
        if int(account.get("active_tasks") or 0):
            raise ValueError("account has active tasks")
        profile = reset_managed_profile(account, self.settings)
        selected_proxy = str(proxy_url or "").strip()
        if not selected_proxy and use_proxy_pool:
            selected_proxy = self._assign_proxy()
        updated = self.db.update_account(
            account_id,
            {
                **profile,
                "proxy_url": normalize_proxy_url(selected_proxy),
                "access_token": "",
                "user_id": "",
                "team_id": "",
                "cookie_header": "",
                "cookie_records": [],
                "status": "pending",
                "last_error": "",
            },
        )
        if start_login:
            self.schedule_login(account_id)
        return self.db.get_account(account_id, include_secrets=False) or updated or {}

    def schedule_login(self, account_id: int) -> None:
        self._logins.submit(self._login_account, int(account_id))

    def _login_account(self, account_id: int) -> dict[str, Any]:
        account = self.db.get_account(account_id)
        if not account:
            raise KeyError("account not found")
        try:
            context = refresh_account_context(account, self.settings)
            self.db.update_account(account_id, context)
            return self.check_account(account_id, recover=False)
        except (AkoolBrowserChallengeError, AkoolBrowserError) as exc:
            self.db.update_account(
                account_id,
                {"status": "login_required", "last_error": str(exc), "last_checked_at": now_ts()},
            )
            raise

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
        task = self.db.create_task(task_id, normalized, caller_request=caller_request or payload)
        self._schedule(task_id)
        return task

    def _schedule(self, task_id: str) -> None:
        with self._future_lock:
            existing = self._futures.get(task_id)
            if existing and not existing.done():
                return
            future = self._tasks.submit(self._run_guarded, task_id)
            self._futures[task_id] = future

    def _run_guarded(self, task_id: str) -> None:
        self._slots.acquire()
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
            available = self.db.available_account_count()
            if available <= 0:
                raise AkoolUpstreamError(
                    "no active Akool account is available",
                    code="NO_AVAILABLE_ACCOUNT",
                    status_code=503,
                )
            if exclude_ids and len(exclude_ids) >= available:
                raise AkoolUpstreamError(
                    "no Akool account has enough available credit for this task",
                    code="INSUFFICIENT_CREDITS",
                    status_code=409,
                )
            time.sleep(1)
        raise TimeoutError("timed out waiting for an available Akool account")

    def _run_task(self, task_id: str) -> None:
        task = self.db.get_task(task_id)
        if not task or task.get("status") in TERMINAL_STATUSES:
            return
        deadline = time.monotonic() + int(self.settings.task_timeout_seconds)
        account: dict[str, Any] | None = None
        balance_fresh = False
        actual_cost: float | None = None
        attempts: list[dict[str, Any]] = []
        try:
            payload = task.get("request") or {}
            generation_id = str(task.get("generation_id") or "")
            excluded_ids: set[int] = set()
            before_balance: float | None = None
            client: AkoolClient | None = None

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
                uploads = []
                sources = (
                    [("image", item) for item in payload.get("_images") or []]
                    + [("video", item) for item in payload.get("_videos") or []]
                    + [("audio", item) for item in payload.get("_audio") or []]
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
                fee_result, client = self._with_recovery(
                    account,
                    client,
                    lambda current: current.calculate_fee(
                        payload, uploads, upstream_request
                    ),
                )
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
                    if payload.get("account_id"):
                        raise AkoolUpstreamError(
                            "the selected Akool account has insufficient available credit",
                            code="INSUFFICIENT_CREDITS",
                            status_code=409,
                        )
                    continue

                generated, client = self._with_recovery(
                    account, client, lambda current: current.generate(upstream_request)
                )
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
                    raise AkoolUpstreamError(
                        failure_reason(detail),
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
        while not self._stop.wait(int(self.settings.account_maintenance_interval_seconds)):
            for account in self.db.list_accounts(include_secrets=False):
                if (
                    account.get("enabled")
                    and account.get("auto_login")
                    and not int(account.get("active_tasks") or 0)
                ):
                    self._maintenance.submit(self._maintenance_check, int(account["id"]))

    def _maintenance_check(self, account_id: int) -> None:
        try:
            self.check_account(account_id, recover=False)
        except Exception as exc:
            LOGGER.info("account %s maintenance check failed: %s", account_id, exc)
            account = self.db.get_account(account_id, include_secrets=False) or {}
            if account.get("enabled") and account.get("auto_login") and account.get(
                "status"
            ) == "login_required":
                self.schedule_login(account_id)

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
