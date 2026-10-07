"""History HTTP reads through admitted capabilities, DynamoDB emulation and SQL."""

import json

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import delete
from starlette.concurrency import run_in_threadpool

from src.agentauth import chat_data_routes
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.shared.models.organization import Team, TeamMembership
from tests.agentauth.test_chat_data_routes import admit, exchange
from tests.agentauth.test_chat_data_routes import client as client_fixture
from tests.agentauth.test_chat_data_routes import runtime as runtime_fixture
from tests.agentauth.test_chat_data_routes import store as store_fixture
from tests.agentauth.test_chat_data_routes import sts as sts_fixture

client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
PREFIX = "/v1/chat/data/history"


@pytest.fixture
async def capability(client, runtime):
    admitted = await admit(client, runtime)
    assert admitted.status_code == 200, admitted.text
    response = await exchange(client)
    assert response.status_code == 200, response.text
    return response.json()["capability"]


def seed_history(runtime, session_id="session-a", *, owner="human", tenant="tenant", team="team", acl=()):
    table = runtime[2]
    key = {"PK": f"session#{session_id}", "SK": "header"}
    previous = table.get_item(Key=key).get("Item", {})
    identity = {"orgId": tenant, "tenantId": tenant, "ownerUserId": owner, "teamId": team}
    header = {**previous, **key, **identity, "ttl": runtime[-1] + 1000, "aclUserIds": list(acl)}
    if not previous:
        header["status"] = "closed"
    table.put_item(Item=header)
    for ordinal, reference in ((1, "message-1"), (3, "message-2")):
        table.put_item(Item={**identity, "PK": key["PK"], "SK": f"item#{ordinal:08d}", "ordinal": ordinal, "type": "msg", "ref": reference})
        table.put_item(
            Item={
                **identity,
                "PK": key["PK"],
                "SK": f"msg#{reference}",
                "role": "assistant",
                "content": f"{owner}: {reference}",
                "ts": "2026-10-03T10:30:00.123456Z",
                "tokens": 4,
                "parts": json.dumps([{"type": "file", "artifactId": "attachment-1"}]),
            }
        )
    table.put_item(Item={**identity, "PK": key["PK"], "SK": "item#00000002", "ordinal": 2, "type": "sum", "ref": "summary-1"})
    table.put_item(
        Item={
            **identity,
            "PK": key["PK"],
            "SK": "sum#summary-1",
            "kind": "leaf",
            "depth": 1,
            "content": "compacted",
            "tokens": 2,
            "sourceIds": ["message-1", "message-2"],
            "earliestAt": "2026-10-03T10:30:00.123456Z",
            "latestAt": "2026-10-03T10:31:00Z",
        }
    )
    return header


async def read(client, capability, operation="read", *, session_id="session-a", headers=None, **fields):
    return await client.post(
        f"{PREFIX}/{operation}",
        json={"run_id": "run-a", "session_id": session_id, **fields},
        headers={"Authorization": f"Bearer {capability}", **(headers or {})},
    )


async def test_owner_reads_ordered_pages_summaries_and_attachment_metadata(client, runtime, capability):
    seed_history(runtime)
    response = await read(client, capability, limit=2)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    first = response.json()
    assert first["status"] == "partial"
    assert [entry["type"] for entry in first["entries"]] == ["msg", "sum"]
    response = await read(client, capability, limit=2, cursor=first["next_cursor"])
    assert response.json()["status"] == "ok"
    assert [entry["ordinal"] for entry in response.json()["entries"]] == [3]
    response = await read(client, capability, "summary", summary_id="summary-1")
    summary = response.json()["entries"][0]["summary"]
    response = await read(client, capability, "messages", ids=summary["sourceIds"])
    assert [entry["ref"] for entry in response.json()["entries"]] == ["message-1", "message-2"]
    message = response.json()["entries"][0]["message"]
    assert message["ts"] == summary["earliestAt"]
    assert message["parts"] == [{"type": "file", "artifactId": "attachment-1"}]


@pytest.mark.parametrize("tenant,acl", [("tenant", ()), ("other-tenant", ()), ("other-tenant", ("human",))])
async def test_private_or_cross_tenant_history_cannot_be_guessed_or_header_spoofed(client, runtime, capability, tenant, acl):
    seed_history(runtime, "other-session", owner="other-user", tenant=tenant, acl=acl)
    for operation, fields in (("read", {}), ("messages", {"ids": ["message-1"]}), ("summary", {"summary_id": "summary-1"})):
        response = await read(
            client,
            capability,
            operation,
            session_id="other-session",
            **fields,
            headers={"X-User-Id": "other-user", "X-Tenant-Id": tenant, "X-Session-Id": "other-session"},
        )
        assert response.status_code == 404
        assert "other-user: message" not in response.text


