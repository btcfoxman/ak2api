"""Offline checks for IJM's bounded, cache-assisted immutable image pull."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from test_pull_verified_image import puller, prepare_main


@pytest.fixture(autouse=True)
def forbid_real_socket(puller, monkeypatch):
    monkeypatch.setattr(puller.socket, 'create_connection', Mock(
        side_effect=AssertionError('test attempted a real socket connection')))


@pytest.fixture
def native_case(puller, monkeypatch, tmp_path):
    revision = 'a' * 40
    diffs = ['sha256:' + value * 64 for value in ('1', '2', '3')]
    runtime = {'Labels': {'org.opencontainers.image.revision': revision}, 'Env': ['APP=fixture']}
    config = {'os': 'linux', 'architecture': 'amd64', 'config': runtime,
              'rootfs': {'type': 'layers', 'diff_ids': diffs}}
    manifest = {'layers': [{'size': size * 1024 ** 2} for size in (80, 10, 10)]}
    reference = 'ghcr.io/btcfoxman/ak2api@sha256:' + 'c' * 64
    actual_id = 'sha256:' + 'd' * 64
    loaded = {'Id': actual_id, 'Config': runtime, 'Os': 'linux', 'Architecture': 'amd64',
              'RootFS': {'Type': 'layers', 'Layers': diffs}, 'Size': 200 * 1024 ** 2}
    case = SimpleNamespace(reference=reference, root=tmp_path, revision=revision,
        manifest=manifest, config=config, info=loaded, cached=diffs[:2], commands=[], disk=[],
        daemon='', metadata_route=None, sampled_proxy='http://127.0.0.1:10809', samples=[
            {'route': 'direct', 'complete': True, 'bytes_per_second': 524288},
            {'route': 'proxy', 'complete': True, 'bytes_per_second': 1048576}])

    def command(args, **kwargs):
        case.commands.append((args, kwargs))
        if args[:2] == ['docker', 'inspect']:
            return b'[{"Image":"old-image-id"}]'
        if args[:3] == ['docker', 'image', 'inspect']:
            if args[-1] == 'old-image-id':
                return json.dumps([{'RootFS': {'Layers': case.cached}}]).encode()
            return json.dumps([case.info]).encode()
        return b''

    monkeypatch.setattr(puller, 'daemon_proxy', lambda: case.daemon)
    monkeypatch.setattr(puller, 'command', command)
    monkeypatch.setattr(puller, 'check_disk_budget', lambda *args: case.disk.append(args))
    case.run = lambda: puller.native_cached_pull(case.reference, case.root, case.manifest,
        case.config, case.revision, 100 * 1024 ** 2, case.samples, case.metadata_route,
        sampled_proxy=case.sampled_proxy)
    return case


@pytest.mark.parametrize('daemon', ['', 'http://127.0.0.1:10809'])
def test_large_cache_uses_actual_daemon_route_and_immutable_digest(puller, native_case, daemon):
    native_case.daemon = daemon
    result = native_case.run()
    assert result['success'] is True
    assert result['route'] == ('proxy' if daemon else 'direct')
    assert result['image_id'] == native_case.info['Id']
    assert result['estimated_missing_compressed_bytes'] == 10 * 1024 ** 2
    pulls = [(args, kwargs) for args, kwargs in native_case.commands if args[1] == 'pull']
    assert pulls == [(['docker', 'pull', '--platform', 'linux/amd64', native_case.reference],
                      {'timeout': 75, 'stage': 'docker_native_cached_pull'})]
    assert native_case.disk and not any(args[1] == 'tag' for args, _ in native_case.commands)


@pytest.mark.parametrize('cached_count', [0, 1])
def test_small_cache_does_not_start_native_pull(native_case, cached_count):
    native_case.cached = native_case.config['rootfs']['diff_ids'][:cached_count]
    # Zero or only the first layer leaves more than the allowed 20 percent.
    if cached_count:
        native_case.manifest['layers'][0]['size'] = 70 * 1024 ** 2
        native_case.manifest['layers'][1]['size'] = 20 * 1024 ** 2
    assert native_case.run() is None
    assert not native_case.disk
    assert not any(args[1] == 'pull' for args, _ in native_case.commands)


def test_cache_advantage_must_beat_fast_archive_route(native_case):
    native_case.samples[1]['bytes_per_second'] = 1024 ** 3
    assert native_case.run() is None
    assert not any(args[1] == 'pull' for args, _ in native_case.commands)


@pytest.mark.parametrize('proxy_sample', [None, {'route': 'proxy', 'complete': False,
                                               'bytes_per_second': 10 ** 9}])
def test_unverified_daemon_proxy_route_is_not_relabelled_as_direct(native_case, proxy_sample):
    native_case.daemon = 'https://daemon-proxy.example.test:8443'
    native_case.samples = [native_case.samples[0]] + ([proxy_sample] if proxy_sample else [])
    assert native_case.run() is None
    assert native_case.commands == []


def test_a_different_daemon_proxy_cannot_borrow_another_proxy_complete_sample(native_case):
    native_case.metadata_route = 'http://sampled-proxy.example.test:10809'
    native_case.daemon = 'http://changed-daemon-proxy.example.test:10809'
    assert native_case.run() is None
    assert native_case.commands == []


def prepare_native_main(puller, monkeypatch, tmp_path, *, daemon=''):
    native = puller.native_cached_pull
    context = prepare_main(puller, monkeypatch, tmp_path, index=True)
    monkeypatch.setattr(puller, 'native_cached_pull', native)
    monkeypatch.setattr(puller, 'daemon_proxy', lambda: daemon)
    original = puller.command
    context.native_commands = []
    context.mutation = lambda info: info
    context.native_failure = None

    def command(args, **kwargs):
        context.native_commands.append((args, kwargs))
        if args[:2] == ['docker', 'inspect']:
            return b'[{"Image":"old-image-id"}]'
        if args[:3] == ['docker', 'image', 'inspect'] and args[-1] == 'old-image-id':
            return json.dumps([{'RootFS': {'Layers': context.configuration['rootfs']['diff_ids']}}]).encode()
        if args[1] == 'pull':
            if context.native_failure:
                raise RuntimeError(context.native_failure)
            return b''
        value = original(args, **kwargs)
        if args[:3] == ['docker', 'image', 'inspect'] and '@sha256:' in args[-1]:
            return json.dumps([context.mutation(json.loads(value)[0])]).encode()
        return value

    monkeypatch.setattr(puller, 'command', command)
    return context


@pytest.mark.parametrize('daemon', ['', 'http://127.0.0.1:10809'])
def test_native_main_verifies_config_rootfs_revision_before_tag_without_archive(
        puller, monkeypatch, tmp_path, capsys, daemon):
    context = prepare_native_main(puller, monkeypatch, tmp_path, daemon=daemon)
    archive = Mock(side_effect=AssertionError('native success must not download a full archive'))
    monkeypatch.setattr(puller, 'acquire_tool', archive)
    puller.main()
    operations = [args for args, _ in context.native_commands]
    reference = 'ghcr.io/btcfoxman/ak2api@' + context.image_digest
    assert ['docker', 'pull', '--platform', 'linux/amd64', reference] in operations
    inspect_at = operations.index(['docker', 'image', 'inspect', reference])
    assert operations[-1] == ['docker', 'tag', context.config, context.image]
    assert inspect_at < len(operations) - 1
    receipt = json.loads((context.root / 'last-image-pull.safe.json').read_text())
    assert receipt['crane_used'] is False and context.pulls == []
    assert receipt['selected_route'] == ('proxy' if daemon else 'direct')
    assert receipt['manifest_digest'] == context.digest
    assert receipt['platform_manifest_digest'] == context.image_digest
    assert receipt['loaded_config_digest'] == context.config
    assert receipt['native_cache_attempt']['estimated_missing_compressed_bytes'] == 0
    assert receipt['docker_daemon_proxy_changed'] is False and receipt['builds_or_dependency_installs'] == 0
    assert 'private' not in capsys.readouterr().out


def test_native_snapshotter_manifest_id_is_proven_by_digest_inspect_before_tag(
        puller, monkeypatch, tmp_path):
    context = prepare_native_main(puller, monkeypatch, tmp_path)
    reference = 'ghcr.io/btcfoxman/ak2api@' + context.image_digest

    def snapshotter_info(info):
        info['Id'] = context.image_digest
        info['RepoDigests'] = [reference]
        return info

    context.mutation = snapshotter_info
    puller.main()
    operations = [args for args, _ in context.native_commands]
    assert ['docker', 'image', 'inspect', reference] in operations
    assert operations[-1] == ['docker', 'tag', context.image_digest, context.image]
    receipt = json.loads((context.root / 'last-image-pull.safe.json').read_text())
    assert receipt['loaded_image_id'] == context.image_digest
    assert receipt['loaded_config_digest'] == context.config
    assert receipt['loaded_image_id'] != receipt['loaded_config_digest']
    assert receipt['crane_used'] is False


@pytest.mark.parametrize('failure', ['verified_image_timeout:docker_native_cached_pull',
                                    'verified_image_failed:docker_native_cached_pull:exit=1'])
@pytest.mark.parametrize('daemon', ['', 'http://127.0.0.1:10809'])
def test_bounded_native_transport_failure_uses_other_archive_route(
        puller, monkeypatch, tmp_path, daemon, failure):
    context = prepare_native_main(puller, monkeypatch, tmp_path, daemon=daemon)
    context.native_failure = failure
    puller.main()
    attempts = [(args, kwargs) for args, kwargs in context.native_commands if args[1] == 'pull']
    assert len(attempts) == 1 and attempts[0][1]['timeout'] == 75
    assert context.pulls == [('ghcr.io/btcfoxman/ak2api@' + context.image_digest,
                              None if daemon else 'http://127.0.0.1:10809')]
    receipt = json.loads((context.root / 'last-image-pull.safe.json').read_text())
    assert receipt['crane_used'] is True and receipt['native_cache_attempt']['success'] is False
    assert receipt['native_cache_attempt']['failure_kind'] == failure
    assert context.commands[-1] == ['docker', 'tag', context.config, context.image]


@pytest.mark.parametrize('corrupt', ['config', 'rootfs', 'revision', 'platform'])
def test_native_verification_failure_never_tags_or_falls_back_to_archive(
        puller, monkeypatch, tmp_path, corrupt):
    context = prepare_native_main(puller, monkeypatch, tmp_path)

    def mutation(info):
        info = deepcopy(info)
        if corrupt == 'config':
            info['Config']['Env'] = ['APP=changed']
        elif corrupt == 'rootfs':
            info['RootFS']['Layers'] = ['sha256:' + 'e' * 64]
        elif corrupt == 'revision':
            info['Config']['Labels']['org.opencontainers.image.revision'] = 'f' * 40
        else:
            info['Architecture'] = 'arm64'
        return info

    context.mutation = mutation
    with pytest.raises(RuntimeError, match='loaded_image_(config|rootfs|revision|platform)_mismatch'):
        puller.main()
    assert not any(args[1] == 'tag' for args, _ in context.native_commands)
    assert not context.pulls and not (context.root / 'last-image-pull.safe.json').exists()


@pytest.mark.parametrize('proxy', ['socks5://127.0.0.1:10809', 'socks5h://proxy.test:10809',
    'ftp://proxy.test:21', 'http://proxy.test', 'http://:10809', 'not-a-proxy'])
def test_unknown_or_non_http_daemon_proxy_is_rejected_without_local_fallback(puller, monkeypatch, proxy):
    monkeypatch.setattr(puller, 'command', lambda *args, **kwargs: json.dumps(proxy).encode())
    with pytest.raises(RuntimeError, match='deployment_proxy_invalid'):
        puller.configured_proxy()
    puller.socket.create_connection.assert_not_called()


def test_daemon_proxy_prefers_https_and_does_not_probe_local_10809(puller, monkeypatch):
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return b'"https://daemon-proxy.example.test:8443"'

    monkeypatch.setattr(puller, 'command', command)
    assert puller.configured_proxy() == 'https://daemon-proxy.example.test:8443'
    assert calls == [['docker', 'info', '--format', '{{json .HTTPSProxy}}']]
    puller.socket.create_connection.assert_not_called()


@pytest.mark.parametrize('listening', [True, False])
def test_server_local_10809_fallback_requires_an_actual_socket_check(puller, monkeypatch, listening):
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return b'""'

    connection = MagicMock()
    connect = Mock(return_value=connection) if listening else Mock(side_effect=ConnectionRefusedError())
    monkeypatch.setattr(puller, 'command', command)
    monkeypatch.setattr(puller.socket, 'create_connection', connect)
    assert puller.configured_proxy() == ('http://127.0.0.1:10809' if listening else '')
    connect.assert_called_once_with(('127.0.0.1', 10809), timeout=.5)
    assert [args[-1] for args in calls] == ['{{json .HTTPSProxy}}', '{{json .HTTPProxy}}']
    if listening:
        connection.__enter__.assert_called_once()
        connection.__exit__.assert_called_once()
