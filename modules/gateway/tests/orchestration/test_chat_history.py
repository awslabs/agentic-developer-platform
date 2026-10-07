"""Real HTTP serialization of owned, expired and bounded chat history."""

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from src.agentauth.chat_session_mailbox import ChatSessionMailbox
from src.orchestration import chat_history as chat
from src.shared.schemas.auth import TokenContext


@pytest.fixture
def user():
    return TokenContext(
        user_id="user", org_id="tenant", team_id="team", department_id="", account_type="human", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )


@pytest.fixture
def row(user):
    return dict(
        session_id="sess-123",
        owner_principal=chat.owner(user),
        owner_user_id=user.user_id,
        user_workspace="user#webchat",
        tenant_id="tenant",
        org_id="tenant",
        channel="webchat",
        expires_at=int(time.time()) + 3600,
        updated_at=123,
        threads={},
        messages=[{"role": "assistant", "content": "Hello", "task_id": "task-1", "timestamp": 123}],
    )


@pytest.fixture
def api(monkeypatch, user, row):
    monkeypatch.setenv("FEATURE_CHAT_ENABLED", "true")
    table = Mock()
    table.get_item.return_value = {"Item": row}
    table.query.return_value = {"Items": [row]}
    app = FastAPI()
    app.include_router(chat.router)
    app.dependency_overrides[chat.get_db] = lambda: None
    app.dependency_overrides[chat.get_current_user] = lambda: user
    app.dependency_overrides[chat.store] = lambda: table
    return TestClient(app), table


def test_session_mode_uses_authenticated_session_owner_and_survives_reload(api, row, monkeypatch):
    client, _ = api
    access = {"allowed": True}

    async def membership(*_):
        return access["allowed"]

    monkeypatch.setattr(chat, "current_chat_member", membership)
    with mock_aws():
        context = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="chat-mode-context",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        )
        client.app.dependency_overrides[chat.mode_store] = lambda: ChatSessionMailbox(context)
        path = "/chat/sessions/sess-123/mode"
        assert client.get(path).json() == {"mode": "ephemeral", "sequence": 0, "health": "idle"}
        selected = client.put(path, json={"mode": "persistent"})
        assert selected.status_code == 200, selected.text
        assert selected.json()["mode"] == "persistent"
        assert client.get(path).json()["mode"] == "persistent"
        context.update_item(
            Key={"PK": "session#sess-123", "SK": "header"},
            UpdateExpression="SET chatLease = :lease",
            ExpressionAttributeValues={":lease": {"expires_at": int(time.time()) + 90}},
        )
        assert client.put(path, json={"mode": "ephemeral"}).status_code == 409
        assert client.get(path).json()["mode"] == "persistent"
        assert client.put(path, json={"mode": "untrusted"}).status_code == 422
        access["allowed"] = False
        assert client.get(path).status_code == 404
        assert client.put(path, json={"mode": "ephemeral"}).status_code == 404
        access["allowed"] = True
        row["owner_user_id"] = "someone-else"
        assert client.get(path).status_code == 404
        assert client.put(path, json={"mode": "ephemeral"}).status_code == 404
        assert context.get_item(Key={"PK": "session#sess-123", "SK": "header"})["Item"]["sessionMode"] == "persistent"


def test_end_route_is_owner_scoped_and_reports_durable_cleanup_status(api, row, monkeypatch):
    client, _ = api

    access = {"allowed": True}

    async def membership(*_):
        return access["allowed"]

    monkeypatch.setattr(chat, "current_chat_member", membership)
    with mock_aws():
        context = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="end-mode-context",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        )
        mailbox = ChatSessionMailbox(context)
        client.app.dependency_overrides[chat.mode_store] = lambda: mailbox
        now = int(time.time())
        mailbox.select_mode(session_id="sess-123", owner=("tenant", "team", "user"), mode="persistent", now=now)
        context.update_item(
            Key={"PK": "session#sess-123", "SK": "header"},
            UpdateExpression="SET chatLease = :lease, sessionState = :active",
            ExpressionAttributeValues={
                ":lease": {"run_id": "run-1", "sandbox_uid": "pod-1", "generation": 1, "expires_at": now + 90},
                ":active": "active",
            },
        )
        path = "/chat/sessions/sess-123/end"
        ended = client.post(path)
        assert ended.status_code == 200, ended.text
        assert ended.json()["health"] == "ending"
        assert client.post(path).json()["health"] == "ending"
        assert context.get_item(Key={"PK": "session#sess-123", "SK": "header"})["Item"]["chatLease"]["expires_at"] == 1
        access["allowed"] = False
        assert client.post(path).status_code == 404
        access["allowed"] = True
        row["owner_user_id"] = "other"
        assert client.post(path).status_code == 404


