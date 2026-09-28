"""Owner renewal preserves the ledger and cannot renew any other authority."""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from src.orchestration.compile import ApprovalContext, plan_hash
from src.orchestration.continuation import digest
from src.orchestration.execution_policy import policy_hash, stamp_policy
from src.orchestration.models import OrchestrationDecision, OrchestrationExecution
from src.orchestration.policy_admission import load_in_force_policy
from src.orchestration.proposal import LoopProposal
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.shared_amendment import SharedAppendError
from src.orchestration.shared_retry import RetryIncreaseRequest, accept_retry_increase, preview_retry_increase, verified_limits_increased
from src.orchestration.shared_window import WindowRenewalError, WindowRenewalRequest, accept_window_renewal, preview_window_renewal
from src.orchestration.state import ActorKind
from tests.orchestration.test_policy_admission import APPROVER
from tests.orchestration.test_shared_policy import dispatch, engine, session, shared  # noqa: F401


@pytest.fixture
async def window(shared):  # noqa: F811
    p = shared.policy.model_copy(deep=True)
    p.policy_id = p.policy_hash = p.principal_id = None
    p.expires_at = datetime.now(UTC) + timedelta(minutes=15)
    p = stamp_policy(p, principal_id=APPROVER, org_id=shared.flow.org_id)
    shared.plan.plan_document = {**shared.plan.plan_document, "execution_policy": p.model_dump(mode="json")}
    shared.plan.plan_hash = digest(shared.plan.plan_document)
    shared.policy = p
    shared.node.state, shared.node.attempts = "running", 2
    row = OrchestrationExecution(
        org_id=shared.flow.org_id,
        flow_id=shared.flow.id,
        node_id=shared.node.id,
        cycle=2,
        phase="awaiting_review",
        status="runnable",
        revision=3,
        accepted_plan_version=shared.plan.version,
        claim_id=shared.claim.id,
        claim_generation=shared.claim.generation,
        attempts=2,
        next_check_at=datetime.now(UTC),
        deadline_at=p.expires_at,
        created_at=datetime.now(UTC),
        progress_note="Reviewing current head",
    )
    shared.session.add(row)
    await shared.session.flush()
    return SimpleNamespace(
        s=shared,
        execution=row,
        actor=ApprovalContext(org_id=shared.flow.org_id, actor_id=APPROVER, actor_role="platform_admin"),
        request=WindowRenewalRequest(
            expected_plan_version=shared.plan.version,
            expected_plan_hash=shared.plan.plan_hash,
            expires_at=datetime.now(UTC) + timedelta(hours=2),
            reason="Owner requests continued delivery without losing active work.",
        ),
    )


async def preview(b):
    return await preview_window_renewal(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=b.request)


async def accept(b):
    observed = await preview(b)
    req = b.request.model_copy(update={"expected_snapshot": observed["snapshot"]})
    return await accept_window_renewal(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=req), req


async def effective(b):
    return await load_in_force_policy(b.s.session, org_id=b.s.flow.org_id, flow_id=b.s.flow.id)


async def test_renewal_preserves_work_meter_and_wall_clock_with_attributed_receipt(window):
    b = window
    before = (await effective(b)).policy
    doc = copy.deepcopy(b.s.plan.plan_document)
    meter = await b.s.client.hgetall(b.s.target.key())
    identity = (
        b.s.node.state,
        b.s.node.attempts,
        b.s.claim.active_run_id,
        b.s.claim.generation,
        b.execution.phase,
        b.execution.status,
        b.execution.attempts,
        b.execution.next_check_at.replace(tzinfo=UTC),
        b.execution.progress_note,
    )
    receipt, req = await accept(b)
    after = (await effective(b)).policy
    assert after.expires_at == req.expires_at and after.limits == before.limits
    assert after.policy_hash == policy_hash(after)
    assert after._shared_window_decision_id == receipt["decision_id"]
    assert verified_limits_increased(before.model_dump(mode="json"), after)
    assert b.execution.deadline_at == b.execution.created_at.replace(tzinfo=UTC) + timedelta(seconds=before.limits.max_wall_clock_seconds)
    assert b.execution.revision == 4
    assert identity == (
        b.s.node.state,
        b.s.node.attempts,
        b.s.claim.active_run_id,
        b.s.claim.generation,
        b.execution.phase,
        b.execution.status,
        b.execution.attempts,
        b.execution.next_check_at.replace(tzinfo=UTC),
        b.execution.progress_note,
    )
    assert b.s.plan.plan_document == doc and await b.s.client.hgetall(b.s.target.key()) == meter
    replay = await accept_window_renewal(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=req)
    assert not replay["created"] and replay["decision_id"] == receipt["decision_id"] and b.execution.revision == 4


