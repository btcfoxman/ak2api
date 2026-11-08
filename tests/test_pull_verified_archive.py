"""Independent offline coverage of the registry/archive/Docker trust chain."""

from copy import deepcopy
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest

REVISION = "a" * 40
TAG = "ghcr.io/btcfoxman/ak2api:i-was-a-digest"
GZIP_MEDIA = "application/vnd.oci.image.layer.v1.tar+gzip"
RAW_MEDIA = "application/vnd.oci.image.layer.v1.tar"


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def archive_puller(monkeypatch):
    path = Path(__file__).parents[1] / "scripts/pull_verified_image.py"
    spec = importlib.util.spec_from_file_location("ak_archive_security_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden(*_args, **_kwargs):
        pytest.fail(
            "archive validation must not invoke a process, network, or extraction"
        )

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    monkeypatch.setattr(module.subprocess, "Popen", forbidden)
    monkeypatch.setattr(module.urllib.request.OpenerDirector, "open", forbidden)
    monkeypatch.setattr(tarfile.TarFile, "extract", forbidden)
    monkeypatch.setattr(tarfile.TarFile, "extractall", forbidden)
    return module


def rootfs_layer(content=b"reviewed application", name="app/hello.txt"):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as bundle:
        entry = tarfile.TarInfo(name)
        entry.size = len(content)
        bundle.addfile(entry, io.BytesIO(content))
    return stream.getvalue()


def image(*, compressed=True):
    plain = rootfs_layer()
    layer = gzip.compress(plain, mtime=0) if compressed else plain
    config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {
            "Env": ["PATH=/usr/local/bin:/usr/bin"],
            "Cmd": ["python", "main.py"],
            "WorkingDir": "/app",
            "Labels": {"org.opencontainers.image.revision": REVISION},
        },
        "rootfs": {"type": "layers", "diff_ids": [digest(plain)]},
    }
    raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    manifest = {
        "config": {"digest": digest(raw), "size": len(raw)},
        "layers": [
            {
                "digest": digest(layer),
                "size": len(layer),
                "mediaType": GZIP_MEDIA if compressed else RAW_MEDIA,
            }
        ],
    }
    return manifest, raw, config, layer, plain


def save_archive(
    tmp_path, raw, layers, *, tags=None, records=None, extra=(), layer_names=None
):
    names = layer_names or [
        "layers/" + str(index) + ".tar" for index in range(len(layers))
    ]
    record = {
        "Config": "config.json",
        "RepoTags": tags if tags is not None else [TAG],
        "Layers": names,
    }
    members = [
        (
            "manifest.json",
            json.dumps(records if records is not None else [record]).encode(),
        ),
        ("config.json", raw),
        *zip(names, layers),
        *extra,
    ]
    target = tmp_path / "image.tar"
    with tarfile.open(target, "w") as bundle:
        for name, value in members:
            entry = tarfile.TarInfo(name)
            if value is None:
                entry.type, entry.linkname = tarfile.SYMTYPE, "../../outside"
                bundle.addfile(entry)
            else:
                entry.size = len(value)
                bundle.addfile(entry, io.BytesIO(value))
    return target


@pytest.mark.parametrize("compressed", [False, True])
def test_valid_raw_and_gzip_archives_verify_both_hashes_without_extracting(
    archive_puller, tmp_path, compressed
):
    manifest, raw, config, layer, plain = image(compressed=compressed)
    assert archive_puller.validate_config(raw, manifest["config"], REVISION) == config
    archive = save_archive(tmp_path, raw, [layer])
    proof = archive_puller.validate_archive(archive, manifest, raw, config, TAG)
    assert proof == {
        "verified_layer_count": 1,
        "verified_expanded_bytes": len(plain),
        "verified_compressed_layer_bytes": len(layer),
    }
    assert list(tmp_path.iterdir()) == [archive]


def test_gzip_header_tamper_fails_even_when_expanded_bytes_are_identical(
    archive_puller, tmp_path
):
    manifest, raw, config, layer, plain = image()
    modified = bytearray(layer)
    modified[4] ^= 1  # A valid gzip timestamp change preserves the exact rootfs.
    assert gzip.decompress(modified) == plain
    archive = save_archive(tmp_path, raw, [modified])
    with pytest.raises(RuntimeError, match="compressed_digest_mismatch"):
        archive_puller.validate_archive(archive, manifest, raw, config, TAG)


def test_new_compressed_hash_cannot_hide_wrong_rootfs_diff_id(archive_puller, tmp_path):
    manifest, raw, config, _, _ = image()
    modified = gzip.compress(rootfs_layer(b"modified application"), mtime=0)
    manifest["layers"][0].update(digest=digest(modified), size=len(modified))
    archive = save_archive(tmp_path, raw, [modified])
    with pytest.raises(RuntimeError, match="rootfs_digest_mismatch"):
        archive_puller.validate_archive(archive, manifest, raw, config, TAG)


