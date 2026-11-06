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
    # The fixture models an enforcing decision; the tests below describe the
    # fail-closed path, so they run under the enforced posture explicitly (#7997).
    monkeypatch.setenv("BG_CHAT_MODEL_POLICY_POSTURE", "enforced")
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
    assert request["chat_posture"] == "enforced"


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
    assert response.json()["detail"] == {
        "error": "chat_model_policy_unavailable",
        "reason": "decision_unbound",
        "persona": "developer",
        "model_id": "global.anthropic.claude-sonnet-5",
        "retryable": False,
    }


def unavailable_policy(reason, *, posture="report_only", verified=True):
    """The shape ``bootstrap_model_policy_live`` returns when the live decision is rejected."""
    policy = {"posture": posture if verified else None, "posture_verified": verified, "status": "unavailable", "reason": reason}
    return {"result": {"nonce": "a" * 64, "context": {"lease_generation": 1}, "model_policy": policy}, "assertion": "synthetic-response-assertion"}


@pytest.mark.parametrize("reason", ["probing_disabled", "evidence_stale", "not_permitted", "not_invocable"])
async def test_rejected_live_decision_names_its_policy_reason_without_a_retry_hint(sandbox_model, reason, caplog):
    """AC-01: a refused decision is an expected policy outcome with a structured, reasoned 503."""
    client, _, token, policy, _ = sandbox_model
    policy.return_value = unavailable_policy(reason)
    with caplog.at_level("WARNING", logger="src.agentauth.chat_model"):
        refused = await decision(client, token)
    assert refused.status_code == 503, refused.text
    assert refused.headers["cache-control"] == "no-store"
    assert refused.json()["detail"] == {
        "error": "chat_model_policy_unavailable",
        "reason": reason,
        "persona": "developer",
        "model_id": None,
        "retryable": False,
    }
    warnings = [record for record in caplog.records if record.getMessage() == "Chat model policy decision unavailable"]
    assert len(warnings) == 1 and warnings[0].reason == reason and warnings[0].persona == "developer"
    assert "sandbox-token" not in caplog.text and token not in caplog.text


@pytest.mark.parametrize("reason", ["runtime_posture_unavailable", "model_validation_unavailable", "decision_unavailable"])
async def test_transient_decision_failure_invites_retry(sandbox_model, reason):
    """AC-02: a store or verifier outage during the live decision carries a retry hint."""
    client, _, token, policy, _ = sandbox_model
    policy.return_value = unavailable_policy(reason, verified=False)
    refused = await decision(client, token)
    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"] == {
        "error": "chat_model_policy_unavailable",
        "reason": reason,
        "persona": "developer",
        "model_id": None,
        "retryable": True,
    }


async def test_authority_store_outage_is_a_retryable_policy_unavailability(sandbox_model):
    """AC-02: the protected store failing while the decision is produced is reported the same way, retryable."""
    from src.agentauth.store import AuthorityStoreError

    client, _, token, policy, _ = sandbox_model
    policy.side_effect = AuthorityStoreError("store unavailable")
    refused = await decision(client, token)
    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"] == {
        "error": "chat_model_policy_unavailable",
        "reason": "authority_store_unavailable",
        "persona": "developer",
        "model_id": None,
        "retryable": True,
    }


async def test_withheld_enforcing_policy_reports_its_reason_instead_of_an_anonymous_conflict(sandbox_model):
    """The worker route's 409 stays a 409 for workers; the sandbox gets the reason it must show."""
    from src.agentauth.routes import ModelPolicyWithheldError

    client, _, token, policy, _ = sandbox_model
    policy.side_effect = ModelPolicyWithheldError("model policy unavailable", reason="snapshot_missing")
    refused = await decision(client, token)
    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"] == {
        "error": "chat_model_policy_unavailable",
        "reason": "snapshot_missing",
        "persona": "developer",
        "model_id": None,
        "retryable": False,
    }


async def test_unavailable_policy_reason_is_never_free_text(sandbox_model):
    client, _, token, policy, _ = sandbox_model
    policy.return_value = unavailable_policy("Traceback: psycopg2.OperationalError at host db-internal")
    refused = await decision(client, token)
    assert refused.status_code == 503
    assert refused.json()["detail"]["reason"] == "policy_unavailable"
    assert "psycopg2" not in refused.text


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
    "path,value,reason",
    [
        (("posture",), "report_only", "posture_not_enforcing"),
        (("posture_verified",), False, "posture_unverified"),
        (("status",), "unavailable", "policy_unavailable"),
        (("decision", "runtime_posture"), "report_only", "posture_not_enforcing"),
        (("decision", "principal_kind"), "service_account", "principal_mismatch"),
        (("decision", "principal_id"), "other-human", "principal_mismatch"),
        (("decision", "tenant_id"), "other-tenant", "decision_unbound"),
        (("decision", "invocation_id"), "other-run", "decision_unbound"),
        (("decision", "resolved_model_id"), "", "model_unresolved"),
        (("assertion",), "", "assertion_missing"),
    ],
)
async def test_sandbox_decision_never_falls_back_to_legacy_policy(sandbox_model, path, value, reason):
    client, _, token, policy, _ = sandbox_model
    response = deepcopy(policy.return_value)
    target = response["result"]["model_policy"]
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    policy.return_value = response
    refused = await decision(client, token)
    assert refused.status_code == 503, refused.text
    detail = refused.json()["detail"]
    assert detail["error"] == "chat_model_policy_unavailable" and detail["reason"] == reason and detail["retryable"] is False
    assert detail["persona"] == "developer"
    policy.assert_awaited_once()


