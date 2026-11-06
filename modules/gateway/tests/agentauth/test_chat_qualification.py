"""E45 admission through ordinary-user and workload-bound HTTP interfaces."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from threading import Barrier, Lock

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth import chat_data_routes
from src.agentauth.chat_qualification import MANIFEST, MANIFEST_DIGEST
from src.agentauth.chat_qualification import digest as evidence_digest
from src.orchestration import chat_history
from src.shared.schemas.auth import TokenContext
from tests.agentauth import test_chat_history_write as fixtures
from tests.agentauth.test_chat_turn_result import cancel as revoke_run

client = fixtures.client
runtime = fixtures.runtime
store = fixtures.store
sts = fixtures.sts
capability = fixtures.capability
retained_input_table = fixtures.retained_input_table
ISSUE = "/chat/sessions/session-a/qualification"
CLAIM = "/v1/chat/data/qualification/claim"
KEY = {"PK": "session#session-a", "SK": "qualification#qualification-example"}
REQUEST = {"run_id": "run-write", "evaluation_id": "qualification-example", "manifest_digest": MANIFEST_DIGEST}
RESULT = "/v1/chat/data/qualification/result"
ACTIVE = "/v1/chat/data/qualification/active"


def result_body(binding):
    return {
        **REQUEST,
        "session_id": "session-a",
        "nonce": binding["nonce"],
        "source": "sandbox_runtime",
        "status": "completed",
        "failure": None,
        "observations": [{"probe": probe, "executed": True, "outcome": "denied", "exposure_digests": []} for probe in MANIFEST["probes"]["a1"]],
    }


async def claimed_request(client, capability):
    response = await issue(client)
    assert response.status_code == 200, response.text
    binding = response.json()
    assert (await claim(client, capability, binding)).status_code == 200
    return binding


async def collect(client, capability, body):
    return await client.post(RESULT, json=body, headers={"Authorization": f"Bearer {capability}"})


def test_manifest_preserves_all_owner_write_controls_and_three_actor_matrix():
    assert sum(len(probes) for probes in MANIFEST["probes"].values()) == 94
    for actor in ("a1", "a2", "b1"):
        probes = MANIFEST["probes"][actor]
        assert len(probes) == len(set(probes))
        assert {"session_write", "message_write", "summary_write", "artifact_write", "memory_write", "sandbox_identity"} <= set(probes)
    for actor in ("a2", "b1"):
        assert {"residue_file", "residue_process", "direct_url_read"} <= set(MANIFEST["probes"][actor])
    assert MANIFEST["limits"]["model_turns"] == 0


async def test_collector_binds_immutable_redacted_receipt_and_preserves_raw_failures(client, runtime, capability, configured):
    binding = await claimed_request(client, capability)
    body = result_body(binding)
    body["observations"][0]["outcome"] = "error"
    body["observations"][-1].update(outcome="allowed", exposure_digests=["f" * 64])
    response = await collect(client, capability, body)
    assert response.status_code == 200, response.text
    readback = await client.post(ISSUE + "/result", json=REQUEST)
    assert readback.status_code == 200, readback.text
    assert readback.headers["cache-control"] == "no-store"
    result = readback.json()
    assert result["state"] == "finished"
    receipt = result["receipt"]
    assert receipt["binding"] == binding and receipt["source"] == "trusted_runtime"
    assert receipt["observations"] == body["observations"]
    assert receipt["recorded_at"] == runtime[-1]
    digest = receipt.pop("result_digest")
    assert digest == evidence_digest(receipt) == response.json()["result_digest"]
    assert (await collect(client, capability, body)).status_code == 404
    assert (await client.post(ISSUE + "/cancel", json=REQUEST)).json() == {"state": "finished"}
    assert (await client.post(ISSUE + "/result", json=REQUEST)).json() == result | {"receipt": receipt | {"result_digest": digest}}


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unexecuted", "unknown", "model", "secret", "binding", "nonce", "digest", "failure"])
async def test_collector_rejects_incomplete_forged_or_unredacted_evidence(client, runtime, capability, configured, mutation):
    binding = await claimed_request(client, capability)
    body = result_body(binding)
    if mutation == "missing":
        body["observations"].pop()
    elif mutation == "duplicate":
        body["observations"][-1] = body["observations"][0]
    elif mutation == "unexecuted":
        body["observations"][0]["executed"] = False
    elif mutation == "unknown":
        body["observations"][0]["probe"] = "arbitrary_shell"
    elif mutation == "model":
        body["source"] = "model"
    elif mutation == "secret":
        body["observations"][0]["exposure_digests"] = ["synthetic-unredacted-secret"]
    elif mutation == "binding":
        body["sandbox_uid"] = "forged"
    elif mutation == "nonce":
        body["nonce"] = "0" * 64
    elif mutation == "digest":
        body["manifest_digest"] = "0" * 64
    else:
        body["failure"] = "deadline"
    assert (await collect(client, capability, body)).status_code in {404, 422}
    assert record(runtime)["state"] == "claimed"
    assert "receipt" not in record(runtime)


async def test_collector_requires_claim_and_workload_identity(client, runtime, capability, configured):
    binding = (await issue(client)).json()
    assert (await collect(client, capability, result_body(binding))).status_code == 404
    assert (await claim(client, capability, binding)).status_code == 200
    response = await client.post(
        RESULT,
        json=result_body(binding),
        headers={
            "Authorization": f"Bearer {capability}",
            "X-Adp-Workload-Token": "forged",
        },
    )
    assert response.status_code == 404
    assert "receipt" not in record(runtime)


@pytest.mark.parametrize("change", ["cancelled", "expired", "generation", "revoked"])
async def test_execution_monitor_and_collection_reject_stale_execution(client, runtime, capability, configured, monkeypatch, change):
    binding = await claimed_request(client, capability)
    active = {**REQUEST, "session_id": "session-a", "nonce": binding["nonce"]}
    headers = {"Authorization": f"Bearer {capability}"}
    assert (await client.post(ACTIVE, json=active, headers=headers)).json()["active"] is True
    if change == "cancelled":
        assert (await client.post(ISSUE + "/cancel", json=REQUEST)).status_code == 200
    elif change == "expired":
        monkeypatch.setattr(chat_data_routes, "clock", lambda: binding["expires_at"])
    elif change == "revoked":
        revoke_run(runtime)
    else:
        header = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
        header["chatLease"]["generation"] += 1
        runtime[2].put_item(Item=header)
    assert (await client.post(ACTIVE, json=active, headers=headers)).status_code == 404
    assert (await collect(client, capability, result_body(binding))).status_code == 404
    assert "receipt" not in record(runtime)


async def test_failed_partial_execution_is_recorded_as_failed_not_completed(client, runtime, capability, configured):
    binding = await claimed_request(client, capability)
    body = result_body(binding)
    body.update(status="failed", failure="probe_unavailable", observations=[])
    assert (await collect(client, capability, body)).status_code == 200
    receipt = (await client.post(ISSUE + "/result", json=REQUEST)).json()["receipt"]
    assert receipt["status"] == "failed" and receipt["observations"] == []
    assert receipt["failure"] == "probe_unavailable"


async def test_result_readback_remains_owner_scoped_after_runtime_cleanup(client, runtime, capability, configured):
    binding = await claimed_request(client, capability)
    assert (await collect(client, capability, result_body(binding))).status_code == 200
    revoke_run(runtime)
    assert (await client.post(ISSUE + "/result", json=REQUEST)).status_code == 200
    configured[1].user_id = "second"
    response = await client.post(ISSUE + "/result", json=REQUEST, headers={"X-User-Id": "human"})
    assert response.status_code == 404
    assert "observations" not in response.text


async def test_concurrent_collectors_only_commit_one_receipt(client, runtime, capability, configured, monkeypatch):
    binding = await claimed_request(client, capability)
    original = runtime[1].store.client.transact_write_items
    barrier, lock = Barrier(2, timeout=10), Lock()

    def transact(**request):
        barrier.wait()
        with lock:
            return original(**request)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", transact)
    results = await asyncio.gather(*(collect(client, capability, result_body(binding)) for _ in range(2)))
    assert sorted(result.status_code for result in results) == [200, 409]
    assert record(runtime)["state"] == "finished"


async def test_qualification_bootstrap_cannot_fall_back_to_model_after_disabling(client, runtime, configured, monkeypatch):
    monkeypatch.delenv("ADP_CHAT_QUALIFICATION_ENABLED")
    response = await client.post("/v1/chat/data/bootstrap", json={})
    assert response.status_code == 200
    assert response.json()["qualification_required"] is True


@pytest.mark.parametrize("model_mode", [False, True])
async def test_non_fixture_bootstrap_is_not_affected_by_qualification_expiry(client, runtime, configured, monkeypatch, model_mode):
    configuration = deepcopy(configured[0])
    configuration["fixtures"]["a1"]["user_id"] = "unrelated-owner"
    configuration["expires_at"] = runtime[-1] + 10
    monkeypatch.setenv("ADP_CHAT_QUALIFICATION_CONFIG", json.dumps(configuration))
    update = {"UpdateExpression": "REMOVE chat_execution_kind"}
    if model_mode:
        update = {"UpdateExpression": "SET chat_execution_kind = :mode", "ExpressionAttributeValues": {":mode": {"S": "model"}}}
    runtime[1].store.client.update_item(TableName=runtime[1].store.table, Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-write"}}, **update)
    for instant in (configuration["expires_at"] - 1, configuration["expires_at"], configuration["expires_at"] + 1):
        monkeypatch.setattr(chat_data_routes, "clock", lambda: instant)
        response = await client.post("/v1/chat/data/bootstrap", json={})
        assert response.status_code == 200, response.text
        assert "qualification_required" not in response.json()


async def test_selected_qualification_cannot_fall_back_after_policy_expiry(client, runtime, configured, monkeypatch):
    configuration = deepcopy(configured[0])
    configuration["expires_at"] = runtime[-1]
    monkeypatch.setenv("ADP_CHAT_QUALIFICATION_CONFIG", json.dumps(configuration))
    response = await client.post("/v1/chat/data/bootstrap", json={})
    assert response.status_code == 200
    assert response.json()["qualification_required"] is True
    response = await issue(client)
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "chat_qualification_target_unavailable"


async def test_model_execution_and_qualification_are_mutually_exclusive(client, runtime, capability, configured):
    from src.agentauth.chat_capability import ChatAuthorizationRefusedError
    from src.agentauth.chat_model_journal import ChatModelJournal

    authority = runtime[1]
    launch = runtime[0].launches.load("run-write")
    journal = ChatModelJournal(authority)
    with pytest.raises(ChatAuthorizationRefusedError):
        journal.claim(launch, operation_id="model-call", request_digest="1" * 64, model_id="test-model", now=runtime[-1])
    assert journal._read("run-write", "model-call") is None
    authority.store.client.update_item(
        TableName=authority.store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-write"}},
        UpdateExpression="SET chat_execution_kind = :model",
        ExpressionAttributeValues={":model": {"S": "model"}},
    )
    response = await client.post("/v1/chat/data/bootstrap", json={})
    assert response.status_code == 409, response.text
    assert (await issue(client)).status_code == 409


async def test_model_and_qualification_cannot_win_concurrent_admission(client, runtime, capability, configured, monkeypatch):
    from src.agentauth.chat_capability import ChatAuthorizationRefusedError
    from src.agentauth.chat_model_journal import ChatModelJournal

    authority = runtime[1]
    authority.store.client.update_item(
        TableName=authority.store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-write"}},
        UpdateExpression="REMOVE chat_execution_kind",
    )
    launch = runtime[0].launches.load("run-write")
    original = authority.store.client.transact_write_items
    barrier, lock = Barrier(2, timeout=10), Lock()

    def transact(**request):
        if any("chat_execution_kind" in change.get("Update", {}).get("UpdateExpression", "") for change in request["TransactItems"]):
            barrier.wait()
        with lock:
            return original(**request)

    monkeypatch.setattr(authority.store.client, "transact_write_items", transact)
    response, model = await asyncio.gather(
        client.post("/v1/chat/data/bootstrap", json={}),
        asyncio.to_thread(
            ChatModelJournal(authority).claim, launch, operation_id="racing-model", request_digest="a" * 64, model_id="test-model", now=runtime[-1]
        ),
        return_exceptions=True,
    )
    if response.status_code == 200:
        assert response.json()["qualification_required"] is True
        assert isinstance(model, ChatAuthorizationRefusedError)
    else:
        assert response.status_code == 409, response.text
        assert model["status"] == "pending"


async def test_revoke_between_collection_validation_and_commit_cannot_publish_evidence(client, runtime, capability, configured, monkeypatch):
    binding = await claimed_request(client, capability)
    original = runtime[1].store.client.transact_write_items

    def revoke_before_commit(**request):
        monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
        revoke_run(runtime)
        return original(**request)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", revoke_before_commit)
    response = await collect(client, capability, result_body(binding))
    assert response.status_code == 409, response.text
    assert record(runtime)["state"] == "claimed"
    assert "receipt" not in record(runtime)


@pytest.fixture
async def configured(client, runtime, capability, monkeypatch):
    now = runtime[-1]
    configuration = {
        "target": "qualification-target",
        "evaluation_id": REQUEST["evaluation_id"],
        "expires_at": now + 600,
        "image_digest": runtime[5].image_digest,
        "secret_canary": "e45-synthetic-canary",
        "fixtures": {
            "a1": {"user_id": "human", "tenant_id": "tenant", "team_id": "team"},
            "a2": {"user_id": "second", "tenant_id": "tenant", "team_id": "team"},
            "b1": {"user_id": "other", "tenant_id": "other-tenant", "team_id": ""},
        },
    }
    monkeypatch.setenv("ADP_CHAT_QUALIFICATION_ENABLED", "true")
    monkeypatch.setenv("ADP_CHAT_QUALIFICATION_TARGET", configuration["target"])
    monkeypatch.setenv("ADP_CHAT_QUALIFICATION_CONFIG", json.dumps(configuration))
    monkeypatch.setenv("FEATURE_CHAT_ENABLED", "true")
    user = TokenContext(
        user_id="human",
        org_id="tenant",
        team_id="team",
        department_id="",
        account_type="human",
        expires_at=datetime.fromtimestamp(now + 600, UTC),
    )
    sessions = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="qualification-sessions",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
    )
    sessions.put_item(
        Item={
            "session_id": "session-a",
            "owner_principal": chat_history.owner(user),
            "owner_user_id": user.user_id,
            "user_workspace": "human#webchat",
            "tenant_id": "tenant",
            "org_id": "tenant",
            "channel": "webchat",
            "expires_at": now + 600,
        }
    )
    app = client._transport.app
    app.dependency_overrides[chat_data_routes.get_current_user] = lambda: user
    app.dependency_overrides[chat_history.store] = lambda: sessions
    bootstrap = await client.post("/v1/chat/data/bootstrap", json={})
    assert bootstrap.status_code == 200, bootstrap.text
    assert bootstrap.json()["qualification_required"] is True
    return configuration, user


async def issue(client, **changes):
    return await client.post(ISSUE, json={**REQUEST, **changes})


async def claim(client, capability, binding, **changes):
    return await client.post(
        CLAIM,
        json={**REQUEST, "session_id": "session-a", "nonce": binding["nonce"], **changes},
        headers={"Authorization": f"Bearer {capability}"},
    )


def record(runtime):
    return runtime[2].get_item(Key=KEY, ConsistentRead=True).get("Item")


async def test_disabled_by_default_cannot_be_enabled_by_request_fields(client, runtime, capability, configured, monkeypatch):
    monkeypatch.delenv("ADP_CHAT_QUALIFICATION_ENABLED")
    response = await issue(client)
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "chat_qualification_disabled"
    assert record(runtime) is None
    assert (await issue(client, enabled=True)).status_code == 422
    response = await client.post(ISSUE, json=REQUEST, headers={"X-Qualification-Approved": "true"})
    assert response.status_code == 503


async def test_issue_and_claim_bind_actual_launch_without_new_authority(client, runtime, capability, configured):
    response = await issue(client)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    binding = response.json()
    assert binding == {
        "nonce": binding["nonce"],
        "manifest_digest": MANIFEST_DIGEST,
        "manifest_version": "e45-v1",
        "evaluation_id": REQUEST["evaluation_id"],
        "target": "qualification-target",
        "actor": "a1",
        "tenant_id": "tenant",
        "user_id": "human",
        "team_id": "team",
        "session_id": "session-a",
        "run_id": "run-write",
        "sandbox_uid": "chat-pod",
        "lease_generation": 1,
        "image_digest": runtime[5].image_digest,
        "attempt": 1,
        "credential_epoch": 1,
        "issued_at": runtime[-1],
        "expires_at": runtime[-1] + 120,
        "limits": MANIFEST["limits"],
    }
    before = runtime[0].launches.load("run-write")
    pending = await client.post(
        "/v1/chat/data/qualification/pending",
        json={"run_id": "run-write", "session_id": "session-a"},
        headers={"Authorization": f"Bearer {capability}"},
    )
    assert pending.status_code == 200, pending.text
    assert pending.json() == {"binding": binding, "manifest": MANIFEST}
    assert record(runtime)["state"] == "issued"
    claimed = await claim(client, capability, binding)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json() == {"binding": binding, "manifest": MANIFEST}
    assert record(runtime)["state"] == "claimed"
    assert runtime[0].launches.load("run-write") == before
    assert (await claim(client, capability, binding)).status_code == 404
    assert (await issue(client)).status_code == 409


@pytest.mark.parametrize("session_id,workload", [("session-other", "chat-token"), ("session-a", "forged-token")])
async def test_pending_nonce_is_only_returned_to_the_bound_sandbox(client, runtime, capability, configured, session_id, workload):
    assert (await issue(client)).status_code == 200
    response = await client.post(
        "/v1/chat/data/qualification/pending",
        json={"run_id": "run-write", "session_id": session_id},
        headers={"Authorization": f"Bearer {capability}", "X-Adp-Workload-Token": workload},
    )
    assert response.status_code == 404
    assert "nonce" not in response.text
    assert record(runtime)["state"] == "issued"


@pytest.mark.parametrize(
    "field,value",
    [
        ("command", "echo unsafe"),
        ("script", "arbitrary"),
        ("url", "https://example.invalid"),
        ("environment", {"AWS_ACCESS_KEY_ID": "synthetic"}),
        ("approved", True),
        ("actor", "a1"),
        ("tenant_id", "tenant"),
        ("lease_generation", 1),
        ("probe_manifest", []),
    ],
)
async def test_callers_cannot_choose_commands_scope_or_approval(client, runtime, configured, field, value):
    response = await issue(client, **{field: value})
    assert response.status_code == 422
    assert record(runtime) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", "second"),
        ("org_id", "other-tenant"),
        ("team_id", "other-team"),
        ("is_admin", True),
        ("account_type", "service"),
    ],
)
async def test_only_authenticated_ordinary_owner_can_issue(client, runtime, configured, field, value):
    _, user = configured
    setattr(user, field, value)
    response = await client.post(ISSUE, json=REQUEST, headers={"X-User-Id": "human", "X-Tenant-Id": "tenant", "X-Qualification-Approved": "true"})
    assert response.status_code in {403, 404}
    assert record(runtime) is None


async def test_attribution_headers_do_not_change_authenticated_owner(client, configured):
    _, user = configured
    user.attributed_org_id = "other-tenant"
    response = await client.post(ISSUE, json=REQUEST, headers={"X-User-Id": "other", "X-Tenant-Id": "other-tenant"})
    assert response.status_code == 200, response.text
    assert response.json()["tenant_id"] == "tenant"
    assert response.json()["user_id"] == "human"


@pytest.mark.parametrize(
    "change,error",
    [
        ("target", "chat_qualification_target_unavailable"),
        ("expired", "chat_qualification_target_unavailable"),
        ("image", "chat_qualification_image_unavailable"),
        ("fixture", "chat_qualification_fixture_unavailable"),
        ("missing", "chat_qualification_configuration_unavailable"),
        ("tenant_matrix", "chat_qualification_configuration_unavailable"),
    ],
)
async def test_missing_admission_is_named_unavailable_not_a_denial_probe(client, runtime, configured, monkeypatch, change, error):
    configuration = deepcopy(configured[0])
    if change == "target":
        configuration["target"] = "different-target"
    elif change == "expired":
        configuration["expires_at"] = runtime[-1]
    elif change == "image":
        configuration["image_digest"] = "sha256:" + "b" * 64
    elif change == "fixture":
        configuration["fixtures"]["a1"]["user_id"] = "not-this-user"
    elif change == "missing":
        del configuration["fixtures"]["b1"]
    else:
        configuration["fixtures"]["b1"]["tenant_id"] = "tenant"
    monkeypatch.setenv("ADP_CHAT_QUALIFICATION_CONFIG", json.dumps(configuration))
    response = await issue(client)
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"] == error
    assert record(runtime) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": "run-a"},
        {"manifest_digest": "0" * 64},
        {"evaluation_id": "not-configured"},
    ],
)
async def test_issue_refuses_wrong_run_manifest_or_evaluation(client, runtime, configured, changes):
    assert (await issue(client, **changes)).status_code == 404
    assert record(runtime) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": "run-a"},
        {"session_id": "session-other"},
        {"nonce": "0" * 64},
        {"manifest_digest": "0" * 64},
        {"evaluation_id": "other-evaluation"},
    ],
)
async def test_claim_refuses_forged_scope_or_nonce(client, runtime, capability, configured, changes):
    binding = (await issue(client)).json()
    response = await claim(client, capability, binding, **changes)
    assert response.status_code in {404, 503}, response.text
    assert record(runtime)["state"] == "issued"


async def test_claim_requires_workload_identity_not_just_capability(client, runtime, capability, configured):
    binding = (await issue(client)).json()
    response = await client.post(
        CLAIM,
        json={**REQUEST, "session_id": "session-a", "nonce": binding["nonce"]},
        headers={"Authorization": f"Bearer {capability}", "X-Adp-Workload-Token": "forged-token"},
    )
    assert response.status_code == 404
    assert record(runtime)["state"] == "issued"


async def test_request_expiry_prevents_execution(client, runtime, capability, configured, monkeypatch):
    binding = (await issue(client)).json()
    monkeypatch.setattr(chat_data_routes, "clock", lambda: binding["expires_at"])
    assert (await claim(client, capability, binding)).status_code == 404
    assert record(runtime)["state"] == "issued"


@pytest.mark.parametrize("field,value", [("generation", 2), ("sandbox_uid", "replacement-pod")])
async def test_stale_sandbox_binding_cannot_claim(client, runtime, capability, configured, field, value):
    binding = (await issue(client)).json()
    header = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
    header["chatLease"][field] = value
    runtime[2].put_item(Item=header)
    assert (await claim(client, capability, binding)).status_code == 404
    assert record(runtime)["state"] == "issued"


async def test_concurrent_claims_consume_exactly_once(client, runtime, capability, configured, monkeypatch):
    """Race requests before commit while serializing Moto's snapshot/rollback."""
    binding = (await issue(client)).json()
    original = runtime[1].store.client.transact_write_items
    barrier, lock = Barrier(2, timeout=10), Lock()

    def transact(**request):
        barrier.wait()
        with lock:
            return original(**request)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", transact)
    responses = await asyncio.gather(*(claim(client, capability, binding) for _ in range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert record(runtime)["state"] == "claimed"


@pytest.mark.parametrize("claimed", [False, True])
async def test_owner_cancellation_preserves_replay_tombstone(client, runtime, capability, configured, claimed):
    binding = (await issue(client)).json()
    if claimed:
        assert (await claim(client, capability, binding)).status_code == 200
    response = await client.post(ISSUE + "/cancel", json=REQUEST)
    assert response.status_code == 200, response.text
    assert record(runtime)["state"] == "cancelled"
    assert (await client.post(ISSUE + "/cancel", json=REQUEST)).status_code == 200
    assert (await claim(client, capability, binding)).status_code == 404
    assert (await issue(client)).status_code == 409
    assert runtime[2].get_item(Key={"PK": "chat-qualification#qualification-example", "SK": "a1"})["Item"]["run_id"] == "run-write"


async def test_storage_failure_is_unavailable_not_success(client, runtime, configured, monkeypatch):
    def unavailable(**_request):
        raise EndpointConnectionError(endpoint_url="https://example.invalid")

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", unavailable)
    response = await issue(client)
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "chat_qualification_storage_unavailable"
    assert record(runtime) is None


@pytest.mark.parametrize("operation", ["issue", "claim"])
async def test_concurrent_run_revocation_fences_admission(client, runtime, capability, configured, monkeypatch, operation):
    binding = (await issue(client)).json() if operation == "claim" else None
    original = runtime[1].store.client.transact_write_items

    def revoke_then_write(**request):
        monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
        revoke_run(runtime)
        return original(**request)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", revoke_then_write)
    response = await claim(client, capability, binding) if binding else await issue(client)
    assert response.status_code == 409, response.text
    if binding:
        assert record(runtime)["state"] == "issued"
    else:
        assert record(runtime) is None


async def test_other_fixture_cannot_cancel_owner_request(client, runtime, configured):
    assert (await issue(client)).status_code == 200
    configured[1].user_id = "second"
    response = await client.post(ISSUE + "/cancel", json=REQUEST, headers={"X-User-Id": "human"})
    assert response.status_code == 404
    assert record(runtime)["state"] == "issued"


@pytest.mark.parametrize("changed", ["disabled", "expired", "removed"])
async def test_cancellation_remains_available_after_qualification_is_revoked(client, runtime, configured, monkeypatch, changed):
    assert (await issue(client)).status_code == 200
    if changed == "disabled":
        monkeypatch.delenv("ADP_CHAT_QUALIFICATION_ENABLED")
    elif changed == "expired":
        configuration = {**configured[0], "expires_at": runtime[-1]}
        monkeypatch.setenv("ADP_CHAT_QUALIFICATION_CONFIG", json.dumps(configuration))
    else:
        monkeypatch.delenv("ADP_CHAT_QUALIFICATION_CONFIG")
    response = await client.post(ISSUE + "/cancel", json=REQUEST)
    assert response.status_code == 200, response.text
    assert record(runtime)["state"] == "cancelled"
