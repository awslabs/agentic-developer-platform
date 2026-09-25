"""Task run transport keeps run credentials host-side and action-scoped."""

from __future__ import annotations

import base64
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
        {"run_credential": "run-secret"},
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
    assert run.bootstrap({"schema_version": "1.0"}) == {"run_credential": "run-secret"}
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
