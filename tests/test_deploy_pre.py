"""Offline release safety, idle guards and graceful owned-browser shutdown."""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest


@pytest.fixture
def deployer(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "scripts/deploy_pre.py"
    spec = importlib.util.spec_from_file_location("ak_deploy_pre_test", source)
    module = importlib.util.module_from_spec(spec)
    mask = os.umask(0o077)
    try:
        spec.loader.exec_module(module)
    finally:
        os.umask(mask)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline test attempted a real Docker command or subprocess")

    for function in ("run", "check_output", "Popen"):
        monkeypatch.setattr(module.subprocess, function, forbidden)
    return module


def database_snapshot(**updates):
    result = {"path": "/isolated/ak2api.db", "statuses": {"succeeded": 1}, "task_hashes": {"old": "task-hash"},
              "account_statuses": {"active": 1}, "account_config": {"1": "account-hash"},
              "account_activity": {"active_tasks": 0, "future_login_leases": 0, "future_maintenance_leases": 0},
              "browser_profiles": [{"account_id": 1, "profile": "/profiles/account-1", "port": 19801}],
              "settings": {}, "manual_cost_config": {}, "protected_tables": {}, "reward_statuses": {}}
    result.update(updates)
    return {"app_hashes": {}, "databases": [result]}


@pytest.mark.parametrize("old,new", [
    (["A=1"], ["A=1", "AK_NEW=2"]), (["A=1"], []), (["A=1"], ["A=2"]),
    (["A=1", "A=1"], ["A=1"]), (["A=1"], ["A=1", "A=1"]),
])
def test_environment_must_be_exact_with_no_special_additions(deployer, old, new):
    with pytest.raises(AssertionError):
        deployer.assert_environment_preserved(old, new)


def test_environment_order_can_change_but_values_are_unchanged(deployer):
    deployer.assert_environment_preserved(["A=1=2", "EMPTY="], ["EMPTY=", "A=1=2"])
    assert not hasattr(deployer, "configured_musapi_additions")
    assert not hasattr(deployer, "MUSAPI_ENV_KEYS")


@pytest.mark.parametrize("status", ["browser_login", "profile_resetting", "login_pending", "logging_in", "maintenance", "checking_in"])
def test_account_work_prevents_release_even_when_tasks_are_terminal(deployer, status):
    assert deployer.busy(database_snapshot(account_statuses={status: 1}))


@pytest.mark.parametrize("key", ["active_tasks", "future_login_leases", "future_maintenance_leases"])
def test_database_activity_prevents_release(deployer, key):
    activity = {"active_tasks": 0, "future_login_leases": 0, "future_maintenance_leases": 0, key: 1}
    assert deployer.busy(database_snapshot(account_activity=activity))


def test_terminal_tasks_and_no_account_activity_are_idle(deployer):
    assert deployer.busy(database_snapshot()) == {}
    assert deployer.busy(database_snapshot(statuses={"running": 1})) == {"running": 1}


def run_snapshot(deployer, monkeypatch, tmp_path, database):
    monkeypatch.setattr(deployer.os, "environ", {
        "AK_DATABASE_PATH": str(database), "AK_CHROME_USER_DATA_ROOT": str(tmp_path / "profiles")})
    source = deployer.SNAPSHOT.replace("base=pathlib.Path('/app')", "base=pathlib.Path(" + repr(str(tmp_path / "app")) + ")")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(compile(source, "offline-snapshot", "exec"), {})
    return json.loads(output.getvalue())


def test_snapshot_normalizes_new_default_columns_and_counts_integer_leases(deployer, monkeypatch, tmp_path):
    database = tmp_path / "ak2api.db"
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE tasks(id TEXT,status TEXT,prompt TEXT);
            INSERT INTO tasks VALUES('task','succeeded','preserved prompt');
            CREATE TABLE accounts(id INTEGER,name TEXT,enabled INTEGER,status TEXT,active_tasks INTEGER,
                profile_dir TEXT,cdp_port INTEGER,cookies_json TEXT);
            INSERT INTO accounts VALUES(1,'original',1,'active',0,'',NULL,'[]');
            CREATE TABLE settings(key TEXT,value TEXT);
        """)
    before = run_snapshot(deployer, monkeypatch, tmp_path, database)
    with sqlite3.connect(database) as db:
        for name, definition in (("session_mode", "TEXT DEFAULT ''"), ("session_source", "TEXT DEFAULT ''"),
                                 ("session_revision", "INTEGER DEFAULT 0"), ("browser_binding_json", "TEXT DEFAULT '{}'"),
                                 ("login_lease_until", "INTEGER DEFAULT 0"), ("maintenance_lease_until", "INTEGER DEFAULT 0")):
            db.execute("ALTER TABLE accounts ADD COLUMN " + name + " " + definition)
    after = run_snapshot(deployer, monkeypatch, tmp_path, database)
    assert before["databases"][0]["account_config"] == after["databases"][0]["account_config"]
    assert before["databases"][0]["task_hashes"] == after["databases"][0]["task_hashes"]
    assert after["databases"][0]["browser_profiles"] == [{"account_id": 1, "profile": str((tmp_path / "profiles/account-1").resolve()), "port": 19801}]
    with sqlite3.connect(database) as db:
        db.execute("UPDATE accounts SET login_lease_until=strftime('%s','now')+100,maintenance_lease_until=strftime('%s','now')+200")
    assert deployer.busy(run_snapshot(deployer, monkeypatch, tmp_path, database)) == {
        "account_future_login_leases": 1, "account_future_maintenance_leases": 1}
    with sqlite3.connect(database) as db:
        db.execute("UPDATE accounts SET login_lease_until=1,maintenance_lease_until=2")
    assert deployer.busy(run_snapshot(deployer, monkeypatch, tmp_path, database)) == {}


def process(proc, pid, arguments, *, state="S", start="123"):
    path = proc / str(pid)
    path.mkdir(parents=True)
    (path / "cmdline").write_bytes(b"\0".join(os.fsencode(item) for item in arguments) + b"\0")
    (path / "stat").write_text(str(pid) + " (name with spaces) " + " ".join([state] + ["0"] * 18 + [start] + ["0"] * 10))
    return path


def chrome(profile, port, *, separate=False):
    return ["/usr/lib/chromium/chromium", *( ["--user-data-dir", str(profile), "--remote-debugging-port", str(port)]
        if separate else ["--user-data-dir=" + str(profile), "--remote-debugging-port=" + str(port)])]


@pytest.mark.parametrize("separate", [False, True])
def test_browser_matching_requires_both_exact_profile_and_cdp_and_skips_children(deployer, tmp_path, separate):
    proc = tmp_path / "proc"
    profile = tmp_path / "profiles/account-1"
    process(proc, 101, chrome(profile, 19801, separate=separate))
    process(proc, 102, chrome(profile, 19802))
    process(proc, 103, chrome(profile, 19801) + ["--type=renderer"])
    process(proc, 104, ["python", "uvicorn", "app.main:app"])
    owned, unknown = deployer._account_browser_processes(proc, [{"account_id": 1, "profile": str(profile), "port": 19801}])
    assert owned == [{"pid": 101, "start": "123", "account_id": 1}]
    assert unknown == [{"pid": 102, "start": "123"}]


def test_unknown_browser_prevents_any_signal(deployer, monkeypatch, tmp_path):
    proc = tmp_path / "proc"
    process(proc, 1, ["uvicorn", "app.main:app"], state="T")
    profile = tmp_path / "profile"
    process(proc, 101, chrome(profile, 19801))
    process(proc, 102, chrome(tmp_path / "unrelated", 19999))
    monkeypatch.setattr(deployer, "_term_browser", lambda *a: pytest.fail("must not signal before all ownership checks"))
    with pytest.raises(AssertionError, match="unrecognized browser"):
        deployer._flush_browser_processes(proc, [{"account_id": 1, "profile": str(profile), "port": 19801}], {"pid": 1, "start": "123"})


def test_browser_flush_requires_same_frozen_application(deployer, monkeypatch, tmp_path):
    proc = tmp_path / "proc"
    process(proc, 1, ["uvicorn", "app.main:app"], state="R")
    monkeypatch.setattr(deployer, "_term_browser", lambda *a: pytest.fail("running ASGI must not lose its Chrome"))
    with pytest.raises(AssertionError, match="application must remain frozen"):
        deployer._flush_browser_processes(proc, [], {"pid": 1, "start": "123"})


def test_old_image_with_five_disabled_accounts_and_no_chrome_needs_no_signal(deployer, monkeypatch, tmp_path):
    proc = tmp_path / "proc"
    process(proc, 1, ["python", "uvicorn", "app.main:app"], state="T")
    bindings = [{"account_id": aid, "profile": str(tmp_path / ("account-" + str(aid))), "port": 19800 + aid}
                for aid in range(1, 6)]
    assert not deployer.busy(database_snapshot(account_statuses={"disabled": 5}, browser_profiles=bindings))
    monkeypatch.setattr(deployer, "_term_browser", lambda *a: pytest.fail("no Chrome should need a signal"))
    assert deployer._flush_browser_processes(proc, bindings, {"pid": 1, "start": "123"}) == {
        "flushed_account_browsers": 0, "graceful_profile_flush": True}


def test_successful_flush_sends_only_term_to_owned_parent_and_keeps_profile(deployer, monkeypatch, tmp_path):
    proc, profile = tmp_path / "proc", tmp_path / "profile"
    profile.mkdir()
    (profile / "Cookies").write_text("preserve")
    process(proc, 1, ["uvicorn", "app.main:app"], state="T")
    parent = process(proc, 101, chrome(profile, 19801))
    child = process(proc, 102, chrome(profile, 19801) + ["--type=renderer"])
    seen = []

    def term(_proc, identity):
        seen.append(identity["pid"])
        (parent / "stat").unlink()
        (parent / "cmdline").unlink()
        parent.rmdir()

    monkeypatch.setattr(deployer, "_term_browser", term)
    assert deployer._flush_browser_processes(proc, [{"account_id": 1, "profile": str(profile), "port": 19801}],
        {"pid": 1, "start": "123"}) == {"flushed_account_browsers": 1, "graceful_profile_flush": True}
    assert seen == [101] and child.exists() and (profile / "Cookies").read_text() == "preserve"


def test_flush_timeout_never_force_kills_browser(deployer, monkeypatch, tmp_path):
    proc, profile = tmp_path / "proc", tmp_path / "profile"
    process(proc, 1, ["uvicorn", "app.main:app"], state="T")
    parent = process(proc, 101, chrome(profile, 19801))
    signals = []
    monkeypatch.setattr(deployer, "_term_browser", lambda path, identity: signals.append((identity["pid"], "SIGTERM")))
    ticks = iter([0, 11])
    monkeypatch.setattr(deployer.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="flush_timeout_no_force_kill"):
        deployer._flush_browser_processes(proc, [{"account_id": 1, "profile": str(profile), "port": 19801}], {"pid": 1, "start": "123"})
    assert signals == [(101, "SIGTERM")] and parent.exists()


@pytest.mark.parametrize("recycled", [False, True])
def test_pidfd_term_is_bound_to_original_process_identity(deployer, monkeypatch, tmp_path, recycled):
    proc = tmp_path / "proc"
    process(proc, 101, ["chromium"], start="456" if recycled else "123")
    opened, sent, closed = [], [], []
    monkeypatch.setattr(deployer.os, "pidfd_open", lambda pid: opened.append(pid) or 999, raising=False)
    monkeypatch.setattr(deployer.signal, "pidfd_send_signal", lambda fd, sig: sent.append((fd, sig)), raising=False)
    monkeypatch.setattr(deployer.os, "close", lambda fd: closed.append(fd))
    deployer._term_browser(proc, {"pid": 101, "start": "123"})
    assert opened == [101] and closed == [999]
    assert sent == ([] if recycled else [(999, deployer.signal.SIGTERM)])


def test_fresh_lease_prevents_generating_browser_signal_probe(deployer, monkeypatch):
    frozen = database_snapshot()
    fresh = database_snapshot(account_activity={"future_login_leases": 1})
    monkeypatch.setattr(deployer, "snapshot", lambda: fresh)
    monkeypatch.setattr(deployer, "python", lambda *a: pytest.fail("fresh activity must block before process signals"))
    with pytest.raises(AssertionError, match="account activity started"):
        deployer.flush_account_browsers(frozen, {"pid": 1, "start": "123"})


def test_flush_probe_is_self_contained_and_has_no_kill_escalation(deployer, monkeypatch):
    frozen, seen = database_snapshot(), []
    monkeypatch.setattr(deployer, "snapshot", lambda: frozen)

    def probe(source):
        ast.parse(source)
        seen.append(source)
        return {"flushed_account_browsers": 0, "graceful_profile_flush": True}

    monkeypatch.setattr(deployer, "python", probe)
    deployer.flush_account_browsers(frozen, {"pid": 1, "start": "123"})
    assert len(seen) == 1 and "pidfd_send_signal" in seen[0] and "SIGTERM" in seen[0]
    assert "SIGKILL" not in seen[0] and "shutil.rmtree" not in seen[0]


def setup_release(deployer, monkeypatch, tmp_path):
    release, directory = tmp_path / "release", tmp_path / "compose"
    release.mkdir()
    directory.mkdir()
    (directory / ".env").write_text("AK_EXISTING=unchanged\n")
    config = directory / "compose.yaml"
    config.write_text("services: {}\n")
    old = {"Id": "container-old", "Image": "sha256:old", "State": {"Running": True},
           "Config": {"Env": ["AK_EXISTING=unchanged"]}, "Mounts": [], "HostConfig": {"PortBindings": {}},
           "NetworkSettings": {"Networks": {"existing": {}}}}
    candidate = {**old, "Id": "container-new", "Image": "sha256:new"}
    current = {"container": old}
    built = {"base_image": "ak-rollback:old", "image": "ghcr.io/btcfoxman/ak2api:release", "image_id": "sha256:new"}
    (release / "build.json").write_text(json.dumps(built))
    manifest = {"image_id": old["Image"], "app_hashes": {}, "files": {}, "release": "isolated", "project": "ak",
                "directory": str(directory), "configs": [str(config)]}
    monkeypatch.setattr(deployer, "ROOT", release)
    monkeypatch.setattr(deployer, "M", manifest)
    monkeypatch.setattr(deployer, "r", SimpleNamespace(inspect=lambda service: current["container"],
        compose=lambda info, override: ["docker", "compose", str(override)], container_python=lambda service, source: ""))
    snapshot, events = database_snapshot(), []
    monkeypatch.setattr(deployer, "sources_guard", lambda: snapshot)
    monkeypatch.setattr(deployer, "snapshot", lambda: snapshot)
    monkeypatch.setattr(deployer, "signal_app", lambda sig, expected=None: events.append(sig) or {"pid": 1, "start": "123"})
    monkeypatch.setattr(deployer, "watchdog", lambda *a, **k: None)
    monkeypatch.setattr(deployer, "resume", lambda identity, **kwargs: events.append("resume") if identity else None)
    monkeypatch.setattr(deployer, "health", lambda *a, **k: {"status": 200})

    def command(args, label, **kwargs):
        events.append(label)
        if label == "verify-rollback-image": return old["Image"]
        if label == "verify-candidate-image": return built["image_id"]
        if label == "start": current["container"] = candidate
        return ""

    monkeypatch.setattr(deployer, "command", command)
    return SimpleNamespace(release=release, old=old, candidate=candidate, built=built, current=current,
                           manifest=manifest, events=events, snapshot=snapshot)


def test_flush_timeout_resumes_asgi_without_stop_or_cutover(deployer, monkeypatch, tmp_path):
    context = setup_release(deployer, monkeypatch, tmp_path)
    monkeypatch.setattr(deployer, "flush_account_browsers", lambda *a: (_ for _ in ()).throw(RuntimeError("flush timeout")))
    with pytest.raises(RuntimeError, match="flush timeout"):
        deployer.deploy()
    assert "SIGSTOP" in context.events and "resume" in context.events
    assert "stop-idle" not in context.events and "start" not in context.events
    assert not list(context.release.glob("stop-handoff-*"))


def test_successful_release_flushes_before_zero_timeout_stop(deployer, monkeypatch, tmp_path):
    context = setup_release(deployer, monkeypatch, tmp_path)
    monkeypatch.setattr(deployer, "flush_account_browsers", lambda *a: context.events.append("profile-flush") or {
        "flushed_account_browsers": 1, "graceful_profile_flush": True})
    deployer.deploy()
    assert context.events.index("profile-flush") < context.events.index("stop-idle") < context.events.index("start")
    receipt = json.loads((context.release / "deployment.json").read_text())
    assert receipt["existing_environment_unchanged"] is True
    assert receipt["profile_flush"]["graceful_profile_flush"] is True
    assert receipt["tasks_preserved"] == 1 and receipt["paid_requests"] == 0


def test_busy_candidate_rollback_defers_without_closing_chrome(deployer, monkeypatch, tmp_path):
    context = setup_release(deployer, monkeypatch, tmp_path)
    context.current["container"] = context.candidate
    monkeypatch.setattr(deployer, "snapshot", lambda: database_snapshot(account_activity={"future_maintenance_leases": 1}))
    monkeypatch.setattr(deployer, "flush_account_browsers", lambda *a: pytest.fail("busy candidate must keep browser"))
    assert deployer.rollback_if_idle(context.old, context.built, ["restore"]) is False
    assert "resume" in context.events and "rollback" not in context.events


def test_rollback_flush_timeout_resumes_candidate_without_replacement(deployer, monkeypatch, tmp_path):
    context = setup_release(deployer, monkeypatch, tmp_path)
    context.current["container"] = context.candidate
    monkeypatch.setattr(deployer, "flush_account_browsers", lambda *a: (_ for _ in ()).throw(RuntimeError("flush timeout")))
    with pytest.raises(RuntimeError, match="flush timeout"):
        deployer.rollback_if_idle(context.old, context.built, ["restore"])
    assert "resume" in context.events and "rollback" not in context.events and "stop-idle-candidate" not in context.events


def test_successful_rollback_flushes_candidate_before_replacing_it(deployer, monkeypatch, tmp_path):
    context = setup_release(deployer, monkeypatch, tmp_path)
    context.current["container"] = context.candidate
    original_command = deployer.command

    def command(args, label, **kwargs):
        result = original_command(args, label, **kwargs)
        if label == "rollback":
            context.current["container"] = context.old
        return result

    monkeypatch.setattr(deployer, "command", command)
    monkeypatch.setattr(deployer, "flush_account_browsers", lambda *a: context.events.append("profile-flush") or {
        "flushed_account_browsers": 1, "graceful_profile_flush": True})
    assert deployer.rollback_if_idle(context.old, context.built, ["restore"]) is True
    assert context.events.index("profile-flush") < context.events.index("stop-idle-candidate") < context.events.index("rollback")
