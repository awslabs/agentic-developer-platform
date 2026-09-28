"""Actual HTTP admission, durable posture and signed SDK launch responses."""

import asyncio

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import update

from src.agentauth.adapter import CREDENTIAL_HEADER
from src.agentauth.bootstrap import envelope_digest
from src.agentauth.envelope import MODEL_POLICY_AUDIENCE, SIGNING_KEY_ENV, SIGNING_KEY_ID_ENV, verify_envelope
from src.agentauth.model_policy import canonical_json
from src.agentauth.runtime_posture import reset_posture_cache
from src.agentauth.workload import WORKLOAD_HEADER
from src.shared.models.persona_models import PersonaModelPolicySetting
from tests.agentauth.test_bootstrap_routes import ENV, http_client, kubernetes, provision, store  # noqa: F401


@pytest.fixture
def sdk_client(store, kubernetes, monkeypatch):  # noqa: F811
    key = Ed25519PrivateKey.generate()
    monkeypatch.setitem(ENV, SIGNING_KEY_ID_ENV, "sdk-test")
    monkeypatch.setitem(
        ENV, SIGNING_KEY_ENV, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    )
    client, _ = http_client(store, kubernetes, monkeypatch)
    envelope, _ = provision(store)
    headers = {"X-Caller-Identity": "worker", WORKLOAD_HEADER: "pod-token", "X-Adp-Model-Policy-Contract": "1"}
    response = client.post(
        "/internal/v1/agent/bootstrap", headers=headers, json={"invocation_id": "run-a", "envelope_digest": envelope_digest(envelope)}
    )
    assert response.status_code == 200, response.text
    headers[CREDENTIAL_HEADER] = response.json()["credential"]
    return client, headers, key.public_key(), response.json()["attempt"]


def test_fresh_sdk_response_signs_unavailable_proposal_and_observes_enforcement(sdk_client):
    client, headers, key, attempt = sdk_client
    nonce = "a" * 64
    response = client.post("/internal/v1/agent/model-decision", headers=headers, json={"nonce": nonce, "model_policy_contract": 1})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    document = response.json()
    policy = document["result"]["model_policy"]
    assert policy["posture"] == "report_only"
    assert policy["status"] == "unavailable"
    verified = verify_envelope(
        document["assertion"],
        public_keys={"sdk-test": key},
        expected_run_id="run-a",
        expected_generation=attempt,
        expected_action="model_policy_response",
        expected_command_id=nonce,
        request_body=canonical_json(document["result"]),
        expected_audience=MODEL_POLICY_AUDIENCE,
    )
    assert verified.tenant_id == "tenant"
    assert verified.principal == f"run-a#{attempt}"

    async def enforce():
        async with client.posture_sessions() as session:
            await session.execute(update(PersonaModelPolicySetting).values(enforcement_posture="enforcing", posture_revision=2))
            await session.commit()

    asyncio.run(enforce())
    reset_posture_cache()
    refused = client.post("/internal/v1/agent/model-decision", headers=headers, json={"nonce": "b" * 64, "model_policy_contract": 1})
    assert refused.status_code == 409
    assert "assertion" not in refused.json()


@pytest.mark.parametrize("header", [CREDENTIAL_HEADER, WORKLOAD_HEADER])
def test_sdk_launch_cannot_use_a_foreign_credential_or_workload(sdk_client, header, kubernetes):  # noqa: F811
    client, headers, _, _ = sdk_client
    if header == WORKLOAD_HEADER:
        kubernetes[1]["uid"] = "another-pod"
    else:
        headers = {**headers, header: "not-this-execution"}
    response = client.post("/internal/v1/agent/model-decision", headers=headers, json={"nonce": "c" * 64, "model_policy_contract": 1})
    assert response.status_code == 404
    assert "assertion" not in response.json()


def test_worker_cannot_supply_persona_or_posture_to_sdk_admission(sdk_client):
    client, headers, _, _ = sdk_client
    response = client.post(
        "/internal/v1/agent/model-decision",
        headers=headers,
        json={"nonce": "d" * 64, "model_policy_contract": 1, "posture": "disabled", "persona": "operations"},
    )
    assert response.status_code == 422