@pytest.mark.parametrize(
    "case", ["version", "hash", "decrease", "unchanged", "too_far", "service", "other_org", "other_principal", "halted", "expired"]
)
async def test_invalid_renewal_cannot_write_or_change_deadline(window, case):
    b = window
    before = b.execution.deadline_at
    if case == "version":
        b.request = b.request.model_copy(update={"expected_plan_version": 999})
    elif case == "hash":
        b.request = b.request.model_copy(update={"expected_plan_hash": "a" * 64})
    elif case in {"decrease", "unchanged", "too_far"}:
        expiry = before - timedelta(seconds=1) if case == "decrease" else before
        if case == "too_far":
            expiry = datetime.now(UTC) + timedelta(days=2)
        b.request = b.request.model_copy(update={"expires_at": expiry})
    elif case == "service":
        b.actor = replace(b.actor, actor_kind=ActorKind.SERVICE)
    elif case == "other_org":
        b.actor = replace(b.actor, org_id="other-org")
    elif case == "other_principal":
        b.actor = replace(b.actor, actor_id="another-admin")
    elif case == "halted":
        b.s.flow.state = "halted"
    else:
        doc = copy.deepcopy(b.s.plan.plan_document)
        doc["execution_policy"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        b.s.plan.plan_document, b.s.plan.plan_hash = doc, digest(doc)
        b.request = b.request.model_copy(update={"expected_plan_hash": b.s.plan.plan_hash})
    await b.s.session.flush()
    with pytest.raises((WindowRenewalError, SharedAppendError, CycleBlockedError)):
        await accept(b)
    assert b.execution.deadline_at == before
    assert not list(await b.s.session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "execution_window_renewed")))


@pytest.mark.parametrize("case", ["actor", "role", "hash", "principal", "contract", "future", "naive", "late", "too_far"])
async def test_unverifiable_receipt_fails_closed(window, case):
    b = window
    receipt, _ = await accept(b)
    row = await b.s.session.get(OrchestrationDecision, receipt["decision_id"])
    data = json.loads(row.reason)
    key, value = {
        "hash": ("plan_hash", "a" * 64),
        "principal": ("principal_id", "other"),
        "contract": ("contract", "unknown"),
        "future": ("plan_version", 999),
        "naive": ("expires_at", "2026-09-23T01:00:00"),
        "late": ("accepted_at", data["expires_at"]),
        "too_far": ("expires_at", (datetime.now(UTC) + timedelta(days=3)).isoformat()),
    }.get(case, ("unused", None))
    data[key] = value
    b.s.session.add(
        OrchestrationDecision(
            org_id=row.org_id,
            flow_id=row.flow_id,
            kind=row.kind,
            actor_id=row.actor_id,
            actor_kind="service" if case == "actor" else "human",
            actor_role="org_admin" if case == "role" else "platform_admin",
            created_at=datetime.now(UTC) + timedelta(seconds=1),
            reason=json.dumps(data),
        )
    )
    await b.s.session.flush()
    assert (await effective(b)).policy is None


@pytest.mark.parametrize("case", ["shorter_deadline", "concluded", "superseded", "other_version"])
async def test_unrelated_deadlines_and_terminal_work_unchanged(window, case):
    b = window
    if case == "shorter_deadline":
        b.execution.deadline_at -= timedelta(minutes=2)
    elif case == "other_version":
        b.execution.accepted_plan_version += 1
    else:
        b.execution.status = case
        b.execution.next_check_at = None
    await b.s.session.flush()
    before = b.execution.deadline_at
    await accept(b)
    assert b.execution.deadline_at == before and b.execution.revision == 3