@pytest.mark.parametrize("revocation", ["acl", "membership", "execution"])
async def test_explicit_shared_reads_use_own_executor_and_current_resource_membership(client, runtime, capability, db_session_factory, revocation):
    async with db_session_factory() as db:
        db.add_all(
            [
                Team(id="shared-team", org_id="tenant", department_id="department", name="Shared team"),
                TeamMembership(id="shared-member", user_id="human", org_id="tenant", team_id="shared-team"),
            ]
        )
        await db.commit()
    target = seed_history(runtime, "shared-session", owner="other-user", team="shared-team", acl=("human",))
    first = await read(client, capability, session_id="shared-session", limit=1)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "partial"
    messages = await read(client, capability, "messages", session_id="shared-session", ids=["message-1"])
    assert messages.json()["entries"][0]["message"]["content"] == "other-user: message-1"
    summary = await read(client, capability, "summary", session_id="shared-session", summary_id="summary-1")
    assert summary.json()["entries"][0]["summary"]["sourceIds"] == ["message-1", "message-2"]
    assert runtime[2].get_item(Key={"PK": target["PK"], "SK": "header"})["Item"] == target
    assert "chatLease" not in target
    assert runtime[0].launches.load("run-a").session_id == "session-a"
    with pytest.raises(ChatAuthorizationRefusedError):
        await run_in_threadpool(runtime[0].verify, capability, run_id="run-a", session_id="shared-session", operation="history.read", now=runtime[-1])

    if revocation == "acl":
        runtime[2].put_item(Item={**target, "aclUserIds": []})
    elif revocation == "membership":
        async with db_session_factory() as db:
            await db.execute(delete(TeamMembership).where(TeamMembership.id == "shared-member"))
            await db.commit()
        assert (await read(client, capability)).status_code == 200
    else:
        runtime[2].update_item(
            Key={"PK": "session#session-a", "SK": "header"},
            UpdateExpression="SET #status = :closed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":closed": "closed"},
        )
    assert (await read(client, capability, session_id="shared-session", limit=1, cursor=first.json()["next_cursor"])).status_code == 404
    assert (await read(client, capability, "messages", session_id="shared-session", ids=["message-1"])).status_code == 404
    assert (await read(client, capability, "summary", session_id="shared-session", summary_id="summary-1")).status_code == 404


async def test_shared_cursor_cannot_be_replayed_for_another_authorized_resource_or_owner(client, runtime, capability):
    first_header = seed_history(runtime, "shared-one", owner="other-user", acl=("human",))
    seed_history(runtime, "shared-two", owner="other-user", acl=("human",))
    first = await read(client, capability, session_id="shared-one", limit=1)
    cursor = first.json()["next_cursor"]
    assert (await read(client, capability, session_id="shared-two", limit=1, cursor=cursor)).status_code == 404
    runtime[2].put_item(Item={**first_header, "ownerUserId": "replacement-owner"})
    assert (await read(client, capability, session_id="shared-one", limit=1, cursor=cursor)).status_code == 404


async def test_owner_identity_does_not_implicitly_widen_bound_session_scope(client, runtime, capability):
    target = seed_history(runtime, "another-owned-session")
    for operation, fields in (("read", {}), ("messages", {"ids": ["message-1"]}), ("summary", {"summary_id": "summary-1"})):
        assert (await read(client, capability, operation, session_id="another-owned-session", **fields)).status_code == 404
    runtime[2].put_item(Item={**target, "aclUserIds": ["human"]})
    assert (await read(client, capability, session_id="another-owned-session")).status_code == 200


@pytest.mark.parametrize(
    "operation,sort_key,fields",
    [("read", "item#00000001", {}), ("messages", "msg#message-1", {"ids": ["message-1"]}), ("summary", "sum#summary-1", {"summary_id": "summary-1"})],
)
@pytest.mark.parametrize("ownership", ["missing", "conflicting"])
async def test_shared_child_ownership_is_not_inferred_from_the_header(client, runtime, capability, operation, sort_key, fields, ownership):
    seed_history(runtime, "shared-session", owner="other-user", acl=("human",))
    key = {"PK": "session#shared-session", "SK": sort_key}
    row = runtime[2].get_item(Key=key)["Item"]
    if ownership == "missing":
        del row["ownerUserId"]
    else:
        row["ownerUserId"] = "conflicting-owner"
    runtime[2].put_item(Item=row)
    response = await read(client, capability, operation, session_id="shared-session", **fields)
    assert response.status_code == 404
    assert "other-user: message" not in response.text