@pytest.mark.parametrize("posture", ["enforced", "advisory"])
async def test_a_decision_bound_to_another_human_is_refused_as_principal_mismatch_with_both_ids(sandbox_model, monkeypatch, posture, caplog):
    """#8010: the model-policy snapshot bound the principal to a different users row
    (on dev, the tenant-local duplicate of the admitted human). Only the principal
    differs, so the 503 must say so rather than hide behind ``decision_unbound``, and
    the gateway warning must name both ids so the drift is diagnosable from logs."""
    client, _, token, policy, _ = sandbox_model
    monkeypatch.setenv("BG_CHAT_MODEL_POLICY_POSTURE", posture)
    response = deepcopy(policy.return_value) if posture == "enforced" else advisory_policy("advisory", rejection_reason="probing_disabled")
    response["result"]["model_policy"]["decision"]["principal_id"] = "1fb07e6f-duplicate-row"
    policy.return_value = response
    with caplog.at_level("WARNING", logger="src.agentauth.chat_model"):
        refused = await decision(client, token)
    assert refused.status_code == 503, refused.text
    detail = refused.json()["detail"]
    assert detail["reason"] == "principal_mismatch" and detail["retryable"] is False
    assert detail["model_id"] == "global.anthropic.claude-sonnet-5"
    warnings = [record for record in caplog.records if record.getMessage() == "Chat model policy decision unavailable"]
    assert len(warnings) == 1
    assert warnings[0].reason == "principal_mismatch"
    assert warnings[0].decision_principal_id == "1fb07e6f-duplicate-row" and warnings[0].launch_user_id == "human"
    assert "sandbox-token" not in caplog.text and token not in caplog.text


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


def advisory_policy(posture, *, rejection_reason=None, verified=None):
    """The shape ``bootstrap_model_policy_live`` returns for a chat posture of advisory or off (#7997)."""
    runtime = {"advisory": "report_only", "off": "disabled"}[posture]
    policy = {
        "posture": posture,
        "posture_verified": (rejection_reason is None) if verified is None else verified,
        "status": "proposed",
        "decision": {
            "runtime_posture": runtime,
            "invocation_id": "run-a",
            "tenant_id": "tenant",
            "principal_kind": "human",
            "principal_id": "human",
            "persona": "developer",
            "resolved_model_id": "global.anthropic.claude-sonnet-5",
        },
        "assertion": "synthetic-decision-assertion",
    }
    if rejection_reason is not None:
        policy["rejection_reason"] = rejection_reason
    return {"result": {"nonce": "a" * 64, "context": {"lease_generation": 1}, "model_policy": policy}, "assertion": "synthetic-response-assertion"}


def test_chat_posture_defaults_to_advisory_and_fails_closed_when_unreadable(monkeypatch):
    from src.agentauth import model_policy

    monkeypatch.delenv("BG_CHAT_MODEL_POLICY_POSTURE", raising=False)
    assert model_policy.chat_model_policy_posture() == "advisory"
    monkeypatch.setenv("BG_CHAT_MODEL_POLICY_POSTURE", "enforced")
    assert model_policy.chat_model_policy_posture() == "enforced"
    monkeypatch.setenv("BG_CHAT_MODEL_POLICY_POSTURE", "permissive")
    assert model_policy.chat_model_policy_posture() == "enforced", "an unrecognised value must not relax the posture"


@pytest.mark.parametrize("reason", ["probing_disabled", "evidence_stale", "not_invocable", "model_validation_mismatch"])
async def test_advisory_posture_answers_on_the_resolved_model_despite_rejected_evidence(sandbox_model, monkeypatch, reason, caplog):
    """AC-01 (#7997): no invocability evidence, posture advisory -> 200 with posture advisory, reply delivered."""
    client, _, token, policy, _ = sandbox_model
    monkeypatch.setenv("BG_CHAT_MODEL_POLICY_POSTURE", "advisory")
    policy.return_value = advisory_policy("advisory", rejection_reason=reason)
    with caplog.at_level("WARNING", logger="src.agentauth.chat_model"):
        response = await decision(client, token)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()["result"]["model_policy"]
    assert body["posture"] == "advisory" and body["posture_verified"] is False and body["rejection_reason"] == reason
    assert body["decision"]["resolved_model_id"] == "global.anthropic.claude-sonnet-5"
    assert response.json()["assertion"] == "synthetic-response-assertion", "the advisory decision is signed like any other"
    assert policy.await_args.kwargs["chat_posture"] == "advisory"
    accepted = [record for record in caplog.records if record.getMessage() == "Chat model policy decision accepted without verified evidence"]
    assert len(accepted) == 1 and accepted[0].rejection_reason == reason and accepted[0].chat_posture == "advisory"
    assert accepted[0].model_id == "global.anthropic.claude-sonnet-5"