@pytest.mark.parametrize("change", ["bytes", "size"])
def test_registry_config_digest_and_size_are_both_required(archive_puller, change):
    manifest, raw, _, _, _ = image()
    if change == "bytes":
        raw += b" "
        manifest["config"]["size"] = len(raw)
    else:
        manifest["config"]["size"] += 1
    with pytest.raises(RuntimeError, match="config_digest_mismatch"):
        archive_puller.validate_config(raw, manifest["config"], REVISION)


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("os", "windows", "platform_mismatch"),
        ("architecture", "arm64", "platform_mismatch"),
        (
            "config",
            {"Labels": {"org.opencontainers.image.revision": "b" * 40}},
            "revision_mismatch",
        ),
        ("rootfs", {"type": "other", "diff_ids": []}, "rootfs_invalid"),
        (
            "rootfs",
            {"type": "layers", "diff_ids": ["invalid"]},
            "rootfs_digest_invalid",
        ),
    ],
)
def test_trusted_config_still_requires_expected_platform_revision_and_rootfs(
    archive_puller, field, value, error
):
    _, _, config, _, _ = image()
    config[field] = value
    raw = json.dumps(config).encode()
    with pytest.raises(RuntimeError, match=error):
        archive_puller.validate_config(
            raw, {"digest": digest(raw), "size": len(raw)}, REVISION
        )


def test_archive_config_must_match_verified_registry_bytes_exactly(
    archive_puller, tmp_path
):
    manifest, raw, config, layer, _ = image()
    archive = save_archive(tmp_path, json.dumps(config, indent=2).encode(), [layer])
    with pytest.raises(RuntimeError, match="archive_config_mismatch"):
        archive_puller.validate_archive(archive, manifest, raw, config, TAG)


@pytest.mark.parametrize(
    "tags", [[TAG, "unreviewed:latest"], ["unreviewed:latest"], []]
)
def test_archive_cannot_import_extra_or_unbound_tags(archive_puller, tmp_path, tags):
    manifest, raw, config, layer, _ = image()
    archive = save_archive(tmp_path, raw, [layer], tags=tags)
    with pytest.raises(RuntimeError, match="tags_mismatch"):
        archive_puller.validate_archive(archive, manifest, raw, config, TAG)


def test_archive_rejects_multiple_image_records(archive_puller, tmp_path):
    manifest, raw, config, layer, _ = image()
    record = {"Config": "config.json", "RepoTags": [TAG], "Layers": ["layers/0.tar"]}
    archive = save_archive(tmp_path, raw, [layer], records=[record, record])
    with pytest.raises(RuntimeError, match="manifest_invalid"):
        archive_puller.validate_archive(archive, manifest, raw, config, TAG)


@pytest.mark.parametrize(
    "changed", [{"Config": None}, {"Layers": [None]}, {"Layers": ["missing.tar"]}]
)
def test_archive_member_references_must_be_existing_string_names(
    archive_puller, tmp_path, changed
):
    manifest, raw, config, layer, _ = image()
    record = {
        "Config": "config.json",
        "RepoTags": [TAG],
        "Layers": ["layers/0.tar"],
        **changed,
    }
    archive = save_archive(tmp_path, raw, [layer], records=[record])
    with pytest.raises(RuntimeError, match="member_reference_invalid"):
        archive_puller.validate_archive(archive, manifest, raw, config, TAG)


@pytest.mark.parametrize("name", ["../outside", "/outside", "layers\\outside"])
def test_even_referenced_members_cannot_traverse_paths(archive_puller, tmp_path, name):
    manifest, raw, config, layer, _ = image()
    archive = save_archive(tmp_path, raw, [layer], layer_names=[name])
    with pytest.raises(RuntimeError, match="member_invalid"):
        archive_puller.validate_archive(archive, manifest, raw, config, TAG)


def test_referenced_layer_cannot_be_a_symlink(archive_puller, tmp_path):
    manifest, raw, config, _, _ = image()
    archive = save_archive(tmp_path, raw, [None])
    with pytest.raises(RuntimeError, match="member_invalid"):
        archive_puller.validate_archive(archive, manifest, raw, config, TAG)


@pytest.mark.parametrize(
    "name,value",
    [
        ("../../outside", b"bad"),
        ("/outside", b"bad"),
        ("layers\\outside", b"bad"),
        ("config.json", b"duplicate"),
        ("link", None),
        ("unreferenced", b"extra"),
    ],
)
def test_archive_rejects_unsafe_duplicate_link_and_extra_members(
    archive_puller, tmp_path, name, value
):
    manifest, raw, config, layer, _ = image()
    archive = save_archive(tmp_path, raw, [layer], extra=[(name, value)])
    with pytest.raises(RuntimeError, match="member_invalid"):
        archive_puller.validate_archive(archive, manifest, raw, config, TAG)
    assert not (tmp_path.parent / "outside").exists()