async def test_empty_missing_denied_expired_and_unavailable_are_distinct(client, runtime, capability, monkeypatch):
    assert (await read(client, capability)).json()["status"] == "empty"
    missing = await read(client, capability, "messages", ids=["missing-source"])
    assert missing.json()["status"] == "partial"
    assert missing.json()["coverage"]["missing_source_ids"] == ["missing-source"]
    assert (await read(client, capability, session_id="not-shared")).status_code == 404
    expired = seed_history(runtime, "expired-session", owner="other-user", acl=("human",))
    runtime[2].put_item(Item={**expired, "ttl": runtime[-1]})
    assert (await read(client, capability, session_id="expired-session")).status_code == 410

    def unavailable(**kwargs):
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "private diagnostics"}}, "Query")

    monkeypatch.setattr(runtime[2], "query", unavailable)
    response = await read(client, capability)
    assert response.status_code == 503
    assert "private diagnostics" not in response.text


async def test_missing_or_forged_capability_and_wrong_run_fail_closed(client, runtime, capability):
    assert (await client.post(f"{PREFIX}/read", json={"run_id": "run-a", "session_id": "session-a"})).status_code == 401
    assert (await read(client, "forged")).status_code == 401
    assert (await read(client, capability, run_id="another-run")).status_code == 404
    assert (await read(client, capability, owner_user_id="other-user")).status_code == 422
    assert (await read(client, capability, limit=101)).status_code == 422
    assert (await read(client, capability, "messages", ids=["session#other/message-1"])).status_code == 422


async def test_guessed_id_in_shared_history_cannot_escape_its_partition(client, runtime, capability):
    seed_history(runtime, "shared-session", owner="other-user", acl=("human",))
    runtime[2].put_item(Item={"PK": "session#private-session", "SK": "msg#private-id", "content": "private content"})
    result = await read(client, capability, "messages", session_id="shared-session", ids=["private-id", "message-1"])
    assert result.status_code == 200
    assert result.json()["status"] == "partial"
    assert result.json()["coverage"]["missing_source_ids"] == ["private-id"]
    assert [entry["ref"] for entry in result.json()["entries"]] == ["message-1"]


async def test_history_routes_remain_disabled_without_rollout_flag(client, capability, monkeypatch):
    monkeypatch.delenv("ADP_CHAT_DATA_ENABLED")
    for operation, fields in (("read", {}), ("messages", {"ids": []}), ("summary", {"summary_id": "summary-1"})):
        response = await read(client, capability, operation, **fields)
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "chat_data_disabled"


async def test_authentication_failures_are_401_while_resource_refusals_stay_404(client, runtime, capability, monkeypatch):
    """A sandbox must be able to tell "refresh or re-bootstrap" apart from "not yours", without enumeration."""
    seed_history(runtime, "other-session", owner="other-user")

    def error(response):
        return response.status_code, response.json()["detail"]["error"]

    body = {"run_id": "run-a", "session_id": "session-a"}
    assert error(await client.post(f"{PREFIX}/read", json=body)) == (401, "capability_invalid")
    assert error(await client.post(f"{PREFIX}/read", json=body, headers={"Authorization": "Basic " + capability})) == (401, "capability_invalid")
    assert error(await read(client, "forged")) == (401, "capability_invalid")
    assert error(await read(client, capability[:-2] + ("AA" if not capability.endswith("AA") else "BB"))) == (401, "capability_invalid")
    assert error(await read(client, capability + ".extra")) == (401, "capability_invalid")
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 300)
    assert error(await read(client, capability)) == (401, "capability_expired")
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] - 1)
    assert error(await read(client, capability)) == (401, "capability_invalid")
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1])
    for operation, fields in (("read", {}), ("messages", {"ids": ["message-1"]}), ("summary", {"summary_id": "summary-1"})):
        assert error(await read(client, capability, operation, session_id="other-session", **fields)) == (404, "chat_scope_refused")
        assert error(await read(client, capability, operation, session_id="guessed-session", **fields)) == (404, "chat_scope_refused")
    assert error(await read(client, capability, run_id="another-run")) == (404, "chat_scope_refused")
    assert (await read(client, capability)).status_code == 200
