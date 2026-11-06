from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.admin.exceptions import AccessDeniedError
from src.orchestration.run_reports import OrchestrationRunReport
from src.orchestration.story_run_insights import current_story_run_insights, project_insight
from src.shared.models.base import utcnow
from tests.orchestration.test_execution_read import session  # noqa: F401
from tests.orchestration.test_run_report_read import seed, terminal

BOARD = {"generation": 1, "sequence": 2, "observed_at": "2026-10-30T10:00:00Z", "text": "- ☐ `test` T1 — Check recovery"}


def row(**updates):
    return SimpleNamespace(
        **{
            "run_id": "run",
            "attempt": 1,
            "persona": "operations",
            "task_board_snapshot": BOARD,
            "terminal_receipt": {
                "contract_version": 1,
                "run_id": "run",
                "attempt": 1,
                "outcome": "failed",
                "failure": {"category": "unknown", "exit_code": 1},
            },
            "operations_report_receipt": {
                "run_id": "run",
                "attempt": 1,
                "recorded": True,
                "report": (
                    "Operations story ended without a success result (error_max_turns). Diagnostic: turn_limit. "
                    "Last assistant text: Remaining checks need target access."
                ),
            },
            **updates,
        }
    )


def test_runtime_reason_and_report_survive_without_becoming_acceptance():
    result = project_insight(row(), may_read_board=True)
    assert result.failure.code == "turn_limit"
    assert result.failure.responsible == "engine"
    assert "saved progress" in result.failure.next_action
    assert "platform setting" in result.failure.next_action
    assert "target access" in result.failure.report_text
    assert result.task_board.text == BOARD["text"]
    assert result.outcome == "failed"


@pytest.mark.parametrize("persona", ["operations", "agent-codex-operations"])
def test_clean_operations_exit_keeps_blocker_when_delivery_failed(persona):
    item = row(persona=persona, node_state="failed")
    item.terminal_receipt = {"contract_version": 1, "run_id": "run", "attempt": 1, "outcome": "complete"}
    item.operations_report_receipt["report"] = "Stopped: native_console_automation_disabled; qualification not produced."
    result = project_insight(item, may_read_board=True)
    assert result.outcome == "complete"
    assert result.failure.code == "runtime_unverified"
    assert "historical agent report" in result.failure.reported_blocker
    assert "current qualification implementation" in result.failure.suggested_fix
    assert "human-observation path" in result.failure.suggested_fix
    assert "keeping browser and AWS Console automation disabled" in result.failure.suggested_fix
    assert "qualification not produced" in result.failure.report_text
    restricted = project_insight(item, may_read_board=False)
    assert restricted.failure.report_text is None
    assert restricted.failure.code == "runtime_unverified"


@pytest.mark.parametrize("state,persona", [("passed", "operations"), ("running", "operations"), ("failed", "developer")])
def test_clean_exit_is_not_mislabeled_outside_failed_operations(state, persona):
    item = row(persona=persona, node_state=state)
    item.terminal_receipt = {"contract_version": 1, "run_id": "run", "attempt": 1, "outcome": "complete"}
    assert project_insight(item, may_read_board=True).failure is None


def test_restricted_reader_gets_no_board_or_model_prose():
    result = project_insight(row(), may_read_board=False)
    assert result.board_access == "restricted" and result.task_board is None
    assert result.failure.report_text is None
    assert "target access" not in result.model_dump_json()


def test_access_denial_is_an_investigation_lead_not_a_replacement_for_runtime_cause():
    item = row()
    item.operations_report_receipt["report"] += " AccessDenied: not authorized to perform: ec2:CreateSecurityGroup on resource private-resource"
    result = project_insight(item, may_read_board=False)
    assert result.failure.code == "turn_limit"
    assert "ec2:CreateSecurityGroup" in result.failure.reported_blocker
    assert "IAM owner" in result.failure.suggested_fix
    assert "private-resource" not in result.model_dump_json()


def test_report_redacts_credentials_and_does_not_infer_runtime_cause_from_model_words():
    item = row()
    item.operations_report_receipt["report"] = "Agent says Diagnostic: turn_limit. Authorization: Bearer secret-value\nAKIAABCDEFGHIJKLMNOP"
    result = project_insight(item, may_read_board=True)
    assert result.failure.code == "unknown"
    assert "secret-value" not in result.failure.report_text
    assert "AKIA" not in result.failure.report_text


@pytest.mark.parametrize("field,value", [("run_id", "other"), ("attempt", 2), ("attempt", True)])
def test_foreign_terminal_and_report_are_not_a_current_diagnosis(field, value):
    item = row()
    item.terminal_receipt[field] = value
    assert project_insight(item, may_read_board=True).failure is None


async def test_latest_current_assignment_only_and_owner_boundary(session, monkeypatch):  # noqa: F811
    flow, node, old = await seed(session)
    old.dispatch_metadata = {"actor": {"user_id": "owner"}}
    old.task_board_snapshot = BOARD
    old.terminal_receipt = terminal(old, "failed")
    old.created_at = utcnow() - timedelta(minutes=1)
    await session.flush()
    monkeypatch.setattr("src.admin.access_control.AccessControl.check_permission", AsyncMock(side_effect=AccessDeniedError(message="denied")))
    monkeypatch.setattr("src.shared.identity.resolver.resolve_canonical_user_id", AsyncMock(return_value="owner"))
    actor = SimpleNamespace(org_id=flow.org_id, user_id="login")
    insights = await current_story_run_insights(session, current_user=actor, flow_id=flow.id)
    assert insights[node.id].task_board.text == BOARD["text"]
    monkeypatch.setattr("src.shared.identity.resolver.resolve_canonical_user_id", AsyncMock(return_value="other-owner"))
    assert (await current_story_run_insights(session, current_user=actor, flow_id=flow.id))[node.id].board_access == "restricted"
    newer = OrchestrationRunReport(
        run_id="new-run",
        credential_hash="b" * 64,
        org_id=flow.org_id,
        flow_id=flow.id,
        node_id=node.id,
        attempt=1,
        persona="agent-codex-reviewer",
        repo=old.repo,
        installation_id=1,
        dispatch_metadata={"actor": {"user_id": "owner"}},
        expires_at=utcnow() + timedelta(days=1),
    )
    session.add(newer)
    await session.flush()
    result = (await current_story_run_insights(session, current_user=actor, flow_id=flow.id))[node.id]
    assert result.run_id == "new-run" and result.task_board is None and result.failure is None
    node.attempts = 2
    await session.flush()
    assert await current_story_run_insights(session, current_user=actor, flow_id=flow.id) == {}


async def test_live_read_projects_engine_failure_after_clean_operations_exit(session, monkeypatch):  # noqa: F811
    flow, node, report = await seed(session)
    node.state = "failed"
    report.persona = "operations"
    report.terminal_receipt = terminal(report, "complete")
    report.operations_report_receipt = {
        "run_id": report.run_id,
        "attempt": report.attempt,
        "recorded": True,
        "report": "Repair blocked: branch_not_mergeable: dirty",
    }
    await session.flush()
    monkeypatch.setattr("src.admin.access_control.AccessControl.check_permission", AsyncMock())
    actor = SimpleNamespace(org_id=flow.org_id, user_id="owner")
    insight = (await current_story_run_insights(session, current_user=actor, flow_id=flow.id))[node.id]
    assert insight.failure.code == "runtime_unverified"
    assert "repair branch" in insight.failure.reported_blocker
    assert "branch_not_mergeable" in insight.failure.report_text
    actor.org_id = "another-tenant"
    assert await current_story_run_insights(session, current_user=actor, flow_id=flow.id) == {}
