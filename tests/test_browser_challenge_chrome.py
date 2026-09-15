"""Native CDP checks on local fixtures; no credentials or live CF requests."""

from __future__ import annotations

import json
import socket
import subprocess

import pytest

from app.browser_context import _CDP, _chrome_executable, _page_state, _wait_target


@pytest.fixture(scope="module")
def chrome(tmp_path_factory):
    try:
        executable = _chrome_executable("")
    except RuntimeError:
        pytest.skip("Chrome is not installed")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    profile = tmp_path_factory.mktemp("cf-detection-chrome")
    process = subprocess.Popen(
        [
            executable,
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    client = None
    try:
        client = _CDP(_wait_target(port, 15)["webSocketDebuggerUrl"])
        client.call("Network.enable")
        client.call("Network.setBlockedURLs", {"urls": ["http://*", "https://*"]})
        yield client
    finally:
        if client:
            try:
                client.call("Browser.close")
            except RuntimeError:
                pass
            client.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


def put_frame(chrome, mode="closed", nested=False):
    chrome.evaluate(
        """(async () => {
      document.title = 'Local browser fixture';
      document.body.innerHTML = '';
      let doc = document;
      if (NESTED) {
        const outer = document.createElement('iframe');
        const loaded = new Promise(resolve => outer.onload = resolve);
        outer.srcdoc = '<!doctype html><body></body>';
        document.body.append(outer);
        await loaded;
        doc = outer.contentDocument;
      }
      const host = doc.createElement('div');
      doc.body.append(host);
      const root = MODE === 'light' ? host : host.attachShadow({mode: MODE});
      const frame = doc.createElement('iframe');
      frame.src = 'https://challenges.cloudflare.com/turnstile/v0/local-fixture';
      frame.style.cssText = 'width:300px;height:65px;border:0';
      root.append(frame);
      window.fixtureFrame = frame;
      window.fixtureHost = host;
    })()""".replace("NESTED", json.dumps(nested)).replace("MODE", json.dumps(mode))
    )


@pytest.mark.parametrize(
    "mode,nested",
    [
        ("light", False),
        ("open", False),
        ("closed", False),
        ("closed", True),
    ],
)
def test_real_cdp_detects_visible_cf_frames(chrome, mode, nested):
    put_frame(chrome, mode, nested)
    if mode == "closed" and not nested:
        assert chrome.evaluate("document.querySelectorAll('iframe').length") == 0
        assert chrome.evaluate("fixtureHost.shadowRoot === null") is True
    assert _page_state(chrome)["hasChallenge"] is True


@pytest.mark.parametrize(
    "style",
    [
        "fixtureFrame.style.display = 'none'",
        "fixtureFrame.style.visibility = 'hidden'",
        "fixtureFrame.style.opacity = '0'",
        "fixtureHost.style.opacity = '0'",
        "fixtureFrame.style.width = '1px'; fixtureFrame.style.height = '1px'",
    ],
)
@pytest.mark.parametrize("mode", ["light", "closed"])
def test_real_cdp_ignores_hidden_or_invisible_cf_frames(chrome, style, mode):
    put_frame(chrome, mode)
    chrome.evaluate(style)
    assert _page_state(chrome)["hasChallenge"] is False


def test_real_cdp_refreshes_challenge_state_when_frame_changes(chrome):
    put_frame(chrome)
    chrome.evaluate("fixtureFrame.remove()")
    assert _page_state(chrome)["hasChallenge"] is False
    put_frame(chrome)
    assert _page_state(chrome)["hasChallenge"] is True
    chrome.evaluate("fixtureHost.style.display = 'none'")
    assert _page_state(chrome)["hasChallenge"] is False


@pytest.mark.parametrize(
    "text", ["Verify you are human", "验证您是人类", "驗證您是人類"]
)
def test_real_cdp_detects_challenge_page_text(chrome, text):
    chrome.evaluate(f"document.body.textContent = {json.dumps(text)}")
    assert _page_state(chrome)["hasChallenge"] is True
