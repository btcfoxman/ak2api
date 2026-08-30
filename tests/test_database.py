from __future__ import annotations

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
