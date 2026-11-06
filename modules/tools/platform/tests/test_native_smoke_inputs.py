"""Offline delivery failures never mint approval or consume a provider fixture."""

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import time
from unittest.mock import patch
from uuid import uuid4

import pytest

from modules.tools.platform.scripts import native_smoke_inputs as inputs
from modules.tools.platform.scripts.native_test_session import (
    AdmissionUnavailable,
    canonical,
)


@pytest.fixture
def delivery(tmp_path):
    started = int(time.time()) - 2
    approval = {
        "version": 1,
        "run_id": str(uuid4()),
        "approval_revision": str(uuid4()),
        "tenant_id": "offline-tenant",
        "deployment_id": "offline-deployment",
        "gateway_origin": "https://facade.example.invalid",
        "source_revision": "a" * 40,
        "definition_revision": "b" * 40,
        "github_run_id": "42",
        "github_run_attempt": "1",
    }
    fixture = {
        "version": 1,
        "run_id": approval["run_id"],
        "approval_revision": approval["approval_revision"],
        "contract_sha256": "c" * 64,
        "arguments": {"query": "explicit approved offline query"},
    }
    receipt = {
        "run_id": approval["run_id"],
        "approval_revision": approval["approval_revision"],
        "scope": "all_in",
        "cap_usd": "20",
        "expires_at": started + 300,
        "run_started_at": started,
    }
    bundle = {
        "version": 1,
        "approval": approval,
        "fixture": fixture,
        "approval_receipt": receipt,
    }
    env = {
        **inputs.binding_environment(approval),
        "GITHUB_REPOSITORY": "aws-e/adp",
        "GITHUB_WORKFLOW": inputs.WORKFLOW,
        "GITHUB_JOB": "release",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_ENV": str(tmp_path / "github-env"),
        "ADP_TOOLS_AWS_REGION": "us-east-1",
        "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-agent-runner-role",
        "AWS_WEB_IDENTITY_TOKEN_FILE": str(tmp_path / "irsa"),
        "ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN": "fixture.access.signature",
        "ADP_TOOLS_SMOKE_EVIDENCE_DIR": str(tmp_path / "evidence"),
        "AWS_ACCESS_KEY_ID": "ambient-static-sentinel",
        "AWS_PROFILE": "owner-sentinel",
        "AWS_SESSION_TOKEN": "ambient-session-sentinel",
        "AWS_SECRET_ACCESS_KEY": "ambient-secret-sentinel",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "github-request-sentinel",
    }
    return env, bundle


class S3:
    def __init__(
        self, bundle, *, missing=0, head_change=None, get_change=None, clock=None
    ):
        self.data = canonical(bundle)
        self.digest = base64.b64encode(hashlib.sha256(self.data).digest()).decode()
        self.missing = missing
        self.head_change, self.get_change = head_change or {}, get_change or {}
        self.calls = []
        self.clock = clock

    def __call__(self, args, env, **kwargs):
        self.calls.append((args, env, kwargs))
        assert 0 < kwargs["timeout"] <= 10
        if self.clock:
            self.clock[0] += kwargs["timeout"]
        if args[:2] == ["sts", "get-caller-identity"]:
            return {
                "Account": "123456789012",
                "Arn": "arn:aws:sts::123456789012:assumed-role/adp-dev-agent-runner-role/offline",
            }
        response = {
            "VersionId": "version-one",
            "ServerSideEncryption": "AES256",
            "ChecksumSHA256": self.digest,
        }
        if args[1] == "head-object":
            if self.missing:
                self.missing -= 1
                return None
            return {
                **response,
                "ContentLength": len(self.data),
                "ETag": '"etag"',
                **self.head_change,
            }
        if args[1] == "get-object":
            Path(args[-1]).write_bytes(self.data)
            return {**response, **self.get_change}
        assert args[1] == "put-object"
        assert args[args.index("--if-none-match") + 1] == "*"
        assert Path(args[args.index("--body") + 1]).read_bytes() == self.data
        return response


def test_download_binds_exact_original_version_and_isolates_runner_identity(delivery):
    env, bundle = delivery
    s3 = S3(bundle)
    values = inputs.download(env, call=s3)
    assert [args[:2] for args, _, _ in s3.calls] == [
        ["sts", "get-caller-identity"],
        ["s3api", "head-object"],
        ["s3api", "get-object"],
    ]
    for args, child, _ in s3.calls:
        assert not any(
            key in child
            for key in (
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_PROFILE",
                "AWS_SESSION_TOKEN",
                "ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN",
            )
        )
        assert child["AWS_ROLE_ARN"] == env["AWS_ROLE_ARN"]
        assert child["AWS_CONFIG_FILE"] == "/dev/null"
    args = s3.calls[-1][0]
    assert args[args.index("--if-match") + 1] == '"etag"'
    assert args[args.index("--expected-bucket-owner") + 1] == "123456789012"
    assert (
        Path(values["ADP_TOOLS_SMOKE_NATIVE_LOGIN_FILE"]).read_text()
        == env["ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN"]
    )
    assert "fixture.access.signature" not in Path(env["GITHUB_ENV"]).read_text()
    assert values["CI_EVIDENCE_RUNNER_ROLE_ARN"] == env["AWS_ROLE_ARN"]
    for name, value in values.items():
        if name.startswith("ADP_"):
            assert Path(value).stat().st_mode & 0o777 == 0o600
    record = json.loads(Path(values["ADP_TOOLS_SMOKE_DELIVERY_FILE"]).read_text())
    assert record["run_started_at"] == bundle["approval_receipt"]["run_started_at"]
    assert record["head_attempts"] == record["get_attempts"] == 1


