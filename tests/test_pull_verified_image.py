"""Offline trust-chain and route-selection checks; never pulls a real image."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
import urllib.error

import pytest


@pytest.fixture
def puller(monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/pull_verified_image.py"
    spec = importlib.util.spec_from_file_location("ak_verified_image_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("test attempted an external request or subprocess")

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    monkeypatch.setattr(module.subprocess, "Popen", forbidden)
    monkeypatch.setattr(module.urllib.request.OpenerDirector, "open", forbidden)
    return module


class Stream(io.BytesIO):
    status = 206


@pytest.mark.parametrize("url", [
    "http://ghcr.io/v2/repo", "https://example.test/layer", "https://ghcr.io.evil.test/layer",
    "https://evilgithubusercontent.com/layer", "https://user:pass@ghcr.io/layer",
    "https://ghcr.io:8443/layer", "file:///tmp/layer",
])
def test_artifact_hosts_and_https_are_enforced_before_open(puller, monkeypatch, url):
    called = []
    monkeypatch.setattr(puller, "transport", lambda proxy: SimpleNamespace(open=lambda *a, **k: called.append(a)))
    with pytest.raises(RuntimeError, match="unexpected_artifact_redirect"):
        puller.response(url, None)
    assert called == []


def test_cross_host_redirect_drops_registry_authorization(puller, monkeypatch):
    seen = []

    def opened(request, **_kwargs):
        seen.append((request.full_url, dict(request.header_items())))
        if len(seen) == 1:
            raise urllib.error.HTTPError(request.full_url, 307, "redirect", {
                "Location": "https://pkg-containers.githubusercontent.com/blob?signature=secret"
            }, None)
        return Stream(b"ok")

    monkeypatch.setattr(puller, "transport", lambda proxy: SimpleNamespace(open=opened))
    with puller.response("https://ghcr.io/v2/repo/blobs/sha256:abc", None,
                         {"Authorization": "Bearer private-token", "Range": "bytes=0-1"}) as result:
        assert result.read() == b"ok"
    assert seen[0][1]["Authorization"] == "Bearer private-token"
    assert "Authorization" not in seen[1][1]
    assert seen[1][1]["Range"] == "bytes=0-1"


def test_redirect_to_unknown_host_is_not_followed(puller, monkeypatch):
    seen = []

    def opened(request, **_kwargs):
        seen.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 302, "redirect", {"Location": "https://evil.test/blob"}, None)

    monkeypatch.setattr(puller, "transport", lambda proxy: SimpleNamespace(open=opened))
    with pytest.raises(RuntimeError, match="unexpected_artifact_redirect"):
        puller.response("https://ghcr.io/v2/repo/blobs/digest", None, {"Authorization": "Bearer token"})
    assert len(seen) == 1


def test_metadata_read_is_bounded(puller, monkeypatch):
    monkeypatch.setattr(puller, "response", lambda *a, **k: Stream(b"123456"))
    with pytest.raises(RuntimeError, match="registry_metadata_too_large"):
        puller.read_bounded("unused", None, limit=5)


def test_route_environment_does_not_inherit_other_proxy_variants(puller, monkeypatch):
    for key in ("http_proxy", "HTTPS_PROXY", "all_proxy", "No_PrOxY"):
        monkeypatch.setenv(key, "old-proxy-secret")
    direct = puller.route_environment(None)
    proxied = puller.route_environment("http://127.0.0.1:10809")
    assert all(value != "old-proxy-secret" for value in direct.values())
    assert direct["NO_PROXY"] == "*" and direct["ALL_PROXY"] == ""
    assert proxied["HTTPS_PROXY"] == "http://127.0.0.1:10809"
    assert "NO_PROXY" not in proxied and "all_proxy" not in proxied


@pytest.mark.parametrize("partial_status,complete", [(206, True), (200, False)])
def test_speed_sample_requires_exact_one_megabyte_range(puller, monkeypatch, partial_status, complete):
    # The pull helper runs on Linux. Replace timers for a deterministic Windows-safe offline test.
    monkeypatch.setattr(puller.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(puller.signal, "ITIMER_REAL", 0, raising=False)
    monkeypatch.setattr(puller.signal, "signal", lambda *_: None)
    monkeypatch.setattr(puller.signal, "setitimer", lambda *_: None, raising=False)
    ticks = iter([0, 2])
    monkeypatch.setattr(puller.time, "monotonic", lambda: next(ticks))
    seen = []

    def response(url, route, headers, **kwargs):
        seen.append((url, route, headers))
        result = Stream(b"x" * 1048576)
        result.status = partial_status
        return result

    monkeypatch.setattr(puller, "response", response)
    result = puller.sample("https://ghcr.io/v2/repo/blobs/private-layer", None, {"Authorization": "Bearer private"})
    assert result["complete"] is complete
    assert result["bytes"] == (1048576 if complete else 0)
    assert seen == [("https://ghcr.io/v2/repo/blobs/private-layer", None, {
        "Authorization": "Bearer private", "Range": "bytes=0-1048575", "Accept-Encoding": "identity"})]


def tool_archive(*, link=False):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as bundle:
        entry = tarfile.TarInfo("crane")
        if link:
            entry.type, entry.linkname = tarfile.SYMTYPE, "../../outside"
            bundle.addfile(entry)
        else:
            value = b"verified-binary-not-executed"
            entry.size = len(value)
            bundle.addfile(entry, io.BytesIO(value))
        extra = tarfile.TarInfo("../../unrelated")
        extra.size = 3
        bundle.addfile(extra, io.BytesIO(b"bad"))
    return data.getvalue()


def test_tool_checksum_fallback_extracts_only_verified_regular_binary(puller, monkeypatch, tmp_path):
    archive = tool_archive()
    monkeypatch.setattr(puller, "TOOL_SHA", hashlib.sha256(archive).hexdigest())
    calls = []

    def download(_url, proxy, **_kwargs):
        calls.append(proxy)
        return Stream(b"corrupt" if proxy is None else archive)

    monkeypatch.setattr(puller, "response", download)
    binary = puller.acquire_tool(tmp_path, [None, "proxy"], tmp_path / "cache")
    assert calls == [None, "proxy"]
    assert binary.read_bytes() == b"verified-binary-not-executed"
    assert not (tmp_path.parent / "unrelated").exists()
    assert not (tmp_path / "crane.download").exists()
    monkeypatch.setattr(puller, "response", lambda *a, **k: pytest.fail("valid checksum cache was refetched"))
    assert puller.acquire_tool(tmp_path, [None], tmp_path / "cache") == binary


def test_tool_archive_symlink_is_never_installed(puller, monkeypatch, tmp_path):
    archive = tool_archive(link=True)
    monkeypatch.setattr(puller, "TOOL_SHA", hashlib.sha256(archive).hexdigest())
    monkeypatch.setattr(puller, "response", lambda *a, **k: Stream(archive))
    with pytest.raises(RuntimeError, match="crane_archive_invalid"):
        puller.acquire_tool(tmp_path, [None], tmp_path / "cache")
    assert not (tmp_path / "crane").exists()


def test_tool_all_checksum_failures_are_bounded_and_remove_partial(puller, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(puller, "response", lambda url, route, **k: calls.append(route) or Stream(b"corrupt"))
    with pytest.raises(RuntimeError, match="verified_crane_download_unavailable"):
        puller.acquire_tool(tmp_path, [None, "proxy"], tmp_path / "cache")
    assert calls == [None, "proxy"] and not (tmp_path / "crane.download").exists()


def test_slow_pull_terminates_only_its_owned_process(puller, monkeypatch, tmp_path):
    class Process:
        returncode = None
        stopped = False

        def poll(self): return self.returncode
        def terminate(self): self.stopped, self.returncode = True, 0
        def wait(self, timeout): return self.returncode
        def kill(self): pytest.fail("terminate succeeded; kill was unnecessary")

    process, calls = Process(), []
    monkeypatch.setattr(puller.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)) or process)
    ticks = iter([0, 46])
    monkeypatch.setattr(puller.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(puller.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="image_download_route_too_slow"):
        puller.pull(tmp_path / "crane", "ghcr.io/repo@sha256:fixed", tmp_path / "image.tar", None)
    assert process.stopped is True and len(calls) == 1
    assert calls[0][0][-2:] == ["--platform", "linux/amd64"]
    assert calls[0][1]["env"]["NO_PROXY"] == "*"


def prepare_main(puller, monkeypatch, tmp_path, *, index=False):
    root = tmp_path / "deploy"
    root.mkdir()
    expected = "/home/btcfoxman/docker/ak2api"
    real_path = Path
    monkeypatch.setattr(puller, "Path", lambda value: root if str(value) == expected else real_path(value))
    revision, config_digest = "a" * 40, "sha256:" + "b" * 64
    manifest = {"config": {"digest": config_digest}, "layers": [{"digest": "sha256:" + "c" * 64, "size": 2097152}]}
    raw_manifest = json.dumps(manifest).encode()
    image_digest = "sha256:" + hashlib.sha256(raw_manifest).hexdigest()
    parent = {"manifests": [{"platform": {"os": "linux", "architecture": "amd64"}, "digest": image_digest}]}
    raw = json.dumps(parent).encode() if index else raw_manifest
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    image = "ghcr.io/btcfoxman/ak2api:" + revision
    monkeypatch.setattr(puller.argparse.ArgumentParser, "parse_args", lambda self: SimpleNamespace(
        image=image, digest=digest, image_size=8388608, deploy_path=expected))
    monkeypatch.setattr(puller, "configured_proxy", lambda: "http://127.0.0.1:10809")
    monkeypatch.setattr(puller, "basic_auth", lambda: "private-base64")
    monkeypatch.setattr(puller.shutil, "disk_usage", lambda path: SimpleNamespace(free=2 ** 40))
    reads, commands, pulls = [], [], []

    def read(url, route, headers=None, **kwargs):
        reads.append((url, route, headers))
        if "/token?" in url: return b'{"token":"private-registry-token"}'
        return raw_manifest if index and url.endswith(image_digest) else raw

    def command(args, **kwargs):
        if args[:2] == ['docker','info']:
            return str(root).encode()
        commands.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": config_digest, "Size":8388608, "Config": {"Labels": {"org.opencontainers.image.revision": revision}}}]).encode()
        return b""

    def pull(binary, reference, archive, route):
        pulls.append((reference, route))
        archive.write_bytes(b"image archive verified by crane")

    monkeypatch.setattr(puller, "read_bounded", read)
    monkeypatch.setattr(puller, "command", command)
    monkeypatch.setattr(puller, "pull", pull)
    monkeypatch.setattr(puller, "acquire_tool", lambda folder, routes, cache: folder / "crane")
    monkeypatch.setattr(puller, "sample", lambda url, route, headers: {
        "route": "proxy" if route else "direct", "complete": True, "bytes": 1048576,
        "seconds": 1 if route else 2, "bytes_per_second": 1048576 if route else 524288})
    return SimpleNamespace(root=root, raw=raw, digest=digest, image_digest=image_digest, config=config_digest,
                           image=image, revision=revision, reads=reads, commands=commands, pulls=pulls)


@pytest.mark.parametrize("index", [False, True])
def test_fastest_complete_route_pulls_immutable_digest_and_verifies_before_sha_tag(
        puller, monkeypatch, tmp_path, capsys, index):
    context = prepare_main(puller, monkeypatch, tmp_path, index=index)
    puller.main()
    assert context.pulls == [("ghcr.io/btcfoxman/ak2api@" + context.image_digest, "http://127.0.0.1:10809")]
    assert [args[1] for args in context.commands] == ["load", "image", "tag"]
    assert context.commands[-1] == ["docker", "tag", context.config, context.image]
    receipt = json.loads((context.root / "last-image-pull.safe.json").read_text())
    assert receipt["selected_route"] == "proxy" and receipt["manifest_digest"] == context.digest
    assert receipt["loaded_config_digest"] == context.config
    assert receipt["builds_or_dependency_installs"] == 0 and receipt["docker_daemon_proxy_changed"] is False
    assert not list((context.root / ".pull-tools").glob("verified-pull-*"))
    output = capsys.readouterr().out
    assert "private" not in output and "127.0.0.1" not in output


def test_unconfirmed_manifest_digest_can_never_reach_pull_or_load(puller, monkeypatch, tmp_path):
    context = prepare_main(puller, monkeypatch, tmp_path)
    monkeypatch.setattr(puller, "read_bounded", lambda url, route, headers=None:
        b'{"token":"private-token"}' if "/token?" in url else context.raw + b" ")
    with pytest.raises(RuntimeError, match="registry_manifest_unavailable"):
        puller.main()
    assert context.pulls == [] and context.commands == []


def test_failed_fastest_route_retries_only_once_on_other_route(puller, monkeypatch, tmp_path):
    context = prepare_main(puller, monkeypatch, tmp_path)

    def pull(binary, reference, archive, route):
        context.pulls.append((reference, route))
        archive.write_bytes(b"partial" if route else b"complete")
        if route: raise RuntimeError("image_download_route_too_slow")

    monkeypatch.setattr(puller, "pull", pull)
    puller.main()
    assert [route for _, route in context.pulls] == ["http://127.0.0.1:10809", None]
    assert json.loads((context.root / "last-image-pull.safe.json").read_text())["selected_route"] == "direct"


def test_partial_sample_never_outranks_a_complete_route(puller, monkeypatch, tmp_path):
    context = prepare_main(puller, monkeypatch, tmp_path)
    monkeypatch.setattr(puller, "sample", lambda url, route, headers: {
        "route": "proxy" if route else "direct", "complete": not bool(route),
        "bytes": 1024 if route else 1048576, "seconds": 1,
        "bytes_per_second": 10 ** 9 if route else 1048576})
    puller.main()
    assert [route for _, route in context.pulls] == [None]


def test_failed_all_routes_do_not_load_or_tag_and_remove_archive(puller, monkeypatch, tmp_path):
    context = prepare_main(puller, monkeypatch, tmp_path)

    def pull(binary, reference, archive, route):
        context.pulls.append((reference, route))
        archive.write_bytes(b"partial")
        raise RuntimeError("crane_pull_failed")

    monkeypatch.setattr(puller, "pull", pull)
    with pytest.raises(RuntimeError, match="all_bounded_image_transfer_routes_failed"):
        puller.main()
    assert len(context.pulls) == 2 and context.commands == []
    assert not list((context.root / ".pull-tools").glob("verified-pull-*"))


def test_loaded_revision_mismatch_never_tags_release(puller, monkeypatch, tmp_path):
    context = prepare_main(puller, monkeypatch, tmp_path)

    def command(args, **kwargs):
        if args[:2] == ['docker','info']:
            return str(context.root).encode()
        context.commands.append(args)
        return json.dumps([{"Id": context.config, "Config": {"Labels": {
            "org.opencontainers.image.revision": "f" * 40}}}]).encode() if args[1] == "image" else b""

    monkeypatch.setattr(puller, "command", command)
    with pytest.raises(RuntimeError, match="loaded_image_identity_mismatch"):
        puller.main()
    assert not any(args[1] == "tag" for args in context.commands)
    assert not (context.root / "last-image-pull.safe.json").exists()


def test_disk_budget_rejection_precedes_image_transfer(puller, monkeypatch, tmp_path):
    context = prepare_main(puller, monkeypatch, tmp_path)
    monkeypatch.setattr(puller.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    with pytest.raises(RuntimeError, match="image_import_disk_budget_unavailable"):
        puller.main()
    assert context.pulls == [] and context.commands == []


def test_docker_storage_disk_is_checked_separately(puller, monkeypatch, tmp_path):
    root,docker=tmp_path/'archive',tmp_path/'docker'
    root.mkdir();docker.mkdir()
    actual=Path.stat
    monkeypatch.setattr(puller,'command',lambda *a,**k:str(docker).encode())
    def stat(path,*args,**kwargs):
        value=actual(path,*args,**kwargs)
        return SimpleNamespace(st_dev=1 if path==root else 2, st_mode=value.st_mode)
    monkeypatch.setattr(Path,'stat',stat)
    monkeypatch.setattr(puller.shutil,'disk_usage',lambda path:SimpleNamespace(free=2**40 if path==root else 1))
    with pytest.raises(RuntimeError,match='image_import_disk_budget_unavailable'):
        puller.check_disk_budget(root,2097152,8388608)
