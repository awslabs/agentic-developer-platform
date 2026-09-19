"""Chat ingress -> immutable root -> actual TokenReview -> signed SDK admission."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import update

from src.agentauth import chat_model
from src.agentauth.bootstrap import envelope_digest
from src.agentauth.envelope import MODEL_POLICY_AUDIENCE, SIGNING_KEY_ENV, SIGNING_KEY_ID_ENV, verify_envelope
from src.agentauth.model_policy import canonical_json
from src.agentauth.routes import AgentRuntime
from src.agentauth.runtime_posture import reset_posture_cache
from src.agentauth.workload import BOOTSTRAP_AUDIENCE, KubernetesWorkloadVerifier
from src.shared.models.persona_models import PersonaModelPolicySetting, PersonaModelPreference
from tests.agentauth.test_external_roots import body, post
from tests.agentauth.test_external_roots import root_client as root_client_fixture
from tests.agentauth.test_external_roots import store as store_fixture
from tests.agentauth.test_external_roots import sts as sts_fixture

root_client = root_client_fixture
store = store_fixture
sts = sts_fixture


@pytest.fixture
async def chat_context(root_client, store, report_only_db, db_session, tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    env = {
        SIGNING_KEY_ID_ENV: "chat-test",
        SIGNING_KEY_ENV: key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
    }
    token_path = tmp_path / "gateway-token"
    token_path.write_text("gateway-token")
    digest = "sha256:" + "a" * 64
    state = {"uid": "chat-pod", "image": digest, "flag": "true"}

    def kube(request):
        if request.url.path.endswith("/tokenreviews"):
            assert json.loads(request.content)["spec"] == {"token": "chat-token", "audiences": [BOOTSTRAP_AUDIENCE]}
            return httpx.Response(
                201,
                json={
                    "status": {
                        "authenticated": True,
                        "audiences": [BOOTSTRAP_AUDIENCE],
                        "user": {
                            "username": "system:serviceaccount:adp-gateway-agents:adp-agent",
                            "extra": {
                                "authentication.kubernetes.io/pod-name": ["chat-a"],
                                "authentication.kubernetes.io/pod-uid": [state["uid"]],
                            },
                        },
                    }
                },
            )
        assert request.url.path == "/api/v1/namespaces/adp-gateway-agents/pods/chat-a"
        return httpx.Response(
            200,
            json={
                "metadata": {"uid": state["uid"], "name": "chat-a", "namespace": "adp-gateway-agents"},
                "spec": {
                    "serviceAccountName": "adp-agent",
                    "containers": [
                        {
                            "name": "chat-agent",
                            "env": [
                                {"name": "ADP_CHAT_MODEL_POLICY_ENABLED", "value": state["flag"]},
                            ],
                        }
                    ],
                },
                "status": {
                    "phase": "Running",
                    "podIP": "10.0.0.5",
                    "containerStatuses": [
                        {
                            "name": "chat-agent",
                            "imageID": f"registry/chat@{state['image']}",
                            "state": {"running": {}},
                        }
                    ],
                },
            },
        )

    verifier = KubernetesWorkloadVerifier(
        client=httpx.Client(base_url="https://kubernetes.test", transport=httpx.MockTransport(kube)),
        image_digests=frozenset({digest}),
        namespace="adp-gateway-agents",
        service_account="adp-agent",
        container_name="chat-agent",
        authority_flag="ADP_CHAT_MODEL_POLICY_ENABLED",
        gateway_token_path=token_path,
    )
    runtime = AgentRuntime(store=store, workloads=verifier, env=env)
    root_client.gateway_app.include_router(chat_model.router)
    root_client.gateway_app.dependency_overrides[chat_model.chat_runtime] = lambda: runtime
    monkeypatch.setattr("src.agentauth.routes.verify_internal_or_irsa", AsyncMock())
    from src.proxy.bedrock_routing import BedrockTarget
    from tests.agentauth.test_model_policy import SONNET, _add_invocability_evidence

    db_session.add(
        PersonaModelPreference(
            org_id="tenant",
            principal_kind="human",
            principal_source="self",
            principal_id="human",
            persona_key="developer",
            canonical_model_id=SONNET,
            revision=1,
            updated_by="human",
            updated_by_source="self",
        )
    )
    _add_invocability_evidence(db_session, account_id="111111111111", outcome="proven", expires_at=datetime.now(UTC) + timedelta(hours=1))
    await db_session.commit()
    monkeypatch.setattr(
        "src.proxy.bedrock_routing.bedrock_routing_resolver.resolve",
        AsyncMock(return_value=BedrockTarget(account_id="111111111111", region="us-east-1", rung="user")),
    )
    document = body()
    registered = await post(root_client, document)
    assert registered.status_code == 200, registered.text
    final = registered.json()["envelope"]
    return (
        root_client,
        state,
        key.public_key(),
        {"invocation_id": "root-a", "envelope_digest": envelope_digest(final), "nonce": "a" * 64, "model_policy_contract": 1},
    )


async def decide(context, **changes):
    client, _, _, request = context
    return await client.post(
        "/internal/v1/agent/chat/model-decision",
        json={**request, **changes},
        headers={"X-Caller-Identity": "chat-role", "X-Adp-Workload-Token": "chat-token"},
    )


async def test_chat_launch_has_signed_binding_and_observes_later_enforcement(chat_context, db_session):
    response = await decide(chat_context)
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["result"]["model_policy"]["posture"] == "report_only"
    verified = verify_envelope(
        document["assertion"],
        public_keys={"chat-test": chat_context[2]},
        expected_run_id="root-a",
        expected_generation=1,
        expected_action="model_policy_response",
        expected_command_id="a" * 64,
        expected_audience=MODEL_POLICY_AUDIENCE,
        request_body=canonical_json(document["result"]),
    )
    assert verified.tenant_id == "tenant"
    await db_session.execute(update(PersonaModelPolicySetting).values(enforcement_posture="enforcing", posture_revision=2))
    await db_session.commit()
    reset_posture_cache()
    enforcing = await decide(chat_context, nonce="b" * 64)
    assert enforcing.status_code == 200, enforcing.text
    from tests.agentauth.test_model_policy import SONNET

    assert enforcing.json()["result"]["model_policy"]["decision"]["resolved_model_id"] == SONNET
    assert enforcing.json()["result"]["model_policy"]["posture"] == "enforcing"


@pytest.mark.parametrize("change", [{"flag": "false"}, {"image": "sha256:" + "b" * 64}])
async def test_unapproved_chat_runtime_cannot_obtain_a_decision(chat_context, change):
    chat_context[1].update(change)
    assert (await decide(chat_context)).status_code == 404


async def test_other_pod_cannot_reuse_a_bound_chat_root(chat_context):
    assert (await decide(chat_context)).status_code == 200
    chat_context[1]["uid"] = "other-chat-pod"
    assert (await decide(chat_context)).status_code == 404


async def test_queue_tampering_or_caller_selected_persona_never_gets_a_decision(chat_context):
    assert (await decide(chat_context, envelope_digest="b" * 64)).status_code == 404
    assert (await decide(chat_context, persona="operations")).status_code == 422
