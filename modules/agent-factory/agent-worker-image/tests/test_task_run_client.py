"""Task run transport keeps run credentials host-side and action-scoped."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.credentials import Credentials

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import task_run_client as client


def token(claims: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_workload_body_is_derived_from_the_same_projected_token(monkeypatch):
    proof = token(
        {
            "kubernetes.io": {
                "namespace": "adp-agents",
                "pod": {"uid": "8ca7fc1c-1a8c-4a18-98c4-e190d120d00d", "name": "worker-1"},
            }
        }
    )
    monkeypatch.setattr(client, "read_workload_token", lambda: proof)
    assert client.workload_identity() == {
        "pod_uid": "8ca7fc1c-1a8c-4a18-98c4-e190d120d00d",
        "namespace": "adp-agents",
        "pod_name": "worker-1",
    }


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv(
        "ADP_AGENT_CONTROL_ENDPOINT",
        "https://gateway.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent",
    )
    proof = token(
        {
            "kubernetes.io": {
                "namespace": "test",
                "pod": {"uid": "8ca7fc1c-1a8c-4a18-98c4-e190d120d00d"},
            }
        }
    )
    monkeypatch.setattr(client, "read_workload_token", lambda: proof)
    monkeypatch.setattr(
        "adp_trigger.transport_identity.worker_credentials",
        lambda _: Credentials("platform-test", "test-secret", "test-session"),
    )
    calls = []
    responses = [
        {"schema_version":"1.0", "task_id":"task-test", "invocation_id":"invocation-test", "generation":1,
         "persona":"agent-task-cyber", "run_credential":"run-secret",
         "run_credential_expires_at":(datetime.now(UTC)+timedelta(seconds=900)).isoformat(),
         "deadline_at":(datetime.now(UTC)+timedelta(hours=6)).isoformat()},
        {"operation_status": "confirmed"},
        {"operation_status": "confirmed"},
    ]

    class ResponseContext:
        status_code = 200

        def __init__(self, value):
            self.raw = Mock()
            self.raw.read.return_value = json.dumps(value).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    class Session:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, **kwargs):
            calls.append((url, kwargs, self.trust_env))
            return ResponseContext(responses.pop(0))

    monkeypatch.setattr(client.requests, "Session", Session)
    return calls, proof


def test_bootstrap_has_only_workload_proof_run_calls_add_opaque_credential(transport):
    calls, proof = transport
    run = client.TaskRunClient()
    assert run.bootstrap({"schema_version": "1.0", "task_id":"task-test", "invocation_id":"invocation-test"})["run_credential"] == "run-secret"
    run.attempt({"schema_version": "1.0"})
    run.settlement({"schema_version": "1.0"})
    bootstrap, attempt, settlement = calls
    assert bootstrap[0].endswith("/task/bootstrap")
    assert "X-Adp-Run-Credential" not in bootstrap[1]["headers"]
    assert attempt[1]["headers"]["X-Adp-Run-Credential"] == "run-secret"
    assert "X-Adp-Run-Credential" not in settlement[1]["headers"]
    assert all(call[1]["headers"]["X-Adp-Workload-Token"] == proof for call in calls)
    assert all(call[2] is False and call[1]["allow_redirects"] is False for call in calls)


@pytest.mark.parametrize(("action", "body", "status", "accepted"), [
    ("artifact", {"content_base64": "eA=="}, 201, True),
    ("artifact", {"operation": "read"}, 200, True),
    ("artifact", {"operation": "read"}, 201, False),
    ("artifact", {"content_base64": "eA=="}, 200, False),
    ("model", {}, 201, False),
])
def test_created_status_is_only_accepted_for_artifact_upload(transport, monkeypatch, action, body, status, accepted):
    session = client.requests.Session
    original = session.post
    def post(self, url, **kwargs):
        response = original(self, url, **kwargs)
        response.status_code = status
        return response
    monkeypatch.setattr(session, "post", post)
    run = client.TaskRunClient()
    run._run_credential = "test-run-secret"
    if accepted:
        assert isinstance(run._post(action, body, run_bound=True), dict)
    else:
        with pytest.raises(client.TaskRunClientError, match="refused"):
            run._post(action, body, run_bound=True)


def test_model_wire_preserves_utf8_instead_of_expanding_unicode(transport):
    calls, _ = transport
    run = client.TaskRunClient()
    run._run_credential = "test-run-secret"
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "€" * 10000}]}]}
    run._post("model", body, run_bound=True)
    wire = calls[-1][1]["data"]
    assert b"\\u20ac" not in wire
    assert len(wire) < 32000
    assert json.loads(wire) == body


def test_cyber_uses_configured_iam_service_with_host_credentials(transport, monkeypatch):
    calls, proof = transport
    endpoint = 'https://cyber.execute-api.us-east-1.amazonaws.com/dev/tools/cyber'
    monkeypatch.setenv(client.CYBER_TOOLS_ENDPOINT_ENV, endpoint)
    run = client.TaskRunClient()
    run._run_credential = 'opaque-run-credential'
    body = {'operation': 'triage', 'payload': {'url': 'https://attacker.example/tools/cyber'}}
    run.cyber(body)
    url, request, trust_env = calls[-1]
    assert url == endpoint
    assert json.loads(request['data']) == body
    assert request['headers'][client.RUN_CREDENTIAL_HEADER] == 'opaque-run-credential'
    assert request['headers'][client.WORKLOAD_HEADER] == proof
    authorization = request['headers']['Authorization']
    assert authorization.startswith('AWS4-HMAC-SHA256 ')
    assert '/us-east-1/execute-api/aws4_request' in authorization
    assert 'x-adp-run-credential' in authorization and 'x-adp-workload-token' in authorization
    assert trust_env is False and request['allow_redirects'] is False
    run.model({'messages': []})
    assert calls[-1][0].endswith('/task/model')


@pytest.mark.parametrize('endpoint', [
    '', 'http://cyber.example/tools/cyber', 'https://cyber.example/task/cyber',
    'https://cyber.example/tools/cyber/', 'https://cyber.example/tools/cyber?redirect=x',
    'https://user:secret@cyber.example/tools/cyber', 'https://cyber.example/tools/cyber#fragment',
    'https://cyber.example/../tools/cyber', 'https://cyber.example/%2e%2e/tools/cyber',
    'https://cyber.example:8443/tools/cyber', 'https://cyber.example/tools/cyber?',
    'https://cyber.example/\ntools/cyber', 'https://cyber.example:bad/tools/cyber',
])
def test_invalid_or_missing_cyber_endpoint_never_falls_back(transport, monkeypatch, endpoint):
    calls, _ = transport
    monkeypatch.setenv(client.CYBER_TOOLS_ENDPOINT_ENV, endpoint)
    run = client.TaskRunClient()
    run._run_credential = 'opaque-run-credential'
    with pytest.raises(client.TaskRunClientError, match='cyber tools endpoint unavailable'):
        run.cyber({'operation': 'cancel_jobs', 'payload': {}})
    assert calls == []


def test_cyber_requires_run_binding_and_freezes_host_endpoint(transport, monkeypatch):
    calls, _ = transport
    endpoint = 'https://cyber.example/tools/cyber'
    monkeypatch.setenv(client.CYBER_TOOLS_ENDPOINT_ENV, endpoint)
    run = client.TaskRunClient()
    monkeypatch.setenv(client.CYBER_TOOLS_ENDPOINT_ENV, 'https://changed.example/tools/cyber')
    with pytest.raises(client.TaskRunClientError, match='run credential unavailable'):
        run.cyber({})
    assert not calls
    run._run_credential = 'opaque-run-credential'
    run.cyber({})
    assert calls[-1][0] == endpoint


def test_cyber_service_configuration_is_not_in_sdk_environment(monkeypatch, tmp_path):
    from lib.task_host import _child_environment

    monkeypatch.setenv(client.CYBER_TOOLS_ENDPOINT_ENV, 'https://cyber.example/tools/cyber')
    monkeypatch.setenv('X_ADP_RUN_CREDENTIAL', 'opaque-run-credential')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'host-secret')
    environment = _child_environment(tmp_path, sdk=True)
    assert client.CYBER_TOOLS_ENDPOINT_ENV not in environment
    assert 'X_ADP_RUN_CREDENTIAL' not in environment
    assert 'AWS_SECRET_ACCESS_KEY' not in environment
    assert environment['ADP_TASK_NETWORK'] == 'host-mediated-sdk'


@pytest.mark.parametrize('cancel', [False, True])
def test_control_transient_failure_retries_identical_read_then_preserves_cancel(transport, monkeypatch, cancel):
    run = client.TaskRunClient()
    run._run_credential = 'host-only-credential'
    requests, delays = [], []
    def post(action, body, **kwargs):
        requests.append((action, dict(body), kwargs))
        if len(requests) < 3:
            raise client.TaskRunClientUnavailable('edge 502')
        return {'cancel_requested': cancel, 'attempt_valid': True}
    monkeypatch.setattr(run, '_post', post)
    monkeypatch.setattr(client.time, 'sleep', delays.append)
    body = {'attempt': {'runtime_attempt_id': 'bound'}, 'last_receipt_cursor': 'cursor'}
    result = run.control(body)
    assert result['cancel_requested'] is cancel and run._stopping is cancel
    assert len(requests) == 3 and all(request == requests[0] for request in requests)
    assert requests[0] == ('control', body, {'run_bound': True})
    assert delays == [0.1, 0.2] and sum(delays) < 1


@pytest.mark.parametrize('error_type,expected_attempts', [(client.TaskRunClientUnavailable, 3), (client.TaskRunClientError, 1)])
def test_control_exhaustion_and_refusal_remain_fail_closed(transport, monkeypatch, error_type, expected_attempts):
    run = client.TaskRunClient()
    calls, delays = [], []
    def post(action, body, **kwargs):
        calls.append(action)
        raise error_type('refused or unavailable')
    monkeypatch.setattr(run, '_post', post)
    monkeypatch.setattr(client.time, 'sleep', delays.append)
    with pytest.raises(error_type):
        run.control({})
    assert calls == ['control'] * expected_attempts
    assert len(delays) == expected_attempts - 1
    assert not run._stopping


def test_control_retry_does_not_extend_to_model_or_cyber_operations(transport, monkeypatch):
    run = client.TaskRunClient()
    calls = []
    def post(action, body, **kwargs):
        calls.append(action)
        raise client.TaskRunClientUnavailable('unknown effect')
    monkeypatch.setattr(run, '_post', post)
    for operation in (run.model, run.cyber):
        with pytest.raises(client.TaskRunClientUnavailable):
            operation({'request_id':'same-operation'})
    assert calls == ['model', 'cyber']


def test_generic_tool_registry_is_exact_and_host_owned(monkeypatch):
    monkeypatch.setenv('ADP_TASK_TOOL_ROUTES', '{"archive.scan":"https://tools.example/dev/tools/archive"}')
    monkeypatch.setenv('ADP_AGENT_CONTROL_ENDPOINT', 'https://gateway.example/internal/v1/agent')
    # Use the existing module fixture constructor configuration.
    monkeypatch.setenv(client.CONTROL_ENDPOINT_ENV, 'https://gateway.example/internal/v1/agent')
    instance = client.TaskRunClient()
    calls = []
    monkeypatch.setattr(instance, '_post', lambda action, body, **kw: calls.append((action, body, kw)) or {'ok': True})
    body = {'operation': 'scan', 'payload': {'url': 'https://untrusted.example/other'}}
    assert instance.tool('archive.scan', body) == {'ok': True}
    assert calls[0][2]['tool_endpoint'] == 'https://tools.example/dev/tools/archive'
    with pytest.raises(client.TaskRunClientError):
        instance.tool('archive.other', body)
    with pytest.raises(client.TaskRunClientError):
        instance.tool('https://untrusted.example', body)


@pytest.mark.parametrize("action", ["repository-publication", "repository-completion"])
def test_repository_delivery_uses_real_allowlisted_signed_transport(transport, action):
    calls, proof = transport
    run = client.TaskRunClient()
    run._run_credential = "test-run-secret"
    run._post(action, {"schema_version": "1.0"}, run_bound=True)
    url, request, trust_env = calls[-1]
    assert url.endswith("/task/" + action)
    assert request["headers"]["X-Adp-Run-Credential"] == "test-run-secret"
    assert request["headers"]["X-Adp-Workload-Token"] == proof
    assert request["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert trust_env is False


def test_trace_headers_are_signed_and_context_does_not_leak(transport):
    calls, _ = transport
    run = client.TaskRunClient()
    parent = "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
    with run.trace_context(parent):
        run.bootstrap({"schema_version": "1.0", "task_id": "task-test", "invocation_id": "invocation-test"})
    run.attempt({"schema_version": "1.0"})
    headers = {key.lower(): value for key, value in calls[0][1]["headers"].items()}
    assert headers["traceparent"] == parent
    assert headers["x-amzn-trace-id"] == "Root=1-12345678-90abcdef1234567890abcdef;Parent=1234567890abcdef;Sampled=1"
    assert "traceparent" in headers["authorization"]
    assert "traceparent" not in {key.lower() for key in calls[1][1]["headers"]}
    with pytest.raises(client.TaskRunClientError):
        with run.trace_context("00-" + "0" * 32 + "-1234567890abcdef-01"):
            pytest.fail("invalid trace accepted")
