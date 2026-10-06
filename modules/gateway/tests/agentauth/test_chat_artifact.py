"""Artifact upload/download through delegated routes with Moto S3/DynamoDB."""

import asyncio
import base64
import hashlib
from threading import Barrier, Lock

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from sqlalchemy import delete

from src.agentauth import chat_data_routes
from src.agentauth.chat_artifact import ARTIFACT_TTL, artifact_prefix
from src.orchestration.chat_data_migration import _owner_fields, inventory
from src.shared.models.organization import TeamMembership
from tests.agentauth.test_chat_history_routes import capability as capability_fixture
from tests.agentauth.test_chat_history_routes import client as client_fixture
from tests.agentauth.test_chat_history_routes import runtime as runtime_fixture
from tests.agentauth.test_chat_history_routes import seed_history
from tests.agentauth.test_chat_history_routes import store as store_fixture
from tests.agentauth.test_chat_history_routes import sts as sts_fixture
from tests.agentauth.test_chat_history_write import HEADER

capability = capability_fixture
client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture


@pytest.fixture
def artifacts(client, runtime):
    table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="artifacts",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
    )
    storage = boto3.client("s3", region_name="us-east-1")
    bucket = "chat-artifacts-test"
    storage.create_bucket(Bucket=bucket)
    client._transport.app.dependency_overrides[chat_data_routes.artifact_storage] = lambda: (table, storage, bucket)
    return table, storage, bucket


async def upload(client, capability, content=b"A durable artifact", **changes):
    return await client.post(
        "/v1/chat/data/artifact/create",
        json={
            "run_id": "run-a",
            "session_id": "session-a",
            "idempotency_key": "artifact-a",
            "filename": "report.txt",
            "content_type": "text/plain",
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "content_base64": base64.b64encode(content).decode(),
            **changes,
        },
        headers={"Authorization": f"Bearer {capability}", "X-User-Id": "other-user", "X-Tenant-Id": "other-tenant"},
    )


async def create(client, capability, **changes):
    response = await upload(client, capability, **changes)
    assert response.status_code == 200, response.text
    return response.json()


async def download(client, capability, reference):
    return await client.get(reference["url"], headers={"Authorization": f"Bearer {capability}"})


def objects(artifacts):
    return artifacts[1].list_objects_v2(Bucket=artifacts[2]).get("Contents", [])


async def test_owner_upload_download_and_retry_never_return_object_capabilities(client, capability, artifacts, runtime):
    reference = await create(client, capability, filename="résumé.html", content=b"<script>untrusted</script>", content_type="text/html")
    assert reference["url"].startswith("/v1/chat/data/artifact/session-a/")
    assert "signature" not in str(reference).lower() and "s3Key" not in reference
    assert reference["source"] == "agent" and reference["scanStatus"] == "not_scanned"
    assert reference["checksum"] == hashlib.sha256(b"<script>untrusted</script>").hexdigest()
    response = await download(client, capability, reference)
    assert response.status_code == 200 and response.content == b"<script>untrusted</script>"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["x-artifact-scan-status"] == "not_scanned"
    assert (await client.get(reference["url"])).status_code == 401
    assert await create(client, capability, filename="résumé.html", content=b"<script>untrusted</script>", content_type="text/html") == reference
    assert len(objects(artifacts)) == 1 and len(artifacts[0].scan()["Items"]) == 1
    row = artifacts[0].scan()["Items"][0]
    assert row["s3Key"].startswith("o/tenant/t/team/u/human/s/session-a/gateway/out/")
    assert row["ttl"] == runtime[-1] + ARTIFACT_TTL
    assert (row["tenantId"], row["teamId"], row["ownerUserId"]) == ("tenant", "team", "human")


