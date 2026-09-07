from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import app.service as service_module
import pytest
from app.akool_client import AkoolAccountSuspended, AkoolRateLimited, AkoolUpstreamError, MediaUpload
from app.db import Database
from app.service import AKService, PUBLIC_MODERATION_FAILURE, _DynamicSlots


def test_dynamic_slots_release_waiters_in_reserved_fifo_order() -> None:
    slots = _DynamicSlots(1)
    first = slots.reserve()
    second = slots.reserve()
    third = slots.reserve()
    order: list[int] = []

    slots.acquire(first)

    def run(token: object, value: int) -> None:
        slots.acquire(token)
        order.append(value)
        slots.release()

    third_thread = threading.Thread(target=run, args=(third, 3))
    second_thread = threading.Thread(target=run, args=(second, 2))
    third_thread.start()
    time.sleep(0.02)
    second_thread.start()
    time.sleep(0.02)
    slots.release()
    second_thread.join(timeout=1)
    third_thread.join(timeout=1)

    assert not second_thread.is_alive()
    assert not third_thread.is_alive()
    assert order == [2, 3]


@pytest.mark.parametrize(
    "message",
    [
        "Height must be between 300px and 6000px.",
        "Image height must be greater than 300 and less than 6000 pixels",
        "Aspect ratio must be between 0.4 and 2.5.",
    ],
)
def test_image_constraint_failure_matches_upstream_variants(message) -> None:
    assert service_module._is_image_constraint_failure(message)


@pytest.mark.parametrize(
    "message",
    [
        "Your content was flagged by our moderation system.",
        "Sorry, the content violates safety rules. Please try a different image or description.",
    ],
)
def test_moderation_failure_matches_upstream_variants(message) -> None:
    assert service_module._is_moderation_failure(message)
    assert AKService.public_failure_message(
        {"error_code": "GENERATION_FAILED", "error_message": message}
    ) == PUBLIC_MODERATION_FAILURE


def settings(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        task_workers=2,
        task_queue_capacity=10,
        poll_interval_seconds=2,
        task_timeout_seconds=60,
        synchronous_timeout_seconds=30,
        request_timeout_seconds=30,
        request_retries=1,
        account_maintenance_interval_seconds=300,
        account_maintenance_workers=1,
        daily_checkin_enabled=True,
        account_default_concurrency=8,
        media_timeout_seconds=30,
        media_max_bytes=1024 * 1024,
        media_fallback_proxy_url="",
        browser_recovery_enabled=False,
        browser_timeout_seconds=30,
        browser_login_workers=1,
        browser_login_stagger_seconds=0,
        browser_challenge_grace_seconds=3,
        chrome_executable="",
        chrome_user_data_root=str(tmp_path / "profiles"),
        chrome_cdp_base_port=19800,
        chrome_headless=True,
        proxy_host_override="",
        proxy_pool_enabled=False,
        proxy_pool="",
        low_balance_disable_threshold=1,
        excess_media_policy="ignore",
        allow_video_reference_inputs=True,
        prompt_media_reference_cleanup_enabled=False,
        model_map=json.dumps(
            {"seedance-mini": "doubao-seedance-2-0-mini-260615"}
        ),
        schema_version="test",
    )


class FakeAkoolClient:
    default_image_uploads: list[int] = []

    def __init__(self, account, settings):
        self.account = dict(account)
        self.settings = settings

    def upload_media(self, source, kind, name=""):
        return MediaUpload(
            profile_id=f"profile-{kind}",
            url=f"https://cdn.example.com/{kind}",
            kind=kind,
            name=name or kind,
            content_type="application/octet-stream",
            size=100,
        )

    def upload_default_text_video_image(self):
        self.default_image_uploads.append(int(self.account["id"]))
        return MediaUpload(
            profile_id=f"profile-{self.account['id']}-black",
            url=f"https://cdn.example.com/{self.account['id']}/black.jpg",
            kind="image",
            name="akool-text-to-video-black-1024.jpg",
            content_type="image/jpeg",
            size=6365,
            width=1024,
            height=1024,
            synthetic=True,
        )

    def build_generation_request(self, payload, uploads):
        return {
            "prompt": payload["prompt"],
            "model_name": "doubao-seedance-2-0-mini-260615/image-to-video",
            "resolution": "480p",
            "video_length": 4,
            "generate_audio": True,
        }

    def calculate_fee(self, payload, uploads, request):
        return {"fee": 4, "request": {"options": {"is_unlimited_model": False}}, "response": {"code": 1000}}

    def generate(self, request):
        return {"generationId": "resource-test", "raw": {"code": 1000}}

    def generation_detail(self, generation_id):
        return {
            "generationId": generation_id,
            "status": "COMPLETE",
            "progress": 100,
            "urls": ["https://cdn.example.com/result.mp4"],
        }

    def account_state(self):
        return {
            "balance": 96,
            "available_balance": 96,
            "plan": "ProMax",
            "buckets": {"credit": 96, "lock_credit": 0},
        }


