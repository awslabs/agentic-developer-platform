"""Sandbox model decisions use the same protected launch and live lease as data tools."""

import hashlib
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.concurrency import run_in_threadpool

from src.agentauth import chat_model
from src.agentauth.envelope import SIGNING_KEY_ENV, SIGNING_KEY_ID_ENV
from src.agentauth.workload import VerifiedPod, WorkloadRefusedError
from tests.agentauth.test_chat_data_routes import admit, exchange
from tests.agentauth.test_chat_data_routes import client as client_fixture
from tests.agentauth.test_chat_data_routes import runtime as runtime_fixture
from tests.agentauth.test_chat_data_routes import store as store_fixture
from tests.agentauth.test_chat_data_routes import sts as sts_fixture

client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
DECISION = "/v1/chat/model/decision"


@pytest.fixture
async def sandbox_model(client, runtime, monkeypatch):
    authority = runtime[1]
    run_hash = hashlib.sha256(b"run-a").hexdigest()
    pod_name = f"chat-turn-{run_hash[:12]}-abcde"
    sandbox = VerifiedPod(
        "chat-pod",
        pod_name,
        "adp-gateway-agents",
        "adp-chat-sandbox",
        "127.0.0.1",
        image_digest=runtime[5].image_digest,
        run_hash=run_hash,
    )
    state = {"pod": sandbox}

    class Workloads:
        def verify(self, token):
            if token != "sandbox-token":
                raise WorkloadRefusedError("sandbox token refused")
            return state["pod"]

        def verify_bound(self, *, name, uid):
            if name != sandbox.name or uid != sandbox.uid:
                raise WorkloadRefusedError("sandbox binding refused")
            return state["pod"]

    monkeypatch.setattr(authority, "workloads", Workloads())
    client.headers["X-Adp-Workload-Token"] = "sandbox-token"
    admitted = await admit(client, runtime, pod_name=pod_name)
    assert admitted.status_code == 200, admitted.text
    exchanged = await exchange(client, **{"X-Adp-Workload-Token": "sandbox-token"})
    assert exchanged.status_code == 200, exchanged.text
    policy = AsyncMock(
        return_value={
            "result": {
                "nonce": "a" * 64,
                "context": {"lease_generation": 1},
                "model_policy": {
                    "posture": "enforcing",
                    "posture_verified": True,
                    "status": "proposed",
                    "decision": {
                        "runtime_posture": "enforcing",
                        "invocation_id": "run-a",
                        "tenant_id": "tenant",
                        "principal_kind": "human",
                        "principal_id": "human",
                        "resolved_model_id": "global.anthropic.claude-sonnet-5",
                    },
                    "assertion": "synthetic-decision-assertion",
                },
            },
            "assertion": "synthetic-response-assertion",
        }
    )
    monkeypatch.setattr(chat_model, "resolved_model_response", policy)
    return client, runtime, exchanged.json()["capability"], policy, state


async def decision(client, capability, *, workload_token="sandbox-token", **changes):
    return await client.post(
        DECISION,
        json={"run_id": "run-a", "session_id": "session-a", "nonce": "a" * 64, "model_policy_contract": 1, **changes},
        headers={"Authorization": f"Bearer {capability}", "X-Adp-Workload-Token": workload_token},
    )


async def test_sandbox_decision_uses_protected_execution_and_grant(sandbox_model):
    client, runtime, token, policy, _ = sandbox_model
    response = await decision(client, token)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["result"]["nonce"] == "a" * 64
    policy.assert_awaited_once()
    request = policy.await_args.kwargs
    assert request["record"].invocation_id == "run-a"
    assert request["record"].tenant_id == "tenant"
    assert request["grant"].grant_id == runtime[5].grant_id
    assert request["runtime"].store is runtime[1].store
    assert request["nonce"] == "a" * 64
    assert request["client_contract"] == 1
    assert request["response_context"] == {"lease_generation": 1}


