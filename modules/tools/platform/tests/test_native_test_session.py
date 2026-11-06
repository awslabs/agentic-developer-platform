"""Offline client contract checks; no fixture or paid admission is created."""

import base64
from datetime import UTC, datetime
import hashlib
from http.client import IncompleteRead
import io
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from uuid import uuid4

import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from modules.tools.platform.scripts import native_test_session as native


class Reply(io.BytesIO):
    status = 200


class Wire:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def open(self, request, timeout):
        assert timeout == 40
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Reply(reply if isinstance(reply, bytes) else json.dumps(reply).encode())


@pytest.fixture
def inputs(tmp_path):
    reference = {
        "version": 1,
        "run_id": str(uuid4()),
        "tenant_id": "offline-tenant",
        "approval_revision": str(uuid4()),
        "deployment_id": "offline-deployment",
        "gateway_origin": "https://gateway.example.invalid",
        "source_revision": "a" * 40,
        "definition_revision": "b" * 40,
        "github_run_id": "42",
        "github_run_attempt": "1",
    }
    manifest = tmp_path / "approval.json"
    manifest.write_text(json.dumps(reference))
    manifest.chmod(0o600)
    login = tmp_path / "dedicated-login"
    login.write_text("fixture.access.signature")
    login.chmod(0o600)
    env = {
        "ADP_TOOLS_SMOKE_APPROVAL_FILE": str(manifest),
        "ADP_TOOLS_SMOKE_NATIVE_LOGIN_FILE": str(login),
        "ADP_TOOLS_SMOKE_GATEWAY_URL": reference["gateway_origin"],
        "ADP_SOURCE_REVISION": "a" * 40,
        "ADP_DEFINITION_REVISION": "b" * 40,
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://pipeline.actions.githubusercontent.com/oidc?api-version=1&audience=old",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "offline-oidc-request",
        "ADP_TOOLS_AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "offline-access",
        "AWS_SECRET_ACCESS_KEY": "offline-secret",
        "AWS_SESSION_TOKEN": "offline-session",
    }
    return reference, env


def exchange_response(**changes):
    return {
        "capability": "adpt1." + str(uuid4()) + "." + "c" * 43,
        "scope": "all_in",
        "all_in_admission": "required",
        **changes,
    }


def test_real_request_shapes_bind_enrollment_and_every_public_operation(inputs):
    reference, env = inputs
    result = exchange_response()
    wire = Wire(
        [
            {"value": "github.oidc.signature"},
            result,
            {"discovery": "fixture"},
            {"status": "confirmed"},
        ]
    )
    session = native.NativeSession.exchange(env, opener=wire)
    first, exchange = wire.requests
    assert first.full_url.endswith(
        "api-version=1&audience=adp-native-test-enrollment-v1"
    )
    assert first.get_header("Authorization") == "Bearer offline-oidc-request"
    assert (
        exchange.full_url
        == reference["gateway_origin"] + "/api/v1/native-tests/capabilities/exchange"
    )
    assert exchange.get_header("Authorization") == "Bearer fixture.access.signature"
    packet = json.loads(exchange.data)
    assert packet.keys() == {"run_id", "github_oidc_token", "producer_proof"}
    assert packet["run_id"] == reference["run_id"]
    proof = json.loads(base64.b64decode(packet["producer_proof"]))
    bound = {
        key: reference[key]
        for key in ["tenant_id", "run_id", "approval_revision", "deployment_id"]
    }
    assert (
        proof["x-adp-work-invocation"]
        == hashlib.sha256(
            native.canonical(
                {**bound, "github_oidc_token": packet["github_oidc_token"]}
            )
        ).hexdigest()
    )
    operation = str(uuid4())
    session.request("GET", "/api/agent/tools?live=true")
    session.request(
        "POST",
        "/api/agent/tools/call",
        payload={"request_id": operation},
        operation_id=operation,
        gateway_url="https://agentcore.example.invalid/mcp",
    )
    for request in wire.requests[2:]:
        assert request.get_header("Authorization") == "Bearer fixture.access.signature"
        assert (
            request.get_header(native.CAPABILITY_HEADER.capitalize())
            == result["capability"]
        )
        native.identifier(request.get_header(native.OPERATION_HEADER.capitalize()))
    assert len(session.operations) == 2
    assert "fixture.access.signature" not in repr(session) and result[
        "capability"
    ] not in repr(session)


def test_producer_signature_matches_independent_botocore_signer(inputs):
    _, env = inputs
    reference = native.ApprovedReference.read(env["ADP_TOOLS_SMOKE_APPROVAL_FILE"], env)
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    proof = json.loads(
        base64.b64decode(
            native.producer_proof(reference, "github.oidc.signature", env, now=now)
        )
    )
    expected = AWSRequest(
        method="POST",
        url="https://sts.us-east-1.amazonaws.com/",
        data="Action=GetCallerIdentity&Version=2011-06-15",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "x-adp-work-invocation": proof["x-adp-work-invocation"],
        },
    )
    with patch("botocore.auth.get_current_datetime", return_value=now):
        SigV4Auth(
            Credentials("offline-access", "offline-secret", "offline-session"),
            "sts",
            "us-east-1",
        ).add_auth(expected)
    assert proof == {key.lower(): value for key, value in expected.headers.items()}


