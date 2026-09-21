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
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .review_cycle import DISPATCH_KIND, CycleBlockedError, CycleObservation
from .run_store import EngineRunStore

ACTOR = "system:review-cycle"


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
                return continuation_run_id(row.operation_key)
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

    async def authority_context(self, session, context, node, binding, run_id, action, *, delivery=False):
        if os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() != "true":
            raise CycleBlockedError("protected_authority_required", BlockCode.AUTHORITY_UNVERIFIABLE)
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
        inputs = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
        if inputs.refusal or inputs.policy is None or inputs.plan_version != context.identity.accepted_plan_version:
            raise CycleBlockedError("policy_changed", BlockCode.AUTHORITY_UNVERIFIABLE)
        from .flow_meter import read_flow_meter
        from .runtime_policy import flow_started_at, policy_github_permissions

        meter = await read_flow_meter(org_id=node.org_id, flow_id=node.flow_id, policy=inputs.policy)
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
        # The initial development attempt and every durable continuation consume
        # one shared allowance. A fresh invocation never restarts this counter.
        used = node.attempts + context.execution.attempts - 1
        auth = replace(auth, observed_attempts=max(0, used), observed_concurrency=max(0, auth.observed_concurrency - 1))
        started = await flow_started_at(session, org_id=node.org_id, flow_id=node.flow_id)
        if started is None or (datetime.now(UTC) - started).total_seconds() >= inputs.policy.limits.max_wall_clock_seconds:
            raise CycleBlockedError("wall_clock_limit_exceeded", BlockCode.ATTEMPTS_EXHAUSTED)
        return raw, grant, inputs, principal, meter, auth

    async def authorize(self, session, context, node, binding, run_id, action, *, reserve=False):
        raw, grant, inputs, principal, meter, auth = await self.authority_context(session, context, node, binding, run_id, action)
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
                raise CycleBlockedError(admission.reason.value, BlockCode.BUDGET_EXHAUSTED)
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
            return False
        if raw.get("tenant_id") != {"S": node.org_id} or raw.get("orchestration_node_id") != {"S": node.id}:
            raise CycleBlockedError("protected_development_mismatch", BlockCode.AUTHORITY_UNVERIFIABLE)
        status = raw.get("status", {}).get("S")
        if status in {"cancelled", "revoked"} or (status == "completed" and raw.get("terminal_outcome") != {"S": "complete"}):
            raise CycleBlockedError("worker_failed_or_halted", BlockCode.HUMAN_INPUT_REQUIRED)
        return status == "completed"

    async def facts(self, session, context, node, binding, dispatches):
        active = continuation_run_id(dispatches[-1].operation_key) if dispatches else attempt_run_id(node.id, node.attempts)
        raw, grant, inputs, _, meter = await self.authorize(session, context, node, binding, active, Action.REVIEW)
        status = raw.get("status", {}).get("S")
        if status in {"cancelled", "revoked"} or (status == "completed" and raw.get("terminal_outcome") != {"S": "complete"}):
            raise CycleBlockedError("worker_failed_or_halted", BlockCode.HUMAN_INPUT_REQUIRED)
        return {
            "active_run_id": active,
            "worker_complete": status == "completed",
            "head_sha": await self.head(binding),
            "remaining_spend_usd": str(inputs.policy.limits.max_spend_usd - meter.total_usd)
            if inputs.policy._budget_enforcement_enabled and meter
            else None,
            "remaining_attempts": inputs.policy.limits.max_attempts_per_node - node.attempts - context.execution.attempts,
        }

    async def recheck(self, session, context, node, binding, facts):
        await self.authorize(session, context, node, binding, facts["active_run_id"], Action.REVIEW)
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
            if await self.head(binding) != action.detail["head_sha"]:
                raise CycleBlockedError("head_changed_before_dispatch")
        return CycleObservation(ObservationKind.READY, operation_key=action.operation_key, snapshot={"replay": dict(action.detail)})

    async def dispatch(self, context, effect):
        try:
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
                await self.authorize(session, context, node, binding, envelope["message_id"], effect.action, reserve=True)
                if await self.head(binding) != effect.intent.detail["head_sha"]:
                    raise CycleBlockedError("head_changed_before_dispatch")
            cfg = self.config or DispatchPassConfig.from_env()
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
            # Typed conditions are picked up by observe on the next bounded tick;
            # the intent remains recoverable and no second identity is minted.
            return EffectResult(EffectOutcome.UNCERTAIN, detail=error.reason)

    async def prepare(self, context, effect):
        cfg = self.config or DispatchPassConfig.from_env()
        detail = effect.intent.detail
        if not cfg.configured or cfg.repo != detail["repo"]:
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
            ):
                raise CycleBlockedError("bound_pr_or_scope_changed", BlockCode.HUMAN_INPUT_REQUIRED)
            old = await session.get(OrchestrationDecision, receipt_id(action.operation_key))
            if old is not None:
                saved = json.loads(old.reason)
                # Only this trusted producer may replay this action's receipt.
                if old.org_id != node.org_id or old.actor_id != ACTOR or saved.get("run_id") != run_id:
                    raise CycleBlockedError("dispatch_receipt_conflict", BlockCode.AUTHORITY_UNVERIFIABLE)
                parent = await self.protected(node.org_id, detail["active_run_id"])
                parent_grant = await asyncio.to_thread(
                    self.writer.store.live_grant,
                    invocation_id=detail["active_run_id"],
                    tenant_id=node.org_id,
                    attempt=int(parent["current_attempt"]["N"]),
                    now=datetime.now(UTC),
                )
                return saved["envelope"], self.child_grant(parent_grant, run_id, binding.repo), saved["execution_metadata"]
            raw, parent, inputs, principal, _ = await self.authorize(
                session, context, node, binding, detail["active_run_id"], effect.action, reserve=True
            )
            if raw.get("status") != {"S": "completed"} or raw.get("terminal_outcome") != {"S": "complete"}:
                raise CycleBlockedError("previous_worker_not_completed")
            if await self.head(binding) != detail["head_sha"]:
                raise CycleBlockedError("head_changed_before_dispatch")
            depth = int(raw["chain_depth"]["N"]) + 1
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
                config=replace(cfg, persona=persona),
                user_id=principal,
                cognito_sub=cognito_sub,
            )
            envelope.update(message_id=run_id, arrived_at=detail["arrived_at"], work_claim_required=True)
            envelope["source_ref"]["provider_repository_id"] = binding.provider_repository_id
            envelope["intent"]["trigger"] = "engine_review_cycle"
            envelope["correlation"].update(
                correlation_id=attempt_run_id(node.id, node.attempts), chain_depth=depth, parent_principal=parent.principal
            )
            envelope["review_cycle_input"] = {
                key: detail[key] for key in ("action", "repo", "pr_number", "head_sha", "accepted_scope", "remaining_attempts", "remaining_spend_usd")
            }
            envelope["review_cycle_input"].update(
                allow_story_repairs=effect.action is Action.REPAIR,
                findings=detail.get("findings", []),
                review_artifact=detail.get("review_artifact"),
                operation_key=action.operation_key,
            )
            if effect.action is Action.REVIEW:
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
                "parent_grant_id": {"S": parent.grant_id},
                "parent_grant_epoch": {"N": str(parent.revocation_epoch)},
            }
            saved = {
                "operation_key": action.operation_key,
                "run_id": run_id,
                "execution_id": context.execution.id,
                "accepted_plan_version": context.identity.accepted_plan_version,
                "claim_generation": context.identity.claim_generation,
                "action": effect.action.value,
                "parent_principal": parent.principal,
                "authority_reference_id": parent.authority.reference_id,
                "envelope": envelope,
                "execution_metadata": metadata,
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
            )
            await session.commit()
            return envelope, self.child_grant(parent, run_id, binding.repo), metadata

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


def cycle_services(factory):
    """Select a deployed transport; accepted policy still gates each flow."""
    if (
        os.environ.get("AGENT_AUTHORITY_ENABLED", "false").strip().lower() != "true"
        and os.environ.get("ADP_SHARED_WORKER_CONTINUATION_ENABLED", "false").strip().lower() == "true"
    ):
        from .shared_cycle import SharedCycleServices

        return SharedCycleServices(factory)
    return ReviewCycleServices(factory)
