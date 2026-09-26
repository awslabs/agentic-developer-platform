import importlib.util
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("chat_cli", Path(__file__).parents[2] / "cli/adp-chat.py")
chat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chat)


@pytest.fixture
def state():
    return dict(
        session_id="sess-123",
        tenant_id="tenant",
        user_id="user",
        expires_at=int(time.time()) + 3600,
        status="idle",
        answer_completion_verified=False,
        redaction="known-secret-patterns",
        truncated=False,
        messages=[{"role": "assistant", "content": "Hello", "task_id": "task-1"}],
        pending_task_ids=[],
    )


@pytest.fixture
def client(state):
    caps = dict(tenant_id="tenant", user_id="user", enabled=True, history_configured=True, general_turns_supported=False, authorized_personas=[])
    mock = Mock()
    mock.get.side_effect = lambda path: caps if path == "/chat/capabilities" else state
    return mock


def test_watch_correlates_exact_task(client):
    args = chat.parser().parse_args(["watch", "--session", "sess-123", "--task-id", "task-1"])
    result = chat.execute(args, client)
    assert result["detail"]["response_observed"] is True
    assert result["detail"]["answer_completion_verified"] is True


def test_watch_other_task_does_not_claim_completion(client, monkeypatch):
    ticks = iter([0, 2])
    monkeypatch.setattr(chat.time, "monotonic", lambda: next(ticks))
    args = chat.parser().parse_args(["watch", "--session", "sess-123", "--task-id", "other", "--timeout", "1"])
    result = chat.execute(args, client)
    assert result["status"] == "pending" and result["detail"]["response_observed"] is False


@pytest.mark.parametrize(
    "field,value", [("tenant_id", "other"), ("user_id", "other"), ("session_id", "other"), ("messages", {}), ("redaction", "none"), ("expires_at", 1)]
)
def test_invalid_or_foreign_readback(client, state, field, value):
    state[field] = value
    with pytest.raises(chat.common.CliError):
        chat.execute(chat.parser().parse_args(["show", "--session", "sess-123"]), client)


def test_export_new_private_file_only(client, tmp_path):
    tmp_path.chmod(0o700)
    output = tmp_path / "transcript.json"
    args = chat.parser().parse_args(["export", "--session", "sess-123", "--output", str(output)])
    chat.execute(args, client)
    assert output.stat().st_mode & 0o777 == 0o600
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        chat.execute(args, client)
    assert output.read_bytes() == original


def test_unavailable_start_never_reads_message_or_dispatches(client):
    args = chat.parser().parse_args(["start", "--persona", "developer", "--message-file", "/nonexistent", "--request-id", "one", "--yes"])
    with pytest.raises(chat.common.CliError, match="unavailable"):
        chat.execute(args, client)
    assert client.get.call_count == 1


def test_truncated_final_response_does_not_claim_complete_answer(client, state):
    state["truncated"] = True
    args = chat.parser().parse_args(["watch", "--session", "sess-123", "--task-id", "task-1"])
    result = chat.execute(args, client)
    assert result["detail"]["response_observed"] is True
    assert result["detail"]["answer_completion_verified"] is False