async def test_stale_preview_does_not_erase_concurrent_deadline_change(window):
    b = window
    old = await preview(b)
    b.execution.deadline_at -= timedelta(minutes=1)
    await b.s.session.flush()
    with pytest.raises(WindowRenewalError, match="window_preview_changed"):
        await accept_window_renewal(
            b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=b.request.model_copy(update={"expected_snapshot": old["snapshot"]})
        )


@pytest.mark.parametrize("renew_first", [True, False])
async def test_renewal_composes_with_retry_receipt(window, renew_first):
    b = window
    before = (await effective(b)).policy
    if renew_first:
        await accept(b)
    req = RetryIncreaseRequest(
        expected_plan_version=b.s.plan.version, expected_plan_hash=b.s.plan.plan_hash, max_attempts_per_node=20, reason=b.request.reason
    )
    p = await preview_retry_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=req)
    await accept_retry_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=req.model_copy(update={"expected_snapshot": p["snapshot"]}))
    if not renew_first:
        await accept(b)
    policy = (await effective(b)).policy
    assert policy.expires_at == b.request.expires_at and policy.limits.max_attempts_per_node == 20
    assert policy._shared_retry_decision_id and policy._shared_window_decision_id
    assert verified_limits_increased(before.model_dump(mode="json"), policy)


async def test_renewal_does_not_reinitialize_unknown_budget(window):
    b = window
    await b.s.client.delete(b.s.target.key())
    await accept(b)
    b.s.node.state = "ready"
    assert not (await dispatch(b.s)).permitted
    assert not await b.s.client.exists(b.s.target.key())


async def test_new_plan_does_not_inherit_renewal(window):
    b = window
    before = (await effective(b)).policy.expires_at
    await accept(b)
    b.s.plan.version += 1
    await b.s.session.flush()
    assert (await effective(b)).policy.expires_at == before


async def test_expired_window_requires_explicit_new_owner_acceptance(window):
    b = window
    p = b.s.policy.model_copy(deep=True)
    p.policy_id = p.policy_hash = p.principal_id = None
    p.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    p = stamp_policy(p, principal_id=APPROVER, org_id=b.s.flow.org_id)
    b.s.plan.plan_document = {**b.s.plan.plan_document, "execution_policy": p.model_dump(mode="json")}
    b.s.plan.plan_hash = digest(b.s.plan.plan_document)
    b.execution.deadline_at = p.expires_at
    await b.s.session.flush()
    b.request = b.request.model_copy(update={"expected_plan_hash": b.s.plan.plan_hash})
    with pytest.raises(WindowRenewalError, match="expired_policy_requires_explicit_reacceptance"):
        await preview(b)
    b.request = b.request.model_copy(update={"resume_expired": True})
    observed = await preview(b)
    assert observed["resume_expired"] is True and observed["before"] == p.expires_at.isoformat()
    receipt, _ = await accept(b)
    decision = await b.s.session.get(OrchestrationDecision, receipt["decision_id"])
    record = json.loads(decision.reason)
    assert datetime.fromisoformat(record["accepted_at"]) > p.expires_at
    assert record["resume_expired"] is True
    after = (await effective(b)).policy
    assert after.expires_at == b.request.expires_at and after.limits == p.limits
    assert b.s.node.state == "running" and b.s.node.attempts == 2
    assert b.s.claim.active_run_id == "run-current" and b.execution.attempts == 2


def test_request_requires_timezone():
    with pytest.raises(ValidationError):
        WindowRenewalRequest(expected_plan_version=1, expected_plan_hash="a" * 64, expires_at="2026-09-23T01:00:00", reason="Owner renewal")


async def test_route_requires_plan_permission(monkeypatch):
    from fastapi import HTTPException

    from src.orchestration import shared_window_routes

    check = AsyncMock(side_effect=HTTPException(403, "plan permission required"))
    monkeypatch.setattr(
        shared_window_routes, "AccessControl", lambda db: SimpleNamespace(check_permission=check, require_platform_admin=lambda user: None)
    )
    acceptor = AsyncMock()
    monkeypatch.setattr(shared_window_routes, "accept_window_renewal", acceptor)
    with pytest.raises(HTTPException):
        await shared_window_routes.accept_window(flow_id="flow", body=None, current_user=SimpleNamespace(org_id="org"), db=None)
    acceptor.assert_not_awaited()