async def test_same_filename_never_overwrites_and_conflicting_retries_fail(client, capability, artifacts):
    first = await create(client, capability)
    assert (await upload(client, capability, content=b"different")).status_code == 409
    assert (await upload(client, capability, filename="changed.txt")).status_code == 409
    second = await create(client, capability, content=b"different", idempotency_key="second", supersedes=first["id"])
    assert second["supersedes"] == first["id"] and first["id"] != second["id"]
    assert (await download(client, capability, first)).content == b"A durable artifact"
    assert (await download(client, capability, second)).content == b"different"
    assert len(objects(artifacts)) == 2


@pytest.mark.parametrize("failure", ["s3-response", "ddb-before", "ddb-response"])
async def test_lost_responses_and_partial_publication_recover_with_one_object(client, capability, artifacts, runtime, monkeypatch, failure):
    target, method = (artifacts[1], "put_object") if failure == "s3-response" else (runtime[1].store.client, "transact_write_items")
    original = getattr(target, method)

    def failed(**kwargs):
        if failure != "ddb-before":
            original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://storage.test")

    monkeypatch.setattr(target, method, failed)
    assert (await upload(client, capability)).status_code == 503
    assert len(objects(artifacts)) == 1
    assert len(artifacts[0].scan()["Items"]) == (1 if failure == "ddb-response" else 0)
    monkeypatch.setattr(target, method, original)
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 10)
    reference = await create(client, capability)
    assert await create(client, capability) == reference
    assert (await download(client, capability, reference)).status_code == 200
    assert len(objects(artifacts)) == 1 and len(artifacts[0].scan()["Items"]) == 1
    assert artifacts[0].scan()["Items"][0]["ttl"] == runtime[-1] + ARTIFACT_TTL


@pytest.mark.parametrize("race", ["grant", "lease"])
async def test_revocation_before_catalog_commit_leaves_no_downloadable_artifact(client, capability, artifacts, runtime, monkeypatch, race):
    authority = runtime[1].store
    original = authority.client.transact_write_items

    def changed(**kwargs):
        if race == "lease":
            row = runtime[2].get_item(Key=HEADER)["Item"]
            row["chatLease"]["generation"] += 1
            runtime[2].put_item(Item=row)
        else:
            authority.client.update_item(
                TableName=authority.table,
                Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "GRANT#run-a#1"}},
                UpdateExpression="SET revoked = :yes",
                ExpressionAttributeValues={":yes": {"BOOL": True}},
            )
        return original(**kwargs)

    monkeypatch.setattr(authority.client, "transact_write_items", changed)
    assert (await upload(client, capability)).status_code == 404
    assert artifacts[0].scan()["Items"] == []
    assert len(runtime[2].scan()["Items"]) == 1


@pytest.mark.parametrize("race", ["grant", "expiry", "catalog"])
async def test_download_reauthorizes_after_reading_object_bytes(client, capability, artifacts, runtime, monkeypatch, race):
    reference = await create(client, capability)
    original = artifacts[1].get_object
    time = {"now": runtime[-1]}
    monkeypatch.setattr(chat_data_routes, "clock", lambda: time["now"])

    def changed(**kwargs):
        response = original(**kwargs)
        if race == "expiry":
            time["now"] += 300
        elif race == "catalog":
            row = artifacts[0].scan()["Items"][0]
            row["scanStatus"] = "quarantined"
            artifacts[0].put_item(Item=row)
        else:
            runtime[1].store.client.update_item(
                TableName=runtime[1].store.table,
                Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "GRANT#run-a#1"}},
                UpdateExpression="SET revoked = :yes",
                ExpressionAttributeValues={":yes": {"BOOL": True}},
            )
        return response

    monkeypatch.setattr(artifacts[1], "get_object", changed)
    response = await download(client, capability, reference)
    # Re-authorization after the object read: an expired capability is 401 (refresh), revocation stays 404.
    assert response.status_code == (401 if race == "expiry" else 404)
    assert b"A durable artifact" not in response.content


