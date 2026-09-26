"""Canonical admission composition over real DynamoDB conditional session writes."""

import copy
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from src.orchestration import chat_history as history
from src.orchestration import chat_tasks as chat
from src.shared.schemas.auth import TokenContext
from src.tasks import errors
from src.tasks.authz import Caller
from src.tasks.read_store import TaskRecord

TASK = "tsk_" + str(uuid.uuid4())
REAL_CALLER_FOR = chat.caller_for


@pytest.fixture
def setup(monkeypatch):
    for flag in ("FEATURE_CHAT_ENABLED", "ADP_TASK_API_HUMAN_ENABLED", "ADP_TASK_API_ADMISSION_ENABLED", "ADP_TASK_API_READ_ENABLED"):
        monkeypatch.setenv(flag, "true")
    user = TokenContext(
        user_id="user", org_id="tenant", team_id="team", department_id="", account_type="human", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    caller = Caller("human:" + str(uuid.uuid4()), "tenant", frozenset({"adp-tasks/submit", "adp-tasks/read", "adp-tasks/input"}))
    monkeypatch.setattr(chat, "caller_for", AsyncMock(return_value=(user, caller)))
    monkeypatch.setattr(chat, "personas", AsyncMock(return_value=[chat.PERSONA]))
    admission = Mock()
    admission.admit = AsyncMock(return_value={"task_id": TASK, "status": "accepted", "schema_version": "1.0"})
    monkeypatch.setattr(chat, "get_admission", lambda: admission)
    record = TaskRecord(
        task_id=TASK,
        invocation_id=str(uuid.uuid4()),
        tenant_id="tenant",
        owner_principal_id=caller.principal_id,
        persona=chat.PERSONA,
        status="running",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        updated_at="2026-09-26T00:00:00Z",
        deadline_at="2026-09-26T01:00:00Z",
    )
    monkeypatch.setattr(chat, "task_record", Mock(return_value=record))
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="sessions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        app = FastAPI()
        app.include_router(history.router)
        app.include_router(chat.router)
        app.dependency_overrides[history.store] = lambda: table
        app.dependency_overrides[history.get_current_user] = lambda: user
        app.dependency_overrides[chat.get_db] = lambda: None
        yield SimpleNamespace(client=TestClient(app), table=table, user=user, caller=caller, admission=admission, record=record)


def body(**extra):
    return {"message": "Explain this result", "request_id": "opening", "persona": chat.PERSONA, **extra}


def test_start_same_request_replays_same_canonical_task(setup):
    s = setup
    first = s.client.post("/chat/sessions", json=body())
    assert first.status_code == 202, first.text
    second = s.client.post("/chat/sessions", json=body())
    assert second.status_code == 202, second.text
    assert first.json()["session_id"] == second.json()["session_id"]
    assert first.json()["task_id"] == second.json()["task_id"] == TASK
    assert s.admission.admit.call_args_list[0].kwargs["idempotency_key"] == s.admission.admit.call_args_list[1].kwargs["idempotency_key"]
    row = s.table.get_item(Key={"session_id": first.json()["session_id"]})["Item"]
    assert len(row["messages"]) == 1 and len(row["chat_requests"]) == 1


def test_changed_request_conflicts_and_dry_run_writes_nothing(setup):
    s = setup
    assert s.client.post("/chat/sessions", json=body(dry_run=True)).json()["dispatched"] is False
    assert s.table.scan()["Count"] == 0
    s.admission.admit.assert_not_called()
    assert s.client.post("/chat/sessions", json=body()).status_code == 202
    assert s.client.post("/chat/sessions", json=body(message="different")).status_code == 409
    assert s.admission.admit.call_count == 1


def test_uncertain_admission_retains_exact_frozen_task_request(setup):
    s = setup
    s.admission.admit.side_effect = [RuntimeError("lost acknowledgement"), {"task_id": TASK, "status": "accepted"}]
    first = s.client.post("/chat/sessions", json=body())
    assert first.status_code == 503
    second = s.client.post("/chat/sessions", json=body())
    assert second.status_code == 202, second.text
    calls = s.admission.admit.call_args_list
    assert calls[0].kwargs["submit"] == calls[1].kwargs["submit"]
    assert calls[0].kwargs["idempotency_key"] == calls[1].kwargs["idempotency_key"]


def test_pending_resume_never_creates_another_task(setup):
    s = setup
    sid = s.client.post("/chat/sessions", json=body()).json()["session_id"]
    reply = s.client.post(f"/chat/sessions/{sid}/turns", json=body(request_id="next"))
    assert reply.status_code == 409
    assert s.admission.admit.call_count == 1
    snapshot = s.client.get(f"/chat/sessions/{sid}").json()
    assert snapshot["task_id"] == TASK and snapshot["task"]["status"] == "running"


def test_terminal_turn_uses_prior_correlated_answer_as_context(setup):
    s = setup
    sid = s.client.post("/chat/sessions", json=body()).json()["session_id"]
    chat.task_record.return_value = replace(
        s.record, status="completed", result={"process_exit_validated": True, "report": {"summary": "Earlier answer", "findings": []}}
    )
    reply = s.client.post(f"/chat/sessions/{sid}/turns", json=body(request_id="next", message="Explain further"))
    assert reply.status_code == 202, reply.text
    submit = s.admission.admit.call_args.kwargs["submit"]
    assert "Earlier answer" in submit["inputs"]["conversation_history"]
    assert submit["instructions"] == "Explain further"


def test_clarification_requires_exact_question_and_reuses_task_commands(setup, monkeypatch):
    s = setup
    sid = s.client.post("/chat/sessions", json=body()).json()["session_id"]
    question = str(uuid.uuid4())
    chat.task_record.return_value = replace(
        s.record, status="waiting_for_input", input_request={"input_request_id": question, "question": "Which result?"}
    )
    assert s.client.post(f"/chat/sessions/{sid}/turns", json=body(request_id="reply")).status_code == 409
    commands = Mock()
    commands.admit.return_value = {"status": "accepted", "command_id": str(uuid.uuid4())}
    monkeypatch.setattr(chat, "get_store", lambda: SimpleNamespace(repository=object()))
    monkeypatch.setattr(chat, "TaskCommands", lambda _: commands)
    result = s.client.post(f"/chat/sessions/{sid}/turns", json=body(request_id="reply", reply_to=question))
    assert result.status_code == 202, result.text
    assert result.json()["task_id"] == TASK
    assert s.admission.admit.call_count == 1
    assert commands.admit.call_args.kwargs["payload"]["reply_to"] == question


def test_dynamo_cas_prevents_stale_session_replacement(setup):
    s = setup
    sid = s.client.post("/chat/sessions", json=body()).json()["session_id"]
    row = s.table.get_item(Key={"session_id": sid})["Item"]
    version = row["chat_version"]
    winner = copy.deepcopy(row)
    winner["chat_version"] += 1
    chat.put(s.table, winner, version)
    with pytest.raises(errors.TaskApiError) as exc:
        chat.put(s.table, row, version)
    assert exc.value.status == 409
    assert s.table.get_item(Key={"session_id": sid})["Item"]["chat_version"] == version + 1


def test_lost_session_claim_ack_reconciles_without_second_turn(setup, monkeypatch):
    s = setup
    original = s.table.put_item
    count = 0

    def lost_ack(**kwargs):
        nonlocal count
        count += 1
        result = original(**kwargs)
        if count == 1:
            raise TimeoutError("lost session acknowledgement")
        return result

    monkeypatch.setattr(s.table, "put_item", lost_ack)
    assert s.client.post("/chat/sessions", json=body()).status_code == 503
    s.admission.admit.assert_not_called()
    reply = s.client.post("/chat/sessions", json=body())
    assert reply.status_code == 202, reply.text
    assert s.admission.admit.call_count == 1


def test_refused_persona_and_revoked_human_do_not_dispatch(setup, monkeypatch):
    s = setup
    assert s.client.post("/chat/sessions", json=body(persona="developer")).status_code == 403
    monkeypatch.setattr(chat, "caller_for", AsyncMock(side_effect=errors.disallowed_scope("revoked")))
    assert s.client.post("/chat/sessions", json=body()).status_code == 403
    assert s.table.scan()["Count"] == 0
    s.admission.admit.assert_not_called()


def test_real_cli_start_transport_composes_canonical_admission(setup, tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("chat_start_contract", Path(__file__).parents[2] / "cli/adp-chat.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    source = tmp_path / "question.txt"
    source.write_text("Explain this result")

    class Transport:
        def get(self, path):
            response = setup.client.get(path)
            assert response.status_code == 200, response.text
            return response.json()

        def post(self, path, body):
            response = setup.client.post(path, json=body)
            assert response.status_code == 202, response.text
            return response.json()

    args = cli.parser().parse_args(["start", "--persona", chat.PERSONA, "--message-file", str(source), "--request-id", "opening", "--yes"])
    result = cli.execute(args, Transport())
    assert result["status"] == "pending" and result["detail"]["task_id"] == TASK
    assert setup.admission.admit.call_args.kwargs["submit"]["instructions"] == "Explain this result"


def test_lost_input_ack_blocks_new_turn_until_same_request_reconciled(setup, monkeypatch):
    s = setup
    sid = s.client.post("/chat/sessions", json=body()).json()["session_id"]
    question = str(uuid.uuid4())
    chat.task_record.return_value = replace(s.record, status="waiting_for_input", input_request={"input_request_id": question})
    commands = Mock()
    commands.admit.side_effect = [TimeoutError("lost input receipt"), {"status": "accepted"}]
    monkeypatch.setattr(chat, "get_store", lambda: SimpleNamespace(repository=object()))
    monkeypatch.setattr(chat, "TaskCommands", lambda _: commands)
    payload = body(request_id="reply", reply_to=question, message="The first result")
    assert s.client.post(f"/chat/sessions/{sid}/turns", json=payload).status_code == 503
    chat.task_record.return_value = replace(s.record, status="completed", result={"process_exit_validated": True, "report": {"summary": "Done"}})
    assert s.client.post(f"/chat/sessions/{sid}/turns", json=body(request_id="next")).status_code == 409
    assert s.admission.admit.call_count == 1
    assert s.client.post(f"/chat/sessions/{sid}/turns", json=payload).status_code == 202
    row = s.table.get_item(Key={"session_id": sid})["Item"]
    assert [message["content"] for message in row["messages"]] == ["Explain this result", "The first result"]
    assert commands.admit.call_args_list[0].kwargs["command_id"] == commands.admit.call_args_list[1].kwargs["command_id"]


@pytest.mark.asyncio
async def test_signed_selected_tenant_binds_chat_row_and_response(setup, monkeypatch):
    from src.auth import tenant_context
    from src.tasks import human_authority

    s = setup
    selected = str(uuid.uuid4())
    s.user.user_id = str(uuid.uuid4())
    user = SimpleNamespace(id=s.user.user_id)
    members = {s.user.org_id: (user, SimpleNamespace(id="original")), selected: (user, SimpleNamespace(id="selected"))}
    monkeypatch.setattr(
        tenant_context,
        "get_settings",
        lambda: SimpleNamespace(token_secret_key="chat-tenant-test-secret-key-with-enough-length", cognito_user_pool_id="pool"),
    )
    monkeypatch.setattr(tenant_context, "memberships_for_login", AsyncMock(side_effect=lambda *a, **k: (None, members)))
    monkeypatch.setattr(tenant_context, "primary_team_for_workspace", AsyncMock(return_value=None))
    lease = await tenant_context.issue_context(object(), s.user, selected)
    s.user._task_tenant_lease = lease["context_token"]
    monkeypatch.setattr(chat, "caller_for", REAL_CALLER_FOR)
    monkeypatch.setattr(chat.authz, "authenticate", lambda _: (s.user, frozenset()))
    monkeypatch.setattr(
        human_authority, "resolve_human", AsyncMock(side_effect=lambda context, db: ("human:" + context.user_id, context.org_id, s.caller.scopes))
    )
    response = s.client.post("/chat/sessions", json=body())
    assert response.status_code == 202, response.text
    assert response.json()["tenant_id"] == selected
    row = s.table.get_item(Key={"session_id": response.json()["session_id"]})["Item"]
    assert row["tenant_id"] == row["org_id"] == selected
    assert row["owner_principal"] == history.owner(s.user)
    assert s.user.team_id == "" and s.user.expires_at <= lease["expires_at"]
    assert s.admission.admit.call_args.kwargs["caller"].tenant_id == selected
