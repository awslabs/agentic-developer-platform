"""Scoped artifact catalog pages through HTTP and DynamoDB/S3 emulation."""

from datetime import UTC, datetime

import pytest
from botocore.exceptions import EndpointConnectionError
from sqlalchemy import delete

from src.agentauth import chat_data_routes
from src.agentauth.chat_artifact import artifact_prefix
from src.orchestration.chat_data_migration import _owner_fields
from src.shared.models.organization import TeamMembership
from tests.agentauth.test_chat_artifact import artifacts as artifacts_fixture
from tests.agentauth.test_chat_artifact import capability as capability_fixture
from tests.agentauth.test_chat_artifact import client as client_fixture
from tests.agentauth.test_chat_artifact import create, download
from tests.agentauth.test_chat_artifact import runtime as runtime_fixture
from tests.agentauth.test_chat_artifact import store as store_fixture
from tests.agentauth.test_chat_artifact import sts as sts_fixture
from tests.agentauth.test_chat_history_routes import seed_history

artifacts = artifacts_fixture
capability = capability_fixture
client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
PATH = "/v1/chat/data/artifact/list"


async def listing(client, capability, **changes):
    return await client.post(
        PATH,
        json={"run_id": "run-a", "session_id": "session-a", **changes},
        headers={"Authorization": f"Bearer {capability}", "X-User-Id": "victim", "X-Tenant-Id": "other-tenant"},
    )


@pytest.fixture
async def catalog(client, capability, artifacts, runtime):
    await create(client, capability)
    template = artifacts[0].scan()["Items"][0]
    artifacts[0].delete_item(Key={"PK": template["PK"], "SK": template["SK"]})
    rows = []
    for index, (filename, content_type) in enumerate(
        (("report.txt", "text/plain"), ("report.json", "application/json"), ("notes.txt", "text/plain"))
    ):
        artifact_id = f"art_{index:032x}"
        stamp = datetime.fromtimestamp(runtime[-1] - index, UTC).isoformat()
        row = {
            **template,
            "SK": f"art#{stamp}#{artifact_id}",
            "id": artifact_id,
            "createdAt": stamp,
            "filename": filename,
            "contentType": content_type,
            "source": "user" if index else "agent",
            "s3Key": template["s3Key"].rsplit("/", 1)[0] + "/" + artifact_id,
        }
        artifacts[0].put_item(Item=row)
        artifacts[1].put_object(Bucket=artifacts[2], Key=row["s3Key"], Body=b"A durable artifact", ContentType=content_type)
        rows.append(row)
    return rows


async def test_ordered_pages_preserve_references_and_require_authorized_download(client, capability, artifacts, catalog):
    cursor, entries = None, []
    for _page in range(4):
        response = await listing(client, capability, limit=1, **({"cursor": cursor} if cursor else {}))
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        page = response.json()
        entries.extend(page["entries"])
        cursor = page["next_cursor"]
        assert page["coverage"]["complete"] is (cursor is None)
        if cursor is None:
            break
        assert page["status"] == "partial"
    assert cursor is None
    assert [entry["id"] for entry in entries] == [row["id"] for row in catalog]
    assert [entry["source"] for entry in entries] == ["agent", "user", "user"]
    assert all("s3Key" not in entry and "ownerUserId" not in entry for entry in entries)
    assert (await download(client, capability, entries[0])).content == b"A durable artifact"
    assert (await client.get(entries[0]["url"])).status_code == 401


async def test_filters_preserve_partial_empty_pages_and_exact_type_matching(client, capability, artifacts, catalog):
    response = await listing(client, capability, limit=1, filename="notes", content_type="text/plain")
    first = response.json()
    assert response.status_code == 200 and first["entries"] == []
    assert first["status"] == "partial" and first["next_cursor"]
    cursor, entries = first["next_cursor"], []
    while cursor:
        page = (await listing(client, capability, limit=1, filename="notes", content_type="text/plain", cursor=cursor)).json()
        entries.extend(page["entries"])
        cursor = page["next_cursor"]
    assert [entry["id"] for entry in entries] == [catalog[2]["id"]]
    assert [entry["id"] for entry in (await listing(client, capability, filename="report")).json()["entries"]] == [row["id"] for row in catalog[:2]]
    assert (await listing(client, capability, content_type="text/pla")).json()["status"] == "empty"


async def test_empty_missing_expired_and_denied_are_distinct(client, capability, artifacts, runtime):
    empty = (await listing(client, capability)).json()
    assert empty["status"] == "empty" and empty["coverage"]["complete"] and empty["entries"] == []
    reference = await create(client, capability)
    row = artifacts[0].scan()["Items"][0]
    row["ttl"] = runtime[-1]
    artifacts[0].put_item(Item=row)
    missing = (await listing(client, capability)).json()
    assert missing["status"] == "partial" and not missing["coverage"]["complete"]
    assert missing["entries"] == [] and missing["coverage"]["missing_source_ids"] == [reference["id"]]
    denied = await listing(client, capability, session_id="guessed-session")
    assert denied.status_code == 404 and denied.json()["detail"]["error"] == "chat_scope_refused"