def test_ephemeral_mode_promotes_only_after_bound_teardown_and_terminal(api, monkeypatch):
    client, _ = api

    async def membership(*_):
        return True

    monkeypatch.setattr(chat, "current_chat_member", membership)
    with mock_aws():
        context = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="pending-mode-context",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        )
        mailbox = ChatSessionMailbox(context)
        client.app.dependency_overrides[chat.mode_store] = lambda: mailbox
        owner = ("tenant", "team", "user")
        now = int(time.time())
        mailbox.select_mode(session_id="sess-123", owner=owner, mode="ephemeral", now=now)
        lease = {"run_id": "run-1", "sandbox_uid": "pod-1", "generation": 1, "expires_at": now + 90}
        context.update_item(
            Key={"PK": "session#sess-123", "SK": "header"}, UpdateExpression="SET chatLease = :lease", ExpressionAttributeValues={":lease": lease}
        )
        launch = SimpleNamespace(
            session_run_id=None,
            tenant_id="tenant",
            team_id="team",
            user_id="user",
            session_id="sess-123",
            sandbox_uid="pod-1",
            lease_generation=1,
            run_id="run-1",
        )
        records = {}
        authority = SimpleNamespace(store=SimpleNamespace(_read=lambda pk, sk: records.get((pk, sk))))
        capabilities = SimpleNamespace(launches=SimpleNamespace(load=lambda _: launch))
        from src.agentauth import chat_data_routes

        monkeypatch.setattr(chat_data_routes, "runtime", lambda: (authority, capabilities))
        path = "/chat/sessions/sess-123/mode"
        assert client.put(path, json={"mode": "persistent"}).json() == {
            "mode": "ephemeral",
            "sequence": 0,
            "health": "idle",
            "pending_mode": "persistent",
        }
        records[("CHAT-LAUNCH#run-1", "TEARDOWN")] = {"removed_at": {"N": str(now)}}
        assert client.get(path).json()["pending_mode"] == "persistent"
        records[("CHAT-DELIVERY#run-1", "TERMINAL")] = {"completion_receipt": {"M": {}}}
        assert client.get(path).json() == {"mode": "persistent", "sequence": 0, "health": "idle"}
        assert context.get_item(Key={"PK": "session#sess-123", "SK": "header"})["Item"].get("chatLease") is None


def test_real_response_and_task_correlation(api):
    client, table = api
    reply = client.get("/chat/sessions/sess-123")
    assert reply.status_code == 200
    assert reply.json()["messages"][0]["task_id"] == "task-1"
    assert reply.json()["answer_completion_verified"] is False
    table.get_item.assert_called_once_with(Key={"session_id": "sess-123"}, ConsistentRead=True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_principal", "foreign"),
        ("owner_user_id", "other"),
        ("tenant_id", "other"),
        ("org_id", "other"),
        ("user_workspace", "other#webchat"),
        ("channel", "slack"),
    ],
)
def test_foreign_and_legacy_are_absent(api, row, field, value):
    client, _ = api
    row[field] = value
    assert client.get("/chat/sessions/sess-123").status_code == 404
    row.pop(field)
    assert client.get("/chat/sessions/sess-123").status_code == 404


def test_expiry_checked_only_after_ownership(api, row):
    client, _ = api
    row["expires_at"] = int(time.time()) - 1
    assert client.get("/chat/sessions/sess-123").status_code == 410
    row["owner_principal"] = "foreign"
    assert client.get("/chat/sessions/sess-123").status_code == 404


def test_list_rechecks_stale_gsi_and_hides_foreign_cursor(api, row):
    client, table = api
    table.query.return_value = {"Items": [row], "LastEvaluatedKey": {"session_id": "foreign-secret", "user_workspace": "user#webchat"}}
    table.get_item.return_value = {"Item": {**row, "owner_principal": "other"}}
    reply = client.get("/chat/sessions")
    assert reply.status_code == 200
    assert reply.json()["items"] == []
    assert reply.json()["next_page"] == 2
    assert "foreign-secret" not in reply.text


def test_bounded_redacted_history(api, row):
    client, _ = api
    row["messages"] = [{"role": "user", "content": "Bearer " + "a" * 60 + "\x1b[2J", "timestamp": 1}] * 101
    reply = client.get("/chat/sessions/sess-123")
    assert reply.status_code == 200
    data = reply.json()
    assert len(data["messages"]) == 100 and data["truncated"]
    assert "a" * 60 not in reply.text and "\\u001b" not in reply.text


def test_store_disabled_failure_and_machine_refusal(api, monkeypatch, user):
    client, table = api
    table.get_item.side_effect = RuntimeError("secret table ARN")
    assert client.get("/chat/sessions/sess-123").status_code == 503
    monkeypatch.setenv("FEATURE_CHAT_ENABLED", "false")
    reply = client.get("/chat/capabilities").json()
    assert reply["general_turns_supported"] is True and reply["authorized_personas"] == []
    table.get_item.reset_mock()
    assert client.get("/chat/sessions/sess-123").status_code == 503
    table.get_item.assert_not_called()
    user.account_type = "service"
    assert client.get("/chat/capabilities").status_code == 403


def test_snapshot_does_not_echo_tool_metadata(api, row):
    client, _ = api
    row["messages"].append({"role": "tool", "content": "secret-tool", "token": "secret"})
    assert "secret-tool" not in client.get("/chat/sessions/sess-123").text


def test_missing_ttl_fails_closed(api, row):
    row.pop("expires_at")
    assert api[0].get("/chat/sessions/sess-123").status_code == 503


def test_cli_consumes_real_http_history_response(api):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("chat_transport_contract", Path(__file__).parents[2] / "cli/adp-chat.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    client, _ = api

    class Transport:
        def get(self, path):
            response = client.get(path)
            assert response.status_code == 200
            return response.json()

    args = cli.parser().parse_args(["watch", "--session", "sess-123", "--task-id", "task-1"])
    result = cli.execute(args, Transport())
    assert result["detail"]["matched_task_id"] == "task-1"
    assert result["detail"]["answer_completion_verified"] is True


def test_task_backed_list_snapshot_does_not_infer_idle_from_legacy_threads(api, row):
    row["chat_task_persona"] = "agent-task-investigator"
    result = api[0].get("/chat/sessions").json()
    assert result["items"][0]["status"] == "unknown"
    assert result["items"][0]["answer_completion_verified"] is False
