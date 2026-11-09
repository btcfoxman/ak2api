"""Offline trust-chain and route-selection checks; never pulls a real image."""
from __future__ import annotations

import hashlib
import gzip
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


@pytest.mark.parametrize("partial_status,complete", [(206, True), (200, True), (500, False)])
def test_speed_sample_requires_exact_one_megabyte_range(puller, monkeypatch, partial_status, complete):
    # The pull helper runs on Linux. Replace timers for a deterministic Windows-safe offline test.
    monkeypatch.setattr(puller.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(puller.signal, "ITIMER_REAL", 0, raising=False)
    monkeypatch.setattr(puller.signal, "signal", lambda *_: None)
    monkeypatch.setattr(puller.signal, "setitimer", lambda *_: None, raising=False)
    ticks = iter([0, 1, 2])
    monkeypatch.setattr(puller.time, "monotonic", lambda: next(ticks))
    seen, streams = [], []

    def response(url, route, headers, **kwargs):
        seen.append((url, route, headers))
        class CountedStream(Stream):
            consumed = 0
            def read(self, size=-1):
                value = super().read(size)
                self.consumed += len(value)
                return value
        result = CountedStream(b"x" * 2097152)
        result.status = partial_status
        streams.append(result)
        return result

    monkeypatch.setattr(puller, "response", response)
    result = puller.sample("https://ghcr.io/v2/repo/blobs/private-layer", None, {"Authorization": "Bearer private"})
    assert result["complete"] is complete
    assert result["bytes"] == (1048576 if complete else 0)
    assert result["failure_kind"] == (None if complete else "http_status_500")
    assert result["seconds"] == 2 and result["ttfb_seconds"] == 1 and result["body_seconds"] == 1
    assert result["bytes_per_second"] == (1048576 if complete else 0)
    assert streams[0].closed and streams[0].consumed == (1048576 if complete else 0)
    assert seen == [("https://ghcr.io/v2/repo/blobs/private-layer", None, {
        "Authorization": "Bearer private", "Range": "bytes=0-1048575", "Accept-Encoding": "identity"})]


@pytest.mark.parametrize("outcome,expected", [("short_json", "short_read"), ("http_error", "http_status_503"),
                                               ("network", "exception_URLError"), ("deadline", "deadline")])
def test_sample_failure_details_are_safe_and_do_not_accept_short_json(puller, monkeypatch, outcome, expected):
    monkeypatch.setattr(puller.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(puller.signal, "ITIMER_REAL", 0, raising=False)
    handler = []
    monkeypatch.setattr(puller.signal, "signal", lambda sig, callback: handler.append(callback))
    monkeypatch.setattr(puller.signal, "setitimer", lambda *_: None, raising=False)
    ticks = iter([0, 1, 2])
    monkeypatch.setattr(puller.time, "monotonic", lambda: next(ticks))

    def response(*args, **kwargs):
        if outcome == "network":raise urllib.error.URLError("credential-and-signed-url")
        if outcome == "http_error":raise puller.ArtifactHTTPError(503)
        if outcome == "deadline":handler[0]()
        stream = Stream(b'{"message":"not an image layer"}')
        stream.status = 200
        return stream

    monkeypatch.setattr(puller, "response", response)
    result = puller.sample("https://ghcr.io/signed-secret", "http://proxy:secret@127.0.0.1:10809", {})
    assert result["complete"] is False and result["failure_kind"] == expected
    assert result["bytes"] < 1048576
    assert "secret" not in json.dumps(result) and "credential" not in json.dumps(result)


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


@pytest.mark.parametrize("kilobytes_per_second", [0, 50])
def test_slow_pull_terminates_only_its_owned_process(puller, monkeypatch, tmp_path, kilobytes_per_second):
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
    if kilobytes_per_second:
        (tmp_path / "image.tar").write_bytes(b"x" * (46 * kilobytes_per_second * 1024))
    with pytest.raises(RuntimeError, match="image_download_route_too_slow"):
        puller.pull(tmp_path / "crane", "ghcr.io/repo@sha256:fixed", tmp_path / "image.tar", None)
    assert process.stopped is True and len(calls) == 1
    assert calls[0][0][-2:] == ["--platform", "linux/amd64"]
    assert calls[0][1]["env"]["NO_PROXY"] == "*"


def prepare_main(puller, monkeypatch, tmp_path, *, index=False):
    monkeypatch.setattr(puller, 'native_cached_pull', lambda *args, **kwargs: None)
    root = tmp_path / "deploy"
    root.mkdir()
    expected = "/home/btcfoxman/docker/ak2api"
    real_path = Path
    monkeypatch.setattr(puller, "Path", lambda value: root if str(value) == expected else real_path(value))
    revision = "a" * 40
    runtime = {"Labels": {"org.opencontainers.image.revision": revision}, "Env": ["APP=fixture"],
               "Cmd": ["python", "-m", "app"], "WorkingDir": "/app"}
    configuration = {"os": "linux", "architecture": "amd64", "config": runtime,
                     "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "d" * 64]}}
    config_raw = json.dumps(configuration).encode()
    config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()
    manifest = {"config": {"digest": config_digest, "size": len(config_raw)}, "layers": [{
        "digest": "sha256:" + "c" * 64, "size": 2097152,
        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}]}
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
        if "/blobs/" in url: return config_raw
        return raw_manifest if index and url.endswith(image_digest) else raw

    def command(args, **kwargs):
        if args[:2] == ['docker','info']:
            return str(root).encode()
        commands.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": config_digest, "Size":8388608, "Config": runtime,
                "Os": "linux", "Architecture": "amd64", "RootFS": {"Type": "layers",
                "Layers": configuration["rootfs"]["diff_ids"]}}]).encode()
        return b""

    def pull(binary, reference, archive, route):
        pulls.append((reference, route))
        archive.write_bytes(b"image archive verified by crane")

    monkeypatch.setattr(puller, "read_bounded", read)
    monkeypatch.setattr(puller, "command", command)
    monkeypatch.setattr(puller, "pull", pull)
    monkeypatch.setattr(puller, "acquire_tool", lambda folder, routes, cache: folder / "crane")
    monkeypatch.setattr(puller, "validate_archive", lambda *args: {
        "verified_layer_count": 1, "verified_expanded_bytes": 16777216, "verified_compressed_layer_bytes": 2097152})
    monkeypatch.setattr(puller, "sample", lambda url, route, headers: {
        "route": "proxy" if route else "direct", "complete": True, "bytes": 1048576,
        "seconds": 1 if route else 2, "bytes_per_second": 1048576 if route else 524288})
    return SimpleNamespace(root=root, raw=raw, digest=digest, image_digest=image_digest, config=config_digest,
                           configuration=configuration, config_raw=config_raw, runtime=runtime,
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
    with pytest.raises(RuntimeError, match="loaded_image_config_mismatch|loaded_image_revision_mismatch"):
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


def test_containerd_manifest_id_and_store_size_difference_preserve_verified_identity(puller, monkeypatch, tmp_path):
    context = prepare_main(puller, monkeypatch, tmp_path)
    original_command = puller.command

    def command(args, **kwargs):
        value = original_command(args, **kwargs)
        if args[:3] == ["docker", "image", "inspect"]:
            item = json.loads(value)[0]
            item["Id"], item["Size"] = context.image_digest, 8388608 - 389
            return json.dumps([item]).encode()
        return value

    monkeypatch.setattr(puller, "command", command)
    puller.main()
    assert context.commands[-1] == ["docker", "tag", context.image_digest, context.image]
    receipt = json.loads((context.root / "last-image-pull.safe.json").read_text())
    assert receipt["loaded_config_digest"] == context.config
    assert receipt["loaded_image_id"] == context.image_digest
    assert receipt["store_size_reporting_difference"] == -389
    assert receipt["archive_proof"]["verified_expanded_bytes"] == 16777216


def test_config_blob_digest_failure_never_downloads_or_loads_image(puller, monkeypatch, tmp_path):
    context = prepare_main(puller, monkeypatch, tmp_path)
    original_read = puller.read_bounded

    def read(url, *args, **kwargs):
        value = original_read(url, *args, **kwargs)
        return value + b" " if "/blobs/" in url else value

    monkeypatch.setattr(puller, "read_bounded", read)
    with pytest.raises(RuntimeError, match="registry_verified_config_unavailable"):
        puller.main()
    assert context.pulls == [] and context.commands == []


def test_archive_proof_failure_never_loads_or_tags(puller, monkeypatch, tmp_path):
    context = prepare_main(puller, monkeypatch, tmp_path)
    monkeypatch.setattr(puller, "validate_archive", lambda *args: (_ for _ in ()).throw(
        RuntimeError("image_archive_rootfs_digest_mismatch")))
    with pytest.raises(RuntimeError, match="image_archive_rootfs_digest_mismatch"):
        puller.main()
    assert len(context.pulls) == 1 and context.commands == []


def test_loaded_runtime_option_change_never_tags_release(puller, monkeypatch, tmp_path):
    context = prepare_main(puller, monkeypatch, tmp_path)
    original_command = puller.command

    def command(args, **kwargs):
        value = original_command(args, **kwargs)
        if args[:3] == ["docker", "image", "inspect"]:
            item = json.loads(value)[0]
            item["Config"]["Env"] = ["APP=changed"]
            return json.dumps([item]).encode()
        return value

    monkeypatch.setattr(puller, "command", command)
    with pytest.raises(RuntimeError, match="loaded_image_config_mismatch"):
        puller.main()
    assert not any(args[1] == "tag" for args in context.commands)


def test_verified_expanded_disk_budget_is_checked_before_load(puller, monkeypatch, tmp_path):
    context = prepare_main(puller, monkeypatch, tmp_path)
    budgets = []

    def check(root, compressed, expanded):
        budgets.append(expanded)
        if len(budgets) == 2:
            raise RuntimeError("image_import_disk_budget_unavailable")

    monkeypatch.setattr(puller, "check_disk_budget", check)
    with pytest.raises(RuntimeError, match="image_import_disk_budget_unavailable"):
        puller.main()
    assert budgets == [8388608, 16777216] and context.commands == []


def test_docker_command_failure_reports_safe_stage_without_stderr_or_arguments(puller, monkeypatch):
    monkeypatch.setattr(puller.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout=b"", stderr=b"proxy-password-and-signed-url"))
    with pytest.raises(RuntimeError) as caught:
        puller.command(["docker", "image", "inspect", "private-reference"], stage="docker_inspect_loaded_tag")
    assert str(caught.value) == "verified_image_failed:docker_inspect_loaded_tag:exit=1"