class DailyCheckinClient(FakeAkoolClient):
    today = ""
    signed = False
    status_calls = 0
    checkin_calls = 0

    def account_state(self):
        balance = 5 if self.signed else 0
        return {
            "balance": balance,
            "available_balance": balance,
            "plan": "ProMax",
            "buckets": {
                "credit": balance,
                "lock_credit": 0,
                "sign_reward_stats": {
                    "last_sign": self.today if self.signed else "2026-09-01",
                    "today_signed": self.signed,
                },
            },
        }

    def daily_checkin_status(self):
        type(self).status_calls += 1
        return {
            "last_sign": "2026-09-01",
            "today_signed": False,
            "can_checkedin": True,
            "total_credits": 20,
        }

    def daily_checkin(self):
        type(self).checkin_calls += 1
        type(self).signed = True
        return {"day_number": 1, "credits": 5, "total_credits": 25}


class InsufficientThenSuccessClient(FakeAkoolClient):
    failure_account_id = 0
    failure_phase = "generate"
    generate_accounts: list[int] = []
    upload_accounts: list[int] = []

    def upload_media(self, source, kind, name=""):
        account_id = int(self.account["id"])
        self.upload_accounts.append(account_id)
        return MediaUpload(
            profile_id=f"profile-{account_id}-{kind}",
            url=f"https://cdn.example.com/{account_id}/{kind}",
            kind=kind,
            name=name or kind,
            content_type="application/octet-stream",
            size=100,
        )

    @staticmethod
    def raise_insufficient_credit() -> None:
        body = {
            "code": 1104,
            "msg": "your credits is not enough",
            "data": {"is_pop_upgrade": False, "sub_info": {}},
        }
        raise AkoolUpstreamError(
            body["msg"],
            code="INSUFFICIENT_CREDITS",
            status_code=409,
            details=body,
        )

    def calculate_fee(self, payload, uploads, request):
        if (
            self.failure_phase == "calculate_fee"
            and int(self.account["id"]) == self.failure_account_id
        ):
            self.raise_insufficient_credit()
        return super().calculate_fee(payload, uploads, request)

    def generate(self, request):
        account_id = int(self.account["id"])
        self.generate_accounts.append(account_id)
        if self.failure_phase == "generate" and account_id == self.failure_account_id:
            self.raise_insufficient_credit()
        return {"generationId": "resource-switched", "raw": {"code": 1000}}

    def account_state(self):
        account_id = int(self.account["id"])
        balance = 0 if account_id == self.failure_account_id else 96
        return {
            "balance": balance,
            "available_balance": balance,
            "plan": "ProMax",
            "buckets": {"credit": balance, "lock_credit": 0},
        }


class SuspendedThenSuccessClient(FakeAkoolClient):
    failure_account_id = 0

    def generate(self, request):
        if int(self.account["id"]) == self.failure_account_id:
            raise AkoolAccountSuspended("Access to your account has been suspended")
        return {"generationId": "resource-after-suspension", "raw": {"code": 1000}}


