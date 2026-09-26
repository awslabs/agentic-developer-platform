"""Real HTTP serialization of owned, expired and bounded chat history."""

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
