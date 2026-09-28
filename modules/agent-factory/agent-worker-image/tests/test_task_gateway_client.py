"""Protected task acquisition never sends a worker-selected task or SQS receipt."""

import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.credentials import Credentials

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import task_gateway_client as client


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv(
        "ADP_AGENT_CONTROL_ENDPOINT",
        "https://gateway.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent",
    )
    monkeypatch.setattr(
        "adp_trigger.transport_identity.worker_credentials",
        lambda _: Credentials("platform-test", "test-secret", "test-session"),
    )
    proofs = ["pod-one"]
    monkeypatch.setattr(client, "read_workload_token", lambda: proofs[0])
    response = Mock(status_code=200)
    response.raw.read.return_value = b'{"body":"own-task"}'
    calls = []

    class ResponseContext:
        def __enter__(self):
            return response

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
            return ResponseContext()

    monkeypatch.setattr(client.requests, "Session", Session)
    return proofs, response, calls


def test_prebootstrap_request_uses_fresh_workload_proof_and_empty_body(transport):
    proofs, _, calls = transport
    assert client.own_task() == "own-task"
    proofs[0] = "pod-two"
    assert client.own_task() == "own-task"
    for index, (url, args, trust_env) in enumerate(calls):
        assert url.endswith("/internal/v1/agent/task/acquire")
        assert args["data"] == b"{}"
        assert args["headers"]["X-Adp-Workload-Token"] == ["pod-one", "pod-two"][index]
        assert "platform-test" in args["headers"]["Authorization"]
        assert "X-Adp-Run-Credential" not in args["headers"]
        assert args["allow_redirects"] is False
        assert trust_env is False


@pytest.mark.parametrize("operation", ["heartbeat_task", "acknowledge_task"])
def test_maintenance_has_no_caller_selected_queue_or_receipt(transport, operation):
    _, response, calls = transport
    response.raw.read.return_value = b'{"accepted":true}'
    getattr(client, operation)()
    assert json.loads(calls[0][1]["data"]) == {}


@pytest.mark.parametrize(
    "response_body",
    [
        {"body": {}, "receipt": "victim"},
        {"body": 123},
        {"body": "task", "receipt_handle": "secret"},
    ],
)
def test_unexpected_task_contract_is_refused(transport, response_body):
    transport[1].raw.read.return_value = json.dumps(response_body).encode()
    with pytest.raises(client.TaskGatewayError, match="invalid task response"):
        client.own_task()


def test_gateway_refusal_never_falls_back_to_sqs(transport, monkeypatch):
    import entrypoint

    transport[1].status_code = 403
    direct = Mock(side_effect=AssertionError("protected worker must not use SQS"))
    monkeypatch.setattr(entrypoint.boto3, "client", direct)
    with pytest.raises(client.TaskGatewayError, match="refused"):
        entrypoint._receive_one_message("victim-queue", "us-east-1")
    with pytest.raises(client.TaskGatewayError, match="refused"):
        entrypoint._delete_message("victim-queue", "us-east-1", "victim-receipt")
    direct.assert_not_called()


def test_initial_workload_refusal_retries_with_fresh_proof(transport, monkeypatch):
    proofs, response, calls = transport
    response.status_code = 404

    def pod_becomes_visible(_seconds):
        proofs[0] = "published-pod"
        response.status_code = 200

    monkeypatch.setattr(client.time, "sleep", pod_becomes_visible)
    assert client.own_task() == "own-task"
    assert len(calls) == 2
    assert [args["headers"]["X-Adp-Workload-Token"] for _, args, _ in calls] == [
        "pod-one", "published-pod"
    ]


def test_permanent_workload_refusal_is_bounded(transport, monkeypatch):
    _, response, calls = transport
    response.status_code = 404
    sleeps = Mock()
    monkeypatch.setattr(client.time, "sleep", sleeps)
    with pytest.raises(client.TaskGatewayError, match="workload unavailable"):
        client.own_task()
    assert len(calls) == 5
    assert sleeps.call_count == 4


@pytest.mark.parametrize("operation", ["heartbeat_task", "acknowledge_task"])
def test_lost_workload_during_maintenance_is_not_a_startup_retry(transport, monkeypatch, operation):
    _, response, calls = transport
    response.status_code = 404
    sleeps = Mock(side_effect=AssertionError("maintenance must not retry a refusal"))
    monkeypatch.setattr(client.time, "sleep", sleeps)
    with pytest.raises(client.TaskGatewayError, match="refused"):
        getattr(client, operation)()
    assert len(calls) == 1


def test_protected_main_stops_early_heartbeat_on_startup_failure(transport, monkeypatch):
    import entrypoint

    heartbeat = Mock()
    monkeypatch.setattr(entrypoint, "VisibilityHeartbeat", Mock(return_value=heartbeat))
    inner = Mock(side_effect=RuntimeError("startup refused"))
    monkeypatch.setattr(entrypoint, "_main", inner)
    with pytest.raises(RuntimeError, match="startup refused"):
        entrypoint.main()
    inner.assert_called_once_with(task_heartbeat=heartbeat)
    heartbeat.stop.assert_called_once()
