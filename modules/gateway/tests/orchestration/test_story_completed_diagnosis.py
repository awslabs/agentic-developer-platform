"""Current-attempt fixed diagnostics augment observations without changing history."""

import copy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

from src.admin.exceptions import AccessDeniedError
from src.orchestration.models import OrchestrationAction
from src.orchestration.story_run_insights import MAX_CURRENT_DIAGNOSES, current_story_run_insights
from src.shared.models.base import utcnow
from tests.orchestration import test_execution_read as fixtures
from tests.orchestration.test_run_report_read import seed, terminal

session = fixtures.session
CODE = "missing_protected_input.ADP_L5_DEVOPS_LIVE_CONFIG_JSON"


async def prepare(session, monkeypatch, *, persona="operations", outcome="complete"):
    flow, node, report = await seed(session)
    node.state, node.attempts = "failed", 8
    report.persona, report.attempt = persona, 8
    report.dispatch_metadata = {"actor": {"user_id": "owner"}}
    report.terminal_receipt = terminal(report, outcome)
    execution = await fixtures.seed_execution(session, flow, node, cycle=8, phase="concluded", status="concluded")
    monkeypatch.setattr("src.admin.access_control.AccessControl.check_permission", AsyncMock())
    monkeypatch.setattr("src.shared.identity.resolver.resolve_canonical_user_id", AsyncMock(return_value="owner"))
    return SimpleNamespace(flow=flow, node=node, report=report, execution=execution, actor=SimpleNamespace(org_id=flow.org_id, user_id="owner"))


async def diagnose(session, execution, *, code=CODE, state="done", cycle=1, org_id=None):
    action = OrchestrationAction(
        org_id=org_id or execution.org_id,
        execution_id=execution.id,
        operation_key=f"operations-task:{cycle}:diagnose",
        kind="operations_task",
        status="succeeded",
        attempt=1,
        detail={"state": state, "diagnosis_code": code, "title": "private title with secret-value", "private": "never publish this"},
        created_at=utcnow() + timedelta(seconds=cycle),
    )
    session.add(action)
    await session.flush()
    return action


async def insight(session, b):
    await session.flush()
    return (await current_story_run_insights(session, current_user=b.actor, flow_id=b.flow.id))[b.node.id]


@pytest.mark.parametrize("persona", ["operations", "agent-codex-operations"])
@pytest.mark.parametrize("code", [CODE, "missing_protected_input.devops_live_config"])
async def test_known_input_and_alias_explain_exhausted_attempt_without_mutation(session, monkeypatch, persona, code):
    b = await prepare(session, monkeypatch, persona=persona)
    action = await diagnose(session, b.execution, code=code)
    original = copy.deepcopy((b.report.terminal_receipt, action.detail, b.node.state, b.node.attempts))
    result = await insight(session, b)
    assert result.failure.code == "runtime_unverified" and result.outcome == "complete"
    assert "completed diagnosis reports missing protected input ADP_L5_DEVOPS_LIVE_CONFIG_JSON" in result.failure.reported_blocker
    assert "release configuration owner" in result.failure.suggested_fix
    assert "genuine qualification inputs" in result.failure.suggested_fix
    assert "secret-value" not in result.model_dump_json() and "never publish" not in result.model_dump_json()
    assert (b.report.terminal_receipt, action.detail, b.node.state, b.node.attempts) == original


@pytest.mark.parametrize("state", ["open", "in_progress", "blocked"])
async def test_newer_unfinished_diagnosis_does_not_erase_completed_input(session, monkeypatch, state):
    b = await prepare(session, monkeypatch)
    await diagnose(session, b.execution)
    await diagnose(session, b.execution, cycle=2, state=state, code="source_defect")
    assert "ADP_L5_DEVOPS_LIVE_CONFIG_JSON" in (await insight(session, b)).failure.reported_blocker


async def test_newer_completed_unknown_code_supersedes_old_input_without_exposing_it(session, monkeypatch):
    b = await prepare(session, monkeypatch)
    await diagnose(session, b.execution)
    await diagnose(session, b.execution, cycle=2, code="missing_protected_input.secret_value_not_allowlisted")
    result = await insight(session, b)
    assert result.failure.reported_blocker is None and result.failure.suggested_fix is None
    assert result.failure.code == "runtime_unverified"
    assert "secret_value" not in result.model_dump_json()


@pytest.mark.parametrize("code", [True, 123, "not a valid code", "x" * 65])
async def test_invalid_completed_codes_do_not_supersede_a_valid_diagnosis(session, monkeypatch, code):
    b = await prepare(session, monkeypatch)
    await diagnose(session, b.execution)
    await diagnose(session, b.execution, cycle=2, code=code)
    assert "ADP_L5_DEVOPS_LIVE_CONFIG_JSON" in (await insight(session, b)).failure.reported_blocker


