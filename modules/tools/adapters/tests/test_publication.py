"""CodeBuild qualification must not gain registry reads or write capability."""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("adapter_publisher", Path(__file__).parents[1] / "infra/publish_image.py")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
SOURCE = "a" * 40
BUILD_UUID = "00000000-0000-0000-0000-000000000001"
ARCHIVE = b"qualified-image-archive"
ARCHIVE_KEY = f"codebuild/artifacts/adp-tools-dev-adapters-build/{SOURCE}-{BUILD_UUID}.tar"
CONFIG_DIGEST = "sha256:" + "c" * 64
ENV = {
    "ADP_SOURCE_SHA": SOURCE,
    "ACCOUNT_ID": "123456789012",
    "AWS_REGION": "us-east-1",
    "ADP_TOOLS_ENVIRONMENT": "dev",
    "ADP_ADAPTER_QUALIFY_ONLY": "true",
    "CODEBUILD_BUILD_ID": "adp-tools-dev-adapters-build:" + BUILD_UUID,
    "ADP_ADAPTER_ARTIFACT_BUCKET": "fictional-private-state",
}


@pytest.fixture
def backend(monkeypatch):
    state = SimpleNamespace(
        calls=[], configs=[], account=ENV["ACCOUNT_ID"], failure=None, wrong_source=False, wrong_id=False
    )

    def run(args, *, env, **kwargs):
        state.calls.append(args)
        assert args[:2] not in (["aws", "ecr"], ["docker", "push"], ["docker", "login"], ["docker", "pull"])
        if "DOCKER_CONFIG" in env:
            path = Path(env["DOCKER_CONFIG"])
            assert path.is_dir()
            state.configs.append(path)
        if args[:3] == ["aws", "sts", "get-caller-identity"]:
            return SimpleNamespace(stdout=state.account, stderr="", returncode=0)
        if args[:3] == ["aws", "s3api", "put-object"]:
            assert args[args.index("--bucket") + 1] == ENV["ADP_ADAPTER_ARTIFACT_BUCKET"]
            assert args[args.index("--key") + 1] == ARCHIVE_KEY
            assert args[args.index("--if-none-match") + 1] == "*"
            assert Path(args[args.index("--body") + 1]).read_bytes() == ARCHIVE
            checksum = base64.b64encode(hashlib.sha256(ARCHIVE).digest()).decode()
            assert args[args.index("--checksum-sha256") + 1] == checksum
            assert kwargs["timeout"] == 600
            if state.failure == "put-object":
                raise publisher.PublicationError("controlled failure")
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "VersionId": "null" if state.failure == "unversioned" else "version-1",
                        "ChecksumSHA256": "wrong" if state.failure == "checksum" else checksum,
                    }
                ),
                stderr="",
                returncode=0,
            )
        if args[:2] == ["docker", "save"]:
            if state.failure == "save":
                raise publisher.PublicationError("controlled failure")
            archive_path = Path(args[args.index("--output") + 1])
            archive_path.write_bytes(ARCHIVE if state.failure != "empty" else b"")
            if state.failure == "too_large":
                with archive_path.open("ab") as stream:
                    stream.truncate(1_073_741_825)
        if args[:3] == ["docker", "image", "inspect"]:
            value = [
                {
                    "Config": {
                        "Labels": {"org.opencontainers.image.revision": "f" * 40 if state.wrong_source else SOURCE},
                        "User": "65532:65532",
                    },
                    "Id": "not-a-digest" if state.wrong_id else CONFIG_DIGEST,
                }
            ]
            return SimpleNamespace(stdout=json.dumps(value), stderr="", returncode=0)
        if args[:2] == ["docker", "ps"] and state.failure == "cleanup":
            return SimpleNamespace(stdout="remaining-container", stderr="", returncode=0)
        if args[0] == "docker" and args[1] == state.failure:
            raise publisher.PublicationError("controlled failure")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(publisher, "run", run)
    return state


def test_build_isolated_image_without_registry_operations(backend):
    assert publisher.publish(ENV) == {
        "schema_version": "1.0",
        "source_revision": SOURCE,
        "repository": "adp-tools-dev-adapters",
        "image_digest": None,
        "reused": False,
        "selfcheck_passed": True,
        "qualified_only": True,
        "artifact_key": ARCHIVE_KEY,
        "artifact_version": "version-1",
        "artifact_sha256": hashlib.sha256(ARCHIVE).hexdigest(),
        "config_digest": CONFIG_DIGEST,
    }
    operations = [args[1] for args in backend.calls if args[0] == "docker"]
    assert operations == ["build", "image", "run", "container", "ps", "save"]
    build = next(args for args in backend.calls if args[:2] == ["docker", "build"])
    assert build[build.index("--build-arg") + 1] == "ADP_SOURCE_REVISION=" + SOURCE
    assert build[build.index("--file") + 1] == "modules/tools/adapters/Dockerfile"
    assert build[build.index("--tag") + 1] == (
        f"123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-tools-dev-adapters:{SOURCE}"
    )
    check = next(args for args in backend.calls if args[:2] == ["docker", "run"])
    assert check[check.index("--network") + 1] == "none"
    assert "--read-only" in check and "--rm" in check
    assert "--env" not in check and "-e" not in check
    assert all(not path.exists() for path in backend.configs)


@pytest.mark.parametrize(
    "changed",
    [
        {"ADP_SOURCE_SHA": "latest"},
        {"ACCOUNT_ID": ""},
        {"AWS_REGION": ""},
        {"ADP_TOOLS_ENVIRONMENT": "unknown"},
        {"IMAGE_TAG": "f" * 40},
        {"PUBLISH_LATEST": "true"},
        {"ADP_ADAPTER_QUALIFY_ONLY": "false"},
        {"CODEBUILD_BUILD_ID": ""},
        {"CODEBUILD_BUILD_ID": "foreign:" + BUILD_UUID},
        {"ADP_ADAPTER_ARTIFACT_BUCKET": ""},
    ],
)
def test_invalid_or_publishing_mode_never_contacts_provider(backend, changed):
    with pytest.raises(publisher.PublicationError, match="invalid_inputs"):
        publisher.publish({**ENV, **changed})
    assert backend.calls == []


def test_wrong_account_cannot_build(backend):
    backend.account = "111122223333"
    with pytest.raises(publisher.PublicationError, match="account_mismatch"):
        publisher.publish(ENV)
    assert len(backend.calls) == 1


@pytest.mark.parametrize(
    "failure", ["build", "run", "cleanup", "save", "empty", "too_large", "put-object", "unversioned", "checksum"]
)
def test_failed_qualification_has_no_receipt_and_cleans_up(backend, failure):
    backend.failure = failure
    with pytest.raises(publisher.PublicationError):
        publisher.publish(ENV)
    assert all(not path.exists() for path in backend.configs)
    if failure in {"empty", "too_large"}:
        assert not any(args[:3] == ["aws", "s3api", "put-object"] for args in backend.calls)
    if failure != "build":
        assert any(args[:3] == ["docker", "container", "rm"] for args in backend.calls)


def test_wrong_image_source_is_refused(backend):
    backend.wrong_source = True
    with pytest.raises(publisher.PublicationError, match="image_identity_mismatch"):
        publisher.publish(ENV)
    assert all(not path.exists() for path in backend.configs)


def test_image_without_config_digest_is_refused_before_archive_upload(backend):
    backend.wrong_id = True
    with pytest.raises(publisher.PublicationError, match="image_identity_mismatch"):
        publisher.publish(ENV)
    assert not any(args[:3] == ["aws", "s3api", "put-object"] for args in backend.calls)
