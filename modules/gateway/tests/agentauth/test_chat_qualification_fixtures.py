"""Fixed qualification fixtures use committed ordinary data operations."""

import base64
import hashlib

import pytest

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_qualification_fixtures import operation_key
from tests.agentauth import test_chat_qualification as qualification
from tests.agentauth.test_chat_artifact import artifacts as artifacts_fixture
from tests.agentauth.test_chat_memory import memory as memory_fixture

client = qualification.client
artifacts = artifacts_fixture
memory = memory_fixture
runtime = qualification.runtime
store = qualification.store
sts = qualification.sts
capability = qualification.capability
configured = qualification.configured
retained_input_table = qualification.retained_input_table
CAPTURE = "/v1/chat/data/qualification/fixtures"
CANARY = "E45 synthetic isolation canary"


async def seed(client, capability, binding):
    payloads = {
        "session_write": ("draft/write", {"draft": {"intent": CANARY}, "expected_version": 0}),
        "message_write": ("history/append", {"content": CANARY, "tokens": 8, "expected_version": 1, "user_turn_id": qualification.fixtures.USER_REF}),
        "summary_write": (
            "history/summary/append",
            {"content": CANARY, "tokens": 8, "expected_version": 2, "source_ids": [qualification.fixtures.USER_REF], "parent_ids": []},
        ),
        "artifact_write": (
            "artifact/create",
            {
                "filename": "e45.txt",
                "content_type": "text/plain",
                "content_sha256": hashlib.sha256(CANARY.encode()).hexdigest(),
                "content_base64": base64.b64encode(CANARY.encode()).decode(),
            },
        ),
        "memory_write": ("memory/write", {"content": CANARY, "kind": "fact", "expected_version": 0}),
    }
    results = {}
    for probe, (route, payload) in payloads.items():
        body = {"run_id": binding["run_id"], "idempotency_key": operation_key(binding, probe), **payload}
        if probe != "memory_write":
            body["session_id"] = binding["session_id"]
        response = await client.post("/v1/chat/data/" + route, json=body, headers={"Authorization": f"Bearer {capability}"})
        assert response.status_code == 200, (probe, response.text)
        results[probe] = response.json()
    return results


async def capture(client, capability, binding, **changes):
    return await client.post(
        CAPTURE,
        headers={"Authorization": f"Bearer {capability}"},
        json={
            **qualification.REQUEST,
            "session_id": binding["session_id"],
            "nonce": binding["nonce"],
            **changes,
        },
    )


def teardown(runtime, binding):
    authority = runtime[1].store
    authority.client.put_item(
        TableName=authority.table,
        Item=_encoded(
            {
                "pk": "CHAT-LAUNCH#" + binding["run_id"],
                "sk": "TEARDOWN",
                "removed_at": runtime[-1],
                "binding": {
                    "run_id": binding["run_id"],
                    "session_id": binding["session_id"],
                    "pod_uid": binding["sandbox_uid"],
                    "image_digest": binding["image_digest"],
                    "lease_generation": binding["lease_generation"],
                    "tenant_id": binding["tenant_id"],
                    "attempt": binding["attempt"],
                    "credential_epoch": binding["credential_epoch"],
                },
            }
        ),
    )
    authority.client.update_item(
        TableName=authority.table,
        Key=_encoded({"pk": "TENANT#tenant", "sk": "EXEC#run-write"}),
        UpdateExpression="SET chat_turn_sealed = :sealed",
        ExpressionAttributeValues=_encoded({":sealed": True}),
    )


@pytest.mark.parametrize("window", ["disabled", "expired"])
async def test_capture_real_writes_and_cleanup_after_qualification_disabled(
    client, runtime, capability, configured, artifacts, memory, monkeypatch, window
):
    binding = await qualification.claimed_request(client, capability)
    results = await seed(client, capability, binding)
    response = await capture(client, capability, binding)
    assert response.status_code == 200, response.text
    references = response.json()
    assert references["owner"] == binding
    for field, probe in (("message_id", "message_write"), ("summary_id", "summary_write"), ("memory_id", "memory_write")):
        assert references[field] == results[probe][field]
    assert references["artifact_id"] == results["artifact_write"]["id"]
    assert (await capture(client, capability, binding)).json() == references
    assert (await qualification.collect(client, capability, qualification.result_body(binding))).status_code == 200
    receipt = (await client.post(qualification.ISSUE + "/result", json=qualification.REQUEST)).json()["receipt"]
    assert receipt["fixture_digest"] == qualification.evidence_digest(references)
    assert runtime[2].get_item(Key=qualification.KEY)["Item"]["ttl"] == configured[0]["expires_at"] + 86400
    assert (await client.post(qualification.ISSUE + "/cleanup", json=qualification.REQUEST)).status_code == 503
    teardown(runtime, binding)
    if window == "disabled":
        monkeypatch.setenv("ADP_CHAT_QUALIFICATION_ENABLED", "false")
    else:
        monkeypatch.setattr(qualification.chat_data_routes, "clock", lambda: configured[0]["expires_at"] + 10)
    response = await client.post(qualification.ISSUE + "/cleanup", json=qualification.REQUEST)
    assert response.status_code == 200, response.text
    assert response.json() == {"cleanup_complete": True, "nonce": binding["nonce"]}
    assert not [row for row in runtime[2].scan()["Items"] if row.get("runId") == binding["run_id"]]
    assert artifacts[0].scan()["Items"] == []
    assert artifacts[1].list_objects_v2(Bucket=artifacts[2]).get("Contents", []) == []
    assert memory.scan()["Items"] == []
    assert (await client.post(qualification.ISSUE + "/cleanup", json=qualification.REQUEST)).json() == response.json()


