"""Persist and reconcile one correction below an active failed evaluation."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from .evaluation_correction_state import CORRECTION_KIND, create_correction_child
from .evaluation_issue_provider import EvaluationIssueProvider, issue_content
from .evaluation_plan import require
from .execution_policy import Action, ResourceRef, authorize_action
from .execution_runner import DecisionKind, EffectOutcome, EffectRequest, EffectResult, HandlerDecision, ObservationKind, OperationIdentity
from .execution_state import ActionIntent, BlockCode, ExecutionPhase, Observation, ObservedOutcome, OutcomeKind
from .execution_store import load_execution, prepare_action, record_observation
from .models import OrchestrationAction
from .review_cycle import CycleBlockedError


class EvaluationCorrections:
    def __init__(self, evaluations, *, provider=None):
        self.evaluations = evaluations
        self.factory = evaluations.factory
        self.provider = provider or EvaluationIssueProvider()

    async def snapshot(self, context):
        from .evaluation_controller import EVIDENCE_KIND, bounded_evaluation_summary
        from .runtime_policy import policy_github_permissions

        async with self.factory() as session:
            node, plan, spec, address, request, deployments, anchor, deployment = await self.evaluations.state(session, context)
            require(node.state == "running" and spec.acceptance_mode == "machine", "evaluation_correction_parent_not_active")
            evidence = list(
                (
                    await session.scalars(
                        select(OrchestrationAction)
                        .where(
                            OrchestrationAction.org_id == node.org_id,
                            OrchestrationAction.execution_id == context.execution.id,
                            OrchestrationAction.kind == EVIDENCE_KIND,
                            OrchestrationAction.status == "succeeded",
                        )
                        .limit(2)
                    )
                ).all()
            )
            require(len(evidence) == 1, "evaluation_correction_failed_evidence_missing")
            failed = evidence[0]
            summary = bounded_evaluation_summary(failed.detail)
            require(summary is not None and summary["mandatory_passed"] is False, "evaluation_correction_failure_unverified")
            source, binding, merge, flow, policy, principal, auth = await self.evaluations.runtime.authority(anchor, address)
            require(context.identity.cycle < policy.limits.max_attempts_per_node, "evaluation_correction_limit_exhausted")
            decision = authorize_action(
                replace(auth, observed_attempts=max(auth.observed_attempts, context.identity.cycle)),
                Action.REPAIR,
                ResourceRef(org_id=node.org_id, repository_id=binding.repo, node_address=address),
                context.identity.accepted_plan_version,
            )
            require(decision.permitted, "evaluation_correction_authority_denied:" + (decision.reason.value if decision.reason else "unknown"))
            require(policy_github_permissions(policy, Action.REPAIR) is not None, "evaluation_correction_scoped_writes_unavailable")
            require(auth.observed_spend_usd is not None, "evaluation_correction_spend_unknown")
            _, _, _, run_id, _ = await self.evaluations.runtime.deployments.state(session, anchor)
            authority = self.evaluations.runtime.deployments.authority
            raw = await authority.protected(node.org_id, run_id)
            grant = await asyncio.to_thread(
                authority.writer.store.live_grant,
                invocation_id=run_id,
                tenant_id=node.org_id,
                attempt=int(raw["current_attempt"]["N"]),
                now=datetime.now(UTC),
            )
            depth = int(raw.get("chain_depth", {}).get("N", "0")) + 1
            require(depth <= grant.max_chain_depth, "evaluation_correction_chain_limit")
            key = OperationIdentity.from_context(context, CORRECTION_KIND, failed.operation_key).key
            content = issue_content(
                operation_key=key,
                evaluation_id=node.id,
                cycle=context.identity.cycle,
                failed_criteria=failed.detail["required_failures"],
                actual_revision=deployment.actual_revision,
                evidence_ref=failed.artifact_ref,
                source_issue=int(source.issue_ref.lstrip("#")),
            )
            data = dict(
                operation_key=key,
                parent_node_id=node.id,
                evaluation_cycle=context.identity.cycle,
                failed_evidence_key=failed.operation_key,
                content=content,
                repo=binding.repo,
                provider_repository_id=binding.provider_repository_id,
                installation_id=binding.installation_id,
                policy_hash=policy.policy_hash,
                accepted_plan_version=context.identity.accepted_plan_version,
                anchor_identity=asdict(anchor.identity),
                source_scope=binding.accepted_scope,
                source_revision=deployment.actual_revision,
                parent_run_id=run_id,
                parent_principal=grant.principal,
                parent_grant_id=grant.grant_id,
                parent_grant_epoch=grant.revocation_epoch,
                chain_depth=depth,
                since=summary["completed_at"],
                remaining_corrections=policy.limits.max_attempts_per_node - context.identity.cycle,
            )
            rows = list(
                (
                    await session.scalars(
                        select(OrchestrationAction)
                        .where(
                            OrchestrationAction.org_id == node.org_id,
                            OrchestrationAction.execution_id == context.execution.id,
                            OrchestrationAction.kind == CORRECTION_KIND,
                        )
                        .limit(2)
                    )
                ).all()
            )
            require(len(rows) <= 1, "evaluation_correction_identity_ambiguous")
            action = rows[0] if rows else None
            if action is not None:
                require(
                    action.operation_key == key and all(action.detail.get(k) == v for k, v in data.items()), "evaluation_correction_scope_changed"
                )
                data = action.detail
            return data, binding, action

    async def observe(self, context):
        from .evaluation_controller import EvaluationObservation

        data, binding, action = await self.snapshot(context)
        if action is not None and data.get("child_node_id"):
            return await self.monitor(context, action)
        issue = await self.provider.find(
            binding, data["content"], since=datetime.fromisoformat(data["since"]), issue_number=(data.get("issue") or {}).get("number")
        )
        if issue is not None:
            return EvaluationObservation(ObservationKind.READY, stage="correction_found", snapshot=data, validated=issue)
        if data.get("creation_started"):
            return EvaluationObservation(
                ObservationKind.WAITING,
                stage="correction_wait",
                detail="Correction issue creation is unresolved; retaining its correlation without a second creation attempt.",
            )
        return EvaluationObservation(ObservationKind.READY, stage="correction_create", snapshot=data)

    async def mark_started(self, context, snapshot):
        fresh, _, _ = await self.snapshot(context)
        require(all(fresh.get(key) == value for key, value in snapshot.items()), "evaluation_correction_authority_changed")
        async with self.factory() as session:
            loaded = await load_execution(session, identity=context.identity, for_update=True)
            require(
                loaded is not None and loaded.kind is OutcomeKind.APPLIED and loaded.record.pending_action_key == snapshot["operation_key"],
                "evaluation_correction_intent_changed",
            )
            action = await session.scalar(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.org_id == context.identity.org_id,
                    OrchestrationAction.execution_id == context.execution.id,
                    OrchestrationAction.operation_key == snapshot["operation_key"],
                )
                .with_for_update()
            )
            require(action is not None and not action.detail.get("creation_started"), "evaluation_correction_creation_already_started")
            action.detail = {**action.detail, "creation_started": True}
            await session.commit()

    async def perform(self, context, effect):
        started = False
        try:
            data, binding, action = await self.snapshot(context)
            require(action is not None and action.operation_key == effect.intent.operation_key, "evaluation_correction_intent_missing")
            found = await self.provider.find(binding, data["content"], since=datetime.fromisoformat(data["since"]))
            if found is not None:
                return EffectResult(EffectOutcome.SUCCEEDED, receipt_ref=found.url, detail="Observed the correlated correction issue.")
            if data.get("creation_started"):
                return EffectResult(EffectOutcome.UNCERTAIN, detail="Retaining the existing issue creation correlation.")

            async def reauthorize():
                nonlocal started
                await self.mark_started(context, data)
                started = True

            issue = await self.provider.create(binding, data["content"], reauthorize=reauthorize)
            return EffectResult(EffectOutcome.SUCCEEDED, receipt_ref=issue.url, detail="Correction issue created; delivery remains pending.")
        except CycleBlockedError as error:
            return EffectResult(EffectOutcome.UNCERTAIN if started else EffectOutcome.FAILED, detail=error.reason)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return EffectResult(EffectOutcome.UNCERTAIN if started else EffectOutcome.FAILED, detail="evaluation_correction_provider_unavailable")

    async def accept_issue(self, session, context, observation):
        data, _, _ = await self.snapshot(context)
        require(data["operation_key"] == observation.snapshot["operation_key"], "evaluation_correction_observation_changed")
        action = await session.scalar(
            select(OrchestrationAction).where(
                OrchestrationAction.org_id == context.identity.org_id,
                OrchestrationAction.execution_id == context.execution.id,
                OrchestrationAction.operation_key == data["operation_key"],
            )
        )
        if action is None:
            prepared = await prepare_action(
                session, identity=context.identity, intent=ActionIntent(data["operation_key"], CORRECTION_KIND, detail=data)
            )
            require(prepared.kind is OutcomeKind.APPLIED, "evaluation_correction_intent_conflict")
            action = await session.scalar(
                select(OrchestrationAction).where(
                    OrchestrationAction.org_id == context.identity.org_id,
                    OrchestrationAction.execution_id == context.execution.id,
                    OrchestrationAction.operation_key == data["operation_key"],
                )
            )
        if action.status == "succeeded":
            require(action.receipt_ref == observation.validated.url, "evaluation_correction_issue_receipt_conflict")
        else:
            recorded = await record_observation(
                session,
                identity=context.identity,
                observation=Observation(
                    data["operation_key"],
                    ObservedOutcome.SUCCEEDED,
                    receipt_ref=observation.validated.url,
                    detail="Authenticated correction issue observed.",
                ),
            )
            require(recorded.kind is OutcomeKind.APPLIED, "evaluation_correction_issue_receipt_conflict")
        await create_correction_child(session, context, action, observation.validated)

    async def monitor(self, context, action):
        # The R2/M2/D3 handoff is wired in the next integration step. Keeping the
        # parent blocked here cannot turn a created issue into acceptance.
        raise CycleBlockedError("evaluation_correction_delivery_pending", BlockCode.DEPENDENCY_UNSATISFIED)

    def decide(self, context, observation):
        if observation.stage == "correction_create":
            data = observation.snapshot
            return HandlerDecision(
                DecisionKind.EFFECT,
                effect=EffectRequest(ActionIntent(data["operation_key"], CORRECTION_KIND, detail=data), Action.REPAIR),
                progress_note="Creating or reconciling the linked evaluation correction.",
            )
        if observation.stage == "correction_found":

            async def settle(session, current):
                await self.accept_issue(session, current, observation)

            return HandlerDecision(
                DecisionKind.ADVANCE,
                phase=ExecutionPhase.EVALUATION_PENDING,
                settlement=settle,
                next_check_at=context.now + timedelta(seconds=30),
                progress_note="Correction child is ready for scoped repair.",
            )
        return HandlerDecision(DecisionKind.WAIT, next_check_at=context.now + timedelta(seconds=30), progress_note=observation.detail)
