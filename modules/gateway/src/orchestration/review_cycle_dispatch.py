"""Trusted continuation of a committed K2 action through existing dispatch stores.

A continuation keeps the flow's claim generation, policy meter and node attempt.
Only the held run's protected terminal receipt permits a successor. The durable
SQL action/decision, protected grant and FIFO message all use the same identity.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from boto3.dynamodb.types import TypeSerializer
from sqlalchemy import select

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.engine import get_engine_authority_writer, validate_engine_authority
from src.agentauth.grants import AgentAction, DelegatedGrant, TargetRelationship
from src.agentauth.launch_configuration import resolve_launch_configuration
from src.shared.identity.resolver import resolve_root_user_entity_id, resolve_user_entity_id

from .dispatch import graph_address
from .dispatch_pass import DispatchPassConfig, _build_envelope, _get_sqs_client, attempt_run_id
from .execution_policy import Action, CredentialScope, ResourceRef, authorize_action
from .execution_runner import EffectOutcome, EffectResult, ObservationKind
from .execution_state import BlockCode, OutcomeKind
from .execution_store import load_execution
from .genesis import resolve_engine_genesis
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim
from .policy_admission import SpendObservation, authorize_node_dispatch, load_in_force_policy, resolve_authorization_context
from .pr_bindings import active_binding_for_node, binding_scope_matches, continuation_binding_hold
from .review_cycle import DISPATCH_KIND, CycleBlockedError, CycleObservation, block
from .run_store import EngineRunStore
from .stage_attempts import stage_attempts
from .startup_recovery import attested_review_startup, is_retryable_bootstrap_failure

ACTOR = "system:review-cycle"


def failed_review(raw):
    """A terminal failed reviewer continuation may retry within its remaining stage allowance.

    This covers the review itself and the reviewer-owned repair the engine hands
    an open defect to (#7482): a reviewer that pushed its repair and then could
    not publish the verdict has exited failed on the same lane, and the bounded
    retry is the recovery, not a human.
    """
    return bool(
        raw
        and raw.get("persona") == {"S": "agent-codex-reviewer"}
        and raw.get("orchestration_continuation_action") in ({"S": "review"}, {"S": "repair"})
        and raw.get("orchestration_continuation_receipt")
        and raw.get("status") == {"S": "completed"}
        and raw.get("terminal_outcome") in ({"S": "failed"}, {"S": "budget_stopped"})
    )


async def require_budget_recovery(session, *, node, active_run_id, raw, policy):
    """Hold native protected budget exits without repeatedly spending stage attempts.

    Only the gateway's terminal status transaction supplies this timestamp.
    A later in-force budget receipt permits normal admission to be tried again;
    it does not prove affordability or reset any attempts.
    """
    if raw.get("terminal_outcome") != {"S": "budget_stopped"}:
        return
    if (
        not failed_review(raw)
        or raw.get("tenant_id") != {"S": node.org_id}
        or raw.get("invocation_id") != {"S": active_run_id}
        or raw.get("orchestration_node_id") != {"S": node.id}
    ):
        raise CycleBlockedError("review_budget_failure_unverifiable", BlockCode.AUTHORITY_UNVERIFIABLE)
    try:
        failed_at = datetime.fromisoformat(raw["terminal_recorded_at"]["S"].replace("Z", "+00:00"))
        if failed_at.tzinfo is None:
            raise ValueError("unverified timestamp")
    except (KeyError, AttributeError, TypeError, ValueError):
        # Older gateways recorded the outcome without time. Keep it visibly held;
        # explicit protected recovery can release it after the budget is fixed.
        raise CycleBlockedError("review_worker_budget_exhausted", BlockCode.BUDGET_EXHAUSTED) from None
    decision_id = policy._shared_budget_decision_id
    decision = await session.get(OrchestrationDecision, decision_id) if decision_id else None
    if (
        decision is not None
        and decision.org_id == node.org_id
        and decision.flow_id == node.flow_id
        and decision.kind == "budget_increased"
        and decision.actor_kind == "human"
        and decision.created_at.replace(tzinfo=decision.created_at.tzinfo or UTC) > failed_at
    ):
        return
    raise CycleBlockedError("review_worker_budget_exhausted", BlockCode.BUDGET_EXHAUSTED)


def continuation_run_id(key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "adp-review-cycle:" + key))


def receipt_id(key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "adp-review-cycle-receipt:" + key))


async def current_author_run(session, *, node, default):
    from .handoff import identity_for_attempt

    identity = await identity_for_attempt(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
    if identity is None:
        return default
    loaded = await load_execution(session, identity=identity)
    if loaded is None or loaded.kind is not OutcomeKind.APPLIED or loaded.record is None:
        return default
    rows = list(
        (
            await session.scalars(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.execution_id == loaded.record.id,
                    OrchestrationAction.kind == DISPATCH_KIND,
                )
                .order_by(OrchestrationAction.created_at.desc(), OrchestrationAction.id.desc())
                .limit(101)
            )
        ).all()
    )
    if len(rows) > 100:
        raise BootstrapRefusedError("continuation history unavailable")
    for row in rows:
        if (row.detail or {}).get("action") == Action.REPAIR.value:
            committed = await session.get(OrchestrationDecision, receipt_id(row.operation_key))
            if committed is not None and committed.org_id == node.org_id and committed.actor_id == ACTOR:
                saved = json.loads(committed.reason)
                envelope = saved.get("envelope") or {}
                cycle = envelope.get("review_cycle_input") or {}
                expected = envelope.get("review_expect") or {}
                run_id = continuation_run_id(row.operation_key)
                # This capability comes from the committed dispatcher receipt,
                # never from review-document claims. Review-and-fix continuations
                # retain the implementation author just as review assignments do.
                if (
                    committed.node_id == node.id
                    and committed.flow_id == node.flow_id
                    and committed.actor_kind == "service"
                    and committed.kind == "agent_dispatched"
                    and saved.get("run_id") == run_id
                    and saved.get("action") == Action.REPAIR.value
                    and envelope.get("persona") == "agent-codex-reviewer"
                    and cycle.get("reviewer_owned_delivery") is True
                    and expected.get("author_run_id") == row.detail.get("author_run_id")
                    and expected.get("author_run_id")
                    and expected["author_run_id"] != run_id
                ):
                    return expected["author_run_id"]
                return run_id
    return default


async def validate_continuation_assignment(session, *, execution, grant, node):
    from .handoff import identity_for_attempt

    identity = await identity_for_attempt(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
    if identity is None:
        raise BootstrapRefusedError("continuation identity unavailable")
    loaded = await load_execution(session, identity=identity)
    if loaded is None or loaded.kind is not OutcomeKind.APPLIED or loaded.record is None:
        raise BootstrapRefusedError("continuation execution unavailable")
    receipt = await session.scalar(
        select(OrchestrationDecision).where(
            OrchestrationDecision.id == execution["orchestration_continuation_receipt"]["S"],
            OrchestrationDecision.org_id == grant.tenant_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.flow_id == grant.flow_id,
            OrchestrationDecision.actor_id == ACTOR,
            OrchestrationDecision.kind == "agent_dispatched",
            OrchestrationDecision.actor_kind == "service",
        )
    )
    data = json.loads(receipt.reason) if receipt else {}
    action = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == grant.tenant_id,
            OrchestrationAction.execution_id == loaded.record.id,
            OrchestrationAction.operation_key == data.get("operation_key"),
            OrchestrationAction.kind == DISPATCH_KIND,
        )
    )
    claim = await session.scalar(
        select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.org_id == grant.tenant_id, OrchestrationWorkClaim.id == identity.claim_id)
    )
    run_id = execution["invocation_id"]["S"]
    if (
        action is None
        or claim is None
        or claim.generation != identity.claim_generation
        or claim.active_run_id != run_id
        or claim.state != "held"
        or data.get("claim_generation") != identity.claim_generation
        or data.get("accepted_plan_version") != identity.accepted_plan_version
        or data.get("execution_id") != loaded.record.id
        or data.get("run_id") != run_id
        or continuation_run_id(action.operation_key) != run_id
        or data.get("action") != execution["orchestration_continuation_action"]["S"]
        or data.get("authority_reference_id") != grant.authority.reference_id
        or data.get("parent_principal") != execution.get("parent_principal", {}).get("S")
        or (execution.get("orchestration_review_repairs") == {"BOOL": True})
        != (
            (data.get("envelope", {}).get("review_cycle_input") or {}).get("allow_story_repairs") is True
            and data.get("action") == Action.REVIEW.value
        )
    ):
        raise BootstrapRefusedError("continuation assignment changed")


class ReviewCycleServices:
    def __init__(self, factory, *, writer=None, queue=None, config=None):
        self.factory, self._writer, self.queue, self.config = factory, writer, queue, config

    @property
    def writer(self):
        return self._writer or get_engine_authority_writer()

    async def protected(self, org_id, run_id):
        return await asyncio.to_thread(self.writer.store._read, f"TENANT#{org_id}", f"EXEC#{run_id}")

    async def authority_context(self, session, context, node, binding, run_id, action, *, delivery=False, recovery_decision_id=None):
        if os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() != "true":
            raise CycleBlockedError("protected_authority_required", BlockCode.AUTHORITY_UNVERIFIABLE)
        if delivery and run_id.startswith("delivery-recovery:"):
            from .delivery_recovery import authority_context

            return await authority_context(self, session, context, node, binding, run_id, action)
        if recovery_decision_id:
            from .developer_review_protocol import reservation_id, verified_decision

            if delivery or action not in {Action.REVIEW, Action.REPAIR}:
                raise CycleBlockedError("recovery_action_not_authorized", BlockCode.AUTHORITY_UNVERIFIABLE)
            if run_id != reservation_id(recovery_decision_id):
                raise CycleBlockedError("recovery_claim_changed", BlockCode.OWNERSHIP_LOST)
            recovered, values = await verified_decision(
                session,
                decision_id=recovery_decision_id,
                context=context,
                node=node,
                binding=binding,
                services=self,
                head_sha=await self.head(binding),
            )
            from .released_reviewer_recovery import CONTRACT as RELEASED_REVIEWER_CONTRACT
            from .retained_work_recovery import CONTRACT as RETAINED_WORK_CONTRACT

            if recovered.get("contract") in {RETAINED_WORK_CONTRACT, RELEASED_REVIEWER_CONTRACT} and action is not Action.REVIEW:
                raise CycleBlockedError("recovery_action_not_authorized", BlockCode.AUTHORITY_UNVERIFIABLE)
            return await self.policy_context(session, context, node, binding, run_id, action, values[0], values[1])
        claim = await session.scalar(
            select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.org_id == node.org_id, OrchestrationWorkClaim.id == context.identity.claim_id)
        )
        if (
            claim is None
            or claim.generation != context.identity.claim_generation
            or claim.active_run_id != run_id
            or claim.state != "held"
            or claim.owner_ref != node.flow_id
            or claim.owner_kind != "engine_flow"
        ):
            raise CycleBlockedError("active_claim_changed", BlockCode.OWNERSHIP_LOST)
        raw = await self.protected(node.org_id, run_id)
        if (
            not raw
            or raw.get("repo") != {"S": binding.repo}
            or raw.get("installation_id") != {"N": str(binding.installation_id)}
            or raw.get("provider_repository_id") != {"N": str(binding.provider_repository_id)}
        ):
            raise CycleBlockedError("protected_execution_missing", BlockCode.AUTHORITY_UNVERIFIABLE)
        try:
            grant = await asyncio.to_thread(
                self.writer.store.live_grant,
                invocation_id=run_id,
                tenant_id=node.org_id,
                attempt=int(raw["current_attempt"]["N"]),
                now=datetime.now(UTC),
            )
            if delivery and action not in {Action.DEPLOY, Action.EVALUATE}:
                raise BootstrapRefusedError("delivery continuation is reserved for engine deployment and evaluation")
            await validate_engine_authority(
                session=session, execution=raw, grant=grant, store=self.writer.store, delivery_identity=context.identity if delivery else None
            )
        except BootstrapRefusedError:
            raise CycleBlockedError("grant_revoked_or_unverifiable", BlockCode.AUTHORITY_UNVERIFIABLE) from None
        return await self.policy_context(session, context, node, binding, run_id, action, raw, grant)

    async def policy_context(self, session, context, node, binding, run_id, action, raw, grant, *, read_only=False):
        """Resolve current policy independently of the caller's ownership proof."""
        inputs = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
        from .plan_lineage import execution_plan_matches

        # Amendments that preserved this node keep its policy authority (#7535).
        if (
            inputs.refusal
            or inputs.policy is None
            or not await execution_plan_matches(
                session,
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                version=context.identity.accepted_plan_version,
                current_version=inputs.plan_version,
            )
        ):
            raise CycleBlockedError("policy_changed", BlockCode.AUTHORITY_UNVERIFIABLE)
        from .flow_meter import read_flow_meter, reconcile_flow_meter
        from .runtime_policy import flow_started_at, policy_github_permissions

        # Repair missed settlement first: a review cycle admits no new dispatch,
        # so without this an abandoned strict marker left the meter unreadable and
        # every reviewer refused with spend_unknown for the rest of the flow (#7451).
        meter = (
            await read_flow_meter(org_id=node.org_id, flow_id=node.flow_id, policy=inputs.policy)
            if read_only
            else await reconcile_flow_meter(session, org_id=node.org_id, flow_id=node.flow_id, policy=inputs.policy)
        )
        principal = await resolve_root_user_entity_id(session, node.org_id, grant.authority.human_id)
        auth = await resolve_authorization_context(
            session,
            policy=inputs.policy,
            plan_version=inputs.plan_version,
            node=node,
            principal_user_id=principal,
            credential_scope=CredentialScope.SCOPED if policy_github_permissions(inputs.policy, action) else CredentialScope.UNSCOPABLE,
            spend=SpendObservation(total_usd=meter.total_usd if meter else None),
            provider_repository_id=binding.provider_repository_id,
            expected_invocation_id=run_id,
        )
        # Reauthorization of an admitted stage does not spend another attempt.
        used = await stage_attempts(session, org_id=node.org_id, node_id=node.id, action=action) - 1
        auth = replace(auth, observed_attempts=max(0, used), observed_concurrency=max(0, auth.observed_concurrency - 1))
        started = await flow_started_at(session, org_id=node.org_id, flow_id=node.flow_id)
        if started is None or (datetime.now(UTC) - started).total_seconds() >= inputs.policy.limits.max_wall_clock_seconds:
            raise CycleBlockedError("wall_clock_limit_exceeded", BlockCode.ATTEMPTS_EXHAUSTED)
        return raw, grant, inputs, principal, meter, auth

    async def authorize(self, session, context, node, binding, run_id, action, *, reserve=False, recovery_decision_id=None):
        hold = await continuation_binding_hold(session, node, binding)
        if hold:
            raise CycleBlockedError(
                hold, BlockCode.AUTHORITY_UNVERIFIABLE if hold == "repository_connection_unavailable" else BlockCode.HUMAN_INPUT_REQUIRED
            )
        recovery = {"recovery_decision_id": recovery_decision_id} if recovery_decision_id else {}
        raw, grant, inputs, principal, meter, auth = await self.authority_context(session, context, node, binding, run_id, action, **recovery)
        flow = await session.get(OrchestrationFlow, node.flow_id)
        decision = authorize_action(
            auth,
            action,
            ResourceRef(repository_id=binding.repo, node_address=graph_address(node, flow_slug=flow.slug), org_id=node.org_id),
            inputs.plan_version,
        )
        if not decision.permitted:
            code = (
                BlockCode.BUDGET_EXHAUSTED
                if "spend" in decision.reason.value or "budget" in decision.reason.value
                else BlockCode.AUTHORITY_UNVERIFIABLE
            )
            raise CycleBlockedError(decision.reason.value, code)
        if reserve:
            admission = await authorize_node_dispatch(
                session,
                node=node,
                principal_user_id=principal,
                target_repository=binding.repo,
                installation_resolved=True,
                provider_repository_id=binding.provider_repository_id,
                expected_invocation_id=run_id,
                action_override=action,
                continuing_node=True,
            )
            if not admission.permitted:
                code = (
                    BlockCode.BUDGET_EXHAUSTED
                    if "spend" in admission.reason.value or "budget" in admission.reason.value
                    else BlockCode.AUTHORITY_UNVERIFIABLE
                )
                raise CycleBlockedError(admission.reason.value, code)
        return raw, grant, inputs, principal, meter

    async def head(self, binding):
        from .pr_identity import PrIdentityError, resolve_pr_identity

        try:
            pr = await resolve_pr_identity(
                org_id=binding.org_id, installation_id=binding.installation_id, repo=binding.repo, pr_number=binding.pr_number
            )
        except PrIdentityError:
            raise CycleBlockedError("pr_head_unavailable", BlockCode.PROVIDER_UNAVAILABLE) from None
        if pr.provider_repository_id != binding.provider_repository_id or pr.provider_pr_node_id != binding.provider_pr_node_id:
            raise CycleBlockedError("pr_identity_changed", BlockCode.HUMAN_INPUT_REQUIRED)
        return pr.head_sha

    async def development_complete(self, context, node):
        raw = await self.protected(node.org_id, attempt_run_id(node.id, node.attempts))
        if not raw:
            raise CycleBlockedError("worker_dispatch_unpublished", BlockCode.PROVIDER_UNAVAILABLE)
        if raw.get("tenant_id") != {"S": node.org_id} or raw.get("orchestration_node_id") != {"S": node.id}:
            raise CycleBlockedError("protected_development_mismatch", BlockCode.AUTHORITY_UNVERIFIABLE)
        status = raw.get("status", {}).get("S")
        if status == "pending" and not raw.get("workload_binding"):
            raise CycleBlockedError("worker_start_pending", BlockCode.PROVIDER_UNAVAILABLE)
        if status in {"cancelled", "revoked"} or (status == "completed" and raw.get("terminal_outcome") != {"S": "complete"}):
            raise CycleBlockedError("worker_failed_or_halted", BlockCode.HUMAN_INPUT_REQUIRED)
        if status not in {"active", "completed"} or (status == "active" and not raw.get("workload_binding")):
            raise CycleBlockedError("worker_start_unverifiable", BlockCode.AUTHORITY_UNVERIFIABLE)
        return status == "completed"

    async def recovery_snapshot(self, session, context, node, binding, dispatches):
        from .developer_review_protocol import recovery_snapshot as developer_recovery

        recovered = await developer_recovery(session, context, node, binding, self, dispatches)
        if recovered is not None:
            return recovered
        from .protected_review_recovery import recovery_snapshot

        return await recovery_snapshot(session, context, node, binding, self, dispatches)

    async def facts(self, session, context, node, binding, dispatches, *, budget_recovery_verified=False):
        active = continuation_run_id(dispatches[-1].operation_key) if dispatches else attempt_run_id(node.id, node.attempts)
        raw, grant, inputs, _, meter = await self.authorize(session, context, node, binding, active, Action.REVIEW)
        status = raw.get("status", {}).get("S")
        bootstrap_failed = bool(dispatches) and is_retryable_bootstrap_failure(raw)
        reset = await session.scalar(
            select(OrchestrationDecision)
            .where(
                OrchestrationDecision.org_id == node.org_id,
                OrchestrationDecision.node_id == node.id,
                OrchestrationDecision.kind == "review_bootstrap_reset",
            )
            .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
            .limit(1)
        )
        history = dispatches
        if reset is not None:
            try:
                reset_data = json.loads(reset.reason)
            except (TypeError, ValueError):
                reset_data = {}
            if reset_data.get("execution_id") == context.execution.id:
                for index, prior in enumerate(dispatches):
                    if prior.operation_key == reset_data.get("last_action_key"):
                        history = dispatches[index + 1 :]
                        break
        prior_failures = 0
        for prior in reversed(history[:-1]):
            if (prior.detail or {}).get("startup_classification", {}).get("exemption_status") != "bootstrap_exempt":
                break
            prior_failures += 1
        if bootstrap_failed and (dispatches[-1].detail or {}).get("action") == Action.REVIEW.value:
            from .run_reports import OrchestrationRunReport

            latest = dispatches[-1]
            classification = attested_review_startup(raw, invocation_id=active, receipt=receipt_id(latest.operation_key))
            report = await session.get(OrchestrationRunReport, active)
            if classification is None or report is not None and (report.worker_receipt or report.terminal_receipt):
                raise CycleBlockedError("review_startup_evidence_unverifiable", BlockCode.AUTHORITY_UNVERIFIABLE)
            if not (latest.detail or {}).get("startup_classification"):
                classification["failure_ordinal"] = prior_failures + 1
                latest.detail = {**latest.detail, "startup_classification": classification}
                await session.commit()
        current_classification = (dispatches[-1].detail or {}).get("startup_classification") if bootstrap_failed and history else None
        bootstrap_failures = prior_failures + 1 if current_classification is not None else 0
        review_failed = bool(dispatches) and failed_review(raw)
        if review_failed and not budget_recovery_verified:
            await require_budget_recovery(session, node=node, active_run_id=active, raw=raw, policy=inputs.policy)
        if (status in {"cancelled", "revoked"} and not bootstrap_failed) or (
            status == "completed" and raw.get("terminal_outcome") != {"S": "complete"} and not bootstrap_failed and not review_failed
        ):
            raise CycleBlockedError("worker_failed_or_halted", BlockCode.HUMAN_INPUT_REQUIRED)
        worker_complete = status == "completed" or bootstrap_failed
        return {
            "active_run_id": active,
            "worker_complete": worker_complete,
            "bootstrap_retry_of": active if bootstrap_failed else None,
            "bootstrap_classification": current_classification,
            "bootstrap_failures": bootstrap_failures,
            "review_retry_of": active if review_failed else None,
            # An in-flight worker cannot advance this cycle. Its next transition
            # still resolves the provider head after protected completion.
            "head_sha": await self.head(binding) if worker_complete else None,
            "remaining_spend_usd": str(inputs.policy.limits.max_spend_usd - meter.total_usd)
            if inputs.policy._budget_enforcement_enabled and meter
            else None,
            "remaining_attempts": inputs.policy.limits.max_attempts_per_node
            - await stage_attempts(session, org_id=node.org_id, node_id=node.id, action=Action.REVIEW),
            "remaining_repair_attempts": inputs.policy.limits.max_attempts_per_node
            - await stage_attempts(session, org_id=node.org_id, node_id=node.id, action=Action.REPAIR),
        }

    async def recheck(self, session, context, node, binding, facts):
        # Shared transport inherits this controller hook and has its own
        # authorization interface. A protected recovery keyword belongs only to
        # observations carrying that protocol's explicit receipt.
        recovery_id = facts.get("developer_recovery_decision_id")
        recovery = {"recovery_decision_id": recovery_id} if recovery_id else {}
        await self.authorize(
            session,
            context,
            node,
            binding,
            facts["active_run_id"],
            Action.REVIEW,
            **recovery,
        )
        if await self.head(binding) != facts["head_sha"]:
            raise CycleBlockedError("head_changed_during_observation")

    async def observe_dispatch(self, context, action):
        raw = await self.protected(context.identity.org_id, continuation_run_id(action.operation_key))
        # A pending credential alone does not prove SQS received the message. A
        # retry republishes the same immutable envelope and protected invocation.
        if raw and raw.get("status", {}).get("S") != "pending":
            return CycleObservation(
                ObservationKind.SUCCEEDED,
                operation_key=action.operation_key,
                receipt_ref=f"dispatch:{continuation_run_id(action.operation_key)}",
                detail="Protected successor observed.",
            )
        async with self.factory() as session:
            node = await session.scalar(
                select(OrchestrationNode).where(OrchestrationNode.org_id == context.identity.org_id, OrchestrationNode.id == context.identity.node_id)
            )
            binding = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
            if (
                binding is None
                or binding.id != action.detail["binding_id"]
                or binding.accepted_scope != action.detail["accepted_scope"]
                or binding.revision != action.detail["binding_revision"]
            ):
                raise CycleBlockedError("bound_pr_or_scope_changed", BlockCode.HUMAN_INPUT_REQUIRED)
            recovery_id = action.detail.get("developer_recovery_decision_id")
            if recovery_id and raw is None:
                from .developer_review_protocol import verified_decision

                committed = await session.get(OrchestrationDecision, receipt_id(action.operation_key))
                await verified_decision(
                    session,
                    decision_id=recovery_id,
                    context=context,
                    node=node,
                    binding=binding,
                    services=self,
                    head_sha=await self.head(binding),
                    successor_action=action if committed is not None else None,
                )
                return CycleObservation(ObservationKind.READY, operation_key=action.operation_key, snapshot={"replay": dict(action.detail)})
            source_run = continuation_run_id(action.operation_key) if raw else action.detail["active_run_id"]
            source = raw or await self.protected(node.org_id, source_run)
            if not source:
                raise CycleBlockedError("protected_execution_missing", BlockCode.AUTHORITY_UNVERIFIABLE)
            try:
                await asyncio.to_thread(
                    self.writer.store.live_grant,
                    invocation_id=source_run,
                    tenant_id=node.org_id,
                    attempt=int(source["current_attempt"]["N"]),
                    now=datetime.now(UTC),
                )
            except BootstrapRefusedError:
                raise CycleBlockedError("grant_revoked_or_unverifiable", BlockCode.AUTHORITY_UNVERIFIABLE) from None
            claim = await session.get(OrchestrationWorkClaim, context.identity.claim_id)
            if raw or claim.active_run_id == source_run:
                await self.authorize(session, context, node, binding, source_run, Action(action.detail["action"]))
            elif claim.active_run_id != continuation_run_id(action.operation_key):
                raise CycleBlockedError("active_claim_changed", BlockCode.OWNERSHIP_LOST)
            current_head = await self.head(binding)
            if current_head != action.detail["head_sha"]:
                # Preparation commits a SQL receipt and transfers the claim
                # before it can provision or publish. Only an intent that never
                # crossed that boundary can be retired without a worker receipt.
                receipt = await session.get(OrchestrationDecision, receipt_id(action.operation_key))
                if raw is None and receipt is None and claim.active_run_id == action.detail["active_run_id"]:
                    return CycleObservation(
                        ObservationKind.READY,
                        snapshot={
                            "retire_unstarted_dispatch": {
                                "action_id": action.id,
                                "operation_key": action.operation_key,
                                "detail": dict(action.detail),
                                "current_head": current_head,
                            }
                        },
                    )
                raise CycleBlockedError("head_changed_before_dispatch")
        return CycleObservation(ObservationKind.READY, operation_key=action.operation_key, snapshot={"replay": dict(action.detail)})

    async def dispatch(self, context, effect):
        try:
            finite_receipt = None
            envelope, grant, metadata = await self.prepare(context, effect)
            await asyncio.to_thread(self.provision, envelope, grant, metadata)
            # Recheck SQL assignment and current grant after provisioning, before
            # publication. An old durable intent cannot outlive revoked authority.
            async with self.factory() as session:
                node = await session.get(OrchestrationNode, context.identity.node_id)
                raw = await self.protected(context.identity.org_id, envelope["message_id"])
                live = await asyncio.to_thread(
                    self.writer.store.live_grant,
                    invocation_id=envelope["message_id"],
                    tenant_id=context.identity.org_id,
                    attempt=1,
                    now=datetime.now(UTC),
                )
                await validate_engine_authority(session=session, execution=raw, grant=live, store=self.writer.store)
                binding = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
                if recovery_id := effect.intent.detail.get("developer_recovery_decision_id"):
                    from .developer_review_protocol import verified_decision

                    action = await session.scalar(
                        select(OrchestrationAction).where(
                            OrchestrationAction.org_id == node.org_id,
                            OrchestrationAction.execution_id == context.execution.id,
                            OrchestrationAction.operation_key == effect.intent.operation_key,
                        )
                    )
                    recovered, _ = await verified_decision(
                        session,
                        decision_id=recovery_id,
                        context=context,
                        node=node,
                        binding=binding,
                        services=self,
                        head_sha=await self.head(binding),
                        successor_action=action,
                    )
                    from . import released_reviewer_recovery, retained_work_recovery

                    for finite in (retained_work_recovery, released_reviewer_recovery):
                        if recovered.get("contract") == finite.CONTRACT:
                            finite_receipt = (finite, recovered)
                _, _, _, principal, _ = await self.authorize(session, context, node, binding, envelope["message_id"], effect.action, reserve=True)
                if await self.head(binding) != effect.intent.detail["head_sha"]:
                    raise CycleBlockedError("head_changed_before_dispatch")
                if recovery_id := effect.intent.detail.get("protected_recovery_decision_id"):
                    from .protected_review_recovery import verified_decision, verify_reviewer_configuration

                    recovered = await verified_decision(
                        session,
                        decision_id=recovery_id,
                        context=context,
                        node=node,
                        binding=binding,
                        services=self,
                        prior_run_id=effect.intent.detail["active_run_id"],
                        head_sha=effect.intent.detail["head_sha"],
                    )
                    selected = await verify_reviewer_configuration(session, recovered, org_id=node.org_id, user_id=principal)
                    if selected is not None and any(envelope.get(key) != value for key, value in selected.items()):
                        raise CycleBlockedError("reviewer_model_selection_changed")
                # Continuations transfer an existing claim rather than calling
                # admit_pending. Attach the parent's immutable model settings
                # here, while the protected child is still pending and before
                # a queued worker can bind it active. Use the same preparation
                # path as ordinary dispatch; runtime posture remains authoritative.
                from src.agentauth.model_policy import ensure_snapshot_report_only

                await ensure_snapshot_report_only(session, store=self.writer.store, invocation_id=envelope["message_id"])
            cfg = self.config or DispatchPassConfig.from_env()
            queue = self.queue or _get_sqs_client(cfg.aws_region)

            def publish():
                if finite_receipt is not None:
                    finite, receipt = finite_receipt
                    finite.require_unexpired(receipt, reconciliation_required=True)
                return queue.send_message(
                    QueueUrl=cfg.queue_url,
                    MessageBody=json.dumps(envelope),
                    MessageGroupId=f"review-cycle-{context.execution.claim_id}",
                    MessageDeduplicationId=envelope["message_id"],
                )

            await asyncio.to_thread(publish)
            return EffectResult(EffectOutcome.SUCCEEDED, receipt_ref=f"dispatch:{envelope['message_id']}")
        except CycleBlockedError as error:
            # A replay can refuse on every tick without observe seeing the refusal.
            # Persist the budget hold while retaining this same recoverable intent.
            return EffectResult(
                EffectOutcome.UNCERTAIN,
                detail=error.reason,
                block=block(error.reason, error.code) if error.code is BlockCode.BUDGET_EXHAUSTED else None,
            )

    async def prepare(self, context, effect):
        cfg = self.config or DispatchPassConfig.from_env()
        detail = effect.intent.detail
        if not cfg.configured:
            raise CycleBlockedError("dispatch_configuration_unavailable", BlockCode.CREDENTIAL_UNAVAILABLE)
        run_id = continuation_run_id(effect.intent.operation_key)
        async with self.factory() as session:
            # Match the store's lock order; the current flow/node gates remain in
            # force. Never turn failed/halted/awaiting_gate into running.
            node = await session.scalar(
                select(OrchestrationNode).where(OrchestrationNode.org_id == context.identity.org_id, OrchestrationNode.id == context.identity.node_id)
            )
            loaded = await load_execution(session, identity=context.identity)
            if (
                loaded is None
                or loaded.kind is not OutcomeKind.APPLIED
                or loaded.record is None
                or node is None
                or node.state != "running"
                or node.attempts != context.identity.cycle
                or loaded.record.status.value != "awaiting_external"
                or loaded.record.pending_action_key != effect.intent.operation_key
            ):
                raise CycleBlockedError("outer_gate_or_execution_changed", BlockCode.HUMAN_INPUT_REQUIRED)
            action = await session.scalar(
                select(OrchestrationAction).where(
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.execution_id == loaded.record.id,
                    OrchestrationAction.operation_key == effect.intent.operation_key,
                )
            )
            if action is None or action.kind != DISPATCH_KIND or action.detail != detail:
                raise CycleBlockedError("durable_dispatch_intent_changed", BlockCode.AUTHORITY_UNVERIFIABLE)
            binding = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
            if (
                binding is None
                or not binding_scope_matches(binding, node)
                or binding.id != detail["binding_id"]
                or binding.accepted_scope != detail["accepted_scope"]
                or binding.revision != detail["binding_revision"]
                or binding.repo != detail["repo"]
            ):
                raise CycleBlockedError("bound_pr_or_scope_changed", BlockCode.HUMAN_INPUT_REQUIRED)
            old = await session.get(OrchestrationDecision, receipt_id(action.operation_key))
            recovery_id = detail.get("developer_recovery_decision_id")
            if old is not None:
                claim = await session.get(OrchestrationWorkClaim, context.identity.claim_id, populate_existing=True)
                if claim is None or claim.active_run_id not in {detail["active_run_id"], run_id}:
                    raise CycleBlockedError("active_claim_changed", BlockCode.OWNERSHIP_LOST)
                if recovery_id and await self.protected(node.org_id, claim.active_run_id) is None:
                    hold = await continuation_binding_hold(session, node, binding)
                    if hold:
                        raise CycleBlockedError(
                            hold, BlockCode.AUTHORITY_UNVERIFIABLE if hold == "repository_connection_unavailable" else BlockCode.HUMAN_INPUT_REQUIRED
                        )
                else:
                    await self.authorize(
                        session,
                        context,
                        node,
                        binding,
                        claim.active_run_id,
                        effect.action,
                        recovery_decision_id=recovery_id if claim.active_run_id == detail["active_run_id"] else None,
                    )
                saved = json.loads(old.reason)
                # Only this trusted producer may replay this action's receipt.
                if old.org_id != node.org_id or old.actor_id != ACTOR or saved.get("run_id") != run_id:
                    raise CycleBlockedError("dispatch_receipt_conflict", BlockCode.AUTHORITY_UNVERIFIABLE)
                if recovery_id:
                    from .developer_review_protocol import verified_decision

                    _, values = await verified_decision(
                        session,
                        decision_id=recovery_id,
                        context=context,
                        node=node,
                        binding=binding,
                        services=self,
                        head_sha=await self.head(binding),
                        successor_action=action,
                    )
                    parent_grant = values[1]
                else:
                    parent = await self.protected(node.org_id, detail["active_run_id"])
                    parent_grant = await asyncio.to_thread(
                        self.writer.store.live_grant,
                        invocation_id=detail["active_run_id"],
                        tenant_id=node.org_id,
                        attempt=int(parent["current_attempt"]["N"]),
                        now=datetime.now(UTC),
                    )
                if reviewer_recovery_id := detail.get("protected_recovery_decision_id"):
                    from .protected_review_recovery import verified_decision

                    recovered = await verified_decision(
                        session,
                        decision_id=reviewer_recovery_id,
                        context=context,
                        node=node,
                        binding=binding,
                        services=self,
                        prior_run_id=detail["active_run_id"],
                        head_sha=await self.head(binding),
                    )
                    if effect.action is not Action.REVIEW:
                        raise CycleBlockedError("reviewer_model_override_unsupported")
                    selected = recovered.get("reviewer_model_configuration")
                    if selected is not None and any(saved["envelope"].get(key) != value for key, value in selected.items()):
                        raise CycleBlockedError("reviewer_model_selection_changed")
                return saved["envelope"], self.child_grant(parent_grant, run_id, binding.repo), saved["execution_metadata"]
            raw, parent, inputs, principal, _ = await self.authorize(
                session, context, node, binding, detail["active_run_id"], effect.action, reserve=True, recovery_decision_id=recovery_id
            )
            allow_story_repairs = effect.action is Action.REPAIR
            allow_review_evidence = effect.action is Action.REVIEW
            if effect.action is Action.REPAIR:
                await self.authorize(session, context, node, binding, detail["active_run_id"], Action.REVIEW)
                allow_review_evidence = True
            if effect.action is Action.REVIEW:
                try:
                    await self.authorize(session, context, node, binding, detail["active_run_id"], Action.REPAIR, recovery_decision_id=recovery_id)
                    allow_story_repairs = True
                except CycleBlockedError:
                    # Explicit review-only policies retain read-only contents
                    # access. The persona itself cannot grant repair authority.
                    pass
            recovered_configuration = None
            if detail.get("protected_recovery_decision_id"):
                from .protected_review_recovery import verified_decision, verify_reviewer_configuration

                recovered = await verified_decision(
                    session,
                    decision_id=detail["protected_recovery_decision_id"],
                    context=context,
                    node=node,
                    binding=binding,
                    services=self,
                    prior_run_id=detail["active_run_id"],
                    head_sha=detail["head_sha"],
                )
                if effect.action is not Action.REVIEW:
                    raise CycleBlockedError("reviewer_model_override_unsupported")
                recovered_configuration = await verify_reviewer_configuration(session, recovered, org_id=node.org_id, user_id=principal)
            bootstrap_retry = detail.get("bootstrap_retry_of") == detail["active_run_id"] and is_retryable_bootstrap_failure(raw)
            review_retry = detail.get("review_retry_of") == detail["active_run_id"] and failed_review(raw)
            retained_recovery = False
            released_review_retry = False
            if recovery_id:
                from .developer_review_protocol import protocol
                from .released_reviewer_recovery import KIND as RELEASED_REVIEWER_KIND
                from .retained_work_recovery import KIND as RETAINED_WORK_KIND

                recovery_kind = (await protocol(session, recovery_id)).KIND
                retained_recovery = recovery_kind == RETAINED_WORK_KIND
                released_review_retry = (
                    recovery_kind == RELEASED_REVIEWER_KIND
                    and detail.get("released_review_retry_of") == raw.get("invocation_id", {}).get("S")
                    and failed_review(raw)
                )
            if (
                not bootstrap_retry
                and not review_retry
                and not retained_recovery
                and not released_review_retry
                and (raw.get("status") != {"S": "completed"} or raw.get("terminal_outcome") != {"S": "complete"})
            ):
                raise CycleBlockedError("previous_worker_not_completed")
            if await self.head(binding) != detail["head_sha"]:
                raise CycleBlockedError("head_changed_before_dispatch")
            delegation_parent = parent
            depth = int(raw["chain_depth"]["N"]) + 1
            if bootstrap_retry or review_retry or released_review_retry:
                # Retrying an engine-owned stage is a sibling execution, not a
                # new delegation by the failed worker. Keep its attenuated grant
                # below, but attach it to the same verified delegation parent.
                delegation_parent, depth = await self.retry_parent(node.org_id, raw, parent)
            if depth > parent.max_chain_depth:
                raise CycleBlockedError("chain_depth_exceeded", BlockCode.ATTEMPTS_EXHAUSTED)
            genesis = await resolve_engine_genesis(session, org_id=node.org_id, decision_id=parent.authority.reference_id)
            flow = await session.get(OrchestrationFlow, node.flow_id)
            cognito_sub = await resolve_user_entity_id(session, node.org_id, principal)
            persona = "agent-codex-reviewer"
            envelope = _build_envelope(
                node=node,
                genesis=genesis,
                graph_address=graph_address(node, flow_slug=flow.slug),
                installation_id=binding.installation_id,
                issue=int(str(node.issue_ref).lstrip("#")),
                config=replace(cfg, repo=binding.repo, persona=persona),
                user_id=principal,
                cognito_sub=cognito_sub,
            )
            try:
                envelope.update(
                    recovered_configuration
                    if recovered_configuration is not None
                    else await resolve_launch_configuration(session, org_id=node.org_id, user_id=principal, persona=persona)
                )
            except Exception:
                raise CycleBlockedError("persona_model_selection_unavailable", BlockCode.AUTHORITY_UNVERIFIABLE) from None
            # Start the worker clock when its assignment is prepared, after any
            # admission wait. The saved receipt preserves this timestamp on replay.
            envelope.update(message_id=run_id, arrived_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), work_claim_required=True)
            envelope["source_ref"]["provider_repository_id"] = binding.provider_repository_id
            envelope["intent"]["trigger"] = "engine_review_cycle"
            envelope["correlation"].update(
                correlation_id=raw["flow_id"]["S"] if retained_recovery or released_review_retry else attempt_run_id(node.id, node.attempts),
                chain_depth=depth,
                parent_principal=delegation_parent.principal,
            )
            envelope["review_cycle_input"] = {
                key: detail[key] for key in ("action", "repo", "pr_number", "head_sha", "accepted_scope", "remaining_attempts", "remaining_spend_usd")
            }
            envelope["review_cycle_input"].update(
                allow_story_repairs=allow_story_repairs,
                reviewer_owned_delivery=allow_review_evidence,
                findings=detail.get("findings", []),
                review_artifact=detail.get("review_artifact"),
                operation_key=action.operation_key,
            )
            if allow_review_evidence:
                envelope["review_expect"] = {
                    "org_id": node.org_id,
                    "flow_id": node.flow_id,
                    "node_id": node.id,
                    "cycle": context.identity.cycle,
                    "accepted_plan_version": context.identity.accepted_plan_version,
                    "claim_id": context.identity.claim_id,
                    "claim_generation": context.identity.claim_generation,
                    "author_run_id": detail["author_run_id"],
                    "execution_id": context.execution.id,
                    "expected_head_sha": detail["head_sha"],
                    "allow_story_repairs": allow_story_repairs,
                    "repo": binding.repo,
                    "pr_number": binding.pr_number,
                    "provider_repository_id": binding.provider_repository_id,
                    "provider_pr_node_id": binding.provider_pr_node_id,
                }
            metadata = {
                "issue_number": {"N": str(envelope["source_ref"]["issue"])},
                "installation_id": {"N": str(binding.installation_id)},
                "provider_repository_id": {"N": str(binding.provider_repository_id)},
                "chain_depth": {"N": str(depth)},
                "orchestration_node_id": {"S": node.id},
                "orchestration_node_attempt": {"N": str(node.attempts)},
                "orchestration_continuation_receipt": {"S": receipt_id(action.operation_key)},
                "orchestration_continuation_action": {"S": effect.action.value},
                "parent_grant_id": {"S": delegation_parent.grant_id},
                "parent_grant_epoch": {"N": str(delegation_parent.revocation_epoch)},
            }
            if effect.action is Action.REVIEW and allow_story_repairs:
                # The credential broker reads trusted execution metadata, never
                # the worker's envelope, when granting branch-write access.
                metadata["orchestration_review_repairs"] = {"BOOL": True}
            saved = {
                "operation_key": action.operation_key,
                "run_id": run_id,
                "execution_id": context.execution.id,
                "accepted_plan_version": context.identity.accepted_plan_version,
                "claim_generation": context.identity.claim_generation,
                "action": effect.action.value,
                "parent_principal": delegation_parent.principal,
                "authority_reference_id": parent.authority.reference_id,
                "envelope": envelope,
                "execution_metadata": metadata,
                **({"developer_recovery_decision_id": recovery_id} if recovery_id else {}),
            }
            if len(json.dumps(saved).encode()) > 65536:
                raise CycleBlockedError("continuation_input_too_large")
            # All provider, grant and budget I/O above ran without ledger locks.
            # Revalidate the captured identity under the store's lock order before
            # committing a receipt and moving the same-generation active run.
            locked_node = await session.scalar(
                select(OrchestrationNode)
                .where(OrchestrationNode.org_id == node.org_id, OrchestrationNode.id == node.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            fresh = await load_execution(session, identity=context.identity, for_update=True)
            await session.refresh(binding)
            if (
                locked_node.state != "running"
                or locked_node.attempts != context.identity.cycle
                or fresh is None
                or fresh.kind is not OutcomeKind.APPLIED
                or fresh.record is None
                or fresh.record.status.value != "awaiting_external"
                or fresh.record.pending_action_key != action.operation_key
                or binding.state != "active"
                or binding.revision != detail["binding_revision"]
                or binding.accepted_scope != detail["accepted_scope"]
                or not binding_scope_matches(binding, locked_node)
            ):
                raise CycleBlockedError("dispatch_authority_changed_during_preparation", BlockCode.AUTHORITY_UNVERIFIABLE)
            raced = await session.get(OrchestrationDecision, receipt_id(action.operation_key))
            if raced is not None:
                if raced.org_id != node.org_id or raced.actor_id != ACTOR or json.loads(raced.reason) != saved:
                    raise CycleBlockedError("dispatch_receipt_conflict", BlockCode.AUTHORITY_UNVERIFIABLE)
                return envelope, self.child_grant(parent, run_id, binding.repo), metadata
            session.add(
                OrchestrationDecision(
                    id=receipt_id(action.operation_key),
                    org_id=node.org_id,
                    flow_id=node.flow_id,
                    node_id=node.id,
                    kind="agent_dispatched",
                    actor_id=ACTOR,
                    actor_role="engine",
                    actor_kind="service",
                    reason=json.dumps(saved),
                )
            )
            from .work_claims import continue_run

            await continue_run(
                session,
                identity=context.identity,
                expected_run_id=detail["active_run_id"],
                run_id=run_id,
                operation_key=action.operation_key,
                completed_execution=raw,
                developer_recovery_decision_id=recovery_id,
                recovery_services=self,
            )
            await session.commit()
            return envelope, self.child_grant(parent, run_id, binding.repo), metadata

    async def retry_parent(self, tenant_id, failed, failed_grant):
        """Reuse verified lineage without resetting depth or widening authority."""
        try:
            parent_id, attempt = failed["parent_principal"]["S"].rsplit("#", 1)
            raw_parent = await self.protected(tenant_id, parent_id)
            parent = await asyncio.to_thread(
                self.writer.store.live_grant, invocation_id=parent_id, tenant_id=tenant_id, attempt=int(attempt), now=datetime.now(UTC)
            )
            depth = int(raw_parent["chain_depth"]["N"]) + 1
            if (
                raw_parent.get("status", {}).get("S") in {"cancelled", "revoked"}
                or parent.grant_id != failed["parent_grant_id"]["S"]
                or parent.revocation_epoch != int(failed["parent_grant_epoch"]["N"])
                or parent.authority != failed_grant.authority
                or not failed_grant.allowed_actions <= parent.delegable_actions
                or depth != int(failed["chain_depth"]["N"])
                or not 0 < depth <= min(parent.max_chain_depth, failed_grant.max_chain_depth)
            ):
                raise ValueError("retry lineage changed")
            return parent, depth
        except (BootstrapRefusedError, KeyError, TypeError, ValueError):
            raise CycleBlockedError("retry_parent_unverifiable", BlockCode.AUTHORITY_UNVERIFIABLE) from None

    @staticmethod
    def child_grant(parent, run_id, repo):
        return DelegatedGrant(
            grant_id=f"grant:{run_id}:1",
            tenant_id=parent.tenant_id,
            principal=f"{run_id}#1",
            authority=parent.authority,
            allowed_actions=frozenset({AgentAction.MONITOR}),
            delegable_actions=frozenset({AgentAction.MONITOR}),
            target_relationships=frozenset({TargetRelationship.SELF}),
            flow_id=parent.flow_id,
            repo_scope=frozenset({repo}),
            expires_at=parent.expires_at,
            max_dispatch_concurrency=parent.max_dispatch_concurrency,
            max_chain_depth=parent.max_chain_depth,
        )

    def provision(self, envelope, grant, metadata):
        serializer = TypeSerializer()
        event = EngineRunStore.build_item(envelope)
        event.update(actor_kind="service", actor_user_id=ACTOR, chain_depth=int(metadata["chain_depth"]["N"]))
        self.writer.store.provision_pending(
            envelope=envelope,
            grant=grant,
            now=datetime.now(UTC),
            execution_metadata=metadata,
            events_table=self.writer.events_table,
            event_item={key: serializer.serialize(value) for key, value in event.items()},
        )


async def cycle_services(factory, context):
    """Route by the accepted flow contract, never by a global mode fallback.

    Both transports may coexist during a rollout. Disabling a transport blocks
    its existing flows explicitly; it never reinterprets their authority.
    Every service still revalidates policy, ownership and receipts at use.
    """
    from .models import OrchestrationAcceptedPlan

    async with factory() as session:
        plan = await session.scalar(
            select(OrchestrationAcceptedPlan).where(
                OrchestrationAcceptedPlan.org_id == context.identity.org_id,
                OrchestrationAcceptedPlan.flow_id == context.execution.flow_id,
                OrchestrationAcceptedPlan.superseded_at.is_(None),
            )
        )
        from .plan_lineage import ancestor_plan

        # Amendments that preserved this node keep its cycle alive (#7535).
        if plan is None or await ancestor_plan(session, plan, context.identity.accepted_plan_version, node_id=context.identity.node_id) is None:
            raise CycleBlockedError("continuation_plan_changed", BlockCode.AUTHORITY_UNVERIFIABLE)
        marker = (plan.plan_document or {}).get("execution_continuation")
        if marker is not None:
            if not isinstance(marker, dict) or marker.get("mode") != "shared_worker_role" or marker.get("contract_version") != 1:
                raise CycleBlockedError("continuation_mode_unrecognized", BlockCode.AUTHORITY_UNVERIFIABLE)
            if os.environ.get("ADP_SHARED_WORKER_CONTINUATION_ENABLED", "false").strip().lower() != "true":
                raise CycleBlockedError("shared_worker_continuation_disabled", BlockCode.HUMAN_INPUT_REQUIRED)
            from .shared_cycle import SharedCycleServices, shared_marker

            await shared_marker(session, org_id=context.identity.org_id, flow_id=context.execution.flow_id)
            return SharedCycleServices(factory)
        if os.environ.get("AGENT_AUTHORITY_ENABLED", "false").strip().lower() != "true":
            raise CycleBlockedError("protected_authority_required", BlockCode.HUMAN_INPUT_REQUIRED)
        return ReviewCycleServices(factory)
