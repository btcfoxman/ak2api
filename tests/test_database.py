from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import Database


def test_duplicate_email_updates_existing_account(tmp_path) -> None:
    db = Database(str(tmp_path / "test.db"), default_concurrency=8)
    first = db.upsert_account({"name": "one", "email": "USER@example.com"})
    second = db.upsert_account(
        {"name": "two", "email": "user@example.com", "proxy_url": "socks5://xray:20002"}
    )

    assert first["id"] == second["id"]
    assert second["proxy_url"] == "socks5://xray:20002"
    assert len(db.list_accounts()) == 1


def test_account_media_cache_persists_and_cascades_on_delete(tmp_path) -> None:
    db = Database(str(tmp_path / "media-cache.db"), default_concurrency=8)
    account = db.upsert_account({"name": "cached", "enabled": False})

    stored = db.set_account_media_cache(
        account["id"],
        "text_to_video_black_1024_v1",
        {
            "profile_id": "profile-black",
            "url": "https://cdn.example.com/black.jpg",
            "content_type": "image/jpeg",
            "size": 6365,
            "width": 1024,
            "height": 1024,
        },
    )

    assert stored["profile_id"] == "profile-black"
    assert stored["width"] == 1024
    assert db.delete_account(account["id"]) is True
    assert (
        db.get_account_media_cache(
            account["id"],
            "text_to_video_black_1024_v1",
        )
        is None
    )


def test_balance_reservation_prevents_oversubscription(tmp_path) -> None:
    db = Database(str(tmp_path / "test.db"), default_concurrency=8)
    db.upsert_account(
        {"name": "ready", "status": "active", "last_balance": 5, "max_concurrency": 8}
    )
    payload = {
        "kind": "video",
        "model": "doubao-seedance-2-0-mini-260615",
        "prompt": "test",
        "duration": 4,
        "resolution": "480p",
        "_estimated_cost": 4,
    }
    db.create_task("one", payload)
    db.create_task("two", payload)

    assert db.acquire_account(task_id="one", reservation_cost=4, minimum_balance=4)
    assert db.acquire_account(task_id="two", reservation_cost=4, minimum_balance=4) is None
    assert db.estimate_cost(payload) == 0


def test_available_account_count_excludes_attempted_accounts(tmp_path) -> None:
    db = Database(str(tmp_path / "count.db"), default_concurrency=8)
    first = db.upsert_account({"name": "first", "status": "active"})
    second = db.upsert_account({"name": "second", "status": "active"})
    db.upsert_account({"name": "disabled", "status": "disabled", "enabled": False})

    assert db.available_account_count() == 2
    assert db.available_account_count({first["id"]}) == 1
    assert db.available_account_count({first["id"], second["id"]}) == 0


def test_available_account_count_applies_balance_reservations(tmp_path) -> None:
    db = Database(str(tmp_path / "funded-count.db"), default_concurrency=8)
    account = db.upsert_account(
        {"name": "funded", "status": "active", "last_balance": 200}
    )
    payload = {"kind": "video", "model": "test", "prompt": "test"}
    db.create_task("reserved", payload)
    assert db.acquire_account(
        preferred_id=account["id"],
        task_id="reserved",
        reservation_cost=60,
    )

    assert db.available_account_count(minimum_balance=140) == 1
    assert db.available_account_count(minimum_balance=141) == 0


def test_account_dispatch_spreads_active_tasks_before_reusing_account(tmp_path) -> None:
    db = Database(str(tmp_path / "spread.db"), default_concurrency=8)
    accounts = [
        db.upsert_account(
            {
                "name": f"account-{index}",
                "status": "active",
                "last_balance": 100 + index * 100,
                "max_concurrency": 8,
            }
        )
        for index in range(3)
    ]
    payload = {"kind": "video", "model": "test", "prompt": "test"}
    for index in range(4):
        db.create_task(f"task-{index}", payload)

    selected = [
        db.acquire_account(task_id=f"task-{index}", reservation_cost=0)
        for index in range(4)
    ]

    assert [item["id"] for item in selected[:3] if item] == [
        account["id"] for account in accounts
    ]
    assert selected[3] is not None
    assert selected[3]["id"] == accounts[0]["id"]


@pytest.mark.parametrize("extra", [{}, {"max_concurrency": None}])
def test_account_sync_keeps_existing_concurrency_when_unspecified(tmp_path, extra) -> None:
    db = Database(str(tmp_path / "sync.db"), default_concurrency=8)
    account = db.upsert_account({"name": "one", "email": "user@example.com", "max_concurrency": 2})

    synced = db.upsert_account({"name": "synced", "email": "user@example.com", "cookie_header": "session=test", **extra})
    assert synced["id"] == account["id"]
    assert synced["max_concurrency"] == 2

    changed = db.upsert_account({"name": "one", "max_concurrency": 3})
    assert changed["max_concurrency"] == 3


def test_parallel_account_acquisition_respects_limit_across_database_instances(tmp_path) -> None:
    databases = [Database(str(tmp_path / "parallel.db")) for _ in range(8)]
    db = databases[0]
    account = db.upsert_account({"name": "one", "status": "active", "max_concurrency": 2})
    for index in range(8):
        db.create_task(str(index), {"kind": "video", "model": "test"})
    barrier = threading.Barrier(8)

    def acquire(index):
        barrier.wait(timeout=5)
        return databases[index].acquire_account(task_id=str(index))

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(acquire, range(8)))

    assert sum(result is not None for result in results) == 2
    assert db.get_account(account["id"])["active_tasks"] == 2