async def test_advisory_posture_accepts_a_verified_decision_without_a_warning(sandbox_model, monkeypatch, caplog):
    client, _, token, policy, _ = sandbox_model
    monkeypatch.setenv("BG_CHAT_MODEL_POLICY_POSTURE", "advisory")
    policy.return_value = advisory_policy("advisory")
    with caplog.at_level("WARNING", logger="src.agentauth.chat_model"):
        response = await decision(client, token)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["model_policy"]["posture_verified"] is True
    assert "accepted without verified evidence" not in caplog.text


@pytest.mark.parametrize("posture", ["advisory", "off"])
async def test_allowlist_refusal_is_refused_in_every_posture(sandbox_model, monkeypatch, posture):
    """AC-03 (#7997): a model outside the tenant allowlist is refused even when the posture is relaxed."""
    client, _, token, policy, _ = sandbox_model
    monkeypatch.setenv("BG_CHAT_MODEL_POLICY_POSTURE", posture)
    policy.return_value = unavailable_policy("not_permitted")
    refused = await decision(client, token)
    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"] == {
        "error": "chat_model_policy_unavailable",
        "reason": "not_permitted",
        "persona": "developer",
        "model_id": None,
        "retryable": False,
    }


@pytest.mark.parametrize(
    "reason,retryable", [("runtime_posture_unavailable", True), ("snapshot_missing", False), ("authority_store_unavailable", True)]
)
async def test_advisory_posture_still_reports_outages_and_structural_failures(sandbox_model, monkeypatch, reason, retryable):
    """Advisory relaxes evidence, not availability: no decision means no turn, with the same reasoned 503 (#7968)."""
    from src.agentauth.store import AuthorityStoreError

    client, _, token, policy, _ = sandbox_model
    monkeypatch.setenv("BG_CHAT_MODEL_POLICY_POSTURE", "advisory")
    if reason == "authority_store_unavailable":
        policy.side_effect = AuthorityStoreError("store unavailable")
    else:
        policy.return_value = unavailable_policy(reason, verified=False)
    refused = await decision(client, token)
    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"]["reason"] == reason and refused.json()["detail"]["retryable"] is retryable


async def test_off_posture_accepts_a_decision_produced_without_evidence(sandbox_model, monkeypatch):
    client, _, token, policy, _ = sandbox_model
    monkeypatch.setenv("BG_CHAT_MODEL_POLICY_POSTURE", "off")
    policy.return_value = advisory_policy("off")
    response = await decision(client, token)
    assert response.status_code == 200, response.text
    body = response.json()["result"]["model_policy"]
    assert body["posture"] == "off" and body["decision"]["runtime_posture"] == "disabled"
    assert policy.await_args.kwargs["chat_posture"] == "off"


@pytest.mark.parametrize(
    "posture,body_posture,runtime_posture",
    [
        ("advisory", "enforcing", "enforcing"),
        ("advisory", "off", "disabled"),
        ("advisory", "advisory", "enforcing"),
        ("off", "advisory", "report_only"),
        ("enforced", "advisory", "report_only"),
    ],
)
async def test_a_decision_produced_under_another_posture_is_not_used(sandbox_model, monkeypatch, posture, body_posture, runtime_posture):
    """The body must have been produced for the posture the route asked for; a mismatch is refused, never reinterpreted."""
    client, _, token, policy, _ = sandbox_model
    monkeypatch.setenv("BG_CHAT_MODEL_POLICY_POSTURE", posture)
    response = deepcopy(policy.return_value)
    response["result"]["model_policy"]["posture"] = body_posture
    response["result"]["model_policy"]["decision"]["runtime_posture"] = runtime_posture
    policy.return_value = response
    refused = await decision(client, token)
    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"]["reason"] == ("posture_not_enforcing" if posture == "enforced" else "posture_mismatch")


@pytest.mark.parametrize(
    "path,value,reason",
    [
        (("decision", "principal_id"), "other-human", "principal_mismatch"),
        (("decision", "tenant_id"), "other-tenant", "decision_unbound"),
        (("decision", "resolved_model_id"), "", "model_unresolved"),
        (("assertion",), "", "assertion_missing"),
        (("status",), "unavailable", "policy_unavailable"),
    ],
)
async def test_advisory_posture_keeps_every_binding_check(sandbox_model, monkeypatch, path, value, reason):
    client, _, token, policy, _ = sandbox_model
    monkeypatch.setenv("BG_CHAT_MODEL_POLICY_POSTURE", "advisory")
    response = advisory_policy("advisory", rejection_reason="probing_disabled")
    target = response["result"]["model_policy"]
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    policy.return_value = response
    refused = await decision(client, token)
    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"]["reason"] == reason
