"""Superseding a node closes its execution lane and frees its issue for the successor.

Production shape (cos-runtime COS-S15, 2026-10-15 13:08Z): the owner split the story by
amendment. The superseding node went ``ready`` but every dispatch was refused
``held_lease_lapsed``: the retired node's claim was still held by a reviewer run that had
already vanished, and its execution still owed a review continuation nothing could
discharge because ``SUPERSEDED`` has no outgoing transition.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.amend import _settle_superseded_lanes, amend_plan
from src.orchestration.execution_state import ExecutionStatus
from src.orchestration.models import OrchestrationExecution, OrchestrationWorkClaim
from src.orchestration.state import NodeState
from tests.orchestration import test_amend as amend_tests
from tests.orchestration.test_dispatch_pass import engine as engine_fixture
from tests.orchestration.test_dispatch_pass import session as session_fixture
from tests.orchestration.test_dispatch_pass import session_factory as session_factory_fixture
from tests.orchestration.test_refused_start_settlement import _running_story

engine = engine_fixture
session_factory = session_factory_fixture
session = session_fixture


async def _superseded_story(session):
    node, run_id = await _running_story(session)
    node.state = NodeState.SUPERSEDED.value
    await session.flush()
    return node, run_id


async def _rows(session, node):
    execution = (await session.scalars(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))).one()
    claim = (await session.scalars(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.owner_ref == node.flow_id))).one()
    return execution, claim


async def test_a_dead_run_lane_is_concluded_and_its_claim_released(session, monkeypatch):
    monkeypatch.setattr("src.orchestration.amend.run_is_live", AsyncMock(return_value=False))
    node, run_id = await _superseded_story(session)

    assert await _settle_superseded_lanes(session, [node], org_id=node.org_id, created_version=2) == 1

    execution, claim = await _rows(session, node)
    assert execution.status == ExecutionStatus.CONCLUDED.value
    assert claim.state == "released" and claim.release_reason == "abandoned" and claim.active_run_id is None
    assert claim.release_reason == "abandoned"  # release_work records the evidence; the successor address admits a fresh generation


async def test_a_live_worker_keeps_its_claim_but_the_lane_is_closed(session, monkeypatch):
    """The still-running worker's own terminal report releases a claim whose execution has concluded."""
    monkeypatch.setattr("src.orchestration.amend.run_is_live", AsyncMock(return_value=True))
    node, run_id = await _superseded_story(session)

    await _settle_superseded_lanes(session, [node], org_id=node.org_id, created_version=2)

    execution, claim = await _rows(session, node)
    assert execution.status == ExecutionStatus.CONCLUDED.value
    assert claim.state == "held" and claim.active_run_id == run_id


async def test_a_node_without_an_execution_is_left_alone(session, monkeypatch):
    monkeypatch.setattr("src.orchestration.amend.run_is_live", AsyncMock(return_value=False))
    from tests.orchestration.test_dispatch_pass import _make_flow, _make_node, _make_org

    await _make_org(session)
    flow = await _make_flow(session, slug="bare")
    node = await _make_node(session, flow, node_ref="bare", issue_ref="1")
    node.state = NodeState.SUPERSEDED.value
    assert await _settle_superseded_lanes(session, [node], org_id=node.org_id, created_version=2) == 0


@pytest.mark.asyncio
async def test_amend_plan_settles_exactly_the_nodes_it_superseded(monkeypatch):
    """Wiring: only newly superseded nodes reach the lane settlement, once, with the new version."""
    seen = {}

    async def record(session, nodes, *, org_id, created_version):
        seen["refs"] = sorted(node.node_ref for node in nodes)
        seen["version"] = created_version
        return len(nodes)

    monkeypatch.setattr("src.orchestration.amend._settle_superseded_lanes", record)
    async for db in amend_tests.session.__wrapped__():  # reuse the SQLite fixture body
        approval, amender = amend_tests.approval.__wrapped__(), amend_tests.amender.__wrapped__()
        original = await amend_tests.compile_original(db, approval)
        await amend_plan(db, original.flow_id, amend_tests.amended_proposal(), amender)
        assert seen == {"refs": ["story-b"], "version": 2}
        # An identical re-amendment is a no-op (nothing written, nothing settled).
        seen.clear()
        await amend_plan(db, original.flow_id, amend_tests.amended_proposal(), amender)
        assert seen == {}