@pytest.mark.parametrize("tenant,acl,status", [("tenant", [], 404), ("tenant", ["human"], 200), ("other-tenant", ["human"], 404)])
async def test_guessed_sessions_and_current_sharing_acl(client, capability, artifacts, runtime, tenant, acl, status):
    reference = await create(client, capability)
    row = artifacts[0].scan()["Items"][0]
    header = seed_history(runtime, "other-session", tenant=tenant, owner="other-user", acl=acl)
    row.update(
        {
            **_owner_fields((tenant, "team", "other-user")),
            "PK": header["PK"],
            "s3Key": artifact_prefix(header, "other-session") + "task/out/report.txt",
        }
    )
    artifacts[0].put_item(Item=row)
    artifacts[1].put_object(Bucket=artifacts[2], Key=row["s3Key"], Body=b"A durable artifact", ContentType="text/plain")
    other = {"url": f"/v1/chat/data/artifact/other-session/{reference['id']}?run_id=run-a"}
    assert (await download(client, capability, other)).status_code == status
    assert (await upload(client, capability, session_id="other-session")).status_code == 404
    if status == 200:
        header["aclUserIds"] = []
        runtime[2].put_item(Item=header)
        assert (await download(client, capability, other)).status_code == 404


@pytest.mark.parametrize("change", ["owner", "alias", "path", "scan"])
async def test_catalog_and_path_must_agree_before_any_object_read(client, capability, artifacts, monkeypatch, change):
    reference = await create(client, capability)
    row = artifacts[0].scan()["Items"][0]
    if change == "owner":
        del row["user_id"]
    elif change == "alias":
        row["ownerUserId"] = "other-user"
    elif change == "path":
        row["s3Key"] = "o/other/t/team/u/victim/s/session-a/private"
    else:
        row["scanStatus"] = "quarantined"
    artifacts[0].put_item(Item=row)

    def forbidden(**kwargs):
        pytest.fail("unauthorized object read")

    monkeypatch.setattr(artifacts[1], "get_object", forbidden)
    assert (await download(client, capability, reference)).status_code == 404


async def test_owned_legacy_catalog_works_without_trusting_its_stored_url(client, capability, artifacts, runtime):
    reference = await create(client, capability)
    row = artifacts[0].scan()["Items"][0]
    for field in ("orgId", "tenantId", "teamId", "ownerUserId", "tenant_id", "owner_user_id", "scanStatus"):
        del row[field]
    row["url"] = "https://untrusted.test/reusable-link"
    artifacts[0].put_item(Item=row)
    runtime[2].delete_item(Key={"PK": HEADER["PK"], "SK": f"artifact-id#{reference['id']}"})
    assert (await download(client, capability, reference)).status_code == 200


@pytest.mark.parametrize("mutation", ["remove", "null"])
async def test_migration_never_authorizes_ownerless_catalog(client, capability, artifacts, runtime, mutation):
    reference = await create(client, capability)
    row = artifacts[0].scan()["Items"][0]
    for field in _owner_fields(("tenant", "team", "human")):
        if mutation == "remove":
            del row[field]
        else:
            row[field] = None
    artifacts[0].put_item(Item=row)
    runtime[2].delete_item(Key={"PK": HEADER["PK"], "SK": f"artifact-id#{reference['id']}"})
    memory = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="memory",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
    )

    response = await download(client, capability, reference)
    assert response.status_code == 404 and response.json()["detail"]["error"] == "chat_scope_refused"
    for apply in (False, True, True):
        assert inventory(runtime[2], artifacts[0], memory, apply=apply)["artifacts"] == {"total": 1, "quarantined": 1}
        assert artifacts[0].scan()["Items"] == [row]
        assert all(row.get(field) is None for field in ("org_id", "team_id", "user_id"))
        response = await download(client, capability, reference)
        assert response.status_code == 404 and response.json()["detail"]["error"] == "chat_scope_refused"
        assert artifacts[1].get_object(Bucket=artifacts[2], Key=row["s3Key"])["Body"].read() == b"A durable artifact"


