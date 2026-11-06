"""Bounded retained receipts reuse the existing publisher and runner identity."""

import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from modules.tools.platform.scripts import native_evidence as evidence
from modules.tools.platform.scripts.native_test_session import AdmissionUnavailable


@pytest.fixture
def uploader(tmp_path, monkeypatch):
    directory = tmp_path / "native-evidence"
    directory.mkdir(mode=0o700)
    for name in ("native-smoke.json", "input-delivery.json"):
        path = directory / name
        path.write_text(json.dumps({"status": "offline-safe-receipt"}))
        path.chmod(0o600)
    private = tmp_path / "login.jwt"
    private.write_text("private-bearer-sentinel")
    private.chmod(0o600)
    env = {
        "GITHUB_REPOSITORY": "aws-e/adp",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "release",
        "GITHUB_WORKSPACE": str(Path(__file__).resolve().parents[4]),
        "GITHUB_WORKFLOW_SHA": "b" * 40,
        "RUNNER_TEMP": str(tmp_path),
        "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-agent-runner-role",
        "AWS_WEB_IDENTITY_TOKEN_FILE": str(tmp_path / "irsa"),
        "AWS_ACCESS_KEY_ID": "ambient-static-sentinel",
        "AWS_PROFILE": "owner-sentinel",
        "ADP_TOOLS_SMOKE_EVIDENCE_DIR": str(directory),
        "ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN": "private-bearer-sentinel",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("subprocess.check_output", lambda *args, **kwargs: "a" * 40)
    calls, objects = [], {}

    def aws(arguments, child, **kwargs):
        calls.append(arguments)
        assert "AWS_ACCESS_KEY_ID" not in child and "AWS_PROFILE" not in child
        assert "ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN" not in child
        assert child["AWS_ROLE_ARN"] == env["AWS_ROLE_ARN"]
        assert kwargs["timeout"] == 10
        if arguments[0] == "sts":
            return {
                "Account": "123456789012",
                "Arn": "arn:aws:sts::123456789012:assumed-role/adp-dev-agent-runner-role/offline",
            }
        assert (
            arguments[arguments.index("--expected-bucket-owner") + 1] == "123456789012"
        )
        key = arguments[arguments.index("--key") + 1]
        if arguments[1] == "put-object":
            assert arguments[arguments.index("--if-none-match") + 1] == "*"
            path = Path(arguments[arguments.index("--body") + 1])
            objects[key] = path.read_bytes()
        data = objects[key]
        return {
            "VersionId": "one",
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(data).digest()).decode(),
            "ContentLength": len(data),
            "ServerSideEncryption": "AES256",
        }

    monkeypatch.setattr(evidence, "aws", aws)
    return directory, calls, objects


def test_publisher_only_retains_allowlisted_receipts_with_five_aws_calls(
    uploader, capsys
):
    _, calls, objects = uploader
    evidence.main()
    assert [args[:2] for args in calls] == [
        ["sts", "get-caller-identity"],
        ["s3api", "put-object"],
        ["s3api", "head-object"],
        ["s3api", "put-object"],
        ["s3api", "head-object"],
    ]
    archive = next(value for key, value in objects.items() if key.endswith(".tar.gz"))
    manifest = next(value for key, value in objects.items() if key.endswith(".json"))
    assert len(archive) <= 65536 and len(manifest) <= 8192
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as packed:
        assert sorted(packed.getnames()) == ["input-delivery.json", "native-smoke.json"]
    assert b"private-bearer-sentinel" not in gzip.decompress(archive)
    assert "private-bearer-sentinel" not in capsys.readouterr().out


def test_publisher_refuses_oversized_or_linked_receipts_before_aws(uploader):
    directory, calls, _ = uploader
    path = directory / "native-smoke.json"
    path.write_bytes(b"x" * 16385)
    with pytest.raises(AdmissionUnavailable):
        evidence.main()
    assert not calls
    path.unlink()
    path.symlink_to(directory / "input-delivery.json")
    with pytest.raises(OSError):
        evidence.main()
    assert not calls


def test_failed_evidence_put_never_retries_or_publishes_manifest(uploader, monkeypatch):
    _, calls, _ = uploader
    original = evidence.aws

    def fail(arguments, child, **kwargs):
        if arguments[0] == "s3api":
            calls.append(arguments)
            raise AdmissionUnavailable("write unconfirmed")
        return original(arguments, child, **kwargs)

    monkeypatch.setattr(evidence, "aws", fail)
    with pytest.raises(AdmissionUnavailable):
        evidence.main()
    assert len(calls) == 2


def test_no_receipts_cannot_create_a_success_evidence_object(uploader):
    directory, calls, _ = uploader
    for path in directory.iterdir():
        path.unlink()
    evidence.main()
    assert not calls
