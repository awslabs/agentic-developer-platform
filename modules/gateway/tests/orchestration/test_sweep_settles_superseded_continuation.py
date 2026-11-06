"""The claim sweep closes a superseded node's open continuation instead of leaving its issue held (#7800).

Production shape (cos-runtime COS-S15, 2026-10-15 13:08Z–13:50Z): the node was superseded before the
amendment-time settlement existed; its reviewer run had vanished and been recorded as exited, but
``release_work`` kept answering ``continuation_outstanding`` for the review handoff the retired node
still owed, so the successor was refused ``held_lease_lapsed`` every tick.
"""

from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy import select

from src.orchestration.execution_state import ExecutionIdentity, ExecutionStatus
from src.orchestration.handoff import handoff_receipt_ref
from src.orchestration.models import OrchestrationExecution, OrchestrationWorkClaim
from src.orchestration.state import NodeState
from src.orchestration.work_admission import recover_exited_claims
from tests.orchestration.test_dispatch_pass import engine as engine_fixture
from tests.orchestration.test_dispatch_pass import session as session_fixture
from tests.orchestration.test_dispatch_pass import session_factory as session_factory_fixture
from tests.orchestration.test_refused_start_settlement import _running_story

engine = engine_fixture
session_factory = session_factory_fixture
session = session_fixture


async def _retired_lane(session, *, node_state):
    node, run_id = await _running_story(session)
    node.state = node_state
    execution = (await session.scalars(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))).one()
    # A committed continuation this lane owes, attributed the way production mints it.
    identity = ExecutionIdentity(
        node.org_id, node.id, execution.cycle, execution.accepted_plan_version, execution.claim_id, execution.claim_generation
    )
    execution.handoff_receipt_ref = handoff_receipt_ref(identity, execution.id)
    await session.flush()
    store = SimpleNamespace(
        _read=Mock(
            return_value={
                "tenant_id": {"S": node.org_id},
                "status": {"S": "completed"},
                "terminal_outcome": {"S": "complete"},
                "pod_name": {"S": "worker-r"},
                "workload_binding": {"S": "uid-r"},
            }
        )
    )
    return node, store


async def test_a_superseded_lane_is_concluded_and_its_claim_released(session, monkeypatch):
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    node, store = await _retired_lane(session, node_state=NodeState.SUPERSEDED.value)

    report = await recover_exited_claims(session, store=store, workloads=SimpleNamespace(has_exited=Mock(return_value=False)))

    assert report.released == 1
    execution = (await session.scalars(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))).one()
    claim = (await session.scalars(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.owner_ref == node.flow_id))).one()
    assert execution.status == ExecutionStatus.CONCLUDED.value
    assert claim.state == "released" and claim.release_reason == "completed"


async def test_a_current_node_keeps_the_continuation_guard(session, monkeypatch):
    """The guard this bypasses exists for live lanes: a running node's handoff must still hold the claim."""
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    node, store = await _retired_lane(session, node_state=NodeState.RUNNING.value)

    report = await recover_exited_claims(session, store=store, workloads=SimpleNamespace(has_exited=Mock(return_value=False)))

    assert report.released == 0
    claim = (await session.scalars(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.owner_ref == node.flow_id))).one()
    assert claim.state == "held"


async def test_a_superseded_lane_under_an_older_plan_version_is_still_closed(session, monkeypatch):
    """The retired node was authorized under plan v1; the flow is on v2 and lineage cannot preserve a retired node.

    That is the production shape (COS-S15 ec2-target under v17 while the flow moved to v19), and it is why the
    first settlement paths answered accepted_plan_version_mismatch and did nothing.
    """
    from datetime import UTC, datetime

    from src.orchestration.models import OrchestrationAcceptedPlan

    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    node, store = await _retired_lane(session, node_state=NodeState.SUPERSEDED.value)
    v1 = (await session.scalars(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == node.flow_id))).one()
    v1.superseded_at = datetime.now(UTC)
    session.add(OrchestrationAcceptedPlan(org_id=node.org_id, flow_id=node.flow_id, version=2, plan_document={"flow_slug": "flow-1"}, plan_hash="h2"))
    await session.flush()

    report = await recover_exited_claims(session, store=store, workloads=SimpleNamespace(has_exited=Mock(return_value=False)))

    assert report.released == 1
    execution = (await session.scalars(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))).one()
    claim = (await session.scalars(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.owner_ref == node.flow_id))).one()
    assert execution.status == ExecutionStatus.CONCLUDED.value and claim.state == "released"