async def test_verified_pod_with_same_uid_but_other_run_cannot_refresh_or_use_model(sandbox_model):
    client, _, token, policy, state = sandbox_model
    original = state["pod"]
    other_hash = hashlib.sha256(b"run-other").hexdigest()
    state["pod"] = VerifiedPod(
        original.uid,
        f"chat-turn-{other_hash[:12]}-abcde",
        original.namespace,
        original.service_account,
        original.ip,
        image_digest=original.image_digest,
        run_hash=other_hash,
    )
    refreshed = await exchange(client, **{"X-Adp-Workload-Token": "sandbox-token"})
    assert refreshed.status_code == 404
    assert (await decision(client, token)).status_code == 404
    policy.assert_not_awaited()


async def test_second_session_lease_keeps_run_attempt_distinct(sandbox_model):
    client, runtime, _, policy, state = sandbox_model
    capabilities = runtime[0]
    launch = capabilities.launches.load("run-a").model_copy(update={"lease_generation": 2})
    capabilities.launches.store.client.put_item(TableName=capabilities.launches.store.table, Item=capabilities.launches.item(launch))
    runtime[2].update_item(
        Key={"PK": "session#session-a", "SK": "header"},
        UpdateExpression="SET chatLease.generation = :generation",
        ExpressionAttributeValues={":generation": 2},
    )
    token = await run_in_threadpool(capabilities.issue, "run-a", state["pod"], now=runtime[-1])
    policy.return_value["result"]["context"]["lease_generation"] = 2
    response = await decision(client, token)
    assert response.status_code == 200, response.text
    assert policy.await_args.kwargs["record"].current_attempt == 1
    assert policy.await_args.kwargs["response_context"] == {"lease_generation": 2}
    exchanged = await exchange(client, **{"X-Adp-Workload-Token": "sandbox-token"})
    assert exchanged.status_code == 200, exchanged.text
    assert (exchanged.json()["attempt"], exchanged.json()["lease_generation"]) == (1, 2)


@pytest.mark.parametrize("forgery", [{"run_id": "run-other"}, {"session_id": "session-other"}, {"user_id": "another"}])
async def test_sandbox_decision_refuses_forged_scope(sandbox_model, forgery):
    client, _, token, policy, _ = sandbox_model
    response = await decision(client, token, **forgery)
    assert response.status_code == (422 if "user_id" in forgery else 404), response.text
    policy.assert_not_awaited()


@pytest.mark.parametrize("change", ["service_account", "uid", "image_digest"])
async def test_sandbox_decision_refuses_substituted_workload(sandbox_model, change):
    client, _, token, policy, state = sandbox_model
    original = state["pod"]
    values = {"service_account": "adp-agent", "uid": "other-pod", "image_digest": "sha256:" + "b" * 64}
    state["pod"] = VerifiedPod(
        values["uid"] if change == "uid" else original.uid,
        original.name,
        original.namespace,
        values["service_account"] if change == "service_account" else original.service_account,
        original.ip,
        image_digest=values["image_digest"] if change == "image_digest" else original.image_digest,
    )
    response = await decision(client, token)
    assert response.status_code == 404, response.text
    policy.assert_not_awaited()


async def test_sandbox_decision_rejects_stale_lease(sandbox_model):
    client, runtime, token, policy, _ = sandbox_model
    runtime[2].update_item(
        Key={"PK": "session#session-a", "SK": "header"},
        UpdateExpression="SET chatLease.generation = :generation",
        ExpressionAttributeValues={":generation": 2},
    )
    response = await decision(client, token)
    assert response.status_code == 404
    policy.assert_not_awaited()


