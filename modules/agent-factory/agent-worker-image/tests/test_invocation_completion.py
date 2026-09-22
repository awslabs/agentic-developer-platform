"""New triggers are new work; completed queue deliveries are not."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import entrypoint
from lib import invocation_completion as completion
from lib import invocation_status


@pytest.fixture
def delivery(monkeypatch):
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "false")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "adp-test-webhook-events")
    envelope = {
        "version": "1.0",
        "channel": "github",
        "tenant_id": "tenant-a",
        "persona": "aidlc",
        "message_id": "gate-answer-1",
        "arrived_at": "2026-09-15T12:00:00Z",
        "source_ref": {"installation_id": 123, "repo": "example/repo", "issue": 42},
        "actor": {"user_id": "user-1", "github_login": "operator", "is_bot": False},
        "intent": {"trigger": "issue_comment", "label": "aidlc"},
    }
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="adp-test-webhook-events",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "event_id", "KeyType": "HASH"},
                {"AttributeName": "arrived_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": name, "AttributeType": "S"} for name in ("event_id", "arrived_at")
            ],
        )
        monkeypatch.setattr(invocation_status, "_ddb", client)
        monkeypatch.setattr(invocation_status, "_table_name", "adp-test-webhook-events")
        seed(client, envelope)
        yield client, envelope


def seed(client, envelope, status="webhook_received"):
    client.put_item(
        TableName="adp-test-webhook-events",
        Item={
            "event_id": {"S": envelope["message_id"]},
            "arrived_at": {"S": envelope["arrived_at"]},
            "tenant_id": {"S": envelope["tenant_id"]},
            "repo": {"S": envelope["source_ref"]["repo"]},
            "persona": {"S": envelope["persona"]},
            "status": {"S": status},
            "summary": {"S": "existing outcome"},
        },
    )


def row(client, envelope):
    return client.get_item(
        TableName="adp-test-webhook-events",
        Key={
            "event_id": {"S": envelope["message_id"]},
            "arrived_at": {"S": envelope["arrived_at"]},
        },
        ConsistentRead=True,
    ).get("Item")


def test_completed_delivery_does_not_block_next_answer_on_same_issue(delivery):
    client, first = delivery
    assert completion.is_delivery_completed(first) is False
    completion.record_delivery_completed(first)
    assert completion.is_delivery_completed(first) is True
    next_answer = {**first, "message_id": "gate-answer-2"}
    seed(client, next_answer)
    assert completion.is_delivery_completed(next_answer) is False
    assert completion.is_delivery_completed(first) is True


@pytest.mark.parametrize("status", ["webhook_received", "in_progress", "failed"])
def test_unfinished_or_retryable_delivery_can_resume(delivery, status):
    client, envelope = delivery
    seed(client, envelope, status)
    assert completion.is_delivery_completed(envelope) is False
    assert row(client, envelope)["status"] == {"S": status}


def test_legacy_completion_and_existing_outcome_are_preserved(delivery):
    client, envelope = delivery
    seed(client, envelope, "complete")
    assert completion.is_delivery_completed(envelope) is True
    assert row(client, envelope)["status"] == {"S": "complete"}
    assert row(client, envelope)["summary"] == {"S": "existing outcome"}


def test_legacy_aidlc_receipt_is_promoted_to_generic_completion(delivery):
    client, envelope = delivery
    client.update_item(
        TableName="adp-test-webhook-events",
        Key={
            "event_id": {"S": envelope["message_id"]},
            "arrived_at": {"S": envelope["arrived_at"]},
        },
        UpdateExpression="SET aidlc_delivery_completed = :done",
        ExpressionAttributeValues={":done": {"BOOL": True}},
    )
    assert completion.is_delivery_completed(envelope) is True
    assert row(client, envelope)["delivery_completed"] == {"BOOL": True}


def test_receipt_survives_later_dashboard_status_updates(delivery):
    _, envelope = delivery
    completion.record_delivery_completed(envelope)
    invocation_status.update_status(envelope["message_id"], envelope["arrived_at"], "in_progress")
    assert completion.is_delivery_completed(envelope) is True


def test_completion_between_checks_defers_then_skips(delivery, monkeypatch):
    client, envelope = delivery
    updates = MagicMock()

    def concurrent_completion(**request):
        if updates.update_item.call_count == 2:
            client.update_item(
                TableName=request["TableName"],
                Key=request["Key"],
                UpdateExpression="SET aidlc_delivery_completed = :done",
                ExpressionAttributeValues={":done": {"BOOL": True}},
            )
        return client.update_item(**request)

    updates.update_item.side_effect = concurrent_completion
    monkeypatch.setattr(completion, "_get_client", lambda: updates)
    with pytest.raises(completion.InvocationCompletionError):
        completion.is_delivery_completed(envelope)
    assert completion.is_delivery_completed(envelope) is True


@pytest.mark.parametrize("field", ["tenant_id", "persona", "repo", "arrived_at", "message_id"])
def test_mismatched_or_missing_row_is_not_created_or_consumed(delivery, field):
    client, envelope = delivery
    other = copy.deepcopy(envelope)
    target = other["source_ref"] if field == "repo" else other
    target[field] = "different"
    before = row(client, envelope)
    for operation in (completion.is_delivery_completed, completion.record_delivery_completed):
        with pytest.raises(completion.InvocationCompletionError):
            operation(other)
    assert row(client, envelope) == before
    assert client.scan(TableName="adp-test-webhook-events")["Count"] == 1


def test_missing_table_configuration_fails_before_work(delivery, monkeypatch):
    _, envelope = delivery
    monkeypatch.delenv("WEBHOOK_EVENTS_TABLE")
    with pytest.raises(completion.InvocationCompletionError):
        completion.is_delivery_completed(envelope)


def test_authority_path_cannot_fall_back_to_direct_receipts(delivery, monkeypatch):
    _, envelope = delivery
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    client = MagicMock()
    monkeypatch.setattr(completion, "_get_client", client)
    for operation in (completion.is_delivery_completed, completion.record_delivery_completed):
        with pytest.raises(completion.InvocationCompletionError):
            operation(envelope)
    client.assert_not_called()


@pytest.fixture
def worker(delivery, monkeypatch, tmp_path):
    client, envelope = delivery
    # main writes run-specific environment variables directly.
    monkeypatch.setattr(entrypoint.os, "environ", dict(os.environ))
    monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789012/agent.fifo")
    monkeypatch.setenv("ADP_GH_TOKEN_BROKER_ENABLED", "0")
    monkeypatch.setattr(entrypoint, "WORK_DIR", tmp_path / "repo")
    (tmp_path / "repo").mkdir()
    monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
    monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")
    for name in (
        "BootstrapLogger",
        "VisibilityHeartbeat",
        "_load_door_api_key",
        "_stop_sigv4_proxy",
        "_setup_agent_control",
        "_teardown_agent_control",
        "_record_session_id",
        "_upload_transcript_to_s3",
    ):
        monkeypatch.setattr(entrypoint, name, MagicMock(return_value=None))
    monkeypatch.setattr(entrypoint, "BootstrapLogger", MagicMock())
    monkeypatch.setattr(entrypoint, "VisibilityHeartbeat", MagicMock())
    monkeypatch.setattr(entrypoint, "_start_sigv4_proxy", MagicMock())
    monkeypatch.setattr(entrypoint, "_read_run_reports", lambda: ("", ""))
    monkeypatch.setattr(entrypoint, "_read_result_metadata", lambda: {})
    monkeypatch.setattr(
        entrypoint,
        "VaultClient",
        MagicMock(
            return_value=MagicMock(
                get_secret=MagicMock(return_value={"app_id": "123", "private_key": "test"})
            )
        ),
    )
    monkeypatch.setattr(entrypoint, "mint_installation_token", MagicMock(return_value="test-token"))
    monkeypatch.setattr(
        entrypoint,
        "create_check_run",
        MagicMock(return_value={"id": 1, "html_url": "https://example.test/check"}),
    )
    monkeypatch.setattr(entrypoint, "update_check_run", MagicMock())
    monkeypatch.setattr(
        entrypoint, "run_cmd", MagicMock(return_value=MagicMock(stdout="", returncode=0))
    )
    monkeypatch.setattr(entrypoint.shutil, "copytree", MagicMock())
    merged = MagicMock(return_value=True)
    monkeypatch.setattr(entrypoint, "_is_already_completed", merged)
    executions = []
    exit_codes = [0]

    def run(command, **kwargs):
        if command[0] == "node":
            executions.append(envelope["message_id"])
            return MagicMock(returncode=exit_codes[-1], stdout="", stderr="")
        return MagicMock(
            returncode=2 if command[:2] == ["git", "ls-remote"] else 0, stdout="", stderr=""
        )

    monkeypatch.setattr(entrypoint.subprocess, "run", run)
    monkeypatch.setattr(
        entrypoint, "_receive_one_message", lambda *_: (json.dumps(envelope), "receipt")
    )
    acknowledgements = MagicMock()
    monkeypatch.setattr(entrypoint, "_delete_message", acknowledgements)

    def terminal(success):
        invocation_status.update_status(
            envelope["message_id"], envelope["arrived_at"], "complete" if success else "failed"
        )
        return 0 if success else exit_codes[-1]

    monkeypatch.setattr(entrypoint, "_handle_success", lambda *args, **kwargs: terminal(True))
    monkeypatch.setattr(entrypoint, "_handle_failure", lambda *args: terminal(False))
    return client, envelope, executions, exit_codes, acknowledgements, merged


def test_main_completed_message_runs_once_and_new_answer_runs(worker):
    client, envelope, executions, _, ack, merged = worker

    def verify_receipt(*args):
        assert row(client, envelope)["delivery_completed"] == {"BOOL": True}

    ack.side_effect = verify_receipt
    assert entrypoint.main() == 0
    assert entrypoint.main() == 0
    assert executions == ["gate-answer-1"]
    assert row(client, envelope)["status"] == {"S": "complete"}
    envelope["message_id"] = "gate-answer-2"
    seed(client, envelope)
    assert entrypoint.main() == 0
    assert executions == ["gate-answer-1", "gate-answer-2"]
    merged.assert_not_called()


def test_shared_codex_engine_uses_report_ownership_not_legacy_dynamodb_receipts(worker, monkeypatch):
    from lib import codex_review_delivery, review_cycle_input

    client, envelope, executions, _, ack, _ = worker
    envelope["persona"] = "agent-codex-reviewer"
    envelope["intent"]["trigger"] = "engine_review_cycle"
    envelope["review_cycle_input"] = {
        "action": "review", "repo": envelope["source_ref"]["repo"], "pr_number": 42,
        "head_sha": "a" * 40, "accepted_scope": "story-revision", "operation_key": "review:1", "findings": [],
    }
    seed(client, envelope)
    monkeypatch.setattr(entrypoint.run_report, "enabled", lambda: True)
    monkeypatch.setattr(entrypoint, "resume_pr_handoff", lambda: False)
    started, terminal = MagicMock(), MagicMock()
    monkeypatch.setattr(entrypoint.run_report, "begin_delivery", started)
    monkeypatch.setattr(entrypoint.run_report, "terminal", terminal)
    monkeypatch.setattr(review_cycle_input, "checkout_cycle_input", lambda *args, **kwargs: ("story", "a" * 40))
    monkeypatch.setattr(codex_review_delivery, "finish_engine_review", lambda *args, **kwargs: "Review evidence recorded")
    forbidden = MagicMock(side_effect=AssertionError("Shared engine ownership must not read legacy receipts"))
    monkeypatch.setattr(entrypoint, "is_delivery_completed", forbidden)
    monkeypatch.setattr(entrypoint, "record_delivery_completed", forbidden)
    assert entrypoint.main() == 0
    assert len(executions) == 1
    started.assert_called_once()
    terminal.assert_called_once_with("complete")
    forbidden.assert_not_called()
    ack.assert_called_once()


def test_server_retired_delivery_acknowledges_without_bootstrap_or_receipt_changes(worker, monkeypatch):
    client, envelope, executions, _, ack, _ = worker
    seed(client, envelope, status="failed")
    original = row(client, envelope)
    monkeypatch.setattr(entrypoint.run_report, "enabled", lambda: True)
    monkeypatch.setattr(entrypoint.run_report, "request", MagicMock(return_value={
        "block_code": "execution_assignment_superseded", "retryable": False,
    }))
    forbidden = MagicMock(side_effect=AssertionError("Retirement must not touch delivery evidence"))
    monkeypatch.setattr(entrypoint.run_report, "read_spool", forbidden)
    monkeypatch.setattr(entrypoint.run_report, "terminal", forbidden)
    ack.side_effect = [RuntimeError("queue acknowledgement lost"), None]
    with pytest.raises(RuntimeError, match="queue acknowledgement lost"):
        entrypoint.main()
    assert entrypoint.main() == 0
    assert ack.call_count == 2
    assert executions == []
    assert row(client, envelope) == original
    entrypoint.VaultClient.assert_not_called()
    forbidden.assert_not_called()


@pytest.mark.parametrize("status", [401, 403, 404, 409, 429, 502])
def test_report_http_failure_never_acknowledges_a_potentially_current_delivery(worker, monkeypatch, status):
    _, _, executions, _, ack, _ = worker
    monkeypatch.setattr(entrypoint.run_report, "enabled", lambda: True)
    monkeypatch.setattr(entrypoint.run_report, "request", MagicMock(
        side_effect=entrypoint.run_report.RunReportError(f"run_report_http_{status}", retryable=status >= 500),
    ))
    assert entrypoint.main() == entrypoint.AGENT_EXIT_RETRYABLE
    assert executions == []
    ack.assert_not_called()
    entrypoint.VaultClient.assert_not_called()


def test_codex_issue_review_redelivery_runs_adapter_once(worker):
    client, envelope, executions, _, ack, merged = worker
    envelope["persona"] = "agent-codex-reviewer"
    envelope["payload"] = {
        "issue": {"number": 42, "title": "Review this"},
        "comment": {"body": "@agent-codex-reviewer review this issue"},
    }
    seed(client, envelope)

    ack.side_effect = [RuntimeError("SQS unavailable"), None]
    with pytest.raises(RuntimeError, match="SQS unavailable"):
        entrypoint.main()
    assert entrypoint.main() == 0

    assert executions == ["gate-answer-1"]
    assert row(client, envelope)["delivery_completed"] == {"BOOL": True}
    merged.assert_not_called()


def test_ack_failure_does_not_rerun_completed_work(worker):
    _, _, executions, _, ack, _ = worker
    ack.side_effect = [RuntimeError("SQS unavailable"), None]
    assert entrypoint.main() == 0
    assert entrypoint.main() == 0
    assert len(executions) == 1


def test_completed_delivery_skips_credentials_and_repository_work(worker):
    client, envelope, executions, _, ack, _ = worker
    seed(client, envelope, "complete")
    ack.side_effect = [RuntimeError("SQS unavailable"), None]
    assert entrypoint.main() == entrypoint.AGENT_EXIT_RETRYABLE
    assert entrypoint.main() == 0
    assert executions == []
    entrypoint.VaultClient.assert_not_called()
    entrypoint.mint_installation_token.assert_not_called()
    entrypoint.run_cmd.assert_not_called()
    assert row(client, envelope)["summary"] == {"S": "existing outcome"}


def test_retryable_exit_preserves_message_then_retries(worker):
    client, envelope, executions, exit_codes, ack, _ = worker
    exit_codes.append(entrypoint.AGENT_EXIT_RETRYABLE)
    assert entrypoint.main() == entrypoint.AGENT_EXIT_RETRYABLE
    ack.assert_not_called()
    assert row(client, envelope)["delivery_completed"] == {"BOOL": False}
    exit_codes.append(0)
    assert entrypoint.main() == 0
    assert len(executions) == 2
    assert row(client, envelope)["delivery_completed"] == {"BOOL": True}


def test_storage_read_failure_runs_and_acknowledges_nothing(worker, monkeypatch):
    _, _, executions, _, ack, _ = worker
    monkeypatch.setattr(
        completion,
        "_get_client",
        MagicMock(
            side_effect=ClientError({"Error": {"Code": "AccessDeniedException"}}, "UpdateItem")
        ),
    )
    assert entrypoint.main() == entrypoint.AGENT_EXIT_RETRYABLE
    assert executions == []
    ack.assert_not_called()


def test_interrupted_execution_can_retry(worker, monkeypatch):
    client, envelope, executions, _, ack, _ = worker
    run = entrypoint.subprocess.run

    def interrupted(command, **kwargs):
        result = run(command, **kwargs)
        if command[0] == "node" and len(executions) == 1:
            raise RuntimeError("worker interrupted")
        return result

    monkeypatch.setattr(entrypoint.subprocess, "run", interrupted)
    with pytest.raises(RuntimeError, match="worker interrupted"):
        entrypoint.main()
    ack.assert_not_called()
    assert row(client, envelope)["delivery_completed"] == {"BOOL": False}
    assert entrypoint.main() == 0
    assert len(executions) == 2
    assert row(client, envelope)["delivery_completed"] == {"BOOL": True}


def test_reported_terminal_failure_is_consumed_once(worker):
    client, envelope, executions, exit_codes, ack, _ = worker
    exit_codes.append(1)
    assert entrypoint.main() == 1
    ack.assert_called_once()
    assert row(client, envelope)["delivery_completed"] == {"BOOL": True}
    assert entrypoint.main() == 0
    assert len(executions) == 1
    assert row(client, envelope)["status"] == {"S": "failed"}


def test_authority_enabled_main_requires_protected_bootstrap(worker, monkeypatch):
    _, envelope, executions, _, ack, _ = worker
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    check = MagicMock()
    receipt = MagicMock()
    monkeypatch.setattr(entrypoint, "is_delivery_completed", check)
    monkeypatch.setattr(entrypoint, "record_delivery_completed", receipt)
    bootstrap = MagicMock(side_effect=RuntimeError("protected dispatch refused"))
    monkeypatch.setattr("lib.run_identity.bootstrap_run_identity", bootstrap)
    with pytest.raises(RuntimeError, match="protected dispatch refused"):
        entrypoint.main()
    bootstrap.assert_called_once_with(envelope)
    check.assert_not_called()
    receipt.assert_not_called()
    ack.assert_not_called()
    assert executions == []


def test_receipt_write_failure_does_not_ack(worker, monkeypatch):
    _, _, executions, _, ack, _ = worker
    monkeypatch.setattr(
        entrypoint,
        "record_delivery_completed",
        MagicMock(side_effect=completion.InvocationCompletionError("unavailable")),
    )
    assert entrypoint.main() == entrypoint.AGENT_EXIT_RETRYABLE
    assert len(executions) == 1
    ack.assert_not_called()
    # The durable legacy status still avoids re-execution on redelivery.
    assert entrypoint.main() == 0
    assert len(executions) == 1


@pytest.mark.parametrize("sha_length", [40, 64])
@pytest.mark.parametrize("ack_fails", [False, True])
def test_stale_pr_review_releases_queue_before_current_review(
    worker, monkeypatch, sha_length, ack_fails
):
    client, envelope, executions, _, ack, _ = worker
    expected, current = "a" * sha_length, "b" * sha_length
    envelope["persona"] = "agent-codex-reviewer"
    envelope["source_ref"].update(pr=42, sha=expected)
    envelope["payload"] = {"pull_request": {"number": 42, "head": {"ref": "agent/issue-42"}}}
    seed(client, envelope)
    monkeypatch.setattr(entrypoint, "_checkout_existing_work_branch", MagicMock())
    monkeypatch.setattr(entrypoint, "run_cmd", MagicMock(return_value=MagicMock(stdout=current)))
    attempts = 0

    def verify_obsolete_receipt(*_args):
        nonlocal attempts
        attempts += 1
        persisted = row(client, envelope)
        assert persisted["status"] == {"S": "skipped"}
        assert persisted["skip_reason"] == {"S": "stale_review_head"}
        assert json.loads(persisted["summary"]["S"]) == {
            "status": "stale",
            "expected": expected,
            "actual": current,
        }
        assert executions == []
        if ack_fails and attempts == 1:
            raise RuntimeError("SQS unavailable")

    ack.side_effect = verify_obsolete_receipt
    if ack_fails:
        with pytest.raises(RuntimeError, match="SQS unavailable"):
            entrypoint.main()
    assert entrypoint.main() == 0
    assert ack.call_count == 1 + int(ack_fails)
    ack.assert_called_with(os.environ["QUEUE_URL"], "us-east-1", "receipt")
    entrypoint.create_check_run.assert_not_called()
    entrypoint._start_sigv4_proxy.assert_not_called()

    # A new event for the actual head must still reach the review adapter.
    ack.side_effect = None
    envelope["message_id"] = "current-review"
    envelope["source_ref"]["sha"] = current
    seed(client, envelope)
    assert entrypoint.main() == 0
    assert executions == ["current-review"]
    assert row(client, envelope)["status"] == {"S": "complete"}


@pytest.mark.parametrize(
    "expected,current", [("", "b" * 40), ("bad-sha", "b" * 40), ("a" * 40, "")]
)
def test_unverifiable_pr_head_does_not_acknowledge(worker, monkeypatch, expected, current):
    client, envelope, executions, _, ack, _ = worker
    envelope["persona"] = "agent-codex-reviewer"
    envelope["source_ref"].update(pr=42, sha=expected)
    envelope["payload"] = {"pull_request": {"number": 42, "head": {"ref": "agent/issue-42"}}}
    seed(client, envelope)
    monkeypatch.setattr(entrypoint, "_checkout_existing_work_branch", MagicMock())
    monkeypatch.setattr(entrypoint, "run_cmd", MagicMock(return_value=MagicMock(stdout=current)))
    with pytest.raises(RuntimeError, match="review head changed before checkout"):
        entrypoint.main()
    ack.assert_not_called()
    assert executions == []


def test_pr_checkout_transport_failure_remains_retryable(worker, monkeypatch):
    client, envelope, executions, _, ack, _ = worker
    envelope["persona"] = "agent-codex-reviewer"
    envelope["source_ref"].update(pr=42, sha="a" * 40)
    envelope["payload"] = {"pull_request": {"number": 42, "head": {"ref": "agent/issue-42"}}}
    seed(client, envelope)
    monkeypatch.setattr(
        entrypoint,
        "_checkout_existing_work_branch",
        MagicMock(side_effect=RuntimeError("fetch unavailable")),
    )
    with pytest.raises(RuntimeError, match="fetch unavailable"):
        entrypoint.main()
    ack.assert_not_called()
    assert executions == []