class ImageConstraintThenSuccessClient(FakeAkoolClient):
    force_flags: list[bool] = []
    generation_count = 0

    def upload_media(
        self,
        source,
        kind,
        name="",
        *,
        force_image_normalization=False,
    ):
        self.force_flags.append(bool(force_image_normalization))
        return MediaUpload(
            profile_id=f"profile-{kind}-{len(self.force_flags)}",
            url=f"https://cdn.example.com/{kind}-{len(self.force_flags)}.jpg",
            kind=kind,
            name=name or kind,
            content_type="image/jpeg",
            size=100,
            width=640,
            height=480,
        )

    def generate(self, request):
        self.__class__.generation_count += 1
        return {
            "generationId": f"resource-{self.generation_count}",
            "raw": {"code": 1000},
        }

    def generation_detail(self, generation_id):
        if generation_id == "resource-1":
            return {
                "generationId": generation_id,
                "status": "FAILED",
                "progress": 100,
                "error": "Height must be between 300px and 6000px.",
            }
        return {
            "generationId": generation_id,
            "status": "COMPLETE",
            "progress": 100,
            "urls": ["https://cdn.example.com/repaired.mp4"],
        }


def test_task_uses_dynamic_fee_and_refreshes_balance(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "AkoolClient", FakeAkoolClient)
    db = Database(str(tmp_path / "test.db"), default_concurrency=8)
    account = db.upsert_account(
        {"name": "ready", "status": "active", "last_balance": 100}
    )
    payload = {
        "kind": "video",
        "model": "doubao-seedance-2-0-mini-260615",
        "prompt": "test",
        "duration": 4,
        "resolution": "480p",
        "aspect_ratio": "adaptive",
        "_images": [{"value": "https://example.com/image.png", "name": "image.png"}],
        "_videos": [],
        "_audio": [],
        "_estimated_cost": 0,
    }
    db.create_task("gen_test", payload)
    gateway = AKService(db, settings(tmp_path))

    try:
        gateway._run_task("gen_test")
    finally:
        gateway.stop()

    task = db.get_task("gen_test")
    refreshed = db.get_account(account["id"])
    assert task["status"] == "succeeded"
    assert task["generation_id"] == "resource-test"
    assert task["actual_cost"] == 4
    assert task["result_urls"] == ["https://cdn.example.com/result.mp4"]
    assert refreshed["last_balance"] == 96
    assert refreshed["active_tasks"] == 0