@pytest.mark.parametrize("field,value", [("chatLease", 2), ("status", "ended")])
async def test_sandbox_decision_refuses_authority_lost_during_policy_lookup(sandbox_model, field, value):
    client, runtime, token, policy, _ = sandbox_model
    reply = policy.return_value

    async def replace_during_lookup(**_):
        runtime[2].update_item(
            Key={"PK": "session#session-a", "SK": "header"},
            UpdateExpression="SET #field.generation = :value" if field == "chatLease" else "SET #field = :value",
            ExpressionAttributeNames={"#field": field},
            ExpressionAttributeValues={":value": value},
        )
        return reply

    policy.side_effect = replace_during_lookup
    response = await decision(client, token)
    assert response.status_code == 404, response.text
    assert response.headers["cache-control"] == "no-store"
    policy.assert_awaited_once()


async def test_sandbox_decision_rejects_unsigned_lease_context(sandbox_model):
    client, _, token, policy, _ = sandbox_model
    policy.return_value["result"]["context"] = {"lease_generation": 2}
    response = await decision(client, token)
    assert response.status_code == 503
    assert response.json()["detail"] == {"error": "chat_authority_unavailable"}


async def test_sandbox_decision_rejects_missing_workload_proof(sandbox_model):
    client, _, token, policy, _ = sandbox_model
    response = await decision(client, token, workload_token="invalid")
    assert response.status_code == 404
    policy.assert_not_awaited()


async def test_sandbox_decision_requires_model_operation(sandbox_model):
    client, runtime, _, policy, state = sandbox_model
    capabilities = runtime[0]
    launch = capabilities.launches.load("run-a")
    restricted = launch.model_copy(update={"operations": frozenset({"history.read"})})
    capabilities.launches.store.client.update_item(
        TableName=capabilities.launches.store.table,
        Key={"pk": {"S": "CHAT-LAUNCH#run-a"}, "sk": {"S": "LAUNCH"}},
        UpdateExpression="SET document = :document",
        ExpressionAttributeValues={":document": {"S": restricted.model_dump_json()}},
    )
    token = await run_in_threadpool(capabilities.issue, "run-a", state["pod"], now=runtime[-1])
    response = await decision(client, token)
    assert response.status_code == 404
    policy.assert_not_awaited()


@pytest.mark.parametrize(
    "path,value",
    [
        (("posture",), "report_only"),
        (("posture_verified",), False),
        (("status",), "unavailable"),
        (("decision", "runtime_posture"), "report_only"),
        (("decision", "principal_kind"), "service_account"),
        (("decision", "principal_id"), "other-human"),
        (("decision", "tenant_id"), "other-tenant"),
        (("decision", "resolved_model_id"), ""),
        (("assertion",), ""),
    ],
)
async def test_sandbox_decision_never_falls_back_to_legacy_policy(sandbox_model, path, value):
    client, _, token, policy, _ = sandbox_model
    response = deepcopy(policy.return_value)
    target = response["result"]["model_policy"]
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    policy.return_value = response
    refused = await decision(client, token)
    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"] == {"error": "chat_authority_unavailable"}
    policy.assert_awaited_once()


async def test_sandbox_only_discovers_public_model_verification_keys(sandbox_model, monkeypatch):
    client, _, token, _, _ = sandbox_model
    signing_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(SIGNING_KEY_ID_ENV, "synthetic-model-key")
    monkeypatch.setenv(
        SIGNING_KEY_ENV,
        signing_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
    )
    headers = {"Authorization": f"Bearer {token}", "X-Adp-Workload-Token": "sandbox-token"}
    response = await client.post("/v1/chat/model/keys", json={"run_id": "run-a", "session_id": "session-a"}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "keys": {
            "synthetic-model-key": signing_key.public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode()
        }
    }
    assert "PRIVATE KEY" not in response.text
    for body, request_headers in (
        ({"run_id": "run-a", "session_id": "another-session"}, headers),
        ({"run_id": "another-run", "session_id": "session-a"}, headers),
        ({"run_id": "run-a", "session_id": "session-a"}, {**headers, "X-Adp-Workload-Token": "invalid"}),
    ):
        refused = await client.post("/v1/chat/model/keys", json=body, headers=request_headers)
        assert refused.status_code == 404