@pytest.mark.parametrize(
    "name,value",
    [
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("GITHUB_JOB", "other"),
        ("GITHUB_REPOSITORY", "other/adp"),
        ("ADP_SOURCE_REVISION", "invalid"),
        ("ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN", ""),
        ("ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN", "refresh-token"),
    ],
)
def test_wrong_job_or_login_refuses_without_aws(delivery, name, value):
    env, bundle = delivery
    env[name] = value
    s3 = S3(bundle)
    with pytest.raises(AdmissionUnavailable):
        inputs.download(env, call=s3)
    assert not s3.calls


@pytest.mark.parametrize(
    "head_change,get_change",
    [
        ({"ContentLength": 32769}, {}),
        ({"ContentLength": 0}, {}),
        ({"VersionId": "null"}, {}),
        ({"ServerSideEncryption": "aws:kms"}, {}),
        ({"ChecksumSHA256": "foreign"}, {}),
        ({}, {"VersionId": "version-two"}),
        ({}, {"ChecksumSHA256": "foreign"}),
        ({}, {"ServerSideEncryption": "aws:kms"}),
    ],
)
def test_rebound_or_oversized_s3_response_never_releases_login(
    delivery, head_change, get_change
):
    env, bundle = delivery
    s3 = S3(bundle, head_change=head_change, get_change=get_change)
    with pytest.raises(AdmissionUnavailable):
        inputs.download(env, call=s3)
    assert not (Path(env["RUNNER_TEMP"]) / "adp-native-tools-inputs/login.jwt").exists()
    assert not Path(env["GITHUB_ENV"]).exists()


def test_missing_object_has_both_attempt_and_wall_clock_bounds(delivery):
    env, bundle = delivery
    clock = [0]
    s3 = S3(bundle, missing=100)
    with pytest.raises(AdmissionUnavailable):
        inputs.download(
            env,
            call=s3,
            monotonic=lambda: clock[0],
            sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
        )
    assert len([args for args, _, _ in s3.calls if args[1] == "head-object"]) == 12
    assert not any(args[1] == "get-object" for args, _, _ in s3.calls)
    assert clock[0] <= 120


def test_slow_probe_stops_at_deadline(delivery):
    env, bundle = delivery
    clock = [0]
    s3 = S3(bundle, missing=100, clock=clock)
    with pytest.raises(AdmissionUnavailable):
        inputs.download(
            env,
            call=s3,
            monotonic=lambda: clock[0],
            sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
        )
    assert clock[0] == 120
    assert len(s3.calls) < 13


@pytest.mark.parametrize(
    "mutation", ["source", "expiry", "old-start", "query", "extra-bearer"]
)
def test_unapproved_or_late_bundle_cannot_be_delivered(delivery, mutation):
    env, bundle = delivery
    if mutation == "source":
        bundle["approval"]["source_revision"] = "d" * 40
    elif mutation == "expiry":
        bundle["approval_receipt"]["expires_at"] = 1
    elif mutation == "old-start":
        bundle["approval_receipt"]["run_started_at"] -= 301
    elif mutation == "query":
        bundle["fixture"]["arguments"] = {}
    else:
        bundle["bearer"] = "owner-token-sentinel"
    with pytest.raises(AdmissionUnavailable):
        inputs.download(env, call=S3(bundle))
    assert not Path(env["GITHUB_ENV"]).exists()


def test_owner_helper_does_one_conditional_put_and_no_auth_or_policy_call(
    delivery, tmp_path
):
    env, bundle = delivery
    files = []
    for name in ("approval", "fixture", "approval_receipt"):
        path = tmp_path / f"owner-{name}.json"
        inputs.private_write(path, canonical(bundle[name]))
        files.append(path)
    s3 = S3(bundle)
    result = inputs.publish(
        *files, account="123456789012", region="us-east-1", environ=env, call=s3
    )
    assert [args[:2] for args, _, _ in s3.calls] == [
        ["sts", "get-caller-identity"],
        ["s3api", "put-object"],
    ]
    assert result["key"].startswith(
        "artifacts/14/aws-e/adp/42/1/release/native-tools-inputs/"
    )
    assert b"fixture.access.signature" not in s3.data
    assert b"owner-token" not in s3.data


def test_aws_timeout_and_once_write_conflict_do_not_retry_or_echo_credentials():
    for response in (
        subprocess.TimeoutExpired("aws", 10),
        subprocess.CompletedProcess(
            "aws", 1, b"secret-output", b"PreconditionFailed private-token"
        ),
    ):
        with patch(
            "subprocess.run",
            side_effect=response if isinstance(response, Exception) else None,
            return_value=response,
        ) as run:
            with pytest.raises(AdmissionUnavailable) as error:
                inputs.aws(["s3api", "put-object"], {})
            assert run.call_count == 1
            assert "private-token" not in str(error.value)
            assert run.call_args.kwargs["env"]["AWS_MAX_ATTEMPTS"] == "1"


def test_aws_only_waits_for_missing_head_errors():
    for stderr in (b"(403)", b"(404)", b"(AccessDenied)", b"(NoSuchKey)"):
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess("aws", 1, b"", stderr),
        ):
            assert inputs.aws(["s3api", "head-object"], {}, missing=True) is None
            with pytest.raises(AdmissionUnavailable):
                inputs.aws(["s3api", "get-object"], {})