async def test_a_lane_that_cannot_be_released_yet_does_not_reject_the_amendment(session, monkeypatch, caplog):
    """Production 2026-10-15 14:50Z: every amendment on two flows was refused with
    "could not release the work claim of superseded node …: continuation_outstanding" while a
    stale lane awaited the sweep. The settlement is best effort; the sweep owns the retry."""
    import logging

    from src.orchestration.execution_state import ExecutionIdentity
    from src.orchestration.handoff import handoff_receipt_ref

    monkeypatch.setattr("src.orchestration.amend.run_is_live", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "src.orchestration.execution_store.advance_execution", AsyncMock(return_value=SimpleNamespace(kind=None, reason="simulated_refusal"))
    )
    node, run_id = await _superseded_story(session)
    execution, claim = await _rows(session, node)
    identity = ExecutionIdentity(
        node.org_id, node.id, execution.cycle, execution.accepted_plan_version, execution.claim_id, execution.claim_generation
    )
    execution.handoff_receipt_ref = handoff_receipt_ref(identity, execution.id)  # a continuation the release guard will see
    await session.flush()

    with caplog.at_level(logging.WARNING, logger="bedrockgateway.orchestration.amend"):
        settled = await _settle_superseded_lanes(session, [node], org_id=node.org_id, created_version=2)

    assert settled == 0
    execution, claim = await _rows(session, node)
    assert claim.state == "held"  # untouched; the sweep will close and release it
    assert any("could not" in record.getMessage() for record in caplog.records)


# --- #7810: the issue claim has moved on, or the lane cannot be read -----------------


async def test_a_lane_whose_claim_a_successor_now_holds_is_closed_and_the_claim_left_alone(session, monkeypatch, caplog):
    """Production 2026-10-15 15:21Z (adp-tools-platform X05 → X05b): the superseding
    attempt re-acquired the issue claim at generation 3 while the retired lane recorded
    generation 2. The store refused ``claim_generation_superseded`` as if the settlement
    were adopting the lane, so X05's lane survived every amendment. The successor's run
    is *not* live here, and its claim must still not be released: it is not this lane's."""
    import logging

    monkeypatch.setattr("src.orchestration.amend.run_is_live", AsyncMock(return_value=False))
    node, run_id = await _superseded_story(session)
    execution, claim = await _rows(session, node)
    claim.generation, claim.active_run_id = claim.generation + 1, "orch:successor-attempt"
    await session.flush()

    with caplog.at_level(logging.INFO, logger="bedrockgateway.orchestration.amend"):
        assert await _settle_superseded_lanes(session, [node], org_id=node.org_id, created_version=3) == 1

    execution, claim = await _rows(session, node)
    assert execution.status == ExecutionStatus.CONCLUDED.value
    assert claim.state == "held" and claim.generation == 5 and claim.active_run_id == "orch:successor-attempt"
    assert any("left alone" in record.getMessage() for record in caplog.records)


async def test_a_lane_whose_claim_was_released_for_completion_is_still_closed(session, monkeypatch):
    """door1 shape: the successor's worker completed and released the issue claim
    (reason ``completed``, generation 4) while the retired lane sat at generation 2 —
    ``claim_not_held`` refused the close before #7810."""
    monkeypatch.setattr("src.orchestration.amend.run_is_live", AsyncMock(return_value=False))
    node, run_id = await _superseded_story(session)
    execution, claim = await _rows(session, node)
    claim.state, claim.release_reason, claim.claim_event_id, claim.active_run_id, claim.generation = (
        "released",
        "completed",
        "orch:successor-attempt",
        None,
        claim.generation + 2,
    )
    await session.flush()

    assert await _settle_superseded_lanes(session, [node], org_id=node.org_id, created_version=None) == 1

    execution, claim = await _rows(session, node)
    assert execution.status == ExecutionStatus.CONCLUDED.value
    assert "the claim sweep" in execution.progress_note
    assert claim.state == "released" and claim.release_reason == "completed" and claim.generation == 6


async def test_an_unreadable_lane_is_logged_and_its_claim_untouched(session, monkeypatch, caplog):
    import logging

    from src.orchestration.execution_state import ExecutionOutcome, OutcomeKind

    monkeypatch.setattr("src.orchestration.amend.run_is_live", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "src.orchestration.execution_store.load_execution",
        AsyncMock(return_value=ExecutionOutcome(kind=OutcomeKind.CONFLICT, record=None, reason="tenant_binding_mismatch")),
    )
    node, run_id = await _superseded_story(session)

    with caplog.at_level(logging.WARNING, logger="bedrockgateway.orchestration.amend"):
        assert await _settle_superseded_lanes(session, [node], org_id=node.org_id, created_version=2) == 0

    execution, claim = await _rows(session, node)
    assert execution.status != ExecutionStatus.CONCLUDED.value and claim.state == "held"
    assert any("could not read" in record.getMessage() and "tenant_binding_mismatch" in record.getMessage() for record in caplog.records)