@pytest.mark.parametrize(
    "error,delays",
    [
        (AkoolRateLimited("Too many requests from your IP, please retry after 28 seconds"), [28, 28, 28]),
        (AkoolRateLimited("Too many requests", retry_after="35"), [35, 35, 35]),
        (AkoolUpstreamError("Too many requests from your IP, please retry after 28 seconds", code="PROVIDER_INVALID_REQUEST", status_code=422), [28, 28, 28]),
        (AkoolUpstreamError("temporarily unavailable", code="RATE_LIMITED", status_code=429), [2, 4, 8]),
        (AkoolUpstreamError("connection reset", code="NETWORK_ERROR"), [2, 4, 8]),
        (AkoolUpstreamError("service unavailable", code="AKOOL_HTTP_ERROR"), [2, 4, 8]),
    ],
)
def test_transient_poll_errors_keep_account_slot_until_original_task_completes(
    tmp_path, monkeypatch, error, delays
) -> None:
    db = Database(str(tmp_path / "poll-retry.db"))
    account = db.upsert_account({"name": "one", "status": "active", "last_balance": 100, "max_concurrency": 1})
    payload = {"kind": "video", "model": "doubao-seedance-2-0-mini-260615", "prompt": "test"}
    db.create_task("running", payload)
    db.create_task("waiting", payload)
    polls = []
    submissions = []
    balance_checks = []
    sleeps = []
    clock = [0.0]

    class LimitedClient(FakeAkoolClient):
        def generate(self, request):
            submissions.append(request)
            return super().generate(request)

        def generation_detail(self, generation_id):
            polls.append(generation_id)
            if len(polls) <= 3:
                raise error
            return super().generation_detail(generation_id)

        def account_state(self):
            balance_checks.append(self.account["id"])
            return super().account_state()

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
        current = db.get_task("running")
        assert current["status"] == "running"
        assert current["generation_id"] == "resource-test"
        assert current["completed_at"] is None
        assert current["progress"] < 100
        assert current["error_code"] == ""
        assert db.get_account(account["id"])["active_tasks"] == 1
        assert db.get_account(account["id"])["reserved_balance"] == 4
        assert db.acquire_account(task_id="waiting") is None

    monkeypatch.setattr(service_module, "AkoolClient", LimitedClient)
    monkeypatch.setattr(service_module, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    config = settings(tmp_path)
    config.task_timeout_seconds = 180
    gateway = AKService(db, config)
    try:
        gateway._run_task("running")
    finally:
        gateway.stop()

    task = db.get_task("running")
    assert task["status"] == "succeeded"
    assert task["error_code"] == ""
    assert sleeps == delays
    assert polls == ["resource-test"] * 4
    assert len(submissions) == 1
    assert balance_checks == [account["id"]]
    assert len([item for item in task["upstream_response"]["attempts"] if "poll_error" in item]) == 3
    assert db.get_account(account["id"])["active_tasks"] == 0
    assert db.get_account(account["id"])["reserved_balance"] == 0
    assert db.acquire_account(task_id="waiting")


def test_rate_limit_wait_is_bounded_by_task_timeout(tmp_path, monkeypatch) -> None:
    db = Database(str(tmp_path / "poll-timeout.db"))
    account = db.upsert_account({"name": "one", "status": "active", "last_balance": 100, "max_concurrency": 1})
    db.create_task("running", {"kind": "video", "model": "doubao-seedance-2-0-mini-260615", "prompt": "test"})
    clock = [0.0]
    polls = []
    sleeps = []

    class LimitedClient(FakeAkoolClient):
        def generation_detail(self, generation_id):
            polls.append(generation_id)
            raise AkoolRateLimited("Too many requests from your IP, please retry after 28 seconds")

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(service_module, "AkoolClient", LimitedClient)
    monkeypatch.setattr(service_module, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    config = settings(tmp_path)
    config.task_timeout_seconds = 5
    gateway = AKService(db, config)
    try:
        gateway._run_task("running")
    finally:
        gateway.stop()

    task = db.get_task("running")
    assert task["status"] == "expired"
    assert task["error_code"] == "TASK_TIMEOUT"
    assert sleeps == [5]
    assert polls == ["resource-test"]
    assert db.get_account(account["id"])["active_tasks"] == 0


def test_real_generation_failure_is_still_terminal(tmp_path, monkeypatch) -> None:
    db = Database(str(tmp_path / "generation-failure.db"))
    account = db.upsert_account({"name": "one", "status": "active", "last_balance": 100})
    db.create_task("failed", {"kind": "video", "model": "doubao-seedance-2-0-mini-260615", "prompt": "test"})

    class FailedClient(FakeAkoolClient):
        def generation_detail(self, generation_id):
            return {"status": "FAILED", "error": "The model could not generate this video"}

    monkeypatch.setattr(service_module, "AkoolClient", FailedClient)
    gateway = AKService(db, settings(tmp_path))
    try:
        gateway._run_task("failed")
    finally:
        gateway.stop()

    task = db.get_task("failed")
    assert task["status"] == "failed"
    assert task["error_code"] == "GENERATION_FAILED"
    assert db.get_account(account["id"])["active_tasks"] == 0


@pytest.mark.parametrize("error_code", ["RATE_LIMITED", "PROVIDER_INVALID_REQUEST", "GENERATION_FAILED"])
def test_retry_of_rate_limited_task_resumes_original_generation_and_account(
    tmp_path, monkeypatch, error_code
) -> None:
    db = Database(str(tmp_path / "resume.db"))
    requested = db.upsert_account({"name": "requested", "status": "active", "last_balance": 100})
    actual = db.upsert_account({"name": "actual", "status": "active", "last_balance": 100, "max_concurrency": 1})
    payload = {"kind": "video", "model": "doubao-seedance-2-0-mini-260615", "prompt": "test", "account_id": requested["id"]}
    db.create_task("old", payload)
    db.update_task(
        "old", status="failed", progress=100, account_id=actual["id"],
        generation_id="original-resource", estimated_cost=4, error_code=error_code,
        error_message="Too many requests from your IP, please retry after 28 seconds",
        completed_at=1,
    )
    polls = []

    class ResumeClient(FakeAkoolClient):
        def generate(self, request):
            raise AssertionError("a submitted task must not be submitted again")

        def generation_detail(self, generation_id):
            polls.append((self.account["id"], generation_id))
            return super().generation_detail(generation_id)

    monkeypatch.setattr(service_module, "AkoolClient", ResumeClient)
    gateway = AKService(db, settings(tmp_path))
    scheduled = []
    monkeypatch.setattr(gateway, "_schedule", scheduled.append)
    try:
        retried = gateway.retry_task("old")
        assert retried["status"] == "submitted"
        assert retried["generation_id"] == "original-resource"
        assert retried["completed_at"] is None
        assert retried["error_code"] == ""
        assert db.get_account(actual["id"])["active_tasks"] == 1
        assert db.get_account(actual["id"])["reserved_balance"] == 4
        gateway._run_task("old")
    finally:
        gateway.stop()

    assert scheduled == ["old"]
    assert db.get_task("old")["status"] == "succeeded"
    assert polls == [(actual["id"], "original-resource")]
    assert db.get_account(actual["id"])["active_tasks"] == 0
    assert db.get_account(requested["id"])["total_uses"] == 0


def test_account_acquisition_fails_fast_when_all_balances_are_too_low(
    tmp_path,
) -> None:
    db = Database(str(tmp_path / "insufficient.db"), default_concurrency=8)
    db.upsert_account(
        {"name": "low", "status": "active", "last_balance": 120}
    )
    payload = {
        "kind": "video",
        "model": "doubao-seedance-2-0-260128",
        "prompt": "test",
        "duration": 15,
        "resolution": "720p",
        "_estimated_cost": 159,
    }
    task = db.create_task("gen_insufficient", payload)
    gateway = AKService(db, settings(tmp_path))

    try:
        with pytest.raises(AkoolUpstreamError) as raised:
            gateway._acquire_task_account(task, time.monotonic() + 10)
    finally:
        gateway.stop()

    assert raised.value.code == "INSUFFICIENT_CREDITS"


def test_text_only_tasks_upload_and_reuse_account_black_image(
    tmp_path, monkeypatch
) -> None:
    FakeAkoolClient.default_image_uploads = []
    monkeypatch.setattr(service_module, "AkoolClient", FakeAkoolClient)
    db = Database(str(tmp_path / "text-video.db"), default_concurrency=8)
    account = db.upsert_account(
        {"name": "ready", "status": "active", "last_balance": 100}
    )
    payload = {
        "kind": "video",
        "model": "doubao-seedance-2-0-mini-260615",
        "prompt": "create from text only",
        "duration": 4,
        "resolution": "480p",
        "aspect_ratio": "16:9",
        "_images": [],
        "_videos": [],
        "_audio": [],
        "_estimated_cost": 0,
    }
    db.create_task("gen_text_one", payload)
    db.create_task("gen_text_two", payload)
    gateway = AKService(db, settings(tmp_path))

    try:
        gateway._run_task("gen_text_one")
        gateway._run_task("gen_text_two")
    finally:
        gateway.stop()

    assert FakeAkoolClient.default_image_uploads == [account["id"]]
    for task_id in ("gen_text_one", "gen_text_two"):
        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        uploads = (task["upstream_request"] or {}).get("uploads") or []
        assert uploads[0]["synthetic"] == "text_to_video_black_image"
        assert uploads[0]["width"] == 1024
        assert uploads[0]["height"] == 1024


def test_task_account_acquisition_avoids_only_active_maintenance(tmp_path) -> None:
    db = Database(str(tmp_path / "maintenance-exclusion.db"), default_concurrency=8)
    first = db.upsert_account(
        {"name": "first", "status": "active", "last_balance": 100}
    )
    second = db.upsert_account(
        {"name": "second", "status": "active", "last_balance": 100}
    )
    task = db.create_task(
        "gen_maintenance_exclusion",
        {
            "kind": "video",
            "model": "doubao-seedance-2-0-mini-260615",
            "prompt": "test",
            "_estimated_cost": 0,
        },
    )
    gateway = AKService(db, settings(tmp_path))
    with gateway._account_guard:
        gateway._running_maintenance.add(int(first["id"]))
        gateway._active_maintenance.add(int(first["id"]))

    try:
        acquired = gateway._acquire_task_account(task, time.monotonic() + 1)
        assert acquired["id"] == second["id"]
    finally:
        if "acquired" in locals():
            db.release_account(acquired["id"], task_id=task["id"])
        gateway.stop()


def test_image_constraint_failure_reencodes_and_submits_once(
    tmp_path, monkeypatch
) -> None:
    ImageConstraintThenSuccessClient.force_flags = []
    ImageConstraintThenSuccessClient.generation_count = 0
    monkeypatch.setattr(
        service_module,
        "AkoolClient",
        ImageConstraintThenSuccessClient,
    )
    db = Database(str(tmp_path / "image-retry.db"), default_concurrency=8)
    db.upsert_account(
        {"name": "ready", "status": "active", "last_balance": 100}
    )
    payload = {
        "kind": "video",
        "model": "doubao-seedance-2-0-mini-260615",
        "prompt": "test",
        "duration": 4,
        "resolution": "480p",
        "aspect_ratio": "16:9",
        "_images": [{"value": "https://example.com/image", "name": "image-1"}],
        "_videos": [],
        "_audio": [],
        "_estimated_cost": 0,
    }
    db.create_task("gen_image_retry", payload)
    gateway = AKService(db, settings(tmp_path))

    try:
        gateway._run_task("gen_image_retry")
    finally:
        gateway.stop()

    task = db.get_task("gen_image_retry")
    assert task["status"] == "succeeded"
    assert task["generation_id"] == "resource-2"
    assert task["result_urls"] == ["https://cdn.example.com/repaired.mp4"]
    assert ImageConstraintThenSuccessClient.force_flags == [False, True]
    attempts = (task["upstream_response"] or {}).get("attempts") or []
    assert sum("image_constraint_retry" in item for item in attempts) == 1


def test_dynamic_fee_shortfall_switches_from_preferred_account(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(service_module, "AkoolClient", FakeAkoolClient)
    db = Database(str(tmp_path / "fee-switch.db"), default_concurrency=8)
    first = db.upsert_account(
        {"name": "low", "status": "active", "last_balance": 2}
    )
    second = db.upsert_account(
        {"name": "ready", "status": "active", "last_balance": 100}
    )
    payload = {
        "kind": "video",
        "model": "doubao-seedance-2-0-mini-260615",
        "prompt": "test",
        "duration": 4,
        "resolution": "480p",
        "aspect_ratio": "adaptive",
        "account_id": first["id"],
        "_images": [{"value": "https://example.com/image.png", "name": "image.png"}],
        "_videos": [],
        "_audio": [],
        "_estimated_cost": 0,
    }
    db.create_task("gen_fee_switch", payload)
    gateway = AKService(db, settings(tmp_path))

    try:
        gateway._run_task("gen_fee_switch")
    finally:
        gateway.stop()

    task = db.get_task("gen_fee_switch")
    assert task["status"] == "succeeded"
    assert task["account_id"] == second["id"]
    assert db.get_account(first["id"])["active_tasks"] == 0
    assert db.get_account(second["id"])["active_tasks"] == 0


@pytest.mark.parametrize("failure_phase", ["calculate_fee", "generate"])
def test_upstream_insufficient_credit_reuploads_and_switches_account(
    tmp_path, monkeypatch, failure_phase
) -> None:
    InsufficientThenSuccessClient.generate_accounts = []
    InsufficientThenSuccessClient.upload_accounts = []
    InsufficientThenSuccessClient.failure_phase = failure_phase
    monkeypatch.setattr(
        service_module, "AkoolClient", InsufficientThenSuccessClient
    )
    db = Database(str(tmp_path / "switch.db"), default_concurrency=8)
    first = db.upsert_account(
        {"name": "first", "status": "active", "last_balance": 100}
    )
    second = db.upsert_account(
        {"name": "second", "status": "active", "last_balance": 100}
    )
    InsufficientThenSuccessClient.failure_account_id = int(first["id"])
    payload = {
        "kind": "video",
        "model": "doubao-seedance-2-5",
        "prompt": "test",
        "duration": 4,
        "resolution": "480p",
        "aspect_ratio": "21:9",
        "account_id": first["id"],
        "_images": [{"value": "https://example.com/image.png", "name": "image.png"}],
        "_videos": [{"value": "https://example.com/video.mp4", "name": "video.mp4"}],
        "_audio": [],
        "_estimated_cost": 0,
    }
    db.create_task("gen_switch", payload)
    gateway = AKService(db, settings(tmp_path))

    try:
        gateway._run_task("gen_switch")
    finally:
        gateway.stop()

    task = db.get_task("gen_switch")
    first_after = db.get_account(first["id"])
    second_after = db.get_account(second["id"])
    assert task["status"] == "succeeded"
    assert task["generation_id"] == "resource-switched"
    assert task["account_id"] == second["id"]
    assert InsufficientThenSuccessClient.generate_accounts == (
        [first["id"], second["id"]]
        if failure_phase == "generate"
        else [second["id"]]
    )
    assert InsufficientThenSuccessClient.upload_accounts == [
        first["id"],
        first["id"],
        second["id"],
        second["id"],
    ]
    assert first_after["status"] == "disabled_low_balance"
    assert first_after["active_tasks"] == 0
    assert second_after["last_balance"] == 96
    assert second_after["active_tasks"] == 0


def test_suspended_account_is_disabled_and_task_switches_account(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(service_module, "AkoolClient", SuspendedThenSuccessClient)
    monkeypatch.setattr(service_module, "stop_managed_browser", lambda *args: None)
    db = Database(str(tmp_path / "suspended-switch.db"), default_concurrency=8)
    first = db.upsert_account(
        {"name": "suspended", "status": "active", "last_balance": 100}
    )
    second = db.upsert_account(
        {"name": "ready", "status": "active", "last_balance": 100}
    )
    SuspendedThenSuccessClient.failure_account_id = int(first["id"])
    payload = {
        "kind": "video",
        "model": "doubao-seedance-2-0-mini-260615",
        "prompt": "test",
        "duration": 4,
        "resolution": "480p",
        "aspect_ratio": "16:9",
        "account_id": first["id"],
        "_images": [{"value": "https://example.com/image.png", "name": "image.png"}],
        "_videos": [],
        "_audio": [],
        "_estimated_cost": 0,
    }
    db.create_task("gen_suspended_switch", payload)
    gateway = AKService(db, settings(tmp_path))

    try:
        gateway._run_task("gen_suspended_switch")
    finally:
        gateway.stop()

    task = db.get_task("gen_suspended_switch")
    first_after = db.get_account(first["id"])
    second_after = db.get_account(second["id"])
    assert task["status"] == "succeeded"
    assert task["generation_id"] == "resource-after-suspension"
    assert task["account_id"] == second["id"]
    assert first_after["enabled"] is False
    assert first_after["status"] == "suspended"
    assert first_after["active_tasks"] == 0
    assert second_after["active_tasks"] == 0


def test_batch_import_supports_leo_formats_and_balances_proxy_pool(
    tmp_path, monkeypatch
) -> None:
    runtime = settings(tmp_path)
    runtime.proxy_pool_enabled = True
    runtime.proxy_pool = "socks5://xray:20001\nsocks5://xray:20002"
    db = Database(str(tmp_path / "batch.db"), default_concurrency=8)
    gateway = AKService(db, runtime)
    started: list[int] = []
    monkeypatch.setattr(
        gateway,
        "schedule_login",
        lambda account_id: not started.append(int(account_id)),
    )

    try:
        result = gateway.batch_import(
            "\n".join(
                (
                    "one@example.com|password-one",
                    "two@example.com----password-two----socks5://xray:20009",
                    "three@example.com,password-three",
                    "one@example.com\tupdated-password\tsocks5://xray:20008",
                )
            ),
            start_login=True,
            use_proxy_pool=True,
        )
    finally:
        gateway.stop()

    assert result["input_count"] == 4
    assert result["count"] == 3
    assert result["duplicate_count"] == 1
    assert result["login_started_count"] == 3
    assert len(started) == 3
    accounts = {item["email"]: item for item in db.list_accounts(include_secrets=True)}
    assert accounts["one@example.com"]["password"] == "updated-password"
    assert accounts["one@example.com"]["proxy_url"] == "socks5://xray:20008"
    assert accounts["two@example.com"]["proxy_url"] == "socks5://xray:20009"
    assert accounts["three@example.com"]["proxy_url"] in {
        "socks5://xray:20001",
        "socks5://xray:20002",
    }


def test_batch_import_can_store_without_starting_login(tmp_path, monkeypatch) -> None:
    db = Database(str(tmp_path / "no-login.db"), default_concurrency=8)
    gateway = AKService(db, settings(tmp_path))
    monkeypatch.setattr(
        gateway,
        "schedule_login",
        lambda _account_id: (_ for _ in ()).throw(AssertionError("login was scheduled")),
    )
    try:
        result = gateway.batch_import(
            "proxy-only@example.com|xray:20001",
            start_login=False,
            use_proxy_pool=False,
        )
    finally:
        gateway.stop()

    assert result["login_started_count"] == 0
    account = db.list_accounts(include_secrets=True)[0]
    assert account["password"] == ""
    assert account["proxy_url"] == "socks5://xray:20001"


def test_runtime_login_limits_update_immediately(tmp_path) -> None:
    db = Database(str(tmp_path / "settings.db"), default_concurrency=8)
    gateway = AKService(db, settings(tmp_path))
    try:
        changed = gateway.update_runtime_settings(
            {
                "browser_login_workers": 4,
                "browser_login_stagger_seconds": 1.5,
                "browser_challenge_grace_seconds": 20,
                "account_maintenance_workers": 5,
            }
        )
    finally:
        gateway.stop()

    assert changed["browser_login_workers"] == 4
    assert changed["browser_login_stagger_seconds"] == 1.5
    assert changed["browser_challenge_grace_seconds"] == 20
    assert gateway._login_slots.limit == 4
    assert gateway._maintenance_slots.limit == 5


def test_video_reference_input_setting_rejects_new_tasks_and_updates_models(
    tmp_path,
) -> None:
    runtime = settings(tmp_path)
    runtime.allow_video_reference_inputs = False
    db = Database(str(tmp_path / "video-input-setting.db"), default_concurrency=8)
    gateway = AKService(db, runtime)

    try:
        models = gateway.models()
        assert models
        assert all(
            model["capabilities"]["media_limits"]["videos"] == 0
            for model in models
        )
        assert gateway.runtime_settings()["media_limits"]["videos"] == 0
        with pytest.raises(ValueError, match="video reference inputs are disabled"):
            gateway.create_task(
                {
                    "model": "doubao-seedance-2-0-mini-260615",
                    "prompt": "test",
                    "duration": 4,
                    "resolution": "480p",
                    "aspect_ratio": "adaptive",
                    "video_urls": ["https://example.com/reference.mp4"],
                }
            )
        assert db.active_task_count() == 0

        updated = gateway.update_runtime_settings(
            {"allow_video_reference_inputs": True}
        )
        assert updated["allow_video_reference_inputs"] is True
        assert gateway.models()[0]["capabilities"]["media_limits"]["videos"] > 0
    finally:
        gateway.stop()


def test_maintenance_checks_in_once_and_refreshes_balance_before_disabling(
    tmp_path,
    monkeypatch,
) -> None:
    DailyCheckinClient.signed = False
    DailyCheckinClient.status_calls = 0
    DailyCheckinClient.checkin_calls = 0
    monkeypatch.setattr(service_module, "AkoolClient", DailyCheckinClient)
    db = Database(str(tmp_path / "daily-checkin.db"), default_concurrency=8)
    account = db.upsert_account(
        {
            "name": "daily",
            "status": "active",
            "last_balance": 0,
            "enabled": True,
        }
    )
    gateway = AKService(db, settings(tmp_path))
    DailyCheckinClient.today = gateway._today_utc()

    try:
        gateway._maintenance_check(account["id"])
        gateway._maintenance_check(account["id"])
    finally:
        gateway.stop()

    refreshed = db.get_account(account["id"], include_secrets=False)
    assert DailyCheckinClient.status_calls == 1
    assert DailyCheckinClient.checkin_calls == 1
    assert refreshed["enabled"] is True
    assert refreshed["last_balance"] == 5
    assert refreshed["balance_details"]["sign_reward_stats"]["last_sign"] == (
        DailyCheckinClient.today
    )


def test_daily_checkin_setting_can_be_disabled_at_runtime(tmp_path) -> None:
    db = Database(str(tmp_path / "daily-setting.db"), default_concurrency=8)
    gateway = AKService(db, settings(tmp_path))
    try:
        updated = gateway.update_runtime_settings({"daily_checkin_enabled": False})
    finally:
        gateway.stop()

    assert updated["daily_checkin_enabled"] is False