async def test_restricted_reader_gets_only_fixed_diagnostic_no_board_or_private_prose(session, monkeypatch):
    b = await prepare(session, monkeypatch)
    await diagnose(session, b.execution)
    b.report.operations_report_receipt = {"run_id": b.report.run_id, "attempt": 8, "recorded": True, "report": "Private run prose"}
    b.report.task_board_snapshot = {"generation": 1, "sequence": 1, "observed_at": utcnow().isoformat(), "text": "Private task text"}
    monkeypatch.setattr("src.admin.access_control.AccessControl.check_permission", AsyncMock(side_effect=AccessDeniedError(message="denied")))
    monkeypatch.setattr("src.shared.identity.resolver.resolve_canonical_user_id", AsyncMock(return_value="other-owner"))
    result = await insight(session, b)
    assert result.board_access == "restricted" and result.task_board is None and result.failure.report_text is None
    assert "ADP_L5_DEVOPS_LIVE_CONFIG_JSON" in result.failure.reported_blocker
    assert "Private" not in result.model_dump_json() and "secret-value" not in result.model_dump_json()


@pytest.mark.parametrize("wrong", ["attempt", "node", "flow", "action_tenant", "execution_tenant", "persona"])
async def test_other_assignment_or_ledger_scope_cannot_supply_current_diagnosis(session, monkeypatch, wrong):
    b = await prepare(session, monkeypatch)
    target = b.execution
    if wrong == "attempt":
        target = await fixtures.seed_execution(session, b.flow, b.node, cycle=7)
    elif wrong == "node":
        other = await fixtures.seed_node(session, b.flow, node_ref="other-node")
        target = await fixtures.seed_execution(session, b.flow, other, cycle=8)
    elif wrong == "flow":
        other = await fixtures.seed_flow(session, slug="other-flow")
        target.flow_id = other.id
    elif wrong == "execution_tenant":
        target.org_id = "other-tenant"
    elif wrong == "persona":
        b.report.persona = "developer"
    await diagnose(session, target, org_id="other-tenant" if wrong == "action_tenant" else None)
    result = await insight(session, b)
    assert result.failure is None or result.failure.reported_blocker is None
    assert "ADP_L5_DEVOPS_LIVE_CONFIG_JSON" not in result.model_dump_json()


async def test_turn_limit_remains_primary_and_human_evidence_gets_fixed_safe_guidance(session, monkeypatch):
    b = await prepare(session, monkeypatch, outcome="failed")
    b.report.operations_report_receipt = {
        "run_id": b.report.run_id,
        "attempt": 8,
        "recorded": True,
        "report": "Operations story ended without a success result (error_max_turns). Diagnostic: turn_limit. Last assistant text: private",
    }
    await diagnose(session, b.execution, code="devops_human_evidence_required")
    result = await insight(session, b)
    assert result.failure.code == "turn_limit" and result.failure.responsible == "engine"
    assert "saved progress" in result.failure.next_action and "platform setting" in result.failure.next_action
    assert "attributed DevOps qualification evidence" in result.failure.reported_blocker
    assert "ADP SSO" in result.failure.suggested_fix and "automation disabled" in result.failure.suggested_fix


async def test_overflow_is_explicit_and_bulk_lookup_is_read_only(session, monkeypatch):
    b = await prepare(session, monkeypatch)
    for cycle in range(1, MAX_CURRENT_DIAGNOSES + 2):
        await diagnose(session, b.execution, cycle=cycle)
    _, other_node, other_report = await seed(session, flow=b.flow)
    other_node.state, other_node.attempts = "failed", 8
    other_report.persona, other_report.attempt = "operations", 8
    other_report.terminal_receipt = terminal(other_report, "complete")
    other_execution = await fixtures.seed_execution(session, b.flow, other_node, cycle=8, phase="concluded", status="concluded")
    await diagnose(session, other_execution, code="missing_protected_input.ADP_L5_SECURITY_LIVE_CONFIG_JSON")
    sql = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        sql.append(statement)

    event.listen(session.bind.sync_engine, "before_cursor_execute", capture)
    try:
        results = await current_story_run_insights(session, current_user=b.actor, flow_id=b.flow.id)
        result = results[b.node.id]
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", capture)
    assert "more completed diagnosis records" in result.failure.reported_blocker
    assert "no partial diagnosis was selected" in result.failure.suggested_fix
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in sql)
    assert len([statement for statement in sql if "orchestration_actions" in statement]) == 1
    assert "ADP_L5_DEVOPS_LIVE_CONFIG_JSON" not in result.model_dump_json()
    assert "ADP_L5_SECURITY_LIVE_CONFIG_JSON" in results[other_node.id].failure.reported_blocker
