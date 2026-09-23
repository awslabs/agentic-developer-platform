"""The existing review-cycle handler on authenticated shared-worker receipts.

The shared worker IAM role is explicitly accepted with its configured provider
permissions. SQL assignments authenticate run reports; they do not claim IAM
isolation. K2 still owns scheduling, intents, retries, and phase transitions.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace

from sqlalchemy import select

from .dispatch import graph_address
from .dispatch_pass import DispatchPassConfig, _build_envelope, _get_sqs_client, attempt_run_id
from .execution_policy import Action
from .execution_runner import EffectOutcome, EffectResult, ObservationKind
from .execution_state import BlockCode, OutcomeKind
from .execution_store import load_execution
from .genesis import GenesisRefusedError, resolve_engine_genesis
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .review_cycle import DISPATCH_KIND, CycleBlockedError, CycleObservation
from .review_cycle_dispatch import ACTOR, ReviewCycleServices, continuation_run_id, receipt_id
from .run_store import EngineRunStore


async def shared_marker(session, *, org_id, flow_id):
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
    )
    marker = (plan.plan_document or {}).get("execution_continuation") if plan else None
    if not marker or marker.get("mode") != "shared_worker_role" or marker.get("contract_version") != 1:
        raise CycleBlockedError("shared_worker_continuation_not_accepted", BlockCode.AUTHORITY_UNVERIFIABLE)
    decision = await session.get(OrchestrationDecision, plan.accepted_by_decision_id)
    if decision is None or decision.org_id != org_id or decision.flow_id != flow_id or decision.actor_kind != "human":
        raise CycleBlockedError("continuation_acceptance_unverifiable", BlockCode.AUTHORITY_UNVERIFIABLE)
    return plan, marker


async def validate_current_report_assignment(session, row):
    """Return (ExecutionRecord, ExecutionIdentity) for the current assigned run.

    Shared report authentication identifies a run. This check proves that the
    accepted flow still assigns that run the current durable action and claim.
    Model and review ingestion reuse it rather than trusting body lineage.
    """
    from .execution_state import ExecutionIdentity
    from .run_reports import RunReportError

    metadata = row.dispatch_metadata
    fences = metadata.get("execution_continuation") or metadata.get("handoff_expect") or {}
    try:
        identity = ExecutionIdentity(
            org_id=row.org_id,
            node_id=row.node_id,
            cycle=row.attempt,
            accepted_plan_version=fences["accepted_plan_version"],
            claim_id=fences["claim_id"],
            claim_generation=fences["claim_generation"],
        )
    except (KeyError, ValueError):
        raise RunReportError("execution_assignment_unverifiable") from None
    loaded = await load_execution(session, identity=identity)
    if loaded is None or loaded.kind is not OutcomeKind.APPLIED or (fences.get("execution_id") and loaded.record.id != fences["execution_id"]):
        raise RunReportError("execution_assignment_superseded")
    claim = await session.get(OrchestrationWorkClaim, identity.claim_id)
    if claim.active_run_id != row.run_id:
        raise RunReportError("execution_assignment_superseded")
    plan, _ = await shared_marker(session, org_id=row.org_id, flow_id=row.flow_id)
    if plan.version != identity.accepted_plan_version:
        raise RunReportError("execution_assignment_superseded")
    operation = (metadata.get("review_cycle_input") or {}).get("operation_key")
    if operation:
        receipt = await session.get(OrchestrationDecision, receipt_id(operation))
        saved = json.loads(receipt.reason) if receipt else {}
        action = await session.scalar(
            select(OrchestrationAction).where(
                OrchestrationAction.org_id == row.org_id,
                OrchestrationAction.execution_id == loaded.record.id,
                OrchestrationAction.operation_key == operation,
                OrchestrationAction.kind == DISPATCH_KIND,
            )
        )
        if (
            action is None
            or receipt is None
            or receipt.org_id != row.org_id
            or receipt.flow_id != row.flow_id
            or receipt.node_id != row.node_id
            or receipt.actor_id != ACTOR
            or receipt.actor_kind != "service"
            or receipt.kind != "agent_dispatched"
            or saved.get("authority_mode") != "shared_worker_role"
            or saved.get("run_id") != row.run_id
            or continuation_run_id(operation) != row.run_id
            or saved.get("action") != action.detail.get("action")
            or saved.get("envelope") != {key: value for key, value in metadata.items() if key != "report_nonce"}
        ):
            raise RunReportError("execution_assignment_unverifiable")
    else:
        await _validate_initial_report_genesis(session, row)
    return loaded.record, identity


async def _validate_initial_report_genesis(session, row):
    """Keep the dispatch's human root distinct from its accepted plan fence.

    Initial dispatch may follow a gate approval newer than plan acceptance. Its
    committed dispatch decision records that root; later approvals must neither
    invalidate it nor silently replace its original human attribution.
    """
    from .run_reports import RunReportError

    decision = await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == row.org_id,
            OrchestrationDecision.flow_id == row.flow_id,
            OrchestrationDecision.node_id == row.node_id,
            OrchestrationDecision.kind == "node_dispatched",
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    try:
        saved = json.loads(decision.reason) if decision else {}
        root = row.dispatch_metadata.get("orchestration", {}).get("root_decision_id")
        if (
            row.run_id != attempt_run_id(row.node_id, row.attempt)
            or decision is None
            or decision.actor_kind != "service"
            or decision.actor_id != "system:orchestration-dispatch"
            or saved.get("run_id") != row.run_id
            or saved.get("attempt") != row.attempt
            or saved.get("root_decision_id") != root
        ):
            raise RunReportError("execution_assignment_unverifiable")
        genesis = await resolve_engine_genesis(session, org_id=row.org_id, decision_id=root)
    except (GenesisRefusedError, TypeError, ValueError, AttributeError):
        raise RunReportError("execution_assignment_unverifiable") from None
    if genesis.flow_id != row.flow_id:
        raise RunReportError("execution_assignment_unverifiable")


async def registration_target_for_report(session, row):
    """Resolve a repair binding only from its committed K2 dispatch assignment."""
    from .execution_state import ExecutionIdentity
    from .pr_bindings import RegistrationTarget
    from .run_reports import RunReportError

    metadata = row.dispatch_metadata
    continuation = metadata.get("execution_continuation") or {}
    operation = (metadata.get("review_cycle_input") or {}).get("operation_key")
    receipt = await session.get(OrchestrationDecision, receipt_id(operation)) if operation else None
    saved = json.loads(receipt.reason) if receipt else {}
    if (
        row.persona not in {"developer", "agent-codex-reviewer"}
        or receipt is None
        or receipt.org_id != row.org_id
        or receipt.flow_id != row.flow_id
        or receipt.node_id != row.node_id
        or receipt.actor_id != ACTOR
        or receipt.actor_kind != "service"
        or receipt.kind != "agent_dispatched"
        or saved.get("authority_mode") != "shared_worker_role"
        or saved.get("action") != Action.REPAIR.value
        or saved.get("run_id") != row.run_id
        or continuation_run_id(operation) != row.run_id
        or saved.get("envelope") != {key: value for key, value in metadata.items() if key != "report_nonce"}
    ):
        raise RunReportError("repair_assignment_unverifiable")
    try:
        identity = ExecutionIdentity(
            org_id=row.org_id,
            node_id=row.node_id,
            cycle=row.attempt,
            accepted_plan_version=continuation["accepted_plan_version"],
            claim_id=continuation["claim_id"],
            claim_generation=continuation["claim_generation"],
        )
    except (KeyError, ValueError):
        raise RunReportError("repair_assignment_unverifiable") from None
    loaded = await load_execution(session, identity=identity)
    if loaded is None or loaded.kind is not OutcomeKind.APPLIED or loaded.record.id != continuation.get("execution_id"):
        raise RunReportError("repair_assignment_superseded")
    action = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == row.org_id,
            OrchestrationAction.execution_id == loaded.record.id,
            OrchestrationAction.operation_key == operation,
            OrchestrationAction.kind == DISPATCH_KIND,
        )
    )
    claim = await session.get(OrchestrationWorkClaim, identity.claim_id)
    node = await session.get(OrchestrationNode, row.node_id)
    binding = await active_binding_for_node(session, org_id=row.org_id, node_id=row.node_id, attempt=row.attempt)
    if (
        action is None
        or action.detail.get("action") != Action.REPAIR.value
        or claim.active_run_id != row.run_id
        or binding is None
        or binding.id != action.detail.get("binding_id")
        or binding.repo != row.repo
        or binding.provider_repository_id != row.provider_repository_id
        or binding.installation_id != row.installation_id
        or binding.accepted_scope != action.detail.get("accepted_scope")
        or not binding_scope_matches(binding, node)
    ):
        raise RunReportError("repair_binding_changed")
    return RegistrationTarget(
        org_id=row.org_id,
        flow_id=row.flow_id,
        node_id=row.node_id,
        attempt=row.attempt,
        run_id=row.run_id,
        repo=row.repo,
        issue=int(str(node.issue_ref).lstrip("#")),
        installation_id=row.installation_id,
        accepted_scope=binding.accepted_scope,
    )


class SharedCycleServices(ReviewCycleServices):
    """Compatibility transport; policy/ledger/controller behavior is unchanged."""

    allows_pending_flow = True

    async def recovery_snapshot(self, session, context, node, binding, dispatches):
        from .review_recovery import recovery_snapshot

        return await recovery_snapshot(session, context, node, binding, self, dispatches)

    async def protected(self, org_id, run_id):
        # The historical method name is the controller interface. This projection
        # always names its actual provenance; it is not a Dynamo authority grant.
        from .run_reports import OrchestrationRunReport

        async with self.factory() as session:
            row = await session.scalar(
                select(OrchestrationRunReport).where(
                    OrchestrationRunReport.org_id == org_id,
                    OrchestrationRunReport.run_id == run_id,
                )
            )
            if row is not None:
                terminal = row.terminal_receipt or {}
                return {
                    "tenant_id": {"S": org_id},
                    "invocation_id": {"S": run_id},
                    "orchestration_node_id": {"S": row.node_id},
                    "orchestration_node_attempt": {"N": str(row.attempt)},
                    "current_attempt": {"N": "1"},
                    "repo": {"S": row.repo},
                    "installation_id": {"N": str(row.installation_id)},
                    "provider_repository_id": {"N": str(row.provider_repository_id)},
                    "status": {"S": "completed" if terminal else "running" if getattr(row, "worker_receipt", None) else "pending"},
                    "terminal_outcome": {"S": terminal.get("outcome", "")},
                    "evidence_origin": {"S": "authenticated_run_report"},
                }
            plans = list(
                (
                    await session.scalars(
                        select(OrchestrationAcceptedPlan).where(
                            OrchestrationAcceptedPlan.org_id == org_id,
                            OrchestrationAcceptedPlan.superseded_at.is_(None),
                        )
                    )
                ).all()
            )
            for candidate in plans:
                marker = (candidate.plan_document or {}).get("execution_continuation") or {}
                initial = next(((node_id, run) for node_id, run in marker.get("initial_runs", {}).items() if run.get("run_id") == run_id), None)
                if initial:
                    await shared_marker(session, org_id=org_id, flow_id=candidate.flow_id)
                    node_id, run = initial
                    if run.get("evidence_origin") != "owner_reconciled_legacy_delivery":
                        return None
                    return {
                        "tenant_id": {"S": org_id},
                        "invocation_id": {"S": run_id},
                        "orchestration_node_id": {"S": node_id},
                        "orchestration_node_attempt": {"N": str(run["attempt"])},
                        "current_attempt": {"N": "1"},
                        "repo": {"S": run["repo"]},
                        "installation_id": {"N": str(run["installation_id"])},
                        "provider_repository_id": {"N": str(run["provider_repository_id"])},
                        "status": {"S": "completed"},
                        "terminal_outcome": {"S": "complete"},
                        "evidence_origin": {"S": run["evidence_origin"]},
                    }
        return None

    async def authority_context(self, session, context, node, binding, run_id, action, *, delivery=False):
        if delivery or action not in {Action.DEVELOP, Action.REVIEW, Action.REPAIR, Action.MERGE}:
            raise CycleBlockedError("shared_continuation_code_delivery_only", BlockCode.HUMAN_GATE_REQUIRED)
        await shared_marker(session, org_id=node.org_id, flow_id=node.flow_id)
        claim = await session.scalar(
            select(OrchestrationWorkClaim)
            .where(
                OrchestrationWorkClaim.org_id == node.org_id,
                OrchestrationWorkClaim.id == context.identity.claim_id,
            )
            .execution_options(populate_existing=True)
        )
        if (
            claim is None
            or claim.state != "held"
            or claim.owner_ref != node.flow_id
            or claim.owner_kind != "engine_flow"
            or claim.generation != context.identity.claim_generation
            or claim.active_run_id != run_id
        ):
            raise CycleBlockedError("active_claim_changed", BlockCode.OWNERSHIP_LOST)
        raw = await self.protected(node.org_id, run_id)
        if (
            raw is None
            or raw.get("orchestration_node_id") != {"S": node.id}
            or raw.get("orchestration_node_attempt") != {"N": str(node.attempts)}
            or raw.get("repo") != {"S": binding.repo}
        ):
            raise CycleBlockedError("authenticated_assignment_missing", BlockCode.AUTHORITY_UNVERIFIABLE)
        from .shared_policy import authorize_shared_action

        inputs, principal, meter, auth = await authorize_shared_action(
            session, context, node, binding, run_id, action, reserve=False, observation=True
        )
        return raw, None, inputs, principal, meter, auth

    async def authorize(self, session, context, node, binding, run_id, action, *, reserve=False):
        raw, _, inputs, principal, meter, _ = await self.authority_context(session, context, node, binding, run_id, action)
        if reserve:
            from .shared_policy import authorize_shared_action

            inputs, principal, meter, _ = await authorize_shared_action(session, context, node, binding, run_id, action, reserve=True)
        return raw, None, inputs, principal, meter

    async def has_delivery_handoff(self, session, context, node):
        _, marker = await shared_marker(session, org_id=node.org_id, flow_id=node.flow_id)
        initial = marker.get("initial_runs", {}).get(node.id)
        if initial and initial["attempt"] == node.attempts and initial["run_id"] == attempt_run_id(node.id, node.attempts):
            return True
        from .run_reports import OrchestrationRunReport

        report = await session.get(OrchestrationRunReport, attempt_run_id(node.id, node.attempts))
        return bool(
            report and report.org_id == node.org_id and report.binding_receipt and (report.terminal_receipt or {}).get("outcome") == "complete"
        )

    async def observe_dispatch(self, context, action):
        raw = await self.protected(context.identity.org_id, continuation_run_id(action.operation_key))
        if raw and raw.get("status") in ({"S": "running"}, {"S": "completed"}):
            return CycleObservation(
                ObservationKind.SUCCEEDED, operation_key=action.operation_key, receipt_ref=f"dispatch:{continuation_run_id(action.operation_key)}"
            )
        async with self.factory() as session:
            node = await session.get(OrchestrationNode, context.identity.node_id)
            binding = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
            if binding is None:
                raise CycleBlockedError("bound_delivery_missing")
            source = continuation_run_id(action.operation_key) if raw else action.detail["active_run_id"]
            waiting = await self.dispatch_readiness(session, context, node, binding, source, Action(action.detail["action"]))
            if waiting is not None:
                return waiting
        return CycleObservation(ObservationKind.READY, operation_key=action.operation_key, snapshot={"replay": dict(action.detail)})

    async def dispatch_readiness(self, session, context, node, binding, run_id, action):
        """Capacity waits happen before reserving another effect attempt."""
        from .shared_policy import authorize_shared_action

        try:
            await authorize_shared_action(session, context, node, binding, run_id, action, reserve=False)
        except CycleBlockedError as error:
            if error.reason != "concurrency_limit_exceeded":
                raise
            from .progress_projection import CAPACITY_WAIT_NOTE

            return CycleObservation(ObservationKind.WAITING, detail=CAPACITY_WAIT_NOTE)
        return None

    async def dispatch(self, context, effect):
        try:
            envelope = await self.prepare(context, effect)
            cfg = self.config or DispatchPassConfig.from_env()
            await asyncio.to_thread(EngineRunStore.from_env().register, envelope)
            # The SQL receipt owns an immutable intent. Recheck authority and head
            # on every publication, including crash replay of a pending message.
            async with self.factory() as session:
                node = await session.get(OrchestrationNode, context.identity.node_id)
                binding = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
                await self.authorize(session, context, node, binding, envelope["message_id"], effect.action, reserve=True)
                if await self.head(binding) != effect.intent.detail["head_sha"]:
                    raise CycleBlockedError("head_changed_before_dispatch")
            queue = self.queue or _get_sqs_client(cfg.aws_region)
            await asyncio.to_thread(
                queue.send_message,
                QueueUrl=cfg.queue_url,
                MessageBody=json.dumps(envelope),
                MessageGroupId=f"review-cycle-{context.execution.claim_id}",
                MessageDeduplicationId=envelope["message_id"],
            )
            return EffectResult(EffectOutcome.SUCCEEDED, receipt_ref=f"dispatch:{envelope['message_id']}")
        except CycleBlockedError as error:
            return EffectResult(EffectOutcome.UNCERTAIN, detail=error.reason)

    async def prepare(self, context, effect):
        from src.shared.identity.resolver import resolve_user_entity_id

        from .run_reports import prepare_run_report
        from .work_claims import continue_run

        detail, run_id = effect.intent.detail, continuation_run_id(effect.intent.operation_key)
        cfg = self.config or DispatchPassConfig.from_env()
        if not cfg.configured or cfg.repo != detail["repo"]:
            raise CycleBlockedError("dispatch_configuration_unavailable", BlockCode.CREDENTIAL_UNAVAILABLE)
        async with self.factory() as session:
            # Report mutations also lock the node. Serialize the claim transfer
            # with their final current-assignment check before creating a successor.
            node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.id == context.identity.node_id).with_for_update())
            loaded = await load_execution(session, identity=context.identity)
            if (
                node is None
                or node.state not in {"running", "awaiting_merge"}
                or node.attempts != context.identity.cycle
                or loaded is None
                or loaded.kind is not OutcomeKind.APPLIED
                or loaded.record.pending_action_key != effect.intent.operation_key
            ):
                raise CycleBlockedError("outer_gate_or_execution_changed", BlockCode.HUMAN_INPUT_REQUIRED)
            action = await session.scalar(
                select(OrchestrationAction).where(
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.execution_id == loaded.record.id,
                    OrchestrationAction.operation_key == effect.intent.operation_key,
                    OrchestrationAction.kind == DISPATCH_KIND,
                )
            )
            if action is None or action.detail != detail:
                raise CycleBlockedError("durable_dispatch_intent_changed", BlockCode.AUTHORITY_UNVERIFIABLE)
            binding = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
            if (
                binding is None
                or binding.id != detail["binding_id"]
                or binding.revision != detail["binding_revision"]
                or binding.accepted_scope != detail["accepted_scope"]
                or not binding_scope_matches(binding, node)
            ):
                raise CycleBlockedError("bound_pr_or_scope_changed", BlockCode.HUMAN_INPUT_REQUIRED)
            old = await session.get(OrchestrationDecision, receipt_id(action.operation_key))
            if old is not None:
                saved = json.loads(old.reason)
                if old.org_id != node.org_id or old.actor_id != ACTOR or saved.get("run_id") != run_id:
                    raise CycleBlockedError("dispatch_receipt_conflict", BlockCode.AUTHORITY_UNVERIFIABLE)
                envelope = copy.deepcopy(saved["envelope"])
                await prepare_run_report(session, envelope)
                await session.commit()
                return envelope
            raw, _, inputs, principal, _ = await self.authorize(session, context, node, binding, detail["active_run_id"], effect.action, reserve=True)
            allow_story_repairs = effect.action is Action.REPAIR
            allow_review_evidence = effect.action is Action.REVIEW
            if effect.action is Action.REPAIR:
                try:
                    await self.authorize(session, context, node, binding, detail["active_run_id"], Action.REVIEW, reserve=False)
                    allow_review_evidence = True
                except CycleBlockedError:
                    pass
            if effect.action is Action.REVIEW:
                try:
                    await self.authorize(session, context, node, binding, detail["active_run_id"], Action.REPAIR, reserve=False)
                    allow_story_repairs = True
                except CycleBlockedError:
                    # A review-only policy still gets its review. It never gains
                    # repair authority merely by selecting a different runtime.
                    pass
            recovery_id = detail.get("recovery_decision_id")
            if recovery_id:
                from .review_recovery import verify_recovery_decision

                await verify_recovery_decision(
                    session,
                    decision_id=recovery_id,
                    context=context,
                    node=node,
                    binding=binding,
                    prior_run_id=detail["active_run_id"],
                    head_sha=detail["head_sha"],
                )
            elif raw.get("status") != {"S": "completed"} or raw.get("terminal_outcome") != {"S": "complete"}:
                raise CycleBlockedError("previous_worker_not_completed")
            if await self.head(binding) != detail["head_sha"]:
                raise CycleBlockedError("head_changed_before_dispatch")
            plan, _ = await shared_marker(session, org_id=node.org_id, flow_id=node.flow_id)
            genesis = await resolve_engine_genesis(session, org_id=node.org_id, decision_id=plan.accepted_by_decision_id)
            flow = await session.get(OrchestrationFlow, node.flow_id)
            envelope = _build_envelope(
                node=node,
                genesis=genesis,
                graph_address=graph_address(node, flow_slug=flow.slug),
                installation_id=binding.installation_id,
                issue=int(str(node.issue_ref).lstrip("#")),
                config=replace(cfg, persona="agent-codex-reviewer"),
                user_id=principal,
                cognito_sub=await resolve_user_entity_id(session, node.org_id, principal),
            )
            envelope.update(message_id=run_id, arrived_at=detail["arrived_at"], work_claim_required=True)
            envelope["action"] = effect.action.value
            envelope["pr_binding_required"] = effect.action is Action.REPAIR and not allow_review_evidence
            envelope["bound_pull_request"] = {
                "repo": binding.repo,
                "pr_number": binding.pr_number,
                "url": f"https://github.com/{binding.repo}/pull/{binding.pr_number}",
                "head_sha": detail["head_sha"],
            }
            envelope["source_ref"]["provider_repository_id"] = binding.provider_repository_id
            envelope["intent"]["trigger"] = "engine_review_cycle"
            envelope["correlation"].update(correlation_id=attempt_run_id(node.id, node.attempts), chain_depth=detail["sequence"])
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
            envelope["execution_continuation"] = {
                "execution_id": context.execution.id,
                "accepted_plan_version": context.identity.accepted_plan_version,
                "claim_id": context.identity.claim_id,
                "claim_generation": context.identity.claim_generation,
            }
            if allow_review_evidence:
                envelope["review_expect"] = {
                    **envelope["execution_continuation"],
                    "org_id": node.org_id,
                    "flow_id": node.flow_id,
                    "node_id": node.id,
                    "cycle": node.attempts,
                    "author_run_id": detail["author_run_id"],
                    "expected_head_sha": detail["head_sha"],
                    "allow_story_repairs": allow_story_repairs,
                    "repo": binding.repo,
                    "pr_number": binding.pr_number,
                    "provider_repository_id": binding.provider_repository_id,
                    "provider_pr_node_id": binding.provider_pr_node_id,
                }
            # Recheck under ledger locks after slow provider/membership reads.
            fresh = await load_execution(session, identity=context.identity, for_update=True)
            await session.refresh(node)
            await session.refresh(binding)
            if (
                fresh is None
                or fresh.kind is not OutcomeKind.APPLIED
                or fresh.record.pending_action_key != action.operation_key
                or node.state not in {"running", "awaiting_merge"}
                or binding.revision != detail["binding_revision"]
                or not binding_scope_matches(binding, node)
            ):
                raise CycleBlockedError("dispatch_authority_changed_during_preparation", BlockCode.AUTHORITY_UNVERIFIABLE)
            saved = {
                "operation_key": action.operation_key,
                "run_id": run_id,
                "execution_id": context.execution.id,
                "accepted_plan_version": context.identity.accepted_plan_version,
                "claim_generation": context.identity.claim_generation,
                "action": effect.action.value,
                "envelope": copy.deepcopy(envelope),
                "authority_mode": "shared_worker_role",
            }
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
            await session.flush()
            await continue_run(
                session,
                identity=context.identity,
                expected_run_id=detail["active_run_id"],
                run_id=run_id,
                operation_key=action.operation_key,
                completed_execution=raw,
                recovery_decision_id=recovery_id,
            )
            await prepare_run_report(session, envelope)
            await session.commit()
            return envelope