def test_slot_acquisition_and_release_are_idempotent_per_task(tmp_path) -> None:
    db = Database(str(tmp_path / "slots.db"))
    account = db.upsert_account({"name": "one", "status": "active", "last_balance": 100, "max_concurrency": 2})
    other = db.upsert_account({"name": "other", "status": "active"})
    for task_id in ("one", "two", "three", "four"):
        db.create_task(task_id, {"kind": "video", "model": "test"})
    for task_id in ("one", "two"):
        assert db.acquire_account(account["id"], task_id=task_id, reservation_cost=4)

    assert db.acquire_account(account["id"], task_id="one")
    assert db.get_account(account["id"])["active_tasks"] == 2
    assert db.get_account(account["id"])["reserved_balance"] == 8
    db.release_account(other["id"], task_id="two")
    assert db.get_account(account["id"])["reserved_balance"] == 8
    db.release_account(account["id"], task_id="one")
    db.release_account(account["id"], task_id="one")
    assert db.get_account(account["id"])["active_tasks"] == 1
    assert db.acquire_account(account["id"], task_id="three")
    assert db.acquire_account(account["id"], task_id="four") is None


def test_restart_reserves_submitted_tasks_before_dispatch_and_recovers_original_account(tmp_path) -> None:
    path = str(tmp_path / "restart.db")
    db = Database(path)
    account = db.upsert_account({"name": "one", "status": "active", "last_balance": 100, "max_concurrency": 1})
    other = db.upsert_account({"name": "other", "status": "active"})
    for task_id in ("queued", "submitted"):
        db.create_task(task_id, {"kind": "video", "model": "test"})
    assert db.acquire_account(account["id"], task_id="submitted", reservation_cost=4)
    db.update_task("submitted", generation_id="upstream-existing", status="running")

    restarted = Database(path)

    assert restarted.get_account(account["id"])["active_tasks"] == 1
    assert restarted.get_account(account["id"])["reserved_balance"] == 4
    assert restarted.acquire_account(account["id"], task_id="queued") is None
    assert [task["id"] for task in restarted.recoverable_tasks()] == ["submitted", "queued"]
    restarted.update_account(account["id"], {"enabled": False})
    recovered = restarted.acquire_account(other["id"], task_id="submitted", recovering=True)
    assert recovered["id"] == account["id"]
    assert recovered["active_tasks"] == 1
    assert recovered["total_uses"] == 1
    restarted.release_account(account["id"], task_id="submitted")
    assert restarted.get_account(account["id"])["active_tasks"] == 0


def test_legacy_database_migration_restores_running_slots_without_reserving_finished_tasks(tmp_path) -> None:
    path = str(tmp_path / "legacy.db")
    db = Database(path)
    account = db.upsert_account({"name": "one", "status": "active", "max_concurrency": 1})
    for task_id in ("running", "finished", "queued"):
        db.create_task(task_id, {"kind": "video", "model": "test"})
    db.update_task("running", account_id=account["id"], generation_id="resource-running", status="running")
    db.update_task("finished", account_id=account["id"], generation_id="resource-finished", status="succeeded")
    with db.connect() as connection:
        connection.execute("DROP INDEX idx_ak_tasks_account_slot")
        connection.execute("ALTER TABLE tasks DROP COLUMN account_slot_acquired")

    migrated = Database(path)

    assert migrated.get_account(account["id"])["active_tasks"] == 1
    assert migrated.acquire_account(task_id="queued") is None
    assert migrated.acquire_account(task_id="running", recovering=True)["active_tasks"] == 1
    assert migrated.clear_finished_tasks() == 1
    assert migrated.get_task("running") is not None


def test_lowered_concurrency_blocks_new_tasks_until_existing_tasks_drain(tmp_path) -> None:
    db = Database(str(tmp_path / "lower-limit.db"))
    account = db.upsert_account({"name": "one", "status": "active", "max_concurrency": 2})
    for task_id in ("first", "second", "queued"):
        db.create_task(task_id, {"kind": "video", "model": "test"})
    assert db.acquire_account(task_id="first")
    assert db.acquire_account(task_id="second")

    db.update_account(account["id"], {"max_concurrency": 1})

    assert db.acquire_account(task_id="queued") is None
    db.release_account(account["id"], task_id="first")
    assert db.acquire_account(task_id="queued") is None
    db.release_account(account["id"], task_id="second")
    assert db.acquire_account(task_id="queued")


def test_clear_finished_tasks_waits_for_account_slot_release(tmp_path) -> None:
    db = Database(str(tmp_path / "clear.db"))
    account = db.upsert_account({"name": "one", "status": "active"})
    db.create_task("finished", {"kind": "video", "model": "test"})
    assert db.acquire_account(task_id="finished")
    db.update_task("finished", status="succeeded")

    assert db.clear_finished_tasks() == 0
    db.release_account(account["id"], task_id="finished")
    assert db.clear_finished_tasks() == 1
    assert db.get_account(account["id"])["active_tasks"] == 0
