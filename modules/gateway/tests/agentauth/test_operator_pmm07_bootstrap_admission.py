"""Exercise actual HTTP admission with the previously shipped client request.

Transport verification, pod binding, stored authority and credential issuance
use the real route/fixtures. Only the policy evaluation result is substituted;
the property under test is whether the route issues authority to an old client
that cannot consume enforcing policy. No live AWS or inference call is made.
"""
# ruff: noqa: F811 - imported pytest fixtures are injected by name.

from unittest.mock import AsyncMock

import pytest

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.workload import WORKLOAD_HEADER
from tests.agentauth.test_bootstrap_routes import (
    http_client,
    kubernetes,  # noqa: F401
    provision,
    store,  # noqa: F401
)


@pytest.mark.parametrize(
    "policy",
    [
        {"posture": "enforcing", "posture_verified": True, "status": "proposed"},
        {"posture": "enforcing", "posture_verified": True, "status": "unavailable", "reason": "evidence_stale"},
        {"posture": None, "posture_verified": False, "status": "unavailable", "reason": "runtime_posture_unavailable"},
    ],
    ids=["enforcing-proposal", "enforcing-refusal", "unknown-posture"],
)
def test_old_client_gets_no_authority_for_enforcing_or_unknown_policy(store, kubernetes, monkeypatch, policy):
    envelope, _ = provision(store)
    client, _ = http_client(store, kubernetes, monkeypatch)
    evaluate = AsyncMock(return_value=policy)
    monkeypatch.setattr("src.agentauth.model_policy.bootstrap_model_policy_live", evaluate)
    response = client.post(
        "/internal/v1/agent/bootstrap",
        json={"invocation_id": "run-a", "envelope_digest": envelope_digest(envelope)},
        headers={"X-Caller-Identity": "registered-worker-transport", WORKLOAD_HEADER: "pod-token"},
    )
    evaluate.assert_awaited_once()
    assert response.status_code >= 400, response.json()
    assert "credential" not in response.json()


def test_old_client_keeps_report_only_unavailable_legacy_admission(store, kubernetes, monkeypatch):
    envelope, _ = provision(store)
    client, _ = http_client(store, kubernetes, monkeypatch)
    monkeypatch.setattr(
        "src.agentauth.model_policy.bootstrap_model_policy_live",
        AsyncMock(return_value={"posture": "report_only", "posture_verified": True, "status": "unavailable", "reason": "evidence_stale"}),
    )
    response = client.post(
        "/internal/v1/agent/bootstrap",
        json={"invocation_id": "run-a", "envelope_digest": envelope_digest(envelope)},
        headers={"X-Caller-Identity": "registered-worker-transport", WORKLOAD_HEADER: "pod-token"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["credential"].startswith("adpr1.")
