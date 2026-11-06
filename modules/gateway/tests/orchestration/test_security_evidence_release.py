"""Real accepted authority and execution transitions fence Security captures."""

import copy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration import flow_controls, routes
from src.orchestration.execution_state import ExecutionIdentity, ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
from src.orchestration.execution_store import advance_execution
from src.orchestration.models import OrchestrationAction, OrchestrationDecision
from src.orchestration.security_evidence_release import release_scope
from src.orchestration.shared_window import accepted_document_hash
from src.shared.schemas.auth import TokenContext
from tests.orchestration.test_l5_observation_scope import product_case, scope_case  # noqa: F401
from tests.orchestration.test_policy_admission import APPROVER, ORG_A, TEAM_A


@pytest.fixture
async def security_release(scope_case):  # noqa: F811
    c = scope_case
    document = copy.deepcopy(c.plan.plan_document)
    release = document["nodes"][0]["operational_release_binding"]
    release["component"] = "l5-security"
    release["delivery_contract"]["resource_id"] = "security"
    c.plan.plan_document = document
    c.plan.plan_hash = accepted_document_hash(c.plan)
    await c.db.commit()
    return c


def identity(c):
    return ExecutionIdentity(
        org_id=ORG_A, node_id=c.node.id, cycle=1, accepted_plan_version=1, claim_id=c.claim.id, claim_generation=c.claim.generation
    )


async def advance(c, phase="delivering", status="awaiting_external"):
    await c.db.refresh(c.execution)
    return await advance_execution(
        c.db,
        identity=identity(c),
        advance=PhaseAdvance(
            phase=ExecutionPhase(phase),
            status=ExecutionStatus(status),
            expected_revision=c.execution.revision,
            next_check_at=datetime.now(UTC) + timedelta(seconds=20),
        ),
    )


async def test_harmless_poll_keeps_legacy_anchor_but_away_and_back_invalidates(security_release):
    c = security_release
    before, _ = await release_scope(c.db, c.action)
    assert before["execution_transition_id"] is None
    assert (await advance(c)).kind == OutcomeKind.APPLIED
    await c.db.commit()
    assert (await release_scope(c.db, c.action))[0] == before
    assert (await advance(c, status="runnable")).kind == OutcomeKind.APPLIED
    await c.db.commit()
    moved, _ = await release_scope(c.db, c.action)
    assert moved["execution_transition_id"] is not None
    assert (await advance(c)).kind == OutcomeKind.APPLIED
    await c.db.commit()
    restored, _ = await release_scope(c.db, c.action)
    assert restored["execution_phase"] == before["execution_phase"]
    assert restored["execution_status"] == before["execution_status"]
    assert restored["execution_transition_id"] != moved["execution_transition_id"]
    assert restored != before
    assert (await advance(c)).kind == OutcomeKind.APPLIED
    await c.db.commit()
    assert (await release_scope(c.db, c.action))[0] == restored


async def test_transition_and_decision_roll_back_together(security_release):
    c = security_release
    before, _ = await release_scope(c.db, c.action)
    action_id = c.action.id
    assert (await advance(c, status="runnable")).kind == OutcomeKind.APPLIED
    await c.db.rollback()
    action = await c.db.get(OrchestrationAction, action_id)
    assert (await release_scope(c.db, action))[0] == before
    assert not list(await c.db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "execution_phase_changed")))


async def test_actual_pause_resume_does_not_restore_prior_control_anchor(security_release, monkeypatch):
    c = security_release
    actor = TokenContext(
        user_id=APPROVER,
        org_id=ORG_A,
        team_id=TEAM_A,
        department_id="",
        account_type="human",
        token_use="access",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    monkeypatch.setattr(flow_controls.AccessControl, "check_permission", AsyncMock())
    monkeypatch.setattr(routes, "_resolve_actor_role", AsyncMock(return_value="owner"))
    before, _ = await release_scope(c.db, c.action)
    await flow_controls.set_flow_execution(c.flow.id, flow_controls.FlowExecutionRequest(paused=True), actor, c.db)
    paused, _ = await release_scope(c.db, c.action)
    await flow_controls.set_flow_execution(c.flow.id, flow_controls.FlowExecutionRequest(paused=False), actor, c.db)
    resumed, _ = await release_scope(c.db, c.action)
    assert len({before["flow_control_id"], paused["flow_control_id"], resumed["flow_control_id"]}) == 3
    assert not c.flow.execution_paused


async def test_displaced_claim_cannot_write_transition(security_release):
    c = security_release
    stale = identity(c)
    c.claim.generation += 1
    await c.db.commit()
    result = await advance_execution(
        c.db,
        identity=stale,
        advance=PhaseAdvance(
            phase=ExecutionPhase.DELIVERING,
            status=ExecutionStatus.RUNNABLE,
            expected_revision=c.execution.revision,
            next_check_at=datetime.now(UTC) + timedelta(seconds=20),
        ),
    )
    assert result.kind == OutcomeKind.CONFLICT
    assert not list(await c.db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "execution_phase_changed")))
