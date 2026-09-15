from contextlib import contextmanager

import pytest
from pydantic import ValidationError

import app.browser_assist as assist
from app.browser_context import AkoolBrowserChallengeError
from app.schemas import BrowserAction


@pytest.mark.parametrize("coordinate", [-0.1, 1.1, float("nan"), float("inf")])
def test_browser_click_rejects_invalid_coordinates(coordinate):
    with pytest.raises(ValidationError):
        BrowserAction(action="click", x=coordinate)


def test_browser_click_dispatches_mouse_input_at_viewport_coordinates(monkeypatch):
    events = []

    class Client:
        def evaluate(self, expression):
            return {"width": 1200, "height": 800}

        def call(self, method, params):
            assert method == "Input.dispatchMouseEvent"
            events.append(params)

    @contextmanager
    def connection(*args, **kwargs):
        yield Client()

    monkeypatch.setattr(assist, "_connection", connection)
    assist.browser_action(
        {}, None, BrowserAction(action="click", x=0.25, y=0.5).model_dump()
    )
    assert [e["type"] for e in events] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
    ]
    assert all(e["x"] == 300 and e["y"] == 400 for e in events)
    assert events[1]["button"] == events[2]["button"] == "left"


@pytest.mark.parametrize("code", [1000, 1102])
def test_capture_checks_existing_session_without_navigation(monkeypatch, code):
    class Client:
        def evaluate(self, expression):
            return {"body": {"code": code}}

        def call(self, method, params=None):
            raise AssertionError(f"Unexpected browser operation: {method}")

    @contextmanager
    def connection(*args, **kwargs):
        yield Client()

    monkeypatch.setattr(assist, "_connection", connection)
    monkeypatch.setattr(
        assist, "_verified_context", lambda *args: {"cookie_header": "session=new"}
    )
    if code == 1000:
        assert assist.capture_browser_session({}, None) == {
            "cookie_header": "session=new"
        }
    else:
        with pytest.raises(AkoolBrowserChallengeError):
            assist.capture_browser_session({}, None)
