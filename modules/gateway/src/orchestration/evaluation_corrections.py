"""Persist and reconcile one correction below an active failed evaluation."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
from sqlalchemy import select

from .evaluation_correction_state import (
    CORRECTION_KIND,
    accepted_source_authorization,
    create_correction_child,
    source_authorization_digest,
)
from .evaluation_issue_provider import EvaluationIssueProvider, issue_content
from .evaluation_plan import require
from .execution_policy import Action, ResourceRef, authorize_action
from .execution_runner import DecisionKind, EffectOutcome, EffectRequest, EffectResult, HandlerDecision, ObservationKind, OperationIdentity
from .execution_state import ActionIntent, ExecutionPhase, Observation, ObservedOutcome, OutcomeKind
from .execution_store import load_execution, prepare_action, record_observation
from .models import OrchestrationAction, OrchestrationFlow
from .review_cycle import CycleBlockedError


class EvaluationCorrections:
    def __init__(self, evaluations, *, provider=None):
        self.evaluations = evaluations
        self.factory = evaluations.factory
        self.provider = provider or EvaluationIssueProvider()

    async def snapshot(self, context):
        from .evaluation_controller import EVIDENCE_KIND, bounded_evaluation_summary
        from .evaluation_plan import accepted_evaluation
        from .runtime_policy import policy_github_permissions

        async with self.factory() as session:
            current_node = await self.evaluations.node(session, context)
            accepted = await accepted_evaluation(session, current_node)
            if accepted is not None and accepted[1].evidence_schema == "multi-repository-evaluation/v1":
                return await self.snapshot_multi(session, context)
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
            affected_source = accepted_source_authorization(sources=[(source, binding)], flow=flow, plan=plan)
            data = dict(
                source_authorization=affected_source,
                source_authorization_digest=source_authorization_digest(affected_source),
                operation_key=key,
                parent_node_id=node.id,
                epic_ref=node.epic_ref,
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

    async def snapshot_multi(self, session, context):
        from .evaluation_contract import verify_multi_repository_receipt
        from .evaluation_plan import identity_for
        from .execution_runner import RunnerContext
        from .models import OrchestrationExecution, OrchestrationNode
        from .pr_bindings import active_binding_for_node
        from .repository_evaluation import multi_source_receipts
        from .repository_producer import producer_state
        from .runtime_policy import policy_github_permissions

        failed = await session.scalar(
            select(OrchestrationAction).where(
                OrchestrationAction.org_id == context.identity.org_id,
                OrchestrationAction.execution_id == context.execution.id,
                OrchestrationAction.kind == "multi_repository_context",
                OrchestrationAction.status == "succeeded",
            )
        )
        require(failed is not None and failed.detail.get("failed_probe"), "evaluation_correction_failed_evidence_missing")
        snapshot = failed.detail["failed_probe"]
        observed_at = datetime.fromisoformat(snapshot["receipt"]["observed_at"])
        node, plan, spec, accepted, admission, sources, _ = await producer_state(session, context, observed_at=observed_at)
        require(spec.mode == "deployed" and spec.acceptance_mode == "machine", "evaluation_correction_parent_not_active")
        resolved = await multi_source_receipts(session, node, plan, spec, sources, snapshot["source_observations"])
        try:
            receipt = verify_multi_repository_receipt(
                spec.model_dump(mode="json"), snapshot["receipt"], resolved, now=datetime.fromisoformat(snapshot["receipt"]["observed_at"])
            )
        except (ValueError, KeyError, TypeError):
            raise CycleBlockedError("evaluation_correction_failure_unverified") from None
        required = {criterion.criterion_id for criterion in spec.criteria if criterion.required}
        affected = {
            criterion.affected_source_address for criterion in receipt.criteria if criterion.criterion_id in required and criterion.outcome == "fail"
        }
        require(receipt.verdict == "fail" and len(affected) == 1 and None not in affected, "evaluation_correction_affected_source_ambiguous")
        address = next(iter(affected))
        declared = next((source for source in sources if source["address"] == address), None)
        require(declared is not None, "evaluation_correction_affected_source_changed")
        source = await session.get(OrchestrationNode, declared["node_id"])
        if admission.get("correction_operation_key"):
            from .evaluation_correction_state import retest_deployment

            previous = await session.scalar(
                select(OrchestrationAction).where(
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.execution_id == admission["correction_parent_execution_id"],
                    OrchestrationAction.operation_key == admission["correction_operation_key"],
                    OrchestrationAction.kind == CORRECTION_KIND,
                    OrchestrationAction.status == "succeeded",
                )
            )
            require(
                previous is not None and previous.detail["source_authorization"]["address"] == address,
                "evaluation_correction_affected_source_changed",
            )
            corrected = await retest_deployment(session, node, failed, plan.version)
            require(source is not None and corrected[1].id == source.id, "evaluation_correction_affected_source_changed")
            source = await session.get(OrchestrationNode, previous.detail["source_authorization"]["node_id"])
        binding = (
            await active_binding_for_node(session, org_id=source.org_id, node_id=source.id, attempt=source.attempts) if source is not None else None
        )
        flow = await session.get(OrchestrationFlow, node.flow_id)
        require(flow is not None and binding is not None, "evaluation_correction_affected_source_changed")
        authorization = accepted_source_authorization(sources=[(source, binding)], flow=flow, plan=plan)
        source_execution = await session.get(OrchestrationExecution, declared["execution_id"])
        require(source_execution is not None, "evaluation_correction_affected_source_changed")
        anchor = RunnerContext(identity_for(source_execution), source_execution, datetime.now(UTC))
        from . import shared_policy
        from .evaluation_authority import evaluation_inputs
        from .execution_policy import CredentialScope
        from .policy_admission import SpendObservation

        inputs, marker = await evaluation_inputs(session, org_id=node.org_id, flow_id=node.flow_id)
        policy = inputs.policy
        require(plan.version == inputs.plan_version and binding.repo in policy.repository_ids, "evaluation_correction_policy_changed")
        require(context.identity.cycle < policy.limits.max_attempts_per_node, "evaluation_correction_limit_exhausted")
        await self.provider.credentials(binding, write=True)
        meter = await shared_policy.read_flow_meter(org_id=node.org_id, flow_id=node.flow_id, policy=policy)
        require(not policy._budget_enforcement_enabled or meter is not None, "evaluation_correction_spend_unknown")
        auth = await shared_policy.resolve_authorization_context(
            session,
            policy=policy,
            plan_version=plan.version,
            node=node,
            principal_user_id=marker.get("principal_user_id") or policy.principal_id,
            credential_scope=CredentialScope.SCOPED,
            spend=SpendObservation(total_usd=meter.total_usd if meter else None),
        )
        auth = replace(auth, work_owned_by_policy_flow=True)
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
        key = OperationIdentity.from_context(context, CORRECTION_KIND, failed.operation_key, address).key
        content = issue_content(
            operation_key=key,
            evaluation_id=node.id,
            cycle=context.identity.cycle,
            failed_criteria=[
                criterion.criterion_id for criterion in receipt.criteria if criterion.criterion_id in required and criterion.outcome == "fail"
            ],
            actual_revision=next(source.deployed_revision for source in receipt.sources if source.address == address),
            evidence_ref=f"github/actions/runs/{snapshot['run']['run_id']}/artifacts/{snapshot['run']['artifact_id']}",
            source_issue=int(source.issue_ref),
        )
        data = dict(
            source_authorization=authorization,
            source_authorization_digest=source_authorization_digest(authorization),
            operation_key=key,
            parent_node_id=node.id,
            epic_ref=node.epic_ref,
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
            source_revision=next(source.deployed_revision for source in receipt.sources if source.address == address),
            parent_run_id=run_id,
            parent_principal=grant.principal,
            parent_grant_id=grant.grant_id,
            parent_grant_epoch=grant.revocation_epoch,
            chain_depth=depth,
            since=receipt.observed_at.isoformat(),
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
                action.operation_key == key and all(action.detail.get(name) == value for name, value in data.items()),
                "evaluation_correction_scope_changed",
            )
            data = action.detail
        return data, binding, action

    async def observe(self, context):
        from .evaluation_controller import EvaluationObservation
        from .tracker_projection import resolve_parent_reference

        data, binding, action = await self.snapshot(context)
        if action is not None and data.get("child_node_id"):
            return await self.monitor(context, action)
        parent_binding = None
        if data["source_authorization"]["product_id"] is not None:
            async with self.factory() as session:
                flow = await session.get(OrchestrationFlow, context.execution.flow_id)
                require(
                    flow is not None and flow.org_id == context.identity.org_id and flow.product_id == data["source_authorization"]["product_id"],
                    "evaluation_correction_flow_changed",
                )
                parent = await resolve_parent_reference(session, org_id=flow.org_id, flow_id=flow.id, epic_ref=data["epic_ref"])
            if parent is None:
                return EvaluationObservation(ObservationKind.WAITING, stage="correction_wait", detail="Evaluation epic parent is not verified.")
            parent_binding = SimpleNamespace(
                org_id=context.identity.org_id,
                repo=parent.full_name,
                provider_repository_id=parent.repository_id,
                installation_id=parent.installation_id,
                issue_number=parent.issue_number,
                issue_id=parent.issue_id,
            )
        issue = await self.provider.find(
            binding, data["content"], since=datetime.fromisoformat(data["since"]), issue_number=(data.get("issue") or {}).get("number")
        )
        if issue is not None:
            if parent_binding is not None:
                try:
                    linked = await self.provider.read_native_parent(binding, parent_binding, issue)
                except CycleBlockedError as error:
                    if error.reason != "evaluation_correction_parent_unavailable":
                        raise
                    return EvaluationObservation(ObservationKind.WAITING, stage="correction_wait", detail=error.reason)
                if not linked:
                    return EvaluationObservation(ObservationKind.READY, stage="correction_create", snapshot=data)
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
            parent = await self.evaluations.node(session, context)
            require(parent.state == "running", "evaluation_correction_parent_not_active")
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
        try:
            data, binding, action = await self.snapshot(context)
            require(action is not None and action.operation_key == effect.intent.operation_key, "evaluation_correction_intent_missing")
            found = await self.provider.find(binding, data["content"], since=datetime.fromisoformat(data["since"]))
            if found is not None:
                await self.link_issue(context, effect, data, binding, found)
                return EffectResult(EffectOutcome.SUCCEEDED, receipt_ref=found.url, detail="Observed the correlated correction issue.")
            if data.get("creation_started"):
                return EffectResult(EffectOutcome.UNCERTAIN, detail="Retaining the existing issue creation correlation.")

            async def reauthorize():
                await self.mark_started(context, data)

            issue = await self.provider.create(binding, data["content"], reauthorize=reauthorize)
            await self.link_issue(context, effect, data, binding, issue)
            return EffectResult(EffectOutcome.SUCCEEDED, receipt_ref=issue.url, detail="Correction issue created; delivery remains pending.")
        except CycleBlockedError as error:
            # One immutable operation spans recoverable admission/transport
            # failures. The next observation rechecks authority; creation_started
            # alone determines whether another POST is permitted.
            return EffectResult(EffectOutcome.UNCERTAIN, detail=error.reason)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return EffectResult(EffectOutcome.UNCERTAIN, detail="evaluation_correction_provider_unavailable")

    async def link_issue(self, context, effect, data, binding, issue):
        if data["source_authorization"]["product_id"] is None:
            return
        from .execution_runner import flow_is_paused, verify_live_authority
        from .tracker_projection import resolve_parent_reference

        async with self.factory() as session:
            parent = await resolve_parent_reference(
                session, org_id=context.identity.org_id, flow_id=context.execution.flow_id, epic_ref=data["epic_ref"]
            )
        require(parent is not None, "evaluation_correction_parent_unverified")
        parent_binding = SimpleNamespace(
            org_id=context.identity.org_id,
            repo=parent.full_name,
            provider_repository_id=parent.repository_id,
            installation_id=parent.installation_id,
            issue_number=parent.issue_number,
            issue_id=parent.issue_id,
        )

        async def reauthorize():
            fresh, _, _ = await self.snapshot(context)
            require(
                all(fresh.get(key) == value for key, value in data.items() if key not in {"creation_started", "issue", "child_node_id"}),
                "evaluation_correction_authority_changed",
            )
            async with self.factory() as session:
                loaded = await load_execution(session, identity=context.identity)
                require(
                    loaded is not None
                    and loaded.kind is OutcomeKind.APPLIED
                    and loaded.record.pending_action_key == effect.intent.operation_key
                    and not await flow_is_paused(session, org_id=context.identity.org_id, flow_id=context.execution.flow_id),
                    "evaluation_correction_link_authority_changed",
                )
                current_parent = await resolve_parent_reference(
                    session, org_id=context.identity.org_id, flow_id=context.execution.flow_id, epic_ref=data["epic_ref"]
                )
                require(current_parent == parent, "evaluation_correction_parent_changed")
            authority_block = await verify_live_authority(self.factory, context.execution, effect, datetime.now(UTC))
            require(authority_block is None, "evaluation_correction_link_authority_changed")

        await self.provider.confirm_native_parent(binding, parent_binding, issue, data["content"], reauthorize=reauthorize)

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
        from .evaluation_controller import EvaluationObservation, specification_hash
        from .evaluation_correction_state import validate_correction
        from .evaluation_plan import accepted_evaluation, current_deployment
        from .models import OrchestrationNode

        async with self.factory() as session:
            node = await self.evaluations.node(session, context)
            accepted = await accepted_evaluation(session, node)
            require(accepted is not None, "evaluation_correction_parent_not_active")
            if accepted[1].evidence_schema == "multi-repository-evaluation/v1":
                data, _, current = await self.snapshot_multi(session, context)
                require(current is not None and current.id == action.id, "evaluation_correction_action_changed")
                plan, spec, address = accepted[0], accepted[1], data["source_authorization"]["address"]
            else:
                node, plan, spec, address, request, deployments, anchor, deployment = await self.evaluations.state(session, context)
            child = await session.scalar(
                select(OrchestrationNode)
                .where(
                    OrchestrationNode.org_id == node.org_id,
                    OrchestrationNode.flow_id == node.flow_id,
                    OrchestrationNode.id == action.detail["child_node_id"],
                )
                .execution_options(populate_existing=True)
            )
            require(child is not None, "evaluation_correction_child_missing")
            await validate_correction(session, child)
            require(child.state not in {"failed", "halted", "rejected_at_gate", "superseded"}, "evaluation_correction_requires_human_recovery")
            if child.state != "passed":
                return EvaluationObservation(
                    ObservationKind.WAITING,
                    stage="correction_wait",
                    detail=f"Correction issue #{child.issue_ref}: {child.state}; review, merge and verified deployment remain required.",
                )
            corrected = await current_deployment(session, child, plan.version, now=datetime.now(UTC))
            if corrected is None:
                return EvaluationObservation(
                    ObservationKind.WAITING, stage="correction_wait", detail="Correction code passed; awaiting its final verified deployment."
                )
            corrected_anchor, _, corrected_deployment = corrected
            require(
                not corrected_deployment.docs_only and corrected_deployment.valid_until > datetime.now(UTC),
                "evaluation_correction_deployment_not_current",
            )
            if spec.evidence_schema == "multi-repository-evaluation/v1":
                from .evaluation_authority import evaluation_inputs

                _, binding, _, _, _ = await self.evaluations.runtime.deployments.state(session, corrected_anchor)
                inputs, _ = await evaluation_inputs(session, org_id=node.org_id, flow_id=node.flow_id)
                require(
                    inputs.plan_version == plan.version and inputs.policy.policy_hash == action.detail["policy_hash"],
                    "evaluation_correction_policy_changed",
                )
            else:
                _, binding, _, _, policy, _, _ = await self.evaluations.runtime.authority(corrected_anchor, address)
                require(policy.policy_hash == action.detail["policy_hash"], "evaluation_correction_policy_changed")
            await self.evaluations.runtime.deployments.provider.contains(
                binding, action.detail["source_revision"], corrected_deployment.actual_revision
            )
            if spec.evidence_schema == "multi-repository-evaluation/v1":
                from .repository_evaluation import multi_repository_sources, sources_for
                from .repository_producer import digest

                require(corrected_deployment.actual_revision != action.detail["source_revision"], "evaluation_correction_revision_unchanged")
                failed = await session.scalar(
                    select(OrchestrationAction).where(
                        OrchestrationAction.org_id == node.org_id,
                        OrchestrationAction.execution_id == context.execution.id,
                        OrchestrationAction.kind == "multi_repository_context",
                        OrchestrationAction.status == "succeeded",
                    )
                )
                previous = {source["address"]: source["deployed_revision"] for source in failed.detail["failed_probe"]["receipt"]["sources"]}
                correction = (address, child, action.operation_key)
                try:
                    fresh = await sources_for(session, node, plan, spec, correction=correction)
                except CycleBlockedError as error:
                    if error.reason != "repository_evaluation_predecessor_deployment_expired":
                        raise
                    return await self.refresh_source(session, context, node, plan, spec, action, failed, previous)
                require(
                    len(fresh) == len(previous) == len(spec.predecessors)
                    and {source["address"] for source in fresh} == set(previous)
                    and all(source["head_sha"] == previous[source["address"]] for source in fresh if source["address"] != address)
                    and next(source for source in fresh if source["address"] == address)["head_sha"] == corrected_deployment.actual_revision,
                    "evaluation_retest_sources_changed",
                )
                keys = sorted(source["deployment_receipt_key"] for source in fresh)
                effective = spec.model_copy(update={"environment": spec.environment.model_copy(update={"deployment_receipt_keys": keys})})
                verified, _, _ = await multi_repository_sources(session, node, plan, effective, correction=correction)
                require(verified == fresh, "evaluation_retest_sources_changed")
                return EvaluationObservation(
                    ObservationKind.READY,
                    stage="correction_retest",
                    snapshot=dict(
                        operation_key=action.operation_key,
                        child_node_id=child.id,
                        deployment_key=corrected_deployment.operation_key,
                        anchor_identity=asdict(corrected_anchor.identity),
                        specification_hash=specification_hash(spec),
                        affected_source_address=address,
                        receipt_keys=keys,
                        unchanged_revisions={key: value for key, value in previous.items() if key != address},
                        source_snapshot_hash=digest(fresh),
                    ),
                )
            return EvaluationObservation(
                ObservationKind.READY,
                stage="correction_retest",
                snapshot=dict(
                    operation_key=action.operation_key,
                    child_node_id=child.id,
                    deployment_key=corrected_deployment.operation_key,
                    anchor_identity=asdict(corrected_anchor.identity),
                    specification_hash=specification_hash(spec),
                ),
            )

    async def refresh_source(self, session, context, node, plan, spec, action, failed, previous):
        from .deployment_controller import RECEIPT_LIFETIME
        from .deployment_runtime_contract import DeploymentReceipt
        from .dispatch import graph_address
        from .evaluation_controller import EvaluationObservation
        from .evaluation_plan import current_deployment
        from .models import OrchestrationFlow, OrchestrationNode
        from .repository_evaluation import correction_source_refresh, verify_original_after_refresh
        from .repository_producer import digest

        flow = await session.get(OrchestrationFlow, node.flow_id)
        require(flow is not None and flow.product_id == plan.plan_document.get("product_id"), "evaluation_source_refresh_scope_changed")
        evaluation_address = graph_address(node, flow_slug=flow.slug)
        for expected in spec.predecessors:
            address = expected.address
            if address == action.detail["source_authorization"]["address"]:
                continue
            original_source = next(item for item in failed.detail["failed_probe"]["receipt"]["sources"] if item["address"] == address)
            parent = await session.get(OrchestrationNode, original_source["node_id"])
            require(parent is not None and parent.flow_id == node.flow_id, "evaluation_source_refresh_scope_changed")
            current = await current_deployment(session, parent, plan.version, now=datetime.now(UTC))
            require(current is not None, "evaluation_source_refresh_original_missing")
            anchor, _, original = current
            require(parent.provider_repository_id == expected.repository_id, "evaluation_source_refresh_original_changed")
            await verify_original_after_refresh(
                session,
                node,
                plan,
                parent,
                address,
                original_source,
                datetime.fromisoformat(failed.detail["failed_probe"]["receipt"]["observed_at"]),
                original,
            )
            require(original.actual_revision == previous[address], "evaluation_source_refresh_original_changed")
            saved = await correction_source_refresh(session, node, plan, parent, address, action.operation_key)
            if saved is not None and saved[0].valid_until > datetime.now(UTC):
                continue
            components = await self.evaluations.runtime.verify(anchor, original, evaluation_address, include_components=True)
            observed_at = min(component.observed_at for component in components)
            _, _, _, _, policy, _, _ = await self.evaluations.runtime.authority(anchor, evaluation_address)
            require(policy.policy_hash == action.detail["policy_hash"], "evaluation_source_refresh_policy_changed")
            key = OperationIdentity.from_context(
                context,
                "multi_repository_source_refresh",
                action.operation_key,
                parent.id,
                original.operation_key,
                saved[0].operation_key if saved is not None else original.operation_key,
            ).key
            receipt = DeploymentReceipt.model_validate(
                original.model_copy(
                    update=dict(
                        operation_key=key,
                        components=components,
                        observed_at=observed_at,
                        valid_until=min(policy.expires_at, observed_at + RECEIPT_LIFETIME),
                    )
                ).model_dump(mode="json")
            )
            require(receipt.valid_until > datetime.now(UTC), "evaluation_source_refresh_expired")
            return EvaluationObservation(
                ObservationKind.READY,
                stage="correction_refresh",
                snapshot=dict(
                    source_node_id=parent.id,
                    source_address=address,
                    correction_operation_key=action.operation_key,
                    original_operation_key=original.operation_key,
                    original_digest=digest(original.model_dump(mode="json")),
                    flow_id=node.flow_id,
                    product_id=plan.plan_document.get("product_id"),
                    accepted_plan_hash=plan.plan_hash,
                    deployment_receipt=receipt.model_dump(mode="json"),
                ),
            )
        raise CycleBlockedError("repository_evaluation_predecessor_deployment_expired")

    async def accept_refresh(self, session, context, observation):
        from .deployment_runtime_contract import DeploymentReceipt
        from .dispatch import graph_address
        from .evaluation_plan import accepted_evaluation, current_deployment
        from .models import OrchestrationFlow, OrchestrationNode
        from .repository_evaluation import verify_refreshed_deployment
        from .repository_producer import digest

        snapshot = observation.snapshot
        node = await self.evaluations.node(session, context)
        accepted = await accepted_evaluation(session, node)
        require(accepted is not None and node.state == "running", "evaluation_source_refresh_parent_changed")
        plan, spec, _ = accepted
        data, _, action = await self.snapshot_multi(session, context)
        require(
            action is not None
            and action.status == "succeeded"
            and action.operation_key == snapshot["correction_operation_key"]
            and snapshot["source_address"] != data["source_authorization"]["address"]
            and snapshot["product_id"] == plan.plan_document.get("product_id")
            and snapshot["flow_id"] == node.flow_id
            and snapshot["accepted_plan_hash"] == plan.plan_hash,
            "evaluation_source_refresh_scope_changed",
        )
        parent = await session.get(OrchestrationNode, snapshot["source_node_id"])
        require(parent is not None and parent.flow_id == node.flow_id, "evaluation_source_refresh_scope_changed")
        source = next((item for item in spec.predecessors if item.address == snapshot["source_address"]), None)
        require(source is not None and parent.provider_repository_id == source.repository_id, "evaluation_source_refresh_scope_changed")
        current = await current_deployment(session, parent, plan.version, now=datetime.now(UTC))
        require(current is not None, "evaluation_source_refresh_original_missing")
        anchor, _, original = current
        require(
            original.operation_key == snapshot["original_operation_key"] and digest(original.model_dump(mode="json")) == snapshot["original_digest"],
            "evaluation_source_refresh_original_changed",
        )
        proof = DeploymentReceipt.model_validate(snapshot["deployment_receipt"])
        verify_refreshed_deployment(original, proof, snapshot)
        now = datetime.now(UTC)
        require(
            proof.observed_at <= now + timedelta(seconds=30) and now - proof.observed_at <= timedelta(seconds=30) and proof.valid_until > now,
            "evaluation_source_refresh_expired",
        )
        flow = await session.get(OrchestrationFlow, node.flow_id)
        require(flow is not None and flow.product_id == snapshot["product_id"], "evaluation_source_refresh_scope_changed")
        _, _, _, _, policy, _, _ = await self.evaluations.runtime.authority(anchor, graph_address(node, flow_slug=flow.slug))
        require(
            policy.policy_hash == action.detail["policy_hash"] and proof.valid_until <= policy.expires_at,
            "evaluation_source_refresh_policy_changed",
        )
        prepared = await prepare_action(
            session,
            identity=context.identity,
            intent=ActionIntent(proof.operation_key, "multi_repository_source_refresh", detail=snapshot),
        )
        require(prepared.kind is OutcomeKind.APPLIED, "evaluation_source_refresh_intent_changed")
        row = await session.scalar(
            select(OrchestrationAction).where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationAction.execution_id == context.execution.id,
                OrchestrationAction.operation_key == proof.operation_key,
            )
        )
        require(row is not None and row.detail == snapshot, "evaluation_source_refresh_intent_changed")
        if row.status != "succeeded":
            recorded = await record_observation(
                session,
                identity=context.identity,
                observation=Observation(
                    proof.operation_key,
                    ObservedOutcome.SUCCEEDED,
                    receipt_ref=proof.operation_key,
                    detail="Current unchanged-source runtime re-observed under evaluation authority.",
                ),
            )
            require(recorded.kind is OutcomeKind.APPLIED, "evaluation_source_refresh_intent_changed")

    async def retest(self, session, context, observation):
        from .evaluation_controller import CONTEXT_KIND, specification_hash
        from .evaluation_correction_state import ACTOR, validate_correction
        from .evaluation_plan import accepted_evaluation, current_deployment
        from .execution_runner import RunnerContext
        from .execution_state import ExecutionIdentity, ExecutionStatus, PhaseAdvance
        from .execution_store import advance_execution, create_execution
        from .models import OrchestrationDecision, OrchestrationNode

        accepted = await accepted_evaluation(session, await self.evaluations.node(session, context))
        if accepted is not None and accepted[1].evidence_schema == "multi-repository-evaluation/v1":
            await self.retest_multi(session, context, observation)
            return
        node, plan, spec, address, request, deployments, _, _ = await self.evaluations.state(session, context)
        require(node.state == "running" and node.attempts == context.identity.cycle, "evaluation_retest_parent_changed")
        child = await session.scalar(
            select(OrchestrationNode)
            .where(
                OrchestrationNode.org_id == node.org_id,
                OrchestrationNode.flow_id == node.flow_id,
                OrchestrationNode.id == observation.snapshot["child_node_id"],
            )
            .execution_options(populate_existing=True)
        )
        require(child is not None and child.state == "passed", "evaluation_retest_child_changed")
        action = await validate_correction(session, child)
        require(action is not None and action.operation_key == observation.snapshot["operation_key"], "evaluation_retest_action_changed")
        corrected = await current_deployment(session, child, plan.version, now=datetime.now(UTC))
        require(corrected is not None, "evaluation_retest_deployment_missing")
        anchor, _, deployment = corrected
        require(
            deployment.operation_key == observation.snapshot["deployment_key"]
            and deployment.valid_until > datetime.now(UTC)
            and asdict(anchor.identity) == observation.snapshot["anchor_identity"]
            and specification_hash(spec) == observation.snapshot["specification_hash"],
            "evaluation_retest_scope_changed",
        )
        _, _, _, _, policy, _, _ = await self.evaluations.runtime.authority(anchor, address)
        require(
            policy.policy_hash == action.detail["policy_hash"] and context.identity.cycle < policy.limits.max_attempts_per_node,
            "evaluation_retest_policy_changed",
        )
        node.attempts += 1
        identity = ExecutionIdentity(node.org_id, node.id, node.attempts, plan.version, anchor.identity.claim_id, anchor.identity.claim_generation)
        created = await create_execution(
            session,
            identity=identity,
            flow_id=node.flow_id,
            phase=ExecutionPhase.EVALUATION_PENDING,
            next_check_at=datetime.now(UTC),
            deadline_at=context.execution.deadline_at,
        )
        require(created.kind is OutcomeKind.APPLIED, "evaluation_retest_execution_conflict")
        current = RunnerContext(identity, created.record, datetime.now(UTC))
        key = OperationIdentity.from_context(current, CONTEXT_KIND, spec.runner.harness_revision, specification_hash(spec)).key
        detail = dict(
            node_id=node.id,
            specification_hash=specification_hash(spec),
            deployments=request.detail["deployments"],
            anchor_identity=asdict(anchor.identity),
            correction_operation_key=action.operation_key,
            correction_parent_execution_id=context.execution.id,
            correction_deployment_key=deployment.operation_key,
            evaluation_cycle=action.detail["evaluation_cycle"],
            remaining_corrections=action.detail["remaining_corrections"],
            issue=action.detail["issue"],
            child_node_id=child.id,
            retest_cycle=node.attempts,
        )
        prepared = await prepare_action(session, identity=identity, intent=ActionIntent(key, CONTEXT_KIND, detail=detail))
        require(prepared.kind is OutcomeKind.APPLIED, "evaluation_retest_context_conflict")
        recorded = await record_observation(
            session,
            identity=identity,
            observation=Observation(
                key,
                ObservedOutcome.SUCCEEDED,
                receipt_ref="evaluation/context/" + created.record.id,
                detail="Fresh evaluation requested for the reviewed and verified correction.",
            ),
        )
        require(recorded.kind is OutcomeKind.APPLIED, "evaluation_retest_context_conflict")
        settled = await advance_execution(
            session,
            identity=context.identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.CONCLUDED,
                status=ExecutionStatus.CONCLUDED,
                expected_revision=context.execution.revision,
                progress_note="Failed evidence retained; corrected deployment entered a new evaluation cycle.",
            ),
        )
        require(settled.kind is OutcomeKind.APPLIED, "evaluation_retest_prior_cycle_conflict")
        action.detail = {**action.detail, "retest_cycle": node.attempts}
        import json

        session.add(
            OrchestrationDecision(
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                kind="result_observed",
                actor_id=ACTOR,
                actor_kind="service",
                actor_role="engine",
                from_state="running",
                to_state="running",
                reason=json.dumps(
                    dict(
                        action="evaluation_retest_requested",
                        prior_execution_id=context.execution.id,
                        execution_id=created.record.id,
                        correction_operation_key=action.operation_key,
                        cycle=node.attempts,
                    ),
                    sort_keys=True,
                ),
            )
        )
        await session.flush()

    async def retest_multi(self, session, context, observation):
        import json

        from .evaluation_controller import specification_hash
        from .evaluation_correction_state import ACTOR, validate_correction
        from .evaluation_plan import accepted_evaluation, current_deployment
        from .execution_runner import RunnerContext
        from .execution_state import ExecutionIdentity, ExecutionStatus, PhaseAdvance
        from .execution_store import advance_execution, create_execution
        from .models import OrchestrationDecision, OrchestrationNode
        from .repository_evaluation import multi_repository_sources, sources_for
        from .repository_producer import MULTI_CONTEXT_KIND, digest

        data, _, action = await self.snapshot_multi(session, context)
        node = await self.evaluations.node(session, context)
        accepted = await accepted_evaluation(session, node)
        require(accepted is not None and action is not None, "evaluation_retest_parent_changed")
        plan, spec, _ = accepted
        require(
            node.state == "running"
            and node.attempts == context.identity.cycle
            and context.identity.cycle < data["evaluation_cycle"] + data["remaining_corrections"],
            "evaluation_retest_limit_exhausted",
        )
        snapshot = observation.snapshot
        require(
            action.operation_key == snapshot["operation_key"]
            and action.detail["child_node_id"] == snapshot["child_node_id"]
            and data["source_authorization"]["address"] == snapshot["affected_source_address"]
            and specification_hash(spec) == snapshot["specification_hash"],
            "evaluation_retest_scope_changed",
        )
        child = await session.get(OrchestrationNode, snapshot["child_node_id"], populate_existing=True)
        require(child is not None and child.state == "passed", "evaluation_retest_child_changed")
        verified = await validate_correction(session, child)
        require(verified is not None and verified.id == action.id, "evaluation_retest_child_changed")
        corrected = await current_deployment(session, child, plan.version, now=datetime.now(UTC))
        require(corrected is not None, "evaluation_retest_deployment_missing")
        anchor, _, deployment = corrected
        require(
            deployment.operation_key == snapshot["deployment_key"]
            and deployment.valid_until > datetime.now(UTC)
            and asdict(anchor.identity) == snapshot["anchor_identity"],
            "evaluation_retest_deployment_changed",
        )
        correction = (snapshot["affected_source_address"], child, action.operation_key)
        fresh = await sources_for(session, node, plan, spec, correction=correction)
        require(
            digest(fresh) == snapshot["source_snapshot_hash"]
            and sorted(source["deployment_receipt_key"] for source in fresh) == snapshot["receipt_keys"]
            and all(
                source["head_sha"] == snapshot["unchanged_revisions"][source["address"]]
                for source in fresh
                if source["address"] != snapshot["affected_source_address"]
            ),
            "evaluation_retest_sources_changed",
        )
        effective = spec.model_copy(update={"environment": spec.environment.model_copy(update={"deployment_receipt_keys": snapshot["receipt_keys"]})})
        verified, _, _ = await multi_repository_sources(session, node, plan, effective, lock=True, correction=correction)
        require(verified == fresh, "evaluation_retest_sources_changed")
        previous = await session.scalar(
            select(OrchestrationAction).where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationAction.execution_id == context.execution.id,
                OrchestrationAction.kind == MULTI_CONTEXT_KIND,
                OrchestrationAction.status == "succeeded",
            )
        )
        require(previous is not None and previous.detail.get("failed_probe"), "evaluation_retest_failure_missing")
        node.attempts += 1
        identity = ExecutionIdentity(node.org_id, node.id, node.attempts, plan.version, context.identity.claim_id, context.identity.claim_generation)
        created = await create_execution(
            session,
            identity=identity,
            flow_id=node.flow_id,
            phase=ExecutionPhase.EVALUATION_PENDING,
            next_check_at=datetime.now(UTC),
            deadline_at=context.execution.deadline_at,
        )
        require(created.kind is OutcomeKind.APPLIED, "evaluation_retest_execution_conflict")
        current = RunnerContext(identity, created.record, datetime.now(UTC))
        key = OperationIdentity.from_context(current, MULTI_CONTEXT_KIND, previous.detail["acceptance_decision_id"]).key
        detail = dict(
            source_scope=[[source["address"], source["node_id"], source["repository_id"]] for source in fresh],
            source_snapshot_hash=digest(fresh),
            acceptance_decision_id=previous.detail["acceptance_decision_id"],
            specification_hash=previous.detail["specification_hash"],
            plan_id=plan.id,
            plan_hash=plan.plan_hash,
            evaluation_policy_hash=previous.detail["evaluation_policy_hash"],
            correction_operation_key=action.operation_key,
            correction_parent_execution_id=context.execution.id,
            correction_deployment_key=deployment.operation_key,
            affected_source_address=snapshot["affected_source_address"],
            receipt_keys=snapshot["receipt_keys"],
            unchanged_revisions=snapshot["unchanged_revisions"],
        )
        require(
            (await prepare_action(session, identity=identity, intent=ActionIntent(key, MULTI_CONTEXT_KIND, detail=detail))).kind
            is OutcomeKind.APPLIED,
            "evaluation_retest_context_conflict",
        )
        require(
            (
                await record_observation(
                    session,
                    identity=identity,
                    observation=Observation(
                        key,
                        ObservedOutcome.SUCCEEDED,
                        receipt_ref="evaluation/context/" + created.record.id,
                        detail="Fresh exact-set evaluation requested for the verified correction.",
                    ),
                )
            ).kind
            is OutcomeKind.APPLIED,
            "evaluation_retest_context_conflict",
        )
        settled = await advance_execution(
            session,
            identity=context.identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.CONCLUDED,
                status=ExecutionStatus.CONCLUDED,
                expected_revision=context.execution.revision,
                progress_note="Failed probe retained; corrected deployment entered a fresh evaluation cycle.",
            ),
        )
        require(settled.kind is OutcomeKind.APPLIED, "evaluation_retest_prior_cycle_conflict")
        action.detail = {**action.detail, "retest_cycle": node.attempts}
        session.add(
            OrchestrationDecision(
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                kind="result_observed",
                actor_id=ACTOR,
                actor_kind="service",
                actor_role="engine",
                from_state="running",
                to_state="running",
                reason=json.dumps(
                    dict(
                        action="evaluation_retest_requested",
                        prior_execution_id=context.execution.id,
                        execution_id=created.record.id,
                        correction_operation_key=action.operation_key,
                        cycle=node.attempts,
                        source_snapshot_hash=digest(fresh),
                    ),
                    sort_keys=True,
                ),
            )
        )
        await session.flush()

    def decide(self, context, observation):
        if observation.stage == "correction_create":
            data = observation.snapshot
            return HandlerDecision(
                DecisionKind.EFFECT,
                effect=EffectRequest(ActionIntent(data["operation_key"], CORRECTION_KIND, detail=data), Action.REPAIR),
                progress_note="Creating or reconciling the linked evaluation correction.",
            )
        if observation.stage == "correction_refresh":

            async def refresh(session, current):
                await self.accept_refresh(session, current, observation)

            return HandlerDecision(
                DecisionKind.ADVANCE,
                phase=ExecutionPhase.EVALUATION_PENDING,
                settlement=refresh,
                next_check_at=context.now + timedelta(seconds=30),
                progress_note="Fresh unchanged-source runtime evidence recorded.",
            )
        if observation.stage == "correction_retest":

            async def retest(session, current):
                await self.retest(session, current, observation)

            return HandlerDecision(
                DecisionKind.ADVANCE,
                phase=ExecutionPhase.EVALUATION_PENDING,
                settlement=retest,
                next_check_at=context.now + timedelta(seconds=30),
                progress_note="Requesting fresh evaluation after the verified correction.",
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
