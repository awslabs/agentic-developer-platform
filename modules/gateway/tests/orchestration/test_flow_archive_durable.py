"""Persisted work, not just the graph label, determines archive safety."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.orchestration.environment_leases import LeaseState
from src.orchestration.execution_state import ActionStatus, ExecutionPhase, ExecutionStatus
from src.orchestration.models import (
    ClaimState,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationEnvironmentLease,
    OrchestrationExecution,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.work_claims import OwnerKind
from tests.orchestration import test_flow_controls as fixtures
from tests.orchestration.test_controls import ORG_A, seed_flow

session, controls = fixtures.session, fixtures.controls


@pytest.mark.parametrize("effect", ["deployment", "prepared", "dispatched", "unknown", "claim", "lease"])
async def test_archive_refuses_durable_work_after_node_passed(session, controls, effect):
    flow = await seed_flow(session)
    node = OrchestrationNode(
        org_id=ORG_A,
        flow_id=flow.id,
        epic_ref="epic",
        wave_ref="wave",
        node_ref="story",
        kind="story",
        title="Passed",
        state="passed",
    )
    session.add(node)
    await session.flush()
    if effect == "claim":
        session.add(
            OrchestrationWorkClaim(
                org_id=ORG_A,
                provider_repository_id=123,
                issue_number=7358,
                owner_kind=OwnerKind.ENGINE_FLOW.value,
                owner_ref=flow.id,
                state=ClaimState.HELD.value,
            )
        )
    else:
        execution = OrchestrationExecution(
            org_id=ORG_A,
            flow_id=flow.id,
            node_id=node.id,
            cycle=1,
            phase=(ExecutionPhase.DEPLOYMENT_PENDING if effect == "deployment" else ExecutionPhase.CONCLUDED).value,
            status=(ExecutionStatus.RUNNABLE if effect == "deployment" else ExecutionStatus.CONCLUDED).value,
            accepted_plan_version=1,
            claim_id="claim-1",
            claim_generation=1,
        )
        session.add(execution)
        await session.flush()
        if effect != "deployment":
            action = OrchestrationAction(
                org_id=ORG_A,
                execution_id=execution.id,
                operation_key="deploy",
                kind="deploy",
                status=(ActionStatus.SUCCEEDED if effect == "lease" else ActionStatus(effect)).value,
            )
            session.add(action)
            await session.flush()
            if effect == "lease":
                session.add(
                    OrchestrationEnvironmentLease(
                        canonical_target_key="target-1",
                        evidence_source="test",
                        evidence_verified_at=datetime.now(UTC),
                        state=LeaseState.HELD.value,
                        owner_org_id=ORG_A,
                        owner_action_id=action.id,
                    )
                )
    await session.commit()

    response = await controls[0].post(f"/orchestration/flows/{flow.id}/archive", json={"reason": "No further work is expected"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "flow_has_active_work"
    await session.refresh(flow)
    assert flow.archived_at is None
    assert list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id))) == []


async def test_completed_execution_and_observed_action_allow_archive(session, controls):
    flow = await seed_flow(session)
    node = OrchestrationNode(
        org_id=ORG_A,
        flow_id=flow.id,
        epic_ref="epic",
        wave_ref="wave",
        node_ref="story",
        kind="story",
        title="Passed",
        state="passed",
    )
    session.add(node)
    await session.flush()
    execution = OrchestrationExecution(
        org_id=ORG_A,
        flow_id=flow.id,
        node_id=node.id,
        cycle=1,
        phase=ExecutionPhase.CONCLUDED.value,
        status=ExecutionStatus.CONCLUDED.value,
        accepted_plan_version=1,
        claim_id="released",
        claim_generation=1,
    )
    session.add(execution)
    await session.flush()
    session.add(
        OrchestrationAction(
            org_id=ORG_A,
            execution_id=execution.id,
            operation_key="complete",
            kind="deploy",
            status=ActionStatus.SUCCEEDED.value,
        )
    )
    await session.commit()
    response = await controls[0].post(f"/orchestration/flows/{flow.id}/archive", json={"reason": "Delivery is finished"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "archived"