async def test_the_claim_sweep_settles_open_superseded_lanes_and_nothing_else(session, monkeypatch):
    """AC-04: a lane an amendment could not close is settled by the next sweep pass,
    without waiting for another amendment; live nodes and settled lanes are untouched."""
    from src.orchestration.amend import settle_open_superseded_lanes

    monkeypatch.setattr("src.orchestration.amend.run_is_live", AsyncMock(return_value=False))
    retired, _ = await _superseded_story(session)
    live, live_run = await _live_story_in_the_same_org(session)

    assert await settle_open_superseded_lanes(session) == 1

    execution, claim = await _rows(session, retired)
    assert execution.status == ExecutionStatus.CONCLUDED.value and claim.state == "released"
    live_execution, live_claim = await _rows(session, live)
    assert live_execution.status != ExecutionStatus.CONCLUDED.value and live_claim.state == "held" and live_claim.active_run_id == live_run
    assert await settle_open_superseded_lanes(session) == 0  # idempotent: nothing left open on a superseded node


async def _live_story_in_the_same_org(session):
    """A second, running node on its own flow and issue claim; must survive the sweep untouched."""
    from src.orchestration.execution_state import ExecutionIdentity, OutcomeKind
    from src.orchestration.execution_store import create_execution
    from src.orchestration.models import OrchestrationAcceptedPlan
    from tests.orchestration.test_dispatch_pass import _make_flow, _make_node

    flow = await _make_flow(session, slug="live-flow")
    node = await _make_node(session, flow, node_ref="live", issue_ref="7351")
    node.state, node.attempts = NodeState.RUNNING.value, 1
    session.add(OrchestrationAcceptedPlan(org_id=flow.org_id, flow_id=flow.id, version=1, plan_document={"flow_slug": flow.slug}, plan_hash="h-live"))
    claim = OrchestrationWorkClaim(
        org_id=flow.org_id,
        provider_repository_id=12345,
        issue_number=7351,
        owner_kind="engine_flow",
        owner_ref=flow.id,
        active_run_id="orch:live-run",
        state="held",
        generation=1,
    )
    session.add(claim)
    await session.flush()
    created = await create_execution(session, identity=ExecutionIdentity(node.org_id, node.id, 1, 1, claim.id, 1), flow_id=flow.id)
    assert created.kind is OutcomeKind.APPLIED
    return node, "orch:live-run"


async def test_an_orphan_older_cycle_row_does_not_select_a_settled_node(session, monkeypatch, caplog):
    """#7873: only the newest cycle is the lane. An older-cycle row left open beside a
    concluded current cycle (claude-desktop run-qualification, 2026-10-11) selected the node
    every minute; the settlement found the current lane closed, counted it anyway, and every
    replica logged ``superseded_lanes_settled count=1`` forever."""
    import logging

    from src.orchestration.amend import settle_open_superseded_lanes
    from src.orchestration.execution_state import ExecutionIdentity, ExecutionPhase, OutcomeKind
    from src.orchestration.execution_store import create_execution

    monkeypatch.setattr("src.orchestration.amend.run_is_live", AsyncMock(return_value=True))
    node, run_id = await _superseded_story(session)
    current, claim = await _rows(session, node)
    orphan = await create_execution(
        session, identity=ExecutionIdentity(node.org_id, node.id, current.cycle - 1, 1, claim.id, claim.generation), flow_id=node.flow_id
    )
    assert orphan.kind is OutcomeKind.APPLIED and orphan.record.status == ExecutionStatus.RUNNABLE.value
    current.status, current.phase = ExecutionStatus.CONCLUDED.value, ExecutionPhase.CONCLUDED.value
    await session.flush()

    with caplog.at_level(logging.INFO, logger="src.orchestration.amend"):
        assert await settle_open_superseded_lanes(session) == 0

    rows = (await session.scalars(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))).all()
    assert {row.cycle: row.status for row in rows} == {
        current.cycle: ExecutionStatus.CONCLUDED.value,
        current.cycle - 1: ExecutionStatus.RUNNABLE.value,
    }
    await session.refresh(claim)
    assert claim.state == "held" and claim.active_run_id == run_id
    assert not [record for record in caplog.records if "could not" in record.getMessage()]
