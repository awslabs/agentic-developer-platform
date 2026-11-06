"""A replaced developer admission must not prevent later plan amendments."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.execution_state import ExecutionIdentity, ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
from src.orchestration.execution_store import advance_execution, create_execution
from src.orchestration.models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationWorkClaim
from src.orchestration.replaced_admission import recover_replaced_admissions
from src.orchestration.shared_amendment import history, quiescence_failures
from src.orchestration.work_admission import admit
from src.orchestration.work_claims import ClaimOwner, OwnerKind, ReleaseReason, release_work
from tests.orchestration.test_dispatch_pass import engine as engine_fixture
from tests.orchestration.test_dispatch_pass import session as session_fixture
from tests.orchestration.test_dispatch_pass import session_factory as factory_fixture
from tests.orchestration.test_refused_start_settlement import _running_story

engine = engine_fixture
session = session_fixture
session_factory = factory_fixture


async def completed_replacement(session):
    node, old_run = await _running_story(session)
    old = await session.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))
    claim = await session.get(OrchestrationWorkClaim, old.claim_id)
    assert (
        await release_work(
            session,
            org_id=node.org_id,
            claim_id=claim.id,
            generation=claim.generation,
            reason=ReleaseReason.FAILED,
            terminal_evidence="fixture protected failed terminal",
        )
    ).admitted
    node.attempts += 1
    new_run = attempt_run_id(node.id, node.attempts)
    receipt = await admit(
        session,
        org_id=node.org_id,
        repository_id=claim.provider_repository_id,
        issue=claim.issue_number,
        owner=ClaimOwner(OwnerKind.ENGINE_FLOW, node.flow_id),
        invocation_id=new_run,
    )
    new_identity = ExecutionIdentity(node.org_id, node.id, node.attempts, 1, claim.id, receipt["generation"])
    made = await create_execution(session, identity=new_identity, flow_id=node.flow_id)
    assert made.kind is OutcomeKind.APPLIED
    ended = await advance_execution(
        session,
        identity=new_identity,
        advance=PhaseAdvance(phase=ExecutionPhase.CONCLUDED, status=ExecutionStatus.CONCLUDED, expected_revision=made.record.revision),
    )
    assert ended.kind is OutcomeKind.APPLIED
    assert (
        await release_work(
            session,
            org_id=node.org_id,
            claim_id=claim.id,
            generation=receipt["generation"],
            reason=ReleaseReason.COMPLETED,
            terminal_evidence="fixture completed replacement",
        )
    ).admitted
    node.state = "passed"
    new = await session.get(OrchestrationExecution, made.record.id)
    raw = {
        "tenant_id": {"S": node.org_id},
        "invocation_id": {"S": old_run},
        "flow_id": {"S": node.flow_id},
        "orchestration_node_id": {"S": node.id},
        "orchestration_node_attempt": {"N": str(old.cycle)},
        "status": {"S": "completed"},
        "terminal_outcome": {"S": "failed"},
    }
    await session.commit()
    return node, old, new, claim, raw


def columns(row):
    result = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    # SQLite drops timezone metadata when a saved row is read back.
    return {key: value.replace(tzinfo=UTC) if isinstance(value, datetime) and value.tzinfo is None else value for key, value in result.items()}


async def test_settles_only_old_admission_and_removes_amendment_block(session, session_factory):
    from src.orchestration.models import OrchestrationFlow

    node, old, new, claim, raw = await completed_replacement(session)
    protected = [columns(r) for r in (node, new, claim)]
    original = columns(old)
    flow = await session.get(OrchestrationFlow, node.flow_id)
    assert quiescence_failures(await history(session, flow=flow)) == ["unfinished_execution"]
    await session.rollback()
    store = SimpleNamespace(_read=Mock(return_value=raw))
    assert await recover_replaced_admissions(session_factory, store=store) == (1, "")
    assert await recover_replaced_admissions(session_factory, store=store) == (0, "")
    for row in (node, old, new, claim):
        await session.refresh(row)
    assert [columns(r) for r in (node, new, claim)] == protected
    assert old.status == "superseded" and old.phase == "concluded" and old.next_check_at is None
    assert old.revision == original["revision"] + 1
    for key in ("attempts", "claim_id", "claim_generation", "accepted_plan_version", "cycle"):
        assert getattr(old, key) == original[key]
    decisions = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "replaced_admission_settled")))
    assert len(decisions) == 1
    evidence = json.loads(decisions[0].reason)
    assert evidence["execution_id"] == old.id and evidence["successor_execution_id"] == new.id
    assert evidence["run_id"] == raw["invocation_id"]["S"]
    await session.refresh(flow)
    assert quiescence_failures(await history(session, flow=flow)) == []
    store._read.assert_called_once_with(f"TENANT#{node.org_id}", f"EXEC#{raw['invocation_id']['S']}")


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", {"S": "other"}),
        ("invocation_id", {"S": "other"}),
        ("flow_id", {"S": "other"}),
        ("orchestration_node_id", {"S": "other"}),
        ("orchestration_node_attempt", {"N": "999"}),
        ("status", {"S": "active"}),
        ("terminal_outcome", None),
        ("terminal_outcome", {"S": "unknown"}),
    ],
)
async def test_requires_exact_protected_terminal_binding(session, session_factory, field, value):
    _, old, _, _, raw = await completed_replacement(session)
    before = columns(old)
    raw[field] = value
    assert await recover_replaced_admissions(session_factory, store=SimpleNamespace(_read=Mock(return_value=raw))) == (0, "")
    await session.refresh(old)
    assert columns(old) == before


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("old", "pending_action_key", "unresolved"),
        ("old", "handoff_receipt_ref", "handoff"),
        ("old", "notification_receipt_ref", "notification"),
        ("old", "attempts", 1),
        ("old", "phase", "awaiting_review"),
        ("old", "status", "blocked"),
        ("node", "state", "running"),
        ("node", "kind", "eval"),
        ("node", "issue_ref", "999"),
        ("new", "pending_action_key", "unresolved"),
        ("new", "phase", "merge_ready"),
        ("new", "status", "awaiting_external"),
        ("new", "claim_generation", 999),
        ("claim", "state", "held"),
        ("claim", "release_reason", "failed"),
        ("claim", "active_run_id", "live"),
        ("claim", "owner_ref", "other"),
    ],
)
async def test_ambiguous_or_current_work_is_untouched(session, session_factory, target, field, value):
    node, old, new, claim, raw = await completed_replacement(session)
    setattr(dict(node=node, old=old, new=new, claim=claim)[target], field, value)
    await session.commit()
    before = [columns(r) for r in (node, old, new, claim)]
    assert await recover_replaced_admissions(session_factory, store=SimpleNamespace(_read=Mock(return_value=raw))) == (0, "")
    for row in (node, old, new, claim):
        await session.refresh(row)
    assert [columns(r) for r in (node, old, new, claim)] == before


@pytest.mark.parametrize("status", ["pending", "succeeded", "failed", "unknown"])
async def test_any_historical_action_prevents_retirement(session, session_factory, status):
    _, old, _, _, raw = await completed_replacement(session)
    session.add(
        OrchestrationAction(org_id=old.org_id, execution_id=old.id, operation_key="historical-effect", kind="merge_pull_request", status=status)
    )
    await session.commit()
    before = columns(old)
    assert await recover_replaced_admissions(session_factory, store=SimpleNamespace(_read=Mock(return_value=raw))) == (0, "")
    await session.refresh(old)
    assert columns(old) == before


@pytest.mark.parametrize("target", ["old", "new"])
async def test_revision_change_during_external_read_is_refused(session, session_factory, target):
    from src.orchestration.execution_store import supersede_replaced_admission

    _, old, new, _, _ = await completed_replacement(session)
    identity = ExecutionIdentity(old.org_id, old.node_id, old.cycle, old.accepted_plan_version, old.claim_id, old.claim_generation)
    revisions = old.revision, new.revision
    (old if target == "old" else new).revision += 1
    await session.commit()
    assert not await supersede_replaced_admission(
        session,
        identity=identity,
        expected_revision=revisions[0],
        successor_id=new.id,
        successor_revision=revisions[1],
        successor_cycle=new.cycle,
        issue_number=7349,
    )
    await session.refresh(old)
    assert old.status == "runnable"


async def test_unavailable_protected_store_preserves_old_admission(session, session_factory):
    _, old, _, _, _ = await completed_replacement(session)
    before = columns(old)
    assert await recover_replaced_admissions(session_factory, store=SimpleNamespace(_read=Mock(side_effect=RuntimeError("unavailable")))) == (0, "")
    await session.refresh(old)
    assert columns(old) == before