def test_status_reads_use_only_original_fixture_and_capability_with_finite_limit(
    inputs,
):
    reference, env = inputs
    admitted = exchange_response()
    wire = Wire(
        [{"value": "github.oidc.signature"}, admitted]
        + [{"schema_version": "1.0", "run_id": reference["run_id"]}]
        * native.MAX_STATUS_READS
    )
    session = native.NativeSession.exchange(env, opener=wire)
    for _ in range(native.MAX_STATUS_READS):
        assert session.status()["run_id"] == reference["run_id"]
    with pytest.raises(native.AdmissionUnavailable, match="read limit"):
        session.status()
    assert session.operations == set()
    assert len(wire.requests) == 2 + native.MAX_STATUS_READS
    for request in wire.requests[2:]:
        assert request.get_method() == "GET" and request.data is None
        assert request.full_url == (
            reference["gateway_origin"]
            + "/api/v1/native-tests/runs/"
            + reference["run_id"]
            + "/status"
        )
        assert request.get_header("Authorization") == "Bearer fixture.access.signature"
        assert (
            request.get_header(native.CAPABILITY_HEADER.capitalize())
            == admitted["capability"]
        )
        assert request.get_header(native.OPERATION_HEADER.capitalize()) is None


def test_unconfirmed_status_read_consumes_allowance_without_retry(inputs):
    _, env = inputs
    wire = Wire(
        [{"value": "github.oidc.signature"}, exchange_response()]
        + [IncompleteRead(b"private response") for _ in range(native.MAX_STATUS_READS)]
    )
    session = native.NativeSession.exchange(env, opener=wire)
    for _ in range(native.MAX_STATUS_READS):
        with pytest.raises(native.AdmissionUnavailable, match="unconfirmed") as error:
            session.status()
        assert "private response" not in str(error.value)
    with pytest.raises(native.AdmissionUnavailable, match="read limit"):
        session.status()
    assert len(wire.requests) == 2 + native.MAX_STATUS_READS


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "model_only"},
        {"all_in_admission": "unavailable"},
        {"capability": ""},
        {"capability": "adpt1." + "0" * 36 + "." + "c" * 43},
    ],
)
def test_model_only_or_invalid_exchange_never_reaches_discovery(inputs, change):
    _, env = inputs
    wire = Wire([{"value": "github.oidc.signature"}, exchange_response(**change)])
    with pytest.raises((native.AdmissionUnavailable, ValueError)):
        native.NativeSession.exchange(env, opener=wire)
    assert len(wire.requests) == 2


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("GITHUB_RUN_ID", "43"),
        ("GITHUB_REF", "refs/heads/other"),
        ("ADP_SOURCE_REVISION", "c" * 40),
        ("ADP_DEFINITION_REVISION", "c" * 40),
        ("ADP_TOOLS_SMOKE_GATEWAY_URL", "https://other.example.invalid"),
    ],
)
def test_unapproved_job_refuses_before_any_http(inputs, key, value):
    _, env = inputs
    env[key] = value
    wire = Wire([])
    with pytest.raises(native.AdmissionUnavailable):
        native.NativeSession.exchange(env, opener=wire)
    assert not wire.requests


@pytest.mark.parametrize("unsafe", ["mode", "symlink", "hardlink", "fifo", "oversized"])
def test_unsafe_login_file_refuses_before_oidc(inputs, tmp_path, unsafe):
    _, env = inputs
    login = Path(env["ADP_TOOLS_SMOKE_NATIVE_LOGIN_FILE"])
    if unsafe == "mode":
        login.chmod(0o644)
    elif unsafe == "oversized":
        login.write_bytes(b"x" * 16385)
    else:
        target = tmp_path / "other-login"
        login.rename(target)
        if unsafe == "symlink":
            login.symlink_to(target)
        elif unsafe == "hardlink":
            os.link(target, login)
        else:
            os.mkfifo(login, 0o600)
    wire = Wire([])
    with pytest.raises((native.AdmissionUnavailable, OSError)):
        native.NativeSession.exchange(env, opener=wire)
    assert not wire.requests


