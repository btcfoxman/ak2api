from dataclasses import replace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def console(tmp_path, monkeypatch):
    import app.config as config

    monkeypatch.setattr(
        config,
        "settings",
        replace(config.settings, database_path=str(tmp_path / "api.db")),
    )
    import app.main as main

    monkeypatch.setattr(
        main, "settings", replace(config.settings, admin_token="test-admin")
    )
    return main, TestClient(main.app)


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("post", "/open"),
        ("get", ""),
        ("post", "/action"),
        ("post", "/complete"),
        ("post", "/close"),
    ],
)
def test_manual_browser_endpoints_require_admin(console, method, suffix):
    _, client = console
    response = client.request(
        method, f"/api/accounts/1/browser{suffix}", json={"action": "click"}
    )
    assert response.status_code == 401


def test_manual_snapshot_is_not_cached_and_action_is_validated(console, monkeypatch):
    main, client = console
    client.cookies.set("ak_admin", "test-admin")
    monkeypatch.setattr(
        main.service,
        "manual_browser_snapshot",
        lambda account_id: {
            "image": "data:image/jpeg;base64,image",
            "challenge_required": True,
        },
    )
    result = client.get("/api/accounts/1/browser")
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["challenge_required"] is True
    for payload in [
        {"action": "evaluate", "script": "alert(1)"},
        {"action": "click", "x": 2},
    ]:
        assert (
            client.post("/api/accounts/1/browser/action", json=payload).status_code
            == 422
        )