@pytest.mark.parametrize(
    "change", [{"filename": "report"}, {"content_type": "text/plain"}, {"limit": 2}, {"session_id": "other-session"}, {"run_id": "other-run"}]
)
async def test_cursor_cannot_be_rebound_to_another_query_or_scope(client, capability, artifacts, catalog, runtime, change):
    seed_history(runtime, "other-session", acl=["human"])
    first = (await listing(client, capability, limit=1)).json()
    response = await listing(client, capability, **{"limit": 1, "cursor": first["next_cursor"], **change})
    assert response.status_code == 404


@pytest.mark.parametrize("change", ["signature", "expired", "version", "acl"])
async def test_stale_or_tampered_cursor_is_refused(client, capability, artifacts, catalog, runtime, monkeypatch, change):
    first = (await listing(client, capability, limit=1)).json()
    cursor = first["next_cursor"]
    if change == "signature":
        cursor = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    elif change == "expired":
        monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 301)
    else:
        header = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
        header.update({"historyVersion": 1} if change == "version" else {"aclUserIds": ["other-user"]})
        runtime[2].put_item(Item=header)
    # The capability and its cursor share one validity window: an expired capability is an
    # authentication failure (401) while a tampered or rebound cursor stays a non-enumerating 404.
    assert (await listing(client, capability, limit=1, cursor=cursor)).status_code == (401 if change == "expired" else 404)


@pytest.mark.parametrize("tenant,acl,status", [("tenant", [], 404), ("tenant", ["human"], 200), ("other-tenant", ["human"], 404)])
async def test_shared_listing_requires_current_same_tenant_acl(client, capability, artifacts, catalog, runtime, tenant, acl, status):
    header = seed_history(runtime, "other-session", tenant=tenant, owner="other-user", acl=acl)
    shared = {
        **catalog[0],
        **_owner_fields((tenant, "team", "other-user")),
        "PK": header["PK"],
        "s3Key": artifact_prefix(header, "other-session") + "gateway/out/" + catalog[0]["id"],
    }
    artifacts[0].put_item(Item=shared)
    response = await listing(client, capability, session_id="other-session")
    assert response.status_code == status
    if status == 200:
        assert response.json()["entries"][0]["id"] == shared["id"]
        runtime[2].put_item(Item={**header, "aclUserIds": []})
        assert (await listing(client, capability, session_id="other-session")).status_code == 404


@pytest.mark.parametrize("change", [{"ownerUserId": "other-user"}, {"s3Key": "another-owner/object"}, {"scanStatus": "quarantined"}])
async def test_catalog_ownership_and_quarantine_apply_before_filtering(client, capability, artifacts, catalog, change):
    artifacts[0].put_item(Item={**catalog[0], **change})
    response = await listing(client, capability, filename="does-not-match")
    assert response.status_code == 404 and "entries" not in response.json()


@pytest.mark.parametrize("change,status", [("removed", 200), ("quarantined", 404), ("metadata", 409), ("grant", 404), ("version", 409)])
async def test_listing_rechecks_catalog_and_authority_after_query(client, capability, artifacts, catalog, runtime, monkeypatch, change, status):
    original = artifacts[0].query

    def changed(**kwargs):
        response = original(**kwargs)
        row = catalog[0]
        if change == "removed":
            artifacts[0].delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
        elif change in {"quarantined", "metadata"}:
            artifacts[0].put_item(Item={**row, **({"scanStatus": "quarantined"} if change == "quarantined" else {"filename": "changed.txt"})})
        elif change == "version":
            runtime[2].update_item(
                Key={"PK": row["PK"], "SK": "header"}, UpdateExpression="SET historyVersion = :next", ExpressionAttributeValues={":next": 1}
            )
        else:
            runtime[1].store.client.update_item(
                TableName=runtime[1].store.table,
                Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "GRANT#run-a#1"}},
                UpdateExpression="SET revoked = :yes",
                ExpressionAttributeValues={":yes": {"BOOL": True}},
            )
        return response

    monkeypatch.setattr(artifacts[0], "query", changed)
    response = await listing(client, capability)
    assert response.status_code == status, response.text
    if change == "removed":
        assert response.json()["status"] == "partial"
        assert response.json()["coverage"]["missing_source_ids"] == [catalog[0]["id"]]
        assert [entry["id"] for entry in response.json()["entries"]] == [row["id"] for row in catalog[1:]]


async def test_membership_revocation_blocks_continuation(client, capability, artifacts, catalog, db_session_factory):
    cursor = (await listing(client, capability, limit=1)).json()["next_cursor"]
    async with db_session_factory() as database:
        await database.execute(delete(TeamMembership))
        await database.commit()
    assert (await listing(client, capability, limit=1, cursor=cursor)).status_code == 404


async def test_storage_failure_is_not_reported_as_empty(client, capability, artifacts, monkeypatch):
    def unavailable(**kwargs):
        raise EndpointConnectionError(endpoint_url="https://storage.test")

    monkeypatch.setattr(artifacts[0], "query", unavailable)
    assert (await listing(client, capability)).status_code == 503


@pytest.mark.parametrize(
    "change", [{"limit": 0}, {"limit": 101}, {"limit": True}, {"cursor": ""}, {"owner_user_id": "victim"}, {"s3Key": "guessed/key"}]
)
async def test_list_request_rejects_scope_overrides_and_unbounded_pages(client, capability, artifacts, change):
    assert (await listing(client, capability, **change)).status_code == 422