@pytest.mark.parametrize(
    "response",
    [
        URLError("private-token-sentinel"),
        IncompleteRead(b"private-response-sentinel", 100),
        HTTPError(
            "https://gateway.example.invalid",
            403,
            "private-token-sentinel",
            {},
            io.BytesIO(b"private-response-sentinel"),
        ),
        b'{"capability":"private-token-sentinel","capability":"duplicate"}',
        b"x" * 262145,
    ],
)
def test_uncertain_exchange_never_retries_or_echoes_credentials(inputs, response):
    _, env = inputs
    wire = Wire([{"value": "github.oidc.signature"}, response])
    with pytest.raises(native.AdmissionUnavailable) as captured:
        native.NativeSession.exchange(env, opener=wire)
    assert len(wire.requests) == 2
    assert "private-" not in str(captured.value)


def test_unknown_operation_keeps_original_id_and_does_not_retry(inputs):
    _, env = inputs
    wire = Wire(
        [{"value": "github.oidc.signature"}, exchange_response(), URLError("unknown")]
    )
    session = native.NativeSession.exchange(env, opener=wire)
    operation = str(uuid4())
    with pytest.raises(native.AdmissionUnavailable):
        session.request(
            "POST",
            "/api/agent/tools/call",
            payload={"request_id": operation},
            operation_id=operation,
        )
    with pytest.raises(native.AdmissionUnavailable):
        session.request(
            "POST",
            "/api/agent/tools/call",
            payload={"request_id": operation},
            operation_id=operation,
        )
    assert len(wire.requests) == 3 and session.operations == {operation}


@pytest.mark.parametrize(
    "path",
    [
        "https://other.example.invalid/api/agent/tools",
        "//other.example.invalid/api/agent/tools",
        "/api/agent/toolsevil",
        "/api/agent/tools/../model",
        "/api/agent/tools/%2e%2e/model",
    ],
)
def test_native_headers_cannot_be_forwarded_to_another_origin_or_route(inputs, path):
    _, env = inputs
    wire = Wire([{"value": "github.oidc.signature"}, exchange_response()])
    session = native.NativeSession.exchange(env, opener=wire)
    with pytest.raises(native.AdmissionUnavailable):
        session.request("GET", path)
    assert len(wire.requests) == 2


def test_redirect_always_refuses():
    with pytest.raises(native.AdmissionUnavailable):
        native.NoRedirects().redirect_request(
            None, None, 302, "redirect", {}, "https://other.example.invalid"
        )


@pytest.mark.parametrize("discoveries", [1, 9])
def test_export_installed_transport_uses_original_authoritative_setup_status(inputs, tmp_path, discoveries):
    reference, env = inputs
    exchange = exchange_response()
    wire = Wire([{"value": "github.oidc.signature"}, exchange, {
        "run_id": reference["run_id"], "run_started_at": int(datetime.now(UTC).timestamp()), "funding_status": "held", "cap_usd": "20",
    }])
    session = native.NativeSession.exchange(env, opener=wire)
    path = session.export_client_transport(tmp_path / "client.json", calls=[{"tool": "knowledge___search", "arguments_sha256": "a" * 64, "max_requests": 1}], models=[], max_discoveries=discoveries)
    value = json.loads(path.read_text())
    assert value["reference"] == {key: val for key, val in reference.items() if key != "version"}
    assert value["capability"] == exchange["capability"] and value["access_token"] == "fixture.access.signature"
    assert path.stat().st_mode & 0o077 == 0 and session.status_reads == 1
    assert len(wire.requests) == 3
    assert value["max_discoveries"] == discoveries


def test_export_cannot_expand_discovery_beyond_the_client_matrix(inputs, tmp_path):
    _, env = inputs
    session = native.NativeSession.exchange(env, opener=Wire([{"value": "github.oidc.signature"}, exchange_response()]))
    with pytest.raises(native.AdmissionUnavailable):
        session.export_client_transport(tmp_path / "client.json", calls=[{"tool": "knowledge___search", "arguments_sha256": "a" * 64, "max_requests": 1}], models=[], max_discoveries=10)
    assert session.status_reads == 0 and not (tmp_path / "client.json").exists()


@pytest.mark.parametrize("fault", ["unknown_funding", "foreign_run", "expired"])
def test_export_cannot_convert_unknown_or_foreign_status_into_client_authority(inputs, tmp_path, fault):
    reference, env = inputs
    status = {"run_id": reference["run_id"], "run_started_at": int(datetime.now(UTC).timestamp()), "funding_status": "held", "cap_usd": "20"}
    if fault == "unknown_funding":
        status["funding_status"] = "unknown"
    elif fault == "foreign_run":
        status["run_id"] = str(uuid4())
    else:
        status["run_started_at"] -= 301
    session = native.NativeSession.exchange(env, opener=Wire([{"value": "github.oidc.signature"}, exchange_response(), status]))
    with pytest.raises(native.AdmissionUnavailable):
        session.export_client_transport(tmp_path / "client.json", calls=[{"tool": "knowledge___search", "arguments_sha256": "a" * 64, "max_requests": 1}], models=[], max_discoveries=1)
    assert not (tmp_path / "client.json").exists()
