from __future__ import annotations

import json
from types import SimpleNamespace

import app.service as service_module
from app.akool_client import MediaUpload
from app.db import Database
from app.service import AKService


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
        prompt_media_reference_cleanup_enabled=False,
        model_map=json.dumps(
            {"seedance-mini": "doubao-seedance-2-0-mini-260615"}
        ),
        schema_version="test",
    )


class FakeAkoolClient:
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
