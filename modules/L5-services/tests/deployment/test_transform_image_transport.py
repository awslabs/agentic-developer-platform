"""Exercise credential boundaries and authenticated Docker archive contents."""

import gzip
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "modules/L5-services/scripts"))
from transform_image_transport import ImageTransport, hashed, verify_archive  # noqa: E402


def image_archive(path, *, containerd=False, compressed=False, fault=None):
    layer = b"synthetic layer bytes"
    config = json.dumps({"os": "linux", "architecture": "amd64", "rootfs": {
        "type": "layers", "diff_ids": [hashed(layer)]}}).encode()
    if compressed:
        layer = gzip.compress(layer)
    config_digest, layer_digest = hashed(config), hashed(layer)
    config_name = "blobs/sha256/" + config_digest[7:]
    layer_name = "blobs/sha256/" + layer_digest[7:]
    raw = json.dumps({"schemaVersion": 2,
                      "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                      "config": {"digest": config_digest, "size": len(config)},
                      "layers": [{"digest": layer_digest, "size": len(layer),
                                  "mediaType": "application/vnd.docker.image.rootfs.diff.tar" + (".gzip" if compressed else "")}]}).encode()
    manifest_digest = hashed(raw)
    descriptor = {"digest": manifest_digest, "size": len(raw),
                  "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                  "annotations": {"config.digest": config_digest}}
    entries = {config_name: config, layer_name: layer,
               "manifest.json": json.dumps([{"Config": config_name, "Layers": [layer_name]}]).encode()}
    if containerd:
        entries["blobs/sha256/" + manifest_digest[7:]] = raw
    if fault == "layer":
        entries[layer_name] = b"substituted layer"
    elif fault == "manifest":
        entries["blobs/sha256/" + manifest_digest[7:]] = raw + b" "
    elif fault == "annotation":
        descriptor["annotations"]["config.digest"] = "sha256:" + "0" * 64
    elif fault == "index":
        descriptor["mediaType"] = "application/vnd.oci.image.index.v1+json"
    elif fault == "path":
        entries["manifest.json"] = json.dumps([{"Config": "../config", "Layers": [layer_name]}]).encode()
    with tarfile.open(path, "w") as archive:
        for name, value in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
            if fault == "duplicate":
                archive.addfile(member, io.BytesIO(value))
    return {"Id": manifest_digest if containerd else config_digest,
            **({"Descriptor": descriptor} if containerd else {})}, config_digest


@pytest.mark.parametrize("containerd,compressed", [(False, False), (True, False), (True, True)])
def test_authenticates_classic_and_containerd_archives(tmp_path, containerd, compressed):
    path = tmp_path / "image.tar"
    observed, config = image_archive(path, containerd=containerd, compressed=compressed)
    assert verify_archive(path, image_id=observed["Id"], descriptor=observed.get("Descriptor")) == config


@pytest.mark.parametrize("fault", ["layer", "manifest", "annotation", "index", "path", "duplicate"])
def test_archive_tampering_is_rejected(tmp_path, fault):
    path = tmp_path / "image.tar"
    observed, _ = image_archive(path, containerd=True, fault=fault)
    with pytest.raises(ValueError):
        verify_archive(path, image_id=observed["Id"], descriptor=observed["Descriptor"])


@pytest.mark.parametrize("host", ["ssh://builder", "tcp://127.0.0.1:2375", "tcp://builder:2376"])
def test_remote_daemon_requires_credential_free_transport(host):
    with pytest.raises(ValueError, match="authorization must stay local"):
        ImageTransport.validate({"DOCKER_HOST": host})


def test_archive_transport_never_sends_secrets_or_registry_authorization_to_docker(tmp_path):
    observed, config = image_archive(tmp_path / "fixture.tar", containerd=True)
    calls = []

    def run(argv, *, env=None, input=None, stdout=None):
        calls.append((argv, env))
        if argv[0] == "aws":
            return b"synthetic-registry-token"
        assert set(env) <= {"HOME", "PATH", "DOCKER_HOST", "SSH_AUTH_SOCK"}
        assert not any("secret" in value for value in env.values())
        if argv[0] == "docker":
            assert argv[3:6] == ["image", "save", observed["Id"]]
            assert input is None
            assert json.loads((Path(argv[2]) / "config.json").read_text()) == {"auths": {}}
            stdout.write((tmp_path / "fixture.tar").read_bytes())
        elif argv[1] == "login":
            assert "DOCKER_HOST" not in env
            assert input == b"synthetic-registry-token"
        return b""

    environment = {"L5_TRANSFORM_IMAGE_TRANSPORT": "archive", "DOCKER_HOST": "ssh://builder",
                   "HOME": "/coordinator", "PATH": "/usr/bin", "SSH_AUTH_SOCK": "/agent",
                   "AWS_SECRET_ACCESS_KEY": "secret", "GITHUB_TOKEN": "secret",
                   "DOCKER_AUTH_CONFIG": "secret", "DOCKER_CONTEXT": "secret", "CUSTOM_SECRET": "secret"}
    transport = ImageTransport(environment, tmp_path, run, "registry.example", "us-east-1")
    archive, verified = transport.export(observed, "provider")
    assert verified == config
    assert all(argv[0] != "aws" for argv, _ in calls)
    transport.push_archive(archive, "registry.example/target:immutable")
    assert not any(argv[0] == "docker" and ("login" in argv or "push" in argv) for argv, _ in calls)
    assert archive.stat().st_mode & 0o777 == 0o600


def test_tampered_export_never_obtains_registry_authorization(tmp_path):
    observed, _ = image_archive(tmp_path / "fixture.tar", containerd=True, fault="layer")

    def run(argv, *, env=None, stdout=None):
        assert argv[0] == "docker"
        stdout.write((tmp_path / "fixture.tar").read_bytes())

    transport = ImageTransport({"L5_TRANSFORM_IMAGE_TRANSPORT": "archive"}, tmp_path, run, "registry", "us-east-1")
    with pytest.raises(ValueError, match="layer differs"):
        transport.export(observed, "provider")


def test_import_rejects_registry_manifest_substitution_before_daemon_call(tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "aws":
            return b"synthetic-token"
        if argv[1] == "inspect":
            return b'{"schemaVersion":2}'
        return b""

    transport = ImageTransport({"L5_TRANSFORM_IMAGE_TRANSPORT": "archive"}, tmp_path, run, "registry", "us-east-1")
    with pytest.raises(ValueError, match="one linux/amd64 image"):
        transport.import_pinned("registry/base@sha256:" + "a" * 64, "base")
    assert not any(argv[0] == "docker" for argv in calls)


@pytest.mark.parametrize("substitute_loaded_image", [False, True])
def test_import_verifies_registry_bytes_again_after_loading_remote_builder(tmp_path, substitute_loaded_image):
    observed, config = image_archive(tmp_path / "fixture.tar", containerd=True)
    with tarfile.open(tmp_path / "fixture.tar") as archive:
        raw = archive.extractfile("blobs/sha256/" + observed["Id"][7:]).read()
    calls = []

    def run(argv, *, env=None, input=None, stdout=None):
        calls.append(argv)
        if argv[0] == "aws":
            return b"synthetic-token"
        if argv[:2] == ["skopeo", "inspect"]:
            return raw
        if argv[:2] == ["skopeo", "copy"]:
            path = argv[-1].split(":", 2)[1]
            Path(path).write_bytes((tmp_path / "fixture.tar").read_bytes())
        if argv[0] == "docker":
            assert input is None and "AWS_SECRET_ACCESS_KEY" not in env
            if argv[3:5] == ["image", "inspect"]:
                return json.dumps([observed]).encode()
            if argv[3:5] == ["image", "save"]:
                if substitute_loaded_image:
                    altered = tmp_path / "substitute.tar"
                    image_archive(altered, containerd=True, fault="layer")
                    stdout.write(altered.read_bytes())
                else:
                    stdout.write((tmp_path / "fixture.tar").read_bytes())
        return b""

    transport = ImageTransport({"L5_TRANSFORM_IMAGE_TRANSPORT": "archive", "DOCKER_HOST": "ssh://builder",
                                "AWS_SECRET_ACCESS_KEY": "synthetic-secret"}, tmp_path, run, "registry", "us-east-1")
    if substitute_loaded_image:
        with pytest.raises(ValueError, match="layer differs"):
            transport.import_pinned("registry/base@" + observed["Id"], "base", config)
    else:
        assert transport.import_pinned("registry/base@" + observed["Id"], "base", config) == \
            "adp-l5-transform-input:" + config[7:]
    assert not any(argv[0] == "docker" and any(x in argv for x in ("login", "pull", "push")) for argv in calls)
