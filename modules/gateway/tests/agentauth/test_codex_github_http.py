"""GitHub HTTP proof, signed context and durable operation wiring."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.agentauth.adapter import CREDENTIAL_HEADER
from src.agentauth.codex_github_session import router
from src.agentauth.envelope import MODEL_POLICY_AUDIENCE, verify_envelope
from src.agentauth.model_policy import canonical_json
from src.agentauth.runtime_posture import reset_posture_cache
from src.shared.models.persona_models import PersonaModelPolicySetting
from tests.agentauth.test_bootstrap_routes import ENV, kubernetes, store  # noqa: F401
from tests.agentauth.test_sdk_model_decision import sdk_client  # noqa: F401
from tests.agentauth.test_task_harness import snapshot


@pytest.fixture
def github_client(sdk_client, store, tmp_path, monkeypatch):  # noqa: F811
    client, headers, key, attempt = sdk_client
    client.app.include_router(router)

    async def seed_posture():
        async with client.posture_sessions() as session:
            session.add(
                PersonaModelPolicySetting(
                    compatibility_class="codex-sdk",
                    harness_contract_revision="0.155.1",
                    revision=1,
                    posture_revision=1,
                    enforcement_posture="report_only",
                )
            )
            await session.commit()

    asyncio.run(seed_posture())
    reset_posture_cache()
    catalogue = tmp_path / "catalogue.json"
    catalogue.write_text(json.dumps({"schemaVersion": 1, "snapshots": [snapshot("architect")]}))
    monkeypatch.setitem(ENV, "ADP_CODEX_PERSONA_CATALOG_FILE", str(catalogue))
    monkeypatch.setitem(ENV, "ADP_CODEX_GITHUB_PERSONAS", "agent-codex-architect")
    monkeypatch.setattr("src.orchestration.work_admission.worker_checkpoint", AsyncMock())
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-a"}},
        UpdateExpression="SET persona = :persona, issue_number = :issue, provider_repository_id = :repo",
        ExpressionAttributeValues={":persona": {"S": "agent-codex-architect"}, ":issue": {"N": "1"}, ":repo": {"N": "123"}},
    )
    return client, headers, key, attempt


def test_http_context_signature_and_operation_settlement(github_client):
    client, headers, key, attempt = github_client
    nonce = "a" * 64
    response = client.post("/internal/v1/agent/codex-persona-session", headers=headers, json={"nonce": nonce, "model_policy_contract": 1})
    assert response.status_code == 200, response.text
    document = response.json()
    context = document["result"]["context"]["codex_persona"]
    assert (context["persona"], context["repository"], context["repositoryId"], context["issue"]) == ("agent-codex-architect", "org/repo", "123", 1)
    assert response.headers["cache-control"] == "no-store"
    verify_envelope(
        document["assertion"],
        public_keys={"sdk-test": key},
        expected_run_id="run-a",
        expected_generation=attempt,
        expected_action="model_policy_response",
        expected_command_id=nonce,
        request_body=canonical_json(document["result"]),
        expected_audience=MODEL_POLICY_AUDIENCE,
    )
    request = {"operation_id": str(uuid4()), "request_digest": "b" * 64, "kind": "tool", "action": "claim"}
    path = "/internal/v1/agent/codex-persona-operation"
    assert client.post(path, headers=headers, json=request).json() == {"status": "admitted"}
    assert client.post(path, headers=headers, json=request).status_code == 409
    assert client.post(path, headers=headers, json={**request, "action": "settle", "result": "verified file"}).json() == {
        "status": "confirmed",
        "result": "verified file",
    }


@pytest.mark.parametrize("endpoint", ["codex-persona-session", "codex-persona-operation"])
@pytest.mark.parametrize("proof", ["transport", "credential", "workload"])
def test_http_rejects_missing_or_foreign_proofs(github_client, kubernetes, endpoint, proof):  # noqa: F811
    client, headers, _, _ = github_client
    headers = dict(headers)
    if proof == "transport":
        headers.pop("X-Caller-Identity")
    elif proof == "credential":
        headers[CREDENTIAL_HEADER] = "foreign-run"
    else:
        kubernetes[1]["uid"] = "another-pod"
    body = (
        {"nonce": "c" * 64, "model_policy_contract": 1}
        if endpoint.endswith("session")
        else {"operation_id": str(uuid4()), "request_digest": "c" * 64, "action": "claim", "kind": "model"}
    )
    response = client.post("/internal/v1/agent/" + endpoint, headers=headers, json=body)
    assert response.status_code == (403 if proof == "transport" else 404), response.text
    assert "assertion" not in response.json()