async def test_explicit_wall_clock_increase_retains_elapsed_time_and_consumed_work(window):
    from src.orchestration.shared_policy import shared_inputs

    b = window
    before = (await effective(b)).policy
    started = datetime.now(UTC) - timedelta(seconds=before.limits.max_wall_clock_seconds + 60)
    document = copy.deepcopy(b.s.plan.plan_document)
    document["execution_continuation"]["accepted_at"] = started.isoformat()
    b.s.plan.plan_document = document
    b.s.plan.plan_hash = digest(document)
    b.execution.created_at = started
    b.execution.deadline_at = started + timedelta(seconds=before.limits.max_wall_clock_seconds)
    await b.s.session.flush()
    b.request = b.request.model_copy(update={"expected_plan_hash": b.s.plan.plan_hash})
    with pytest.raises(CycleBlockedError, match="wall_clock_limit_exceeded"):
        await preview(b)
    original_deadline = b.execution.deadline_at
    b.request = b.request.model_copy(update={"max_wall_clock_seconds": before.limits.max_wall_clock_seconds + 10_800})
    receipt, _ = await accept(b)
    inputs, marker = await shared_inputs(b.s.session, org_id=b.s.flow.org_id, flow_id=b.s.flow.id)
    assert inputs.policy.limits.max_wall_clock_seconds == b.request.max_wall_clock_seconds
    assert marker["accepted_at"] == started.isoformat()
    assert b.s.plan.plan_document == document
    assert b.s.node.attempts == 2 and b.execution.attempts == 2
    assert b.s.claim.active_run_id == "run-current"
    assert b.execution.deadline_at > original_deadline
    assert b.execution.deadline_at <= started + timedelta(seconds=b.request.max_wall_clock_seconds)
    assert verified_limits_increased(before.model_dump(mode="json"), inputs.policy)
    row = await b.s.session.get(OrchestrationDecision, receipt["decision_id"])
    evidence = json.loads(row.reason)
    assert evidence["wall_clock_started_at"] == started.isoformat()
    assert evidence["before_wall_clock_seconds"] == before.limits.max_wall_clock_seconds
    assert evidence["max_wall_clock_seconds"] == b.request.max_wall_clock_seconds


@pytest.mark.parametrize("delta", [-1, 0, 86_401])
async def test_wall_clock_increase_requires_positive_bounded_explicit_ceiling(window, delta):
    b = window
    before = (await effective(b)).policy
    b.request = b.request.model_copy(update={"max_wall_clock_seconds": before.limits.max_wall_clock_seconds + delta})
    with pytest.raises(WindowRenewalError, match="wall_clock_must_increase"):
        await accept(b)
    assert (await effective(b)).policy.limits == before.limits


async def test_later_expiry_renewal_retains_previously_authorized_wall_clock(window):
    b = window
    before = (await effective(b)).policy
    ceiling = before.limits.max_wall_clock_seconds + 3600
    b.request = b.request.model_copy(update={"max_wall_clock_seconds": ceiling})
    await accept(b)
    b.request = b.request.model_copy(update={"max_wall_clock_seconds": None, "expires_at": b.request.expires_at + timedelta(hours=1)})
    await accept(b)
    assert (await effective(b)).policy.limits.max_wall_clock_seconds == ceiling


@pytest.mark.parametrize("value", [True, 0, 604_801])
def test_wall_clock_request_rejects_invalid_ceiling(value):
    with pytest.raises(ValidationError):
        WindowRenewalRequest(
            expected_plan_version=1,
            expected_plan_hash="a" * 64,
            expires_at="2026-09-23T01:00:00Z",
            reason="Owner renewal",
            max_wall_clock_seconds=value,
        )


