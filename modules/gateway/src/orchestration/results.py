"""Observe completed engine attempts without equating worker exit with success.

Stories require merged code and successful checks. Evaluations present their
finished run for human evidence review; only the existing approval boundary may
accept that result. Missing observations leave the node unchanged.

## Two evidence paths, and the boundary between them (#5301)

`GitHubEvidenceSource.merged_story` asks whether the *issue* was closed by a merged
PR with green checks. That question is unanswerable for a PR whose body says
`Issue #5049` rather than `Closes #5049`: no closing event is ever created, the
issue stays open, and the story waits in `awaiting_merge` forever while its
implementation PR sits merged. That is the observed U11 shape, and it is a missing
*association* rather than polling latency.

So a dispatch that carries a durable PR binding is reconciled from that binding —
`bound_pull_request` reads provider truth about the specific PR, and
`pr_bindings.evidence_for_binding` decides completion from it. GitHub issue closure
becomes a projection of delivery rather than its sole authority.

The boundary between the two paths is deliberate and is **not** "no binding falls
back to the issue-closure path". Applied unconditionally that would let newly
unregistered work complete through issue closure anyway, which is exactly the
unregistered-PR case the issue requires to be refused. Instead the dispatch record
itself says which contract it was dispatched under: `dispatch_pass` stamps
`pr_binding_required` on the `NODE_DISPATCHED` decision it writes, so

* a **binding-required** dispatch with no valid association **holds**, with the
  specific reason surfaced, and
* a **legacy** dispatch with no recovered binding keeps the issue-closure path;
  an attributed recovered binding takes precedence over that fallback.

Reading the marker off the decision rather than comparing timestamps is what makes
the boundary deterministic and testable: a dispatch's own record states its
contract, so no wall-clock or deploy-time reasoning is involved.

## A merged PR is not the end of delivery (#5144)

Merge evidence answers "did this code land". It says nothing about the review,
deployment and evaluation a worker's clean exit can leave outstanding — the worker
process exits 0, the PR is merged, and nothing durable records what is still owed.

So a dispatch marked `handoff_required` must ALSO carry a durable continuation
receipt before it may pass, and a missing or unattributable one holds the node
rather than completing it. The check sits after the merge evidence deliberately: an
unmarked legacy dispatch reaches exactly the code it reached before, and the marked
case pays one extra read only on the path that was about to pass anyway.

The receipt is read, never created. Manufacturing an execution row to hang a receipt
on would fabricate the evidence being checked for, so an absent receipt is a hold and
an unverifiable one is treated as absent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

import httpx  # noqa: F401 - retained legacy monkeypatch/import compatibility
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import utcnow

from .dispatch_pass import attempt_run_id, resolve_installation_id
from .execution_state import TERMINAL_EXECUTION_STATUSES, BlockCode, ExecutionIdentity, ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
from .execution_store import advance_execution, load_execution
from .handoff import current_identity, handoff_required, missing_receipt_hold, outstanding_block, receipt_for
from .merge_evidence import GitHubEvidenceSource
from .models import DecisionKind, NodeKind, OrchestrationAction, OrchestrationDecision, OrchestrationNode, OrchestrationWorkClaim
from .pr_bindings import (
    BindingError,
    BindingRefusal,
    MergeEvidence,
    active_binding_for_node,
    binding_scope_matches,
    binding_snapshot,
    completion_candidate,
    evidence_for_binding,
    hold_explanation,
)
from .run_reports import run_result_for_assignment
from .run_store import EngineRunStore
from .state import ActorKind, NodeState, transition

logger = logging.getLogger(__name__)


@dataclass
class ResultReport:
    examined: int = 0
    advanced: int = 0
    errors: int = 0
    waiting: int = 0
    reasons: dict[str, str] = field(default_factory=dict)


# The legacy hold text, kept verbatim for dispatches that predate PR binding so
# their observable behaviour does not change.
_LEGACY_WAIT = "Agent finished. Waiting for the issue to be completed by a merged pull request with successful checks."


def binding_required(dispatch: dict[str, Any]) -> bool:
    """Whether this dispatch must be reconciled from a durable PR binding (#5301).

    Read from the dispatch record's own marker rather than from a date, so the
    contract a run was dispatched under is a property of that run. A dispatch
    written before this contract existed carries no marker and keeps the legacy
    issue-closure path; everything dispatched after it must produce a binding, and
    holds rather than falling back if it has none.

    The fallback is deliberately not universal. "No binding, so use issue closure"
    applied to every dispatch would let newly unregistered work complete on a
    closing keyword, which is the unregistered-PR case #5301 requires to be
    refused.
    """
    return bool(dispatch.get("pr_binding_required"))


async def _story_evidence(
    session: AsyncSession,
    *,
    node: OrchestrationNode,
    dispatch: dict[str, Any],
    source: Any,
    installation_id: int,
    observation: dict | None = None,
) -> tuple[str | None, str]:
    """Completion evidence for one finished story attempt.

    Returns `(url, hold_reason)`. A non-None url means the story may pass; otherwise
    the hold reason is specific enough for an operator to act on, which the original
    generic "waiting for the issue to be completed" was not — it described, in U11's
    case, something that could never happen.
    """
    try:
        binding = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
    except BindingError as exc:
        return None, hold_explanation(exc.code)
    if binding is None and not binding_required(dispatch):
        # Legacy dispatch: unchanged issue-closure evidence.
        url = await asyncio.wait_for(
            source.merged_story(
                org_id=node.org_id,
                installation_id=installation_id,
                repo=dispatch["repo"],
                issue=dispatch["issue"],
                since=dispatch["arrived_at"],
            ),
            timeout=15,
        )
        return url, _LEGACY_WAIT

    if binding is not None and observation is not None:
        observation["binding"] = binding_snapshot(binding)
    refusal = completion_candidate(binding)
    if refusal is not None:
        # No binding, superseded, or a reviewer artifact — refused before any
        # provider call, since none of those can be fixed by asking GitHub.
        return None, hold_explanation(refusal)
    assert binding is not None  # completion_candidate returns NO_BINDING otherwise
    if not binding_scope_matches(binding, node):
        return None, hold_explanation(BindingRefusal.SCOPE_CHANGED)

    if binding.installation_id and binding.installation_id != installation_id:
        # The binding was registered against a different GitHub installation than
        # this tenant now resolves to. Reading the PR through the current one could
        # answer about a different repository of the same name.
        return None, hold_explanation(BindingRefusal.REPOSITORY_MISMATCH)

    from .delivery_progress import provider_progress

    try:
        provider_evidence: MergeEvidence | None = await asyncio.wait_for(
            source.bound_pull_request(
                org_id=node.org_id,
                installation_id=binding.installation_id or installation_id,
                repo=binding.repo,
                pr_number=binding.pr_number,
            ),
            timeout=15,
        )
    except Exception:
        # Persist a distinct diagnosis instead of retaining yesterday's successful
        # evidence or describing a provider outage as an unmerged PR.
        provider_evidence = None
        if observation is not None:
            observation["provider_unavailable"] = True
    if observation is not None:
        observation["delivery_progress"] = provider_progress(binding, provider_evidence)
    url, refusal = evidence_for_binding(binding, provider_evidence)
    if url:
        if observation is not None:
            observation["merge_receipt"] = asdict(provider_evidence)
        # #5144: provider evidence about the PR is necessary but not sufficient. A run
        # dispatched under the handoff contract must also have committed a durable
        # continuation receipt, because a merged PR says nothing about the review,
        # deployment and evaluation the worker's clean exit left outstanding.
        #
        # Checked AFTER the merge evidence rather than before it, so an unmarked legacy
        # dispatch reaches exactly the code it reached before and the marked case pays
        # one extra read only on the path that was about to pass.
        if handoff_required(dispatch):
            receipt = await _delivery_receipt(session, node=node)
            if receipt is None:
                # Absent OR unattributable. Fail closed: a receipt this attempt cannot
                # be shown to own is not evidence about this attempt.
                return None, missing_receipt_hold()
            if observation is not None:
                observation["handoff_receipt_ref"] = receipt
        return url, ""
    return None, hold_explanation(refusal) if refusal else _LEGACY_WAIT


async def _delivery_receipt(session: AsyncSession, *, node: OrchestrationNode, lock: bool = False) -> str | None:
    """This node's durable continuation receipt, or ``None`` when there is none.

    A read only. The absence of a receipt leaves the node held by the caller — nothing
    is created, advanced or repaired here, because inventing an execution row to hang a
    receipt on would manufacture the very evidence being checked for.

    ``lock=True`` for the commit-time revalidation (#5144 F2): the receipt's authority
    is re-derived under the execution store's lock order rather than trusted from the
    earlier lock-free snapshot. Both the identity and the receipt are resolved again,
    because a handover advances the claim generation on the *identity*, and re-checking
    only the stored string would compare a fresh receipt against stale fences.
    """
    identity = await current_identity(session, org_id=node.org_id, node_id=node.id)
    if identity is None:
        # Marked as requiring a receipt but carrying no execution row. Fail closed: the
        # caller holds, rather than passing work whose continuation cannot be verified.
        return None
    return await receipt_for(session, identity=identity, lock=lock)


async def _persist_missing_handoff_block(
    session: AsyncSession, *, node: OrchestrationNode, identity: ExecutionIdentity | None, detail: str
) -> dict[str, Any]:
    """Record the hold without borrowing a replacement owner's authority.

    The snapshot was resolved before external observations. The store rechecks it
    under its normal locks. A missing execution or displaced claim cannot authorize
    a ledger write, so its typed refusal stays in the node's existing decision log.
    """
    block = outstanding_block(
        BlockCode.AUTHORITY_UNVERIFIABLE,
        owner="platform-operator",
        required_input="reconcile this attempt's ownership and obtain its committed continuation receipt",
        detail=detail,
    )
    record = None
    refusal = "execution_missing"
    recorded_in = "decision"
    if identity is not None and identity.cycle == node.attempts:
        outcome = await load_execution(session, identity=identity, for_update=True)
        record = outcome.record if outcome is not None else None
        refusal = outcome.reason if outcome is not None else "execution_missing"
        if outcome is not None and outcome.kind is OutcomeKind.APPLIED and record is not None:
            if record.status not in TERMINAL_EXECUTION_STATUSES:
                # Do not replace an existing budget, human or other recovery gate.
                if record.block is None:
                    outcome = await advance_execution(
                        session,
                        identity=identity,
                        advance=PhaseAdvance(
                            phase=record.phase,
                            status=ExecutionStatus.BLOCKED,
                            expected_revision=record.revision,
                            next_check_at=record.next_check_at or utcnow() + timedelta(seconds=300),
                        ),
                        block=block,
                        pending_action_key=record.pending_action_key,
                    )
                    if outcome.kind is not OutcomeKind.BLOCKED:
                        raise RuntimeError("handoff block could not be persisted under its locked authority")
                    recorded_in = "execution"
                elif record.block.code == block.code and record.block.required_input == block.required_input:
                    recorded_in = "execution"
    elif identity is not None:
        refusal = "execution_cycle_mismatch"
    if refusal and ("claim" in refusal or "owner" in refusal):
        block = outstanding_block(BlockCode.OWNERSHIP_LOST, owner=block.owner, required_input=block.required_input, detail=detail)
    return {
        "issue": "5144",
        "block_code": block.code.value,
        "owner": block.owner,
        "required_input": block.required_input,
        "remaining_gates": list(block.remaining_gates),
        "detail": block.detail,
        "identity": asdict(identity) if identity is not None else {"org_id": node.org_id, "node_id": node.id, "cycle": node.attempts},
        "execution_id": record.id if record is not None else None,
        "recorded_in": recorded_in,
        "authority_refusal": refusal,
    }


async def observe_results(session: AsyncSession, *, run_store: Any | None = None, evidence: Any | None = None) -> ResultReport:
    report = ResultReport()
    deadline = time.monotonic() + 45
    # Oldest observation first: a long-running node cannot starve later work.
    # Persist the cursor as an audit row, so cold starts and overlapping ticks
    # have the same bounded sweep. Twenty external reads fit the tick's budget.
    last_check = (
        select(OrchestrationDecision.node_id, func.max(OrchestrationDecision.created_at).label("checked_at"))
        .where(
            OrchestrationDecision.kind == DecisionKind.RESULT_CHECKED.value,
        )
        .group_by(OrchestrationDecision.node_id)
        .subquery()
    )
    nodes = (
        (
            await session.execute(
                select(OrchestrationNode)
                .outerjoin(
                    last_check,
                    last_check.c.node_id == OrchestrationNode.id,
                )
                .where(
                    OrchestrationNode.state.in_([NodeState.RUNNING.value, NodeState.AWAITING_MERGE.value]),
                    OrchestrationNode.kind.in_([NodeKind.STORY.value, NodeKind.EVAL.value]),
                )
                .order_by(last_check.c.checked_at.asc().nullsfirst(), OrchestrationNode.id)
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    for node in nodes:
        if time.monotonic() >= deadline:
            break
        node_id, org_id, flow_id, attempt = node.id, node.org_id, node.flow_id, node.attempts
        report.examined += 1
        try:
            async with session.begin_nested():
                from .delivery_adoption import observe_historical_delivery

                adoption = await observe_historical_delivery(
                    session,
                    node=node,
                    source=evidence if evidence is not None else GitHubEvidenceSource(),
                )
                if adoption is not None:
                    advanced, detail = adoption
                    report.advanced += int(advanced)
                    report.waiting += int(not advanced)
                    report.reasons[node.id] = detail
                    continue
                decision = (
                    await session.execute(
                        select(OrchestrationDecision)
                        .where(
                            OrchestrationDecision.org_id == node.org_id,
                            OrchestrationDecision.node_id == node.id,
                            OrchestrationDecision.kind == DecisionKind.NODE_DISPATCHED.value,
                        )
                        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if decision is None:
                    report.waiting += 1
                    continue
                dispatch = json.loads(decision.reason or "{}")
                if dispatch.get("attempt") != node.attempts or dispatch.get("run_id") != attempt_run_id(node.id, node.attempts):
                    raise ValueError("dispatch record does not match the current attempt")
                row = await run_result_for_assignment(session, node=node, dispatch=dispatch)
                authenticated_assignment = row is not None
                if row is None:
                    row = await protected_failure_for_assignment(node=node, dispatch=dispatch)
                    authenticated_assignment = row is not None
                if row is None:
                    store = run_store if run_store is not None else EngineRunStore.from_env()
                    row = await asyncio.to_thread(store.get, dispatch["run_id"], dispatch["arrived_at"])
                recovered_without_run_record = False
                recovered_from_skipped_run = False
                recoverable_skipped_run = row is not None and row.get("status") == "skipped" and row.get("skip_reason") == "idempotency_merged_pr"
                if row is None or recoverable_skipped_run:
                    # An attributed recovery is specifically the operator attesting that
                    # historical work predates (or escaped) the normal run/binding seam.
                    # Requiring the missing run row before inspecting that binding makes
                    # the recovery circular: the exact absence it exists to repair keeps
                    # the story running forever (#5358).  Only a human-established,
                    # current-attempt recovery gets this path. Ordinary worker bindings
                    # still require their completion receipt below.
                    if node.kind != NodeKind.STORY.value:
                        report.waiting += 1
                        continue
                    recovered = await active_binding_for_node(
                        session,
                        org_id=node.org_id,
                        node_id=node.id,
                        attempt=node.attempts,
                    )
                    if recovered is None or recovered.registered_by_kind != ActorKind.HUMAN.value or not recovered.recovery_reason:
                        report.waiting += 1
                        continue
                    if row is None:
                        row = {
                            "tenant_id": node.org_id,
                            "engine_node_id": node.id,
                            "engine_attempt": node.attempts,
                            "status": "complete",
                        }
                        recovered_without_run_record = True
                    else:
                        # The worker can positively suppress a duplicate attempt
                        # after finding an already-merged PR. Preserve and validate
                        # that run's stored identity below, but let the attributed
                        # binding prove which exact PR completed the accepted scope.
                        row = {**row, "status": "complete"}
                        recovered_from_skipped_run = True
                if row.get("tenant_id") != node.org_id or row.get("engine_node_id") != node.id or row.get("engine_attempt") != node.attempts:
                    raise ValueError("run record does not match node/tenant/attempt")
                status = row.get("status")
                policy_failure = False
                if node.kind == NodeKind.STORY.value:
                    from .policy_admission import load_in_force_policy

                    policy = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
                    governed = policy.policy is not None or policy.refusal is not None
                    policy_failure = governed and authenticated_assignment and status == "failed"
                    if governed and not policy_failure:
                        # The durable handler owns policy-enabled delivery. Neither
                        # worker exit nor a merged PR may bypass review/repair and
                        # the later merge/deployment/evaluation phase handlers.
                        # An authenticated failure, however, must reach FAILED so
                        # the existing attributed resume can retry it. Keeping the
                        # outer node RUNNING strands a terminal run indefinitely.
                        # Advisory DDB failure alone cannot take this exception.
                        report.waiting += 1
                        continue
                from .evaluation_plan import managed_evaluation

                if await managed_evaluation(session, node):
                    report.waiting += 1
                    continue
                observation: dict = {}
                receipt_identity = None
                if recovered_without_run_record:
                    observation["recovered_without_run_record"] = True
                if recovered_from_skipped_run:
                    observation.update(
                        recovered_from_skipped_run=True,
                        recovered_run_status="skipped",
                        recovered_skip_reason="idempotency_merged_pr",
                    )
                if status in {"failed", "budget_stopped", "aborted", "cancelled"}:
                    target, detail = NodeState.FAILED, f"Worker reported {status}; inspect the run before retrying."
                elif status == "complete":
                    if node.kind == NodeKind.EVAL.value:
                        # A green worker exit is not a test verdict. Require a real
                        # report and human review, including all deferred criteria.
                        if not row.get("transcript_key"):
                            report.waiting += 1
                            continue
                        target, detail = (
                            NodeState.AWAITING_GATE,
                            "Evaluation run finished. Review its test evidence and deferred criteria before accepting.",
                        )
                    else:
                        installation_id = await resolve_installation_id(session, org_id=node.org_id)
                        if installation_id is None:
                            raise ValueError("GitHub installation is unresolved")
                        source = evidence if evidence is not None else GitHubEvidenceSource()
                        if handoff_required(dispatch):
                            receipt_identity = await current_identity(session, org_id=node.org_id, node_id=node.id)
                        url, hold = await _story_evidence(
                            session,
                            node=node,
                            dispatch=dispatch,
                            source=source,
                            installation_id=installation_id,
                            observation=observation,
                        )
                        if observation.get("provider_unavailable"):
                            report.errors += 1
                        if not url:
                            target, detail = NodeState.AWAITING_MERGE, hold
                        else:
                            target, detail = NodeState.PASSED, f"Story completed by merged pull request with successful checks: {url}"
                else:
                    report.waiting += 1
                    continue
                observed_state = node.state
                # Provider reads run without locks. Before writing either a hold
                # or completion, serialize with registration/replacement and retry,
                # then revalidate the exact revision the provider evidence names.
                locked = (
                    await session.execute(
                        select(OrchestrationNode)
                        .where(
                            OrchestrationNode.id == node.id,
                            OrchestrationNode.org_id == node.org_id,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if locked.state != observed_state or locked.attempts != dispatch["attempt"]:
                    report.waiting += 1
                    continue
                if policy_failure:
                    # A continuation can own the same node attempt. Serialize
                    # with its claim transfer before applying the original run's
                    # failure, using the existing node -> flow/claim/plan/execution
                    # lock order. A stale developer cannot fail a live reviewer.
                    identity = await current_identity(session, org_id=locked.org_id, node_id=locked.id)
                    current = (
                        await load_execution(session, identity=identity, for_update=True, released_failure_run_id=dispatch["run_id"])
                        if identity is not None
                        else None
                    )
                    claim = await session.get(OrchestrationWorkClaim, identity.claim_id) if identity is not None else None
                    if (
                        identity is None
                        or identity.cycle != locked.attempts
                        or current is None
                        or current.kind is not OutcomeKind.APPLIED
                        or current.record is None
                        or current.record.status in TERMINAL_EXECUTION_STATUSES
                        or current.record.pending_action_key is not None
                        or claim is None
                        or not (
                            claim.active_run_id == dispatch["run_id"]
                            or (claim.state == "released" and claim.release_reason == "failed" and claim.claim_event_id == dispatch["run_id"])
                        )
                    ):
                        report.waiting += 1
                        report.reasons[node.id] = "The failed worker no longer owns the current delivery execution; reconcile its current owner."
                        continue
                    from .review_recovery import pending_recovery_for_report
                    from .run_reports import OrchestrationRunReport

                    prior_report = await session.get(OrchestrationRunReport, dispatch["run_id"])
                    if prior_report and await pending_recovery_for_report(session, prior_report):
                        report.waiting += 1
                        report.reasons[node.id] = "A fresh review was requested for the retained PR; awaiting its own evidence."
                        continue
                    successor = await session.scalar(
                        select(OrchestrationAction.id)
                        .where(
                            OrchestrationAction.org_id == locked.org_id,
                            OrchestrationAction.execution_id == current.record.id,
                            OrchestrationAction.kind == "review_cycle_dispatch",
                        )
                        .limit(1)
                    )
                    if successor is not None:
                        report.waiting += 1
                        report.reasons[node.id] = "A continuation was recorded for this attempt; reconcile it before retrying the original worker."
                        continue
                if node.kind == NodeKind.STORY.value and status == "complete":
                    try:
                        current = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
                    except BindingError as exc:
                        current = None
                        target, detail = NodeState.AWAITING_MERGE, hold_explanation(exc.code)
                        observation.clear()
                    snapshot = observation.get("binding")
                    changed = (
                        snapshot is not None
                        and (
                            current is None
                            or current.id != snapshot["id"]
                            or current.revision != snapshot["revision"]
                            or (target == NodeState.PASSED and not binding_scope_matches(current, node))
                        )
                    ) or (snapshot is None and current is not None)
                    # The node lock precedes the store's flow/claim/plan/execution
                    # locks, as in the worker write path. Missing receipts need the
                    # same guarded, durable block on holds as on attempted completion.
                    if handoff_required(dispatch):
                        revalidated = await _delivery_receipt(session, node=locked, lock=True)
                        receipt_changed = target == NodeState.PASSED and revalidated != observation.get("handoff_receipt_ref")
                        if revalidated is None or receipt_changed:
                            block_detail = missing_receipt_hold(
                                "the continuation receipt's authority changed during verification" if receipt_changed else ""
                            )
                            if target == NodeState.PASSED:
                                target, detail = NodeState.AWAITING_MERGE, block_detail
                            observation.pop("merge_receipt", None)
                            observation.pop("handoff_receipt_ref", None)
                            observation["handoff_block"] = await _persist_missing_handoff_block(
                                session, node=locked, identity=receipt_identity, detail=block_detail
                            )
                    if changed:
                        target = NodeState.AWAITING_MERGE
                        detail = "The implementation binding changed during verification; its current head and scope will be verified again."
                        observation.pop("merge_receipt", None)
                        observation["binding"] = binding_snapshot(current) if current else None
                payload = {"attempt": dispatch["attempt"], "run_id": dispatch["run_id"], "evidence": detail, **observation}
                if target.value == node.state:
                    previous = (
                        await session.execute(
                            select(OrchestrationDecision)
                            .where(
                                OrchestrationDecision.org_id == node.org_id,
                                OrchestrationDecision.node_id == node.id,
                                OrchestrationDecision.kind == DecisionKind.RESULT_OBSERVED.value,
                            )
                            .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if previous is None or json.loads(previous.reason or "{}") != payload:
                        session.add(
                            OrchestrationDecision(
                                org_id=node.org_id,
                                flow_id=node.flow_id,
                                node_id=node.id,
                                kind=DecisionKind.RESULT_OBSERVED.value,
                                actor_id="system:orchestration-results",
                                actor_role="engine",
                                actor_kind=ActorKind.SERVICE.value,
                                from_state=node.state,
                                to_state=node.state,
                                reason=json.dumps(payload),
                                rejection_reason=json.dumps(observation["handoff_block"]) if "handoff_block" in observation else None,
                            )
                        )
                        await session.flush()
                    report.waiting += 1
                    report.reasons[node.id] = detail
                    continue
                result = transition(observed_state, target, actor_kind=ActorKind.SERVICE, reason=detail)
                if not result.allowed:
                    raise ValueError(result.rejection_reason)
                rows = (
                    await session.execute(
                        update(OrchestrationNode)
                        .where(
                            OrchestrationNode.id == node.id,
                            OrchestrationNode.org_id == node.org_id,
                            OrchestrationNode.state == observed_state,
                            OrchestrationNode.attempts == dispatch["attempt"],
                        )
                        .values(state=target.value, updated_at=utcnow())
                    )
                ).rowcount
                if rows:
                    if policy_failure:
                        from .work_claims import Disposition, ReleaseReason, release_work

                        ended = await advance_execution(
                            session,
                            identity=identity,
                            released_failure_run_id=dispatch["run_id"],
                            advance=PhaseAdvance(
                                phase=ExecutionPhase.CONCLUDED,
                                status=ExecutionStatus.CONCLUDED,
                                expected_revision=current.record.revision,
                                progress_note=detail,
                            ),
                        )
                        if ended.kind is not OutcomeKind.APPLIED:
                            raise ValueError("failed-worker execution changed before settlement")
                        released = await release_work(
                            session,
                            org_id=node.org_id,
                            claim_id=identity.claim_id,
                            generation=identity.claim_generation,
                            reason=ReleaseReason.FAILED,
                            terminal_evidence=f"{row.get('status_source', 'authenticated_run_report')}:{dispatch['run_id']}",
                        )
                        if released.disposition not in {Disposition.ADMITTED, Disposition.DUPLICATE}:
                            raise ValueError("failed-worker claim release refused")
                        payload["failed_execution_id"] = current.record.id
                    session.add(
                        OrchestrationDecision(
                            org_id=node.org_id,
                            flow_id=node.flow_id,
                            node_id=node.id,
                            kind=DecisionKind.RESULT_OBSERVED.value,
                            actor_id="system:orchestration-results",
                            actor_role="engine",
                            actor_kind=ActorKind.SERVICE.value,
                            from_state=observed_state,
                            to_state=target.value,
                            reason=json.dumps(payload),
                            rejection_reason=json.dumps(observation["handoff_block"]) if "handoff_block" in observation else None,
                        )
                    )
                    await session.flush()
                    report.advanced += 1
                    report.reasons[node.id] = detail
        except Exception:
            report.errors += 1
            logger.exception("Could not observe result for engine node %s", node_id)
        finally:
            session.add(
                OrchestrationDecision(
                    org_id=org_id,
                    flow_id=flow_id,
                    node_id=node_id,
                    kind=DecisionKind.RESULT_CHECKED.value,
                    actor_id="system:orchestration-results",
                    actor_role="engine",
                    actor_kind=ActorKind.SERVICE.value,
                    reason=f"Checked attempt {attempt}",
                )
            )
            await session.flush()
    return report


async def protected_failure_for_assignment(*, node, dispatch, store=None):
    """Settle terminal failures from gateway-owned authority, never activity hints.

    Protected workers publish an atomic terminal outcome in EXEC instead of a
    shared-role SQL report. Only a positively recorded failure of this exact
    flow/node/attempt can use the failure path; success still needs delivery.
    """
    if os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() != "true" and store is None:
        return None
    if store is None:
        from src.agentauth.engine import get_engine_authority_writer

        store = get_engine_authority_writer().store
    raw = await asyncio.to_thread(store._read, f"TENANT#{node.org_id}", f"EXEC#{dispatch['run_id']}")
    if not raw or raw.get("status") != {"S": "completed"}:
        return None
    outcome = raw.get("terminal_outcome", {}).get("S")
    if outcome not in {"failed", "aborted", "cancelled", "budget_stopped"}:
        return None
    expected = {
        "tenant_id": {"S": node.org_id},
        "invocation_id": {"S": dispatch["run_id"]},
        "flow_id": {"S": node.flow_id},
        "orchestration_node_id": {"S": node.id},
        "orchestration_node_attempt": {"N": str(node.attempts)},
    }
    if any(raw.get(key) != value for key, value in expected.items()):
        raise ValueError("protected failure does not match node/tenant/flow/attempt")
    return {
        "tenant_id": node.org_id,
        "engine_node_id": node.id,
        "engine_attempt": node.attempts,
        "status": "failed",
        "status_source": "protected_execution",
        "terminal_outcome": outcome,
        "persona": raw.get("persona", {}).get("S"),
    }