@pytest.mark.parametrize("change", ["missing_write", "nonce", "scope", "generation", "extra_reference"])
async def test_capture_rejects_missing_or_forged_fixtures(client, runtime, capability, configured, artifacts, memory, change):
    binding = await qualification.claimed_request(client, capability)
    if change != "missing_write":
        await seed(client, capability, binding)
    changes = {}
    if change == "nonce":
        changes["nonce"] = "0" * 64
    elif change == "scope":
        changes["session_id"] = "other-session"
    elif change == "extra_reference":
        changes["object_key"] = "arbitrary-object"
    elif change == "generation":
        row = runtime[2].get_item(
            Key={"PK": "session#session-a", "SK": "write#" + hashlib.sha256(operation_key(binding, "message_write").encode()).hexdigest()}
        )["Item"]
        runtime[2].put_item(Item={**row, "leaseGeneration": 999})
    response = await capture(client, capability, binding, **changes)
    assert response.status_code in {404, 422}, response.text
    assert "fixtures" not in runtime[2].get_item(Key=qualification.KEY)["Item"]


async def test_cleanup_refuses_new_generation_and_does_not_delete_its_rows(client, runtime, capability, configured, artifacts, memory):
    binding = await qualification.claimed_request(client, capability)
    await seed(client, capability, binding)
    teardown(runtime, binding)
    row = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "draft"})["Item"]
    runtime[2].put_item(Item={**row, "leaseGeneration": 2})
    response = await client.post(qualification.ISSUE + "/cleanup", json=qualification.REQUEST)
    assert response.status_code == 404, response.text
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "draft"})["Item"]["leaseGeneration"] == 2


async def test_readiness_names_missing_resource_capability(client, runtime, capability, configured, artifacts, memory, monkeypatch):
    url = "/chat/qualification?evaluation_id=" + qualification.REQUEST["evaluation_id"]
    assert (await client.get(url)).status_code == 503
    monkeypatch.setenv("ARTIFACTS_TABLE", artifacts[0].name)
    monkeypatch.setenv("ARTIFACTS_BUCKET", artifacts[2])
    response = await client.get(url)
    assert response.status_code == 200, response.text
    assert response.json()["actor"] == "a1"


@pytest.mark.parametrize("deletion_observed", [False, True])
async def test_cleanup_verifies_orphaned_upload_absence(client, runtime, capability, configured, artifacts, memory, monkeypatch, deletion_observed):
    binding = await qualification.claimed_request(client, capability)
    artifact_id = "art_" + hashlib.sha256(f"session#session-a#tenant#human#{operation_key(binding, 'artifact_write')}".encode()).hexdigest()[:32]
    path = f"o/tenant/t/team/u/human/s/session-a/gateway/out/{artifact_id}"
    artifacts[1].put_object(Bucket=artifacts[2], Key=path, Body=CANARY.encode())
    preserved = {"PK": "session#session-a", "SK": "msg#other-run", "runId": "unrelated-run", "content": "preserve"}
    runtime[2].put_item(Item=preserved)
    teardown(runtime, binding)
    if not deletion_observed:
        monkeypatch.setattr(artifacts[1], "delete_object", lambda **_kwargs: {})
    response = await client.post(qualification.ISSUE + "/cleanup", json=qualification.REQUEST)
    assert response.status_code == (200 if deletion_observed else 503), response.text
    assert bool(runtime[2].get_item(Key=qualification.KEY)["Item"].get("cleanup_complete")) is deletion_observed
    assert runtime[2].get_item(Key={"PK": preserved["PK"], "SK": preserved["SK"]})["Item"] == preserved
    objects = artifacts[1].list_objects_v2(Bucket=artifacts[2]).get("Contents", [])
    assert bool(objects) is not deletion_observed


@pytest.mark.parametrize("field", ["tenant_id", "pod_uid", "image_digest", "lease_generation", "attempt", "credential_epoch"])
async def test_cleanup_requires_matching_trusted_teardown(client, runtime, capability, configured, artifacts, memory, field):
    binding = await qualification.claimed_request(client, capability)
    teardown(runtime, binding)
    authority = runtime[1].store
    row = authority._read("CHAT-LAUNCH#run-write", "TEARDOWN")
    row["binding"]["M"][field] = {"N": "99"} if field in {"lease_generation", "attempt", "credential_epoch"} else {"S": "wrong"}
    authority.client.put_item(TableName=authority.table, Item=row)
    result = await client.post(qualification.ISSUE + "/result", json=qualification.REQUEST)
    assert result.json()["sandbox_removed"] is False
    response = await client.post(qualification.ISSUE + "/cleanup", json=qualification.REQUEST)
    assert response.status_code == 503, response.text
    assert not runtime[2].get_item(Key=qualification.KEY)["Item"].get("cleanup_complete")
