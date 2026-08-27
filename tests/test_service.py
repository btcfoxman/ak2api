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