def test_no_transfer_route_prints_safe_sample_diagnostics_before_failure(puller, monkeypatch, tmp_path, capsys):
    context = prepare_main(puller, monkeypatch, tmp_path)
    monkeypatch.setattr(puller, "sample", lambda url, route, headers: {
        "route": "proxy" if route else "direct", "complete": False,
        "failure_kind": "exception_URLError" if route else "short_read", "bytes": 0,
        "seconds": 1, "bytes_per_second": 0})
    with pytest.raises(RuntimeError, match="image_transfer_routes_unavailable"):
        puller.main()
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == "image_transfer_samples"
    assert {item["failure_kind"] for item in report["route_samples"]} == {"short_read", "exception_URLError"}
    assert "private" not in json.dumps(report) and "127.0.0.1" not in json.dumps(report)
    assert context.pulls == [] and context.commands == []


def test_registry_http_status_is_preserved_without_url_or_response_details(puller, monkeypatch):
    def opened(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 500, "sensitive-upstream-detail", {}, None)
    monkeypatch.setattr(puller, "transport", lambda proxy: SimpleNamespace(open=opened))
    with pytest.raises(puller.ArtifactHTTPError) as caught:
        puller.response("https://ghcr.io/private?signature=secret", None)
    assert caught.value.status == 500
    assert str(caught.value) == "registry_http_rejected:status=500"
