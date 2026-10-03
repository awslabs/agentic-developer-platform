"""Bounded development -> review -> repair -> fresh review on one bound PR.

The K2 runner owns intents, retries and scheduling. This handler stops at
merge_ready. Worker exit, prose and a provider's merged bit cannot accept delivery.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from .execution_policy import Action
from .execution_runner import (
    DecisionKind,
    EffectRequest,
    HandlerDecision,
    HandlerObservation,
    ObservationKind,
    OperationIdentity,
    RunnerContext,
)
from .execution_state import ActionIntent, BlockCode, BlockRecord, ExecutionPhase, OutcomeKind
from .execution_store import load_execution
from .models import OrchestrationAction, OrchestrationNode
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .state import NodeState

DISPATCH_KIND = "review_cycle_dispatch"
PHASES = (
    ExecutionPhase.ADMITTED,
    ExecutionPhase.PREPARING,
    ExecutionPhase.DELIVERING,
    ExecutionPhase.SUBMITTING,
    ExecutionPhase.AWAITING_REVIEW,
    ExecutionPhase.REPAIRING,
)
MAX_HISTORY = 100


class CycleBlockedError(Exception):
    def __init__(self, reason: str, code: BlockCode = BlockCode.DEPENDENCY_UNSATISFIED):
        self.reason, self.code = reason, code
        super().__init__(reason)


def block(reason: str, code: BlockCode = BlockCode.DEPENDENCY_UNSATISFIED) -> BlockRecord:
    required_input = {
        "worker_dispatch_unpublished": (
            "Worker dispatch has no protected execution record. The engine will replay a saved dispatch; "
            "historical assignments without one require recovery."
        ),
        "worker_start_pending": (
            "The assignment is prepared but no worker has acknowledged startup. "
            "The engine will replay the same assignment within its accepted bounds."
        ),
        "shared_worker_continuation_disabled": (
            "The accepted flow uses shared workers, but that transport is disabled. "
            "Have the platform operator restore the approved transport or migrate the flow before resuming."
        ),
        "protected_authority_required": "Have the platform operator restore protected execution support before resuming this flow.",
        "continuation_mode_unrecognized": "Reconcile the accepted continuation contract; no alternate authority mode will be selected.",
    }.get(reason, f"Resolve review cycle condition: {reason}")
    return BlockRecord(
        code=code,
        owner="engine" if reason in {"worker_dispatch_unpublished", "worker_start_pending"} else "orchestration-owner",
        required_input=required_input,
        detail=required_input if reason in {"worker_dispatch_unpublished", "worker_start_pending"} else reason,
    )


@dataclass(frozen=True)
class CycleObservation(HandlerObservation):
    snapshot: dict[str, Any] | None = None


class ReviewCycleHandler:
    def __init__(self, factory, services=None):
        self.factory, self.services = factory, services

    async def observe(self, context: RunnerContext) -> HandlerObservation:
        try:
            return await self._observe(context)
        except CycleBlockedError as error:
            return CycleObservation(ObservationKind.BLOCKED, block=block(error.reason, error.code))
        except Exception:
            # Malformed or unavailable protected/provider state cannot imply
            # success. Keep the condition visible without exposing provider data.
            return CycleObservation(ObservationKind.BLOCKED, block=block("review_cycle_evidence_unavailable", BlockCode.PROVIDER_UNAVAILABLE))

    async def _observe(self, context):
        from .review_cycle_dispatch import cycle_services

        services = self.services or await cycle_services(self.factory, context)
        async with self.factory() as session:
            loaded = await load_execution(session, identity=context.identity)
            if loaded is None or loaded.kind is not OutcomeKind.APPLIED or loaded.record is None:
                raise CycleBlockedError("execution_authority_changed", BlockCode.AUTHORITY_UNVERIFIABLE)
            node = await session.scalar(
                select(OrchestrationNode).where(OrchestrationNode.id == context.identity.node_id, OrchestrationNode.org_id == context.identity.org_id)
            )
            if node is None or node.state not in {NodeState.RUNNING.value, NodeState.AWAITING_MERGE.value} or node.attempts != context.identity.cycle:
                raise CycleBlockedError("outer_gate_not_running", BlockCode.HUMAN_INPUT_REQUIRED)
            binding = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
            if binding is None:
                if not await services.development_complete(context, node):
                    return CycleObservation(ObservationKind.WAITING, detail="Development is still producing its bound PR.")
                raise CycleBlockedError("implementation_pr_missing", BlockCode.HUMAN_INPUT_REQUIRED)
            if not binding_scope_matches(binding, node):
                raise CycleBlockedError("accepted_scope_changed", BlockCode.HUMAN_INPUT_REQUIRED)
            rows = list(
                (
                    await session.scalars(
                        select(OrchestrationAction)
                        .where(
                            OrchestrationAction.org_id == node.org_id,
                            OrchestrationAction.execution_id == context.execution.id,
                            OrchestrationAction.kind.in_([DISPATCH_KIND, "review_evidence", "merge_repair_request"]),
                        )
                        .order_by(OrchestrationAction.created_at, OrchestrationAction.id)
                        .limit(MAX_HISTORY + 1)
                    )
                ).all()
            )
            if len(rows) > MAX_HISTORY:
                raise CycleBlockedError("review_history_limit", BlockCode.ATTEMPTS_EXHAUSTED)
            dispatches = [r for r in rows if r.kind == DISPATCH_KIND]
            pending = next((r for r in dispatches if r.operation_key == context.execution.pending_action_key), None)
            # Resolve every policy/claim/grant before a provider read, and again at
            # dispatch. No cached decision licenses a new worker.
            if pending is not None:
                return await services.observe_dispatch(context, pending)
            if dispatches:
                from .review_assignment import reviewer_owns_delivery
                from .review_cycle_dispatch import continuation_run_id

                if await reviewer_owns_delivery(
                    session, org_id=node.org_id, node_id=node.id, run_id=continuation_run_id(dispatches[-1].operation_key)
                ):
                    head = await services.head(binding)
                    for evidence in reversed(rows):
                        data = evidence.detail or {}
                        cycle = json.loads(data.get("cycle_input", "{}"))
                        if (
                            evidence.kind == "review_evidence"
                            and evidence.status == "succeeded"
                            and data.get("reviewed_head_sha") == head
                            and data.get("complete_review") == "true"
                            and data.get("publication_outstanding") != "true"
                            and cycle.get("reviewer_run_id") == continuation_run_id(dispatches[-1].operation_key)
                            and cycle.get("author_run_id") == dispatches[-1].detail.get("author_run_id")
                        ):
                            # Observe delivery as soon as review evidence exists.
                            # A missing terminal report after merge must not cause
                            # another reviewer to be scheduled.
                            return CycleObservation(
                                ObservationKind.SUCCEEDED,
                                snapshot={
                                    "merge_ready": True,
                                    "binding_id": binding.id,
                                    "binding_revision": binding.revision,
                                    "accepted_scope": binding.accepted_scope,
                                    "head_sha": head,
                                },
                                receipt_ref=evidence.receipt_ref,
                            )
            recover = getattr(services, "recovery_snapshot", None)
            recovered = await recover(session, context, node, binding, dispatches) if recover else None
            if recovered is not None:
                waiting = await services.dispatch_readiness(session, context, node, binding, recovered["active_run_id"], Action.REVIEW)
                return waiting or CycleObservation(ObservationKind.READY, snapshot=recovered)
            facts = await services.facts(session, context, node, binding, dispatches)
            if not facts["worker_complete"]:
                return CycleObservation(ObservationKind.WAITING, detail="Waiting for the current protected worker to finish.")
            if not dispatches:
                from .results import _delivery_receipt

                receipt = await _delivery_receipt(session, node=node, lock=False)
                shared_handoff = getattr(services, "has_delivery_handoff", None)
                if receipt is None and not (shared_handoff and await shared_handoff(session, context, node)):
                    raise CycleBlockedError("development_handoff_missing")
            snapshot = {
                **facts,
                "binding_id": binding.id,
                "binding_revision": binding.revision,
                "repo": binding.repo,
                "pr_number": binding.pr_number,
                "provider_repository_id": binding.provider_repository_id,
                "provider_pr_node_id": binding.provider_pr_node_id,
                "accepted_scope": binding.accepted_scope,
                "sequence": len(dispatches) + 1,
            }
            latest = dispatches[-1] if dispatches else None
            repairs = [row for row in rows if row.kind == "merge_repair_request" and row.status == "succeeded"]
            repair_request = repairs[-1] if repairs else None
            if latest is not None and facts.get("review_retry_of"):
                from .review_assignment import reviewer_owns_delivery

                if await reviewer_owns_delivery(session, org_id=node.org_id, node_id=node.id, run_id=facts["active_run_id"]):
                    for evidence in rows:
                        if evidence.kind != "review_evidence" or evidence.status != "succeeded":
                            continue
                        data = evidence.detail or {}
                        try:
                            reviewed = json.loads(data.get("cycle_input", "{}"))
                        except (TypeError, ValueError):
                            continue
                        if reviewed.get("reviewer_run_id") == facts["active_run_id"] and data.get("reviewed_head_sha") == facts["head_sha"]:
                            raise CycleBlockedError("reviewer_delivery_blocked", BlockCode.HUMAN_INPUT_REQUIRED)
            if latest is not None and facts["active_run_id"] in {facts.get("bootstrap_retry_of"), facts.get("review_retry_of")}:
                # Retry a terminal failure with remaining allowance through a new
                # durable action, retaining its PR, author, findings and allowance.
                snapshot.update(next_action=latest.detail["action"], author_run_id=latest.detail["author_run_id"])
                for key in ("findings", "review_artifact"):
                    if key in latest.detail:
                        snapshot[key] = latest.detail[key]
            elif repair_request is not None and (latest is None or repair_request.created_at > latest.created_at):
                snapshot.update(
                    next_action=Action.REPAIR.value,
                    findings=[{"summary": repair_request.detail["reason"], "source": "merge-controller"}],
                    review_artifact=repair_request.receipt_ref,
                    author_run_id=latest.detail["author_run_id"] if latest else facts["active_run_id"],
                )
            elif latest is None:
                snapshot["next_action"] = Action.REVIEW.value
                snapshot["author_run_id"] = facts["active_run_id"]
            else:
                evidence = [
                    r
                    for r in rows
                    if r.kind == "review_evidence" and r.status == "succeeded" and (r.detail or {}).get("reviewed_head_sha") == facts["head_sha"]
                ]
                matching = []
                for row in evidence:
                    try:
                        item = json.loads((row.detail or {}).get("cycle_input", "{}"))
                    except (TypeError, ValueError):
                        continue
                    if item.get("reviewer_run_id") == facts["active_run_id"] and item.get("author_run_id") == latest.detail.get("author_run_id"):
                        matching.append((row, item))
                if not matching:
                    # Older author-only repairs have no review receipt. A new
                    # reviewer-owned repair supplies its own exact-head evidence
                    # and proceeds through the same merge-ready path below.
                    if latest.detail.get("action") == Action.REPAIR.value:
                        snapshot.update(next_action=Action.REVIEW.value, author_run_id=facts["active_run_id"])
                    # A push invalidates the prior approval; it requires another
                    # exact-head review, never a repair based on stale findings.
                    elif latest.detail.get("head_sha") != facts["head_sha"]:
                        snapshot.update(next_action=Action.REVIEW.value, author_run_id=latest.detail["author_run_id"])
                    else:
                        raise CycleBlockedError("fresh_verified_review_missing")
                else:
                    review, data = max(matching, key=lambda pair: pair[1].get("observed_at", ""))
                    if review.detail.get("complete_review") == "true":
                        # Observe authority/head again before the pure phase move.
                        await services.recheck(session, context, node, binding, facts)
                        return CycleObservation(
                            ObservationKind.SUCCEEDED,
                            snapshot={**snapshot, "merge_ready": True},
                            receipt_ref=review.receipt_ref,
                            detail="Exact-head review is complete; merge eligibility remains a separate gate.",
                        )
                    if review.detail.get("publication_outstanding") == "true":
                        raise CycleBlockedError("review_publication_outstanding")
                    from .review_assignment import reviewer_owns_delivery

                    if await reviewer_owns_delivery(session, org_id=node.org_id, node_id=node.id, run_id=facts["active_run_id"]):
                        # This reviewer already owned repair. An explicit blocker
                        # is not permission to dispatch the same work again.
                        raise CycleBlockedError("reviewer_delivery_blocked", BlockCode.HUMAN_INPUT_REQUIRED)
                    findings = data.get("findings")
                    if not isinstance(findings, list) or not findings or data.get("blocked"):
                        raise CycleBlockedError("review_inconclusive")
                    snapshot.update(
                        next_action=Action.REPAIR.value,
                        findings=findings,
                        review_artifact=review.artifact_ref,
                        author_run_id=latest.detail["author_run_id"],
                    )
            if snapshot["next_action"] == Action.REPAIR.value:
                snapshot["remaining_attempts"] = facts.get("remaining_repair_attempts", facts["remaining_attempts"])
            await services.recheck(session, context, node, binding, facts)
            readiness = getattr(services, "dispatch_readiness", None)
            if readiness is not None:
                waiting = await readiness(session, context, node, binding, facts["active_run_id"], Action(snapshot["next_action"]))
                if waiting is not None:
                    return waiting
            return CycleObservation(ObservationKind.READY, snapshot=snapshot)

    def decide(self, context, observation):
        if observation.kind is ObservationKind.BLOCKED:
            return HandlerDecision(DecisionKind.BLOCK, block=observation.block)
        if observation.kind is ObservationKind.SUCCEEDED and not getattr(observation, "snapshot", None):
            return HandlerDecision(
                DecisionKind.ADVANCE,
                phase=context.execution.phase,
                progress_note="Worker start acknowledged; observing the current review assignment.",
            )
        snapshot = dict(getattr(observation, "snapshot", None) or {})
        if snapshot.get("merge_ready"):

            async def settle(session, current):
                # The next handler compares R1's reviewed head with the binding.
                # Refresh only this already-verified PR pointer, atomically with
                # merge_ready; never rebind the work or reset its accepted scope.
                from .pr_bindings import refresh_reviewed_head

                await refresh_reviewed_head(
                    session,
                    identity=current.identity,
                    binding_id=snapshot["binding_id"],
                    revision=snapshot["binding_revision"],
                    accepted_scope=snapshot["accepted_scope"],
                    head_sha=snapshot["head_sha"],
                )

            return HandlerDecision(
                DecisionKind.ADVANCE,
                phase=ExecutionPhase.MERGE_READY,
                settlement=settle,
                progress_note="Current head reviewed. Ready for the separately authorized merge phase.",
            )
        if "replay" in snapshot:
            detail = snapshot["replay"]
            action = Action(detail["action"])
            return self._effect(context, action, observation.operation_key, detail)
        if observation.kind is not ObservationKind.READY or not snapshot:
            return HandlerDecision(
                DecisionKind.WAIT,
                next_check_at=context.now + timedelta(seconds=60),
                progress_note=observation.detail or "Waiting for protected review-cycle evidence.",
            )
        if snapshot["remaining_attempts"] <= 0:
            return HandlerDecision(DecisionKind.BLOCK, block=block("continuation_attempts_exhausted", BlockCode.ATTEMPTS_EXHAUSTED))
        action = Action(snapshot.pop("next_action"))
        key = OperationIdentity.from_context(
            context, DISPATCH_KIND, action.value, str(snapshot["sequence"]), snapshot["head_sha"], snapshot["active_run_id"]
        ).key
        detail = {**snapshot, "action": action.value, "arrived_at": context.now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        return self._effect(context, action, key, detail)

    @staticmethod
    def _effect(context, action, key, detail):
        return HandlerDecision(
            DecisionKind.EFFECT,
            phase=ExecutionPhase.REPAIRING if action is Action.REPAIR else ExecutionPhase.AWAITING_REVIEW,
            effect=EffectRequest(ActionIntent(operation_key=key, kind=DISPATCH_KIND, detail=detail), action),
            progress_note="Repairing the bound PR." if action is Action.REPAIR else "Reviewing the current PR head in a distinct execution.",
        )

    async def perform(self, context, effect):
        from .execution_runner import EffectOutcome, EffectResult
        from .review_cycle_dispatch import cycle_services

        try:
            services = self.services or await cycle_services(self.factory, context)
        except CycleBlockedError as error:
            return EffectResult(EffectOutcome.UNCERTAIN, detail=error.reason)
        return await services.dispatch(context, effect)


def handlers(factory):
    handler = ReviewCycleHandler(factory)
    return dict.fromkeys(PHASES, handler)