@pytest.fixture
async def protected_window(window, monkeypatch):
    b = window
    monkeypatch.setenv("ADP_SHARED_WORKER_CONTINUATION_ENABLED", "false")
    policy = b.s.policy.model_copy(deep=True)
    policy.schema_version = 1
    policy.user_credentials = None
    policy.expires_at = datetime.now(UTC) + timedelta(days=7)
    policy.policy_id = policy.policy_hash = policy.principal_id = None
    policy = stamp_policy(policy, principal_id=f"sub:{APPROVER}", org_id=b.s.flow.org_id)
    proposal = LoopProposal(
        flow_slug=b.s.flow.slug,
        title=b.s.flow.title,
        org_id=b.s.flow.org_id,
        spec_revision="window-regression",
        execution_policy=policy,
        description="Owner-authored provenance retained by normal acceptance.",
    )
    document = proposal.model_dump(mode="json")
    b.s.plan.plan_document, b.s.plan.plan_hash = document, plan_hash(proposal)
    assert b.s.plan.plan_hash != digest(document)
    started = datetime.now(UTC) - timedelta(seconds=policy.limits.max_wall_clock_seconds + 60)
    b.s.session.add(
        OrchestrationDecision(
            org_id=b.s.flow.org_id,
            flow_id=b.s.flow.id,
            kind="node_dispatched",
            actor_kind="service",
            actor_id="engine",
            actor_role="service",
            created_at=started,
        )
    )
    b.s.node.state, b.s.node.attempts = "failed", 1
    b.execution.status, b.execution.next_check_at = "concluded", None
    await b.s.session.flush()
    b.request = b.request.model_copy(
        update={
            "expected_plan_hash": b.s.plan.plan_hash,
            "expires_at": policy.expires_at,
            "max_wall_clock_seconds": policy.limits.max_wall_clock_seconds + 14400,
        }
    )
    return b


async def test_protected_elapsed_window_can_renew_without_changing_expiry_or_failed_attempt(protected_window):
    from src.orchestration.runtime_policy import flow_started_at

    b = protected_window
    before = (await effective(b)).policy
    document = copy.deepcopy(b.s.plan.plan_document)
    started = await flow_started_at(b.s.session, org_id=b.s.flow.org_id, flow_id=b.s.flow.id)
    deadline = b.execution.deadline_at
    receipt, request = await accept(b)
    after = (await effective(b)).policy
    assert after.expires_at == before.expires_at
    assert after.limits.max_wall_clock_seconds == request.max_wall_clock_seconds
    assert after.limits.max_attempts_per_node == before.limits.max_attempts_per_node
    assert after._shared_window_decision_id == receipt["decision_id"]
    assert b.s.plan.plan_document == document
    assert b.s.node.state == "failed" and b.s.node.attempts == 1
    assert b.execution.status == "concluded" and b.execution.deadline_at == deadline
    assert await flow_started_at(b.s.session, org_id=b.s.flow.org_id, flow_id=b.s.flow.id) == started


@pytest.mark.parametrize("case", ["unchanged", "decrease", "too_large", "other_owner", "expired"])
async def test_protected_same_expiry_requires_owner_and_positive_bounded_increase(protected_window, case):
    b = protected_window
    before = (await effective(b)).policy
    changes = {
        "unchanged": {"max_wall_clock_seconds": before.limits.max_wall_clock_seconds},
        "decrease": {"max_wall_clock_seconds": before.limits.max_wall_clock_seconds - 1},
        "too_large": {"max_wall_clock_seconds": before.limits.max_wall_clock_seconds + 86401},
        "expired": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
    }
    b.request = b.request.model_copy(update=changes.get(case, {}))
    if case == "other_owner":
        b.actor = replace(b.actor, actor_id="other-admin")
    with pytest.raises(WindowRenewalError):
        await accept(b)
    assert (await effective(b)).policy == before


@pytest.mark.parametrize("case", ["actor", "plan_hash", "wall_clock", "past_acceptance"])
async def test_protected_receipt_tampering_refuses_admission(protected_window, case):
    b = protected_window
    receipt, _ = await accept(b)
    row = await b.s.session.get(OrchestrationDecision, receipt["decision_id"])
    data = json.loads(row.reason)
    if case == "plan_hash":
        data["plan_hash"] = "a" * 64
    elif case == "wall_clock":
        data["max_wall_clock_seconds"] = data["before_wall_clock_seconds"]
    elif case == "past_acceptance":
        data["accepted_at"] = data["expires_at"]
    b.s.session.add(
        OrchestrationDecision(
            org_id=row.org_id,
            flow_id=row.flow_id,
            kind=row.kind,
            actor_id="other-admin" if case == "actor" else row.actor_id,
            actor_kind=row.actor_kind,
            actor_role=row.actor_role,
            created_at=datetime.now(UTC) + timedelta(seconds=1),
            reason=json.dumps(data),
        )
    )
    await b.s.session.flush()
    assert (await effective(b)).refusal is not None


