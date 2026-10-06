"""Trusted admission and actual TypeScript context ports over HTTP and Moto."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_history_store import ChatHistoryStore
from src.agentauth.external_roots import provision_root
from src.shared.models.organization import Organization, Team, TeamMembership, User
from tests.agentauth.test_chat_data_routes import admit
from tests.agentauth.test_chat_memory_port_integration import gateway_http as gateway_http_fixture
from tests.agentauth.test_chat_memory_port_integration import node_tools
from tests.agentauth.test_chat_user_turn import accept, prepare, rows
from tests.agentauth.test_chat_user_turn import client as client_fixture
from tests.agentauth.test_chat_user_turn import retained_input_table as retained_input_table_fixture
from tests.agentauth.test_chat_user_turn import runtime as runtime_fixture
from tests.agentauth.test_chat_user_turn import store as store_fixture
from tests.agentauth.test_chat_user_turn import sts as sts_fixture
from tests.agentauth.test_work_producer import ROLE

pytestmark = [pytest.mark.integration, pytest.mark.chat_ports]
client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
retained_input_table = retained_input_table_fixture
gateway_http = gateway_http_fixture
DRIVER = Path(__file__).with_name("fixtures") / "chat_context_port.cjs"


def seed_owned_history(runtime, count):
    header = {**runtime[3], "historyNextOrdinal": count + 1}
    del header["chatLease"]
    runtime[2].put_item(Item=header)
    identity = {field: header[field] for field in ("orgId", "tenantId", "teamId", "ownerUserId")}
    for ordinal in range(1, count + 1):
        reference = f"historical-{ordinal}"
        provenance = {**identity, "PK": header["PK"]}
        runtime[2].put_item(Item={**provenance, "SK": f"item#{ordinal:08d}", "ordinal": ordinal, "type": "msg", "ref": reference, "tokens": 10})
        runtime[2].put_item(
            Item={
                **provenance,
                "SK": f"msg#{reference}",
                "role": "user" if ordinal % 2 else "assistant",
                "content": f"Historical message {ordinal}",
                "ts": datetime.fromtimestamp(runtime[-1] - count + ordinal - 1, UTC).isoformat(),
                "tokens": 10,
                "parts": [{"type": "file", "artifactId": "art_0123456789ab"}] if ordinal == 1 else [],
            }
        )


async def admit_other_user(client, runtime, db_session_factory, monkeypatch, tenant):
    team = "team" if tenant == "tenant" else "other-team"
    async with db_session_factory() as db:
        if tenant != "tenant":
            db.add_all([Organization(id=tenant, name="Other tenant"), Team(id=team, org_id=tenant, department_id="department", name="Other team")])
        db.add_all(
            [
                User(id="other-human", org_id=tenant, team_id=team, email="other@example.test"),
                TeamMembership(id="other-member", org_id=tenant, user_id="other-human", team_id=team, is_primary=True),
            ]
        )
        await db.commit()
    monkeypatch.setenv(
        "ADP_MODEL_ROOT_BINDINGS", json.dumps([{"source": "chat", "producer_role": ROLE, "tenant_id": tenant, "personas": ["developer"]}])
    )
    envelope = {
        "message_id": "run-other",
        "session_id": "session-b",
        "tenant_id": tenant,
        "persona": "developer",
        "source_ref": {"repo": "chat/session-b"},
        "arrived_at": datetime.fromtimestamp(runtime[-1], UTC).isoformat(),
        "message": "Another user's prompt",
        "attachments": [],
    }
    provision_root(runtime[1].store, envelope, source="chat", human_id="other-human", now=datetime.fromtimestamp(runtime[-1], UTC))
    runtime[4]["uid"] = "other-chat-pod"
    response = await admit(client, runtime, run_id="run-other", envelope_digest=envelope_digest(envelope), pod_uid="other-chat-pod")
    assert response.status_code == 200, response.text


async def test_complete_turn_roundtrip_and_later_context(client, runtime, gateway_http):
    assert rows(runtime) == []
    envelope = prepare(runtime)
    admitted = await accept(client, runtime, envelope)
    assert admitted.status_code == 200, admitted.text
    result = await node_tools(gateway_http, runtime[-1], driver=DRIVER, mode="record", message=envelope["message"])
    assert result["before"]["messages"] == []
    assert result["first"] == result["transcript"]
    assert result["version"] == 2
    assert [entry["ordinal"] for entry in result["transcript"]] == [1, 2]
    user, assistant = [entry["message"] for entry in result["transcript"]]
    assert (user["role"], user["content"]) == ("user", envelope["message"])
    assert user["parts"] == [{"type": "file", "artifactId": envelope["attachments"][0]}]
    assert (assistant["role"], assistant["content"]) == ("assistant", "I inspected the attachment.")
    assert result["wrongTurn"] == {"error": "denied", "status": 404}
    assert result["stale"] == {"error": "conflict", "status": 409}
    assert next(row for row in rows(runtime) if row.get("role") == "assistant")["userTurnId"] == result["accepted"]["message_id"]

    runtime[1].store.client.update_item(
        TableName=runtime[1].store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-user"}},
        UpdateExpression="SET #status = :completed",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":completed": {"S": "completed"}},
    )
    runtime[4]["uid"] = "next-chat-pod"
    following = prepare(runtime, message_id="run-next", message="What did you find?", attachments=[])
    assert (await accept(client, runtime, following, pod_uid="next-chat-pod")).status_code == 200
    later = await node_tools(gateway_http, runtime[-1], driver=DRIVER, mode="assemble", message=following["message"])
    assert later["messages"] == [{"role": "user", "content": user["content"]}, {"role": "assistant", "content": assistant["content"]}]

    runtime[1].store.client.update_item(
        TableName=runtime[1].store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "GRANT#run-next#1"}},
        UpdateExpression="SET revoked = :revoked",
        ExpressionAttributeValues={":revoked": {"BOOL": True}},
    )
    assert await node_tools(gateway_http, runtime[-1], driver=DRIVER, mode="denied") == {"error": "denied", "status": 404}


@pytest.mark.parametrize("lost_response", [False, True])
async def test_paginated_context_compaction_expansion_and_lost_response_recovery(client, runtime, gateway_http, monkeypatch, lost_response):
    seed_owned_history(runtime, 104)
    envelope = prepare(runtime)
    assert (await accept(client, runtime, envelope)).status_code == 200
    original = runtime[1].store.client.transact_write_items
    lost = []

    def commit(**kwargs):
        result = original(**kwargs)
        if (
            lost_response
            and not lost
            and any(
                operation.get("Put", {}).get("Item", {}).get("SK", {}).get("S", "").startswith("compaction-write#")
                for operation in kwargs["TransactItems"]
            )
        ):
            lost.append(True)
            raise EndpointConnectionError(endpoint_url="https://storage.test")
        return result

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    result = await node_tools(gateway_http, runtime[-1], driver=DRIVER, mode="compact", message=envelope["message"])
    assert bool(lost) is lost_response
    assert result["firstPage"]["status"] == "partial" and not result["firstPage"]["coverage"]["complete"]
    assert len(result["firstPage"]["entries"]) == 100
    assert result["secondPage"]["status"] == "ok" and result["secondPage"]["coverage"]["complete"]
    assert [entry["ordinal"] for entry in result["secondPage"]["entries"]] == list(range(101, 106))
    assert [item["ordinal"] for item in result["before"]["items"]] == list(range(1, 106))
    assert len(result["assembled"]["messages"]) == 104
    assert result["after"] == result["replay"]
    assert result["after"]["version"] == 3
    assert [item["ordinal"] for item in result["after"]["items"]] == [1, *range(95, 107)]
    assert result["after"]["items"][0]["type"] == "sum"
    assert result["summary"]["sourceIds"] == [f"historical-{ordinal}" for ordinal in range(1, 95)]
    assert result["summary"]["content"] == "Brief."
    assert result["sources"][0]["parts"] == [{"type": "file", "artifactId": "art_0123456789ab"}]
    assert result["summary"]["earliestAt"] == result["sources"][0]["ts"]
    assert result["summary"]["latestAt"] == result["sources"][-1]["ts"]
    expanded = result["expanded"]["content"][0]["text"]
    assert "Source messages: 94" in expanded
    assert all(f"Historical message {ordinal}\n" in expanded for ordinal in range(1, 95))
    assert len(result["summarizations"]) == 1
    assert result["staleCompaction"] == result["summaryConflict"] == {"error": "conflict", "status": 409}
    assert result["staleCursor"] == {"error": "denied", "status": 404}
    assert result["standalone"] == result["standaloneReplay"]
    assert len([row for row in rows(runtime) if row["SK"].startswith("compaction-write#")]) == 1
    assert len([row for row in rows(runtime) if row["SK"].startswith("msg#")]) == 106
    assert len(result["transcript"]) == 13

    runtime[2].delete_item(Key={"PK": "session#session-a", "SK": "msg#historical-1"})
    missing = await node_tools(gateway_http, runtime[-1], driver=DRIVER, mode="probe", ids=["historical-1"], summaryId=result["summaryId"])
    assert missing == {
        "messages": {"error": "incomplete"},
        "summary": {"error": None},
        "expansion": {"error": "incomplete"},
        "transcript": {"error": None},
    }


async def test_empty_and_missing_sources_are_distinct_from_denied_access(client, runtime, gateway_http):
    assert (await admit(client, runtime)).status_code == 200
    empty = await node_tools(gateway_http, runtime[-1], driver=DRIVER, mode="read")
    assert empty["status"] == "empty" and empty["entries"] == [] and empty["coverage"]["complete"]
    missing = await node_tools(gateway_http, runtime[-1], driver=DRIVER, mode="probe", ids=["missing-message"], summaryId="missing-summary")
    assert missing == {
        "messages": {"error": "incomplete"},
        "summary": {"error": "incomplete"},
        "expansion": {"error": "incomplete"},
        "transcript": {"error": None},
    }
    runtime[2].delete_item(Key={"PK": "session#session-a", "SK": "header"})
    assert await node_tools(gateway_http, runtime[-1], driver=DRIVER, mode="denied") == {"error": "denied", "status": 404}


async def test_missing_timeline_source_is_not_returned_as_partial_transcript(client, runtime, gateway_http):
    seed_owned_history(runtime, 3)
    envelope = prepare(runtime)
    assert (await accept(client, runtime, envelope)).status_code == 200
    runtime[2].delete_item(Key={"PK": "session#session-a", "SK": "msg#historical-2"})
    result = await node_tools(gateway_http, runtime[-1], driver=DRIVER, mode="probe", ids=["historical-2"], summaryId="missing-summary")
    assert result["messages"] == result["transcript"] == {"error": "incomplete"}


@pytest.mark.parametrize("tenant", ["tenant", "other-tenant"])
async def test_other_users_cannot_reuse_history_ids_or_cursors(client, runtime, gateway_http, db_session_factory, monkeypatch, tenant):
    seed_owned_history(runtime, 104)
    envelope = prepare(runtime)
    assert (await accept(client, runtime, envelope)).status_code == 200
    owner = await node_tools(gateway_http, runtime[-1], driver=DRIVER, mode="compact", message=envelope["message"])
    await admit_other_user(client, runtime, db_session_factory, monkeypatch, tenant)
    result = await node_tools(
        gateway_http,
        runtime[-1],
        driver=DRIVER,
        mode="probe",
        session="session-b",
        otherSession="session-a",
        ids=["historical-1"],
        summaryId=owner["summaryId"],
        cursor=owner["cursorPage"]["next_cursor"],
    )
    assert result == {
        "messages": {"error": "incomplete"},
        "summary": {"error": "incomplete"},
        "expansion": {"error": "incomplete"},
        "transcript": {"error": None},
        "cursor": {"error": "denied", "status": 404},
        "otherSession": {"error": "scope_mismatch"},
    }


async def test_revoked_grant_rejects_next_page_with_cached_port_capability(client, runtime, gateway_http, monkeypatch):
    seed_owned_history(runtime, 3)
    envelope = prepare(runtime)
    assert (await accept(client, runtime, envelope)).status_code == 200
    original = ChatHistoryStore.read_page

    def revoke_after_first_page(store, *args, **kwargs):
        result = original(store, *args, **kwargs)
        runtime[1].store.client.update_item(
            TableName=runtime[1].store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "GRANT#run-user#1"}},
            UpdateExpression="SET revoked = :revoked",
            ExpressionAttributeValues={":revoked": {"BOOL": True}},
        )
        return result

    monkeypatch.setattr(ChatHistoryStore, "read_page", revoke_after_first_page)
    result = await node_tools(gateway_http, runtime[-1], driver=DRIVER, mode="revoke")
    assert result["first"]["status"] == "partial"
    assert result["next"] == {"error": "denied", "status": 404}