def test_archive_expanded_size_is_bounded_before_import(
    archive_puller, tmp_path, monkeypatch
):
    manifest, raw, config, layer, plain = image()
    archive = save_archive(tmp_path, raw, [layer])
    monkeypatch.setattr(archive_puller, "MAX_EXPANDED_BYTES", len(plain) - 1)
    with pytest.raises(RuntimeError, match="expansion_budget_exhausted"):
        archive_puller.validate_archive(archive, manifest, raw, config, TAG)


def test_archive_validation_has_a_wall_clock_deadline(
    archive_puller, tmp_path, monkeypatch
):
    manifest, raw, config, layer, _ = image()
    archive = save_archive(tmp_path, raw, [layer])
    ticks = iter([0, 181])
    monkeypatch.setattr(archive_puller.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="validation_timeout"):
        archive_puller.validate_archive(archive, manifest, raw, config, TAG)


def loaded_image(config, identity):
    return {
        "Id": identity,
        "Config": deepcopy(config["config"]),
        "Os": "linux",
        "Architecture": "amd64",
        "RootFS": {"Type": "layers", "Layers": list(config["rootfs"]["diff_ids"])},
        "Size": 12345,
    }


@pytest.mark.parametrize("store", ["legacy-config-id", "containerd-manifest-id"])
def test_docker_store_id_difference_is_allowed_only_with_matching_semantics(
    archive_puller, store
):
    manifest, _, config, _, _ = image()
    actual_id = (
        manifest["config"]["digest"]
        if store == "legacy-config-id"
        else "sha256:" + "b" * 64
    )
    assert (
        archive_puller.verify_loaded_image(
            loaded_image(config, actual_id), config, REVISION
        )
        == actual_id
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("Env", ["PATH=/malicious"]),
        ("Cmd", ["unreviewed"]),
        ("Entrypoint", ["unreviewed"]),
        ("User", "unexpected"),
        ("WorkingDir", "/unexpected"),
        ("UnrecognizedConfig", None),
        ("Healthcheck", {"Test": ["CMD", "unreviewed"]}),
    ],
)
@pytest.mark.parametrize("store", ["legacy-config-id", "containerd-manifest-id"])
def test_neither_docker_store_can_hide_config_changes(
    archive_puller, field, value, store
):
    manifest, _, config, _, _ = image()
    identity = (
        manifest["config"]["digest"]
        if store == "legacy-config-id"
        else "sha256:" + "b" * 64
    )
    info = loaded_image(config, identity)
    info["Config"][field] = value
    with pytest.raises(RuntimeError, match="config_mismatch"):
        archive_puller.verify_loaded_image(info, config, REVISION)


def test_unknown_healthcheck_fields_are_not_silently_treated_as_defaults(
    archive_puller,
):
    _, _, config, _, _ = image()
    info = loaded_image(config, "sha256:" + "c" * 64)
    info["Config"]["Healthcheck"] = {"UnknownBehavior": 0}
    with pytest.raises(RuntimeError, match="config_mismatch"):
        archive_puller.verify_loaded_image(info, config, REVISION)


def test_only_actual_docker_default_values_are_ignored(archive_puller):
    _, _, config, _, _ = image()
    info = loaded_image(config, "sha256:" + "c" * 64)
    info["Config"].update(
        Hostname="",
        AttachStdin=False,
        Entrypoint=None,
        Volumes={},
        ExposedPorts=None,
        Healthcheck={"Test": [], "Interval": 0},
    )
    assert archive_puller.verify_loaded_image(info, config, REVISION) == info["Id"]


@pytest.mark.parametrize(
    "change,error",
    [
        ({"Id": "invalid"}, "id_invalid"),
        ({"Os": "windows"}, "platform_mismatch"),
        ({"Architecture": "arm64"}, "platform_mismatch"),
        ({"Size": 0}, "size_invalid"),
        ({"Size": True}, "size_invalid"),
        ({"RootFS": {"Type": "other", "Layers": []}}, "rootfs_mismatch"),
        (
            {"RootFS": {"Type": "layers", "Layers": ["sha256:" + "f" * 64]}},
            "rootfs_mismatch",
        ),
    ],
)
def test_loaded_identity_platform_size_and_rootfs_remain_mandatory(
    archive_puller, change, error
):
    _, _, config, _, _ = image()
    info = loaded_image(config, "sha256:" + "c" * 64)
    info.update(change)
    with pytest.raises(RuntimeError, match=error):
        archive_puller.verify_loaded_image(info, config, REVISION)