@pytest.mark.parametrize("field", ["title", "policy"])
async def test_protected_window_rejects_changed_executable_document(protected_window, field):
    b = protected_window
    document = copy.deepcopy(b.s.plan.plan_document)
    if field == "title":
        document["title"] += " changed"
    else:
        document["execution_policy"]["limits"]["max_attempts_per_node"] += 1
    b.s.plan.plan_document = document
    await b.s.session.flush()
    with pytest.raises(WindowRenewalError, match="accepted_document_hash_changed"):
        await preview(b)


async def test_protected_window_uses_compiler_provenance_hash_rules(protected_window):
    b = protected_window
    document = copy.deepcopy(b.s.plan.plan_document)
    document["description"] = "Updated provenance, with exactly the same executable plan."
    b.s.plan.plan_document = document
    await b.s.session.flush()
    await accept(b)
    assert (await effective(b)).policy.limits.max_wall_clock_seconds == b.request.max_wall_clock_seconds


async def test_protected_window_receipt_rechecks_canonical_owner(protected_window):
    from src.shared.models.organization import User

    b = protected_window
    receipt, _ = await accept(b)
    row = await b.s.session.get(OrchestrationDecision, receipt["decision_id"])
    assert row.actor_id == APPROVER
    assert json.loads(row.reason)["principal_id"] == f"sub:{APPROVER}"
    assert (await effective(b)).policy is not None
    user = await b.s.session.get(User, APPROVER)
    user.cognito_sub = "replacement-login-subject"
    await b.s.session.flush()
    assert (await effective(b)).refusal is not None


async def test_protected_retry_supplement_preserves_failed_attempt_and_budget(protected_window):
    from src.orchestration.shared_retry import RetryIncreaseRequest, accept_retry_increase, preview_retry_increase

    b = protected_window
    before = (await effective(b)).policy
    document = copy.deepcopy(b.s.plan.plan_document)
    request = RetryIncreaseRequest(
        expected_plan_version=b.s.plan.version,
        expected_plan_hash=b.s.plan.plan_hash,
        max_attempts_per_node=before.limits.max_attempts_per_node + 1,
        reason="Owner authorizes a replacement review after infrastructure failure.",
    )
    preview = await preview_retry_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=request)
    receipt = await accept_retry_increase(
        b.s.session,
        flow_id=b.s.flow.id,
        actor=b.actor,
        request=request.model_copy(update={"expected_snapshot": preview["snapshot"]}),
    )
    after = (await effective(b)).policy
    assert after.limits.max_attempts_per_node == request.max_attempts_per_node
    assert after._shared_retry_decision_id == receipt["decision_id"]
    assert after.limits.max_spend_usd == before.limits.max_spend_usd
    assert after.limits.max_wall_clock_seconds == before.limits.max_wall_clock_seconds
    assert b.s.plan.plan_document == document
    assert b.s.node.attempts == 1 and b.s.node.state == "failed"
    assert b.execution.status == "concluded"


async def test_protected_retry_requires_original_owner(protected_window):
    from src.orchestration.shared_retry import RetryIncreaseError, RetryIncreaseRequest, preview_retry_increase

    b = protected_window
    before = (await effective(b)).policy
    request = RetryIncreaseRequest(
        expected_plan_version=b.s.plan.version,
        expected_plan_hash=b.s.plan.plan_hash,
        max_attempts_per_node=before.limits.max_attempts_per_node + 1,
        reason="A different administrator cannot borrow the plan owner authority.",
    )
    with pytest.raises(RetryIncreaseError, match="original_principal_required"):
        await preview_retry_increase(b.s.session, flow_id=b.s.flow.id, actor=replace(b.actor, actor_id="other-owner"), request=request)