@pytest.mark.parametrize("failure", ["missing", "checksum", "type", "expired"])
async def test_missing_expired_or_corrupt_objects_are_not_successful_downloads(client, capability, artifacts, runtime, failure):
    reference = await create(client, capability)
    row = artifacts[0].scan()["Items"][0]
    if failure == "expired":
        row["ttl"] = runtime[-1]
        artifacts[0].put_item(Item=row)
    elif failure == "missing":
        artifacts[1].delete_object(Bucket=artifacts[2], Key=row["s3Key"])
    else:
        artifacts[1].put_object(
            Bucket=artifacts[2],
            Key=row["s3Key"],
            Body=b"corrupted" if failure == "checksum" else b"A durable artifact",
            ContentType="image/png" if failure == "type" else "text/plain",
        )
    response = await download(client, capability, reference)
    assert response.status_code == (404 if failure in {"missing", "expired"} else 503)
    assert response.content != b"A durable artifact"


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "user"},
        {"s3Key": "other/path"},
        {"filename": "../file"},
        {"content_type": "text/html\r\nInjected: yes"},
        {"content_sha256": "0" * 64},
        {"content_base64": "!!!!"},
    ],
)
async def test_input_cannot_override_identity_paths_or_integrity(client, capability, artifacts, changes):
    assert (await upload(client, capability, **changes)).status_code == 422
    assert artifacts[0].scan()["Items"] == [] and objects(artifacts) == []


async def test_streaming_upload_enforces_actual_body_bound(client, capability, artifacts, monkeypatch):
    monkeypatch.setattr(chat_data_routes, "MAX_ARTIFACT_BYTES", 64)

    async def chunks():
        for _chunk in range(3):
            yield b"x" * 3000

    response = await client.post(
        "/v1/chat/data/artifact/create", content=chunks(), headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert artifacts[0].scan()["Items"] == [] and objects(artifacts) == []


async def test_membership_revocation_blocks_downloads_and_receipt_replay(client, capability, artifacts, db_session_factory):
    reference = await create(client, capability)
    async with db_session_factory() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.id == "member"))
        await db.commit()
    assert (await download(client, capability, reference)).status_code == 404
    assert (await upload(client, capability)).status_code == 404


def test_personal_prefix_cannot_alias_a_valid_team_identity():
    header = {"tenantId": "tenant", "teamId": "", "ownerUserId": "human"}
    assert artifact_prefix(header, "session-a") == "o/tenant/t/~personal/u/human/s/session-a/"


async def test_concurrent_retries_publish_one_catalog_entry_and_object(client, capability, artifacts, runtime, monkeypatch):
    original = runtime[1].store.client.transact_write_items
    barrier, lock = Barrier(2, timeout=10), Lock()

    def commit(**kwargs):
        barrier.wait()
        with lock:
            return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    responses = await asyncio.gather(upload(client, capability), upload(client, capability))
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert len(objects(artifacts)) == 1 and len(artifacts[0].scan()["Items"]) == 1


async def test_inventory_recognizes_personal_artifacts_only_with_explicit_ownership(client, capability, artifacts, runtime):
    await create(client, capability)
    original = artifacts[0].scan()["Items"][0]
    owner = _owner_fields(("tenant", "", "human"))
    header = {**owner, "PK": "session#personal", "SK": "header"}
    runtime[2].put_item(Item=header)
    row = {**original, **owner, "PK": header["PK"], "s3Key": artifact_prefix(header, "personal") + "gateway/out/artifact"}
    artifacts[0].put_item(Item=row)
    unverified = {**row, "SK": "art#unverified"}
    del unverified["ownerUserId"]
    artifacts[0].put_item(Item=unverified)

    class EmptyMemory:
        def scan(self):
            return {"Items": []}

    before = artifacts[0].scan()["Items"]
    for apply in (False, True, True):
        assert inventory(runtime[2], artifacts[0], EmptyMemory(), apply=apply)["artifacts"] == {"total": 3, "owned": 2, "quarantined": 1}
        assert artifacts[0].scan()["Items"] == before
