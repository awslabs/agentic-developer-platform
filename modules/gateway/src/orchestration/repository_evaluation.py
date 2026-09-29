"""Tick-owned repository evaluation without workers, issue claims, or deployment claims.

The decision ledger is the authority for this read-only observer. Source story
actions retain their real execution/claim identities; an external evidence read
does not manufacture an execution or take ownership of the referenced issue.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal

from pydantic import Field, StrictBool, model_validator
from sqlalchemy import select

from .dispatch import graph_address
from .evaluation_plan import accepted_evaluation
from .execution_policy import Action, CredentialScope, ResourceRef, authorize_action
from .merge_controller import MERGE_KIND, MergeReceipt
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
)
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .repository_evaluation_contract import Contract, Digest, Name, PullReceipt, WorkflowReceipt, canonical, harness_digest
from .repository_evaluation_provider import RepositoryEvidenceProvider, require
from .review_cycle import CycleBlockedError
from .state import ActorKind, NodeState, transition

ACTOR = "system:repository-evaluation"
logger = logging.getLogger(__name__)
OBSERVATION_TIMEOUT_SECONDS = 15


class RepositoryEvaluationReceipt(Contract):
    evidence_schema: Literal["repository-evaluation-receipt/v1"] = "repository-evaluation-receipt/v1"
    org_id: Name
    flow_id: Name
    node_id: Name
    cycle: int = Field(gt=0)
    accepted_plan_version: int = Field(gt=0)
    accepted_plan_hash: Digest
    acceptance_decision_id: Name | None = None
    evaluation_policy_hash: Digest
    specification_hash: Digest
    harness_sha256: Digest
    collector_harness_sha256: Digest | None = None
    source_snapshot_hash: Digest
    observed_at: datetime
    # Provider-specific records retain exact PR/check/run/attempt/archive identities.
    pull_requests: list[PullReceipt] = Field(max_length=160)
    workflows: list[WorkflowReceipt] = Field(max_length=16)
    mandatory_passed: StrictBool
    live_attestation: Literal[False] = False

    @model_validator(mode="after")
    def proof(self):
        if not self.pull_requests and not self.workflows:
            raise ValueError("evidence receipt cannot be empty")
        if self.mandatory_passed != all(item.passed for row in self.workflows for item in row.criteria):
            raise ValueError("verdict must follow mandatory criterion outcomes")
        if self.observed_at.tzinfo is None:
            raise ValueError("observation timestamp must be aware")
        return self


async def sources_for(session, node, plan, spec, *, lock=False, preview_gate_id=None):
    query = (
        select(OrchestrationNode)
        .join(OrchestrationEdge, OrchestrationEdge.from_node_id == OrchestrationNode.id)
        .where(
            OrchestrationEdge.org_id == node.org_id,
            OrchestrationEdge.to_node_id == node.id,
            OrchestrationNode.org_id == node.org_id,
            OrchestrationNode.flow_id == node.flow_id,
        )
        .order_by(OrchestrationNode.id)
        .limit(129)
        .execution_options(populate_existing=True)
    )
    parents = list(await session.scalars(query.with_for_update(of=OrchestrationNode) if lock else query))
    require(
        len(parents) <= 128
        and all(
            parent.state == "passed" or (parent.id == preview_gate_id and parent.kind == "gate" and parent.state == "awaiting_gate")
            for parent in parents
        ),
        "predecessors_not_complete",
    )
    flow = await session.get(OrchestrationFlow, node.flow_id)
    stories = {graph_address(parent, flow_slug=flow.slug): parent for parent in parents if parent.kind == "story"}
    require(set(stories) == {item.address for item in spec.predecessors}, "predecessor_set_changed")
    sources = []
    for expected in spec.predecessors:
        parent = stories[expected.address]
        binding = await active_binding_for_node(session, org_id=node.org_id, node_id=parent.id, attempt=parent.attempts)
        require(binding is not None and binding_scope_matches(binding, parent), "predecessor_binding_missing_or_changed")
        require(
            binding.repo == spec.runner.repository and binding.provider_repository_id == spec.runner.repository_id, "predecessor_repository_changed"
        )
        # A later evaluation-only amendment may consume earlier delivered code.
        # Its exact original plan/execution stays on the source receipt; the
        # currently accepted node scope must still match the immutable binding.
        rows = list(
            await session.execute(
                select(OrchestrationAction, OrchestrationExecution)
                .join(OrchestrationExecution, OrchestrationExecution.id == OrchestrationAction.execution_id)
                .where(
                    OrchestrationExecution.org_id == node.org_id,
                    OrchestrationExecution.flow_id == node.flow_id,
                    OrchestrationExecution.node_id == parent.id,
                    OrchestrationExecution.cycle == parent.attempts,
                    OrchestrationExecution.accepted_plan_version <= plan.version,
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.kind == MERGE_KIND,
                    OrchestrationAction.status == "succeeded",
                )
                .limit(101)
            )
        )
        require(len(rows) <= 100, "merge_history_limit")
        matches = []
        for action, execution in rows:
            raw = (action.detail or {}).get("merge_receipt")
            if raw:
                receipt = MergeReceipt.model_validate(raw)
                require(
                    receipt.org_id == node.org_id
                    and receipt.flow_id == node.flow_id
                    and receipt.node_id == parent.id
                    and receipt.execution_id == execution.id
                    and receipt.operation_key == action.operation_key
                    and receipt.accepted_plan_version == execution.accepted_plan_version
                    and receipt.cycle == parent.attempts
                    and receipt.claim_id == execution.claim_id
                    and receipt.claim_generation == execution.claim_generation,
                    "merge_receipt_scope_changed",
                )
                matches.append(receipt)
        require(len(matches) == 1, "merge_receipt_missing_or_ambiguous")
        receipt = matches[0]
        require(
            receipt.repo == binding.repo
            and receipt.provider_repository_id == binding.provider_repository_id
            and receipt.pr_number == binding.pr_number
            and receipt.provider_pr_node_id == binding.provider_pr_node_id
            and receipt.reviewed_head_sha == binding.head_sha,
            "merge_binding_changed",
        )
        sources.append(
            dict(
                **(
                    {"issue_number": int(parent.issue_ref)}
                    if getattr(spec, "qualification", None) is not None and str(parent.issue_ref).isdigit()
                    else {}
                ),
                address=expected.address,
                node_id=parent.id,
                attempt=parent.attempts,
                accepted_plan_version=receipt.accepted_plan_version,
                execution_id=receipt.execution_id,
                binding_id=binding.id,
                binding_revision=binding.revision,
                pr_number=receipt.pr_number,
                head_sha=receipt.reviewed_head_sha,
                merge_sha=receipt.merge_sha,
                provider_pr_node_id=receipt.provider_pr_node_id,
                review_ref=receipt.review_ref,
                merge_operation_key=receipt.operation_key,
                required_checks=[item.model_dump() for item in expected.required_checks],
            )
        )
    sources.extend(item.model_dump() for item in spec.external_pull_requests)
    return sources


async def authorize(session, node, plan, spec, binding, provider, *, exclude_current_evaluation=False):
    from . import shared_policy
    from .policy_admission import SpendObservation

    # This observer uses its own short-lived repository-read token, never an
    # agent's broad role. Minting must really succeed before claiming SCOPED.
    require(getattr(spec, "qualification", None) is None or node.issue_ref == str(spec.qualification.owner_issue), "cli_qualification_owner_changed")
    await provider.token(binding)
    from .evaluation_authority import evaluation_inputs

    inputs, marker = await evaluation_inputs(session, org_id=node.org_id, flow_id=node.flow_id)
    require(inputs.plan_version == plan.version and spec.runner.repository in inputs.policy.repository_ids, "policy_changed")
    from .evaluation_acceptance import accepted_contract

    attached = await accepted_contract(session, node=node, plan=plan)
    require(attached is None or attached[1] == spec, "accepted_specification_changed")
    policy = attached[2] if attached is not None else inputs.policy
    policy._budget_enforcement_enabled = inputs.policy._budget_enforcement_enabled
    policy._budget_accounting_incomplete = inputs.policy._budget_accounting_incomplete
    meter = await shared_policy.read_flow_meter(org_id=node.org_id, flow_id=node.flow_id, policy=inputs.policy)
    require(
        not policy._budget_enforcement_enabled or (meter is not None and meter.total_usd >= Decimal(marker["prior_spend_usd"])), "budget_unavailable"
    )
    context = await shared_policy.resolve_authorization_context(
        session,
        policy=policy,
        plan_version=plan.version,
        node=node,
        principal_user_id=policy.principal_id,
        credential_scope=CredentialScope.SCOPED,
        spend=SpendObservation(total_usd=meter.total_usd if meter else None),
    )
    flow = await session.get(OrchestrationFlow, node.flow_id)
    address = graph_address(node, flow_slug=flow.slug)
    rows = [row for row in (plan.plan_document or {}).get("nodes", []) if row.get("address") == address]
    require(
        len(rows) == 1 and all(rows[0].get(key) == getattr(node, key) for key in ("kind", "title", "issue_ref")),
        "accepted_node_changed",
    )
    # Ownership here is the accepted flow's evaluation node. Reading an external
    # issue does not acquire the issue's coding lane and cannot dispatch changes.
    context = replace(
        context,
        work_owned_by_policy_flow=True,
        evaluation_operation="collect" if spec.evidence_schema == "workflow-evaluation/v1" else "accept",
        observed_attempts=max(0, context.observed_attempts - int(exclude_current_evaluation and node.state == "running")),
        observed_concurrency=max(
            0,
            await shared_policy._active_count(session, org_id=node.org_id, flow_id=node.flow_id, initial_runs=marker.get("initial_runs"))
            - int(exclude_current_evaluation and node.kind == "eval" and node.state == "running"),
        ),
    )
    decision = authorize_action(
        context, Action.EVALUATE, ResourceRef(repository_id=binding.repo, node_address=address, org_id=node.org_id), plan.version
    )
    require(decision.permitted, "authority_" + (decision.reason.value if decision.reason else "denied"))
    return (attached[0].id if attached is not None else None), policy.policy_hash


async def record(session, node, payload, *, rejection=False, before=None):
    text = canonical(payload)
    require(len(text) <= 512 * 1024, "receipt_size_limit")
    # A repeated blocker still advances the observation cursor so the next tick
    # gives another ready evaluation its bounded turn without growing the ledger.
    node.updated_at = datetime.now(UTC)
    previous = await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == node.org_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.actor_id == ACTOR,
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    if previous is not None and previous.reason == text:
        return
    session.add(
        OrchestrationDecision(
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind="transition_rejected" if rejection else "result_observed",
            actor_id=ACTOR,
            actor_role="engine",
            actor_kind="service",
            from_state=before or node.state,
            to_state=None if rejection else node.state,
            reason=text,
            rejection_reason=text if rejection else None,
        )
    )
    await session.flush()


async def observe_repository_evaluation(session, node, *, provider=None):
    """One bounded observer after story admission; no slow read owns the tick."""
    try:
        async with asyncio.timeout(OBSERVATION_TIMEOUT_SECONDS):
            async with session.begin_nested():
                return await _observe_repository_evaluation(session, node, provider=provider)
    except TimeoutError:
        await session.refresh(node)
        await record(
            session,
            node,
            dict(
                action="repository_evaluation_blocked",
                block_code="repository_evaluation_time_budget",
                owner="engine",
                required_input="Complete authenticated evidence reads within the per-tick observation budget.",
                next_action="Retry this evidence observation on a later engine tick without delaying worker admission.",
            ),
            rejection=True,
        )
        return True


async def _observe_repository_evaluation(session, node, *, provider=None):
    accepted = await accepted_evaluation(session, node)
    if accepted is None or accepted[1].evidence_schema not in {"repository-evaluation/v1", "cli-live-evaluation/v1", "workflow-evaluation/v1"}:
        return False
    plan, spec, _ = accepted
    provider = provider or RepositoryEvidenceProvider()
    settlement = None
    expected_plan = plan.id, plan.version, plan.plan_hash
    expected_attempt = node.attempts
    try:
        require(node.state == "ready", "node_not_ready")
        flow = await session.get(OrchestrationFlow, node.flow_id)
        require(flow is not None and flow.org_id == node.org_id and flow.state in {"pending", "running"}, "flow_not_running")
        if spec.producer is not None:
            from .repository_producer import admit_producer

            async with session.begin_nested():
                return await admit_producer(session, node, provider=provider)
        require(spec.runner.harness_sha256 == harness_digest(), "harness_changed")
        from .dispatch_pass import resolve_installation_id

        installation = await resolve_installation_id(session, org_id=node.org_id)
        require(installation is not None, "installation_missing")
        binding = SimpleNamespace(
            org_id=node.org_id, installation_id=installation, repo=spec.runner.repository, provider_repository_id=spec.runner.repository_id
        )
        authority = await authorize(session, node, plan, spec, binding, provider)
        sources = await sources_for(session, node, plan, spec)
        spec_hash = hashlib.sha256(canonical(spec.model_dump(mode="json")).encode()).hexdigest()
        snapshot_hash = hashlib.sha256(canonical(sources).encode()).hexdigest()
        observed = await provider.observe(binding, spec, sources)
        settlement = await session.begin_nested()
        # Lock/re-read accepted state at the write boundary, including all direct
        # predecessors. An amendment or binding replacement invalidates the read.
        await session.scalar(
            select(OrchestrationFlow)
            .where(OrchestrationFlow.id == node.flow_id, OrchestrationFlow.org_id == node.org_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current = await session.scalar(
            select(OrchestrationAcceptedPlan)
            .where(
                OrchestrationAcceptedPlan.org_id == node.org_id,
                OrchestrationAcceptedPlan.flow_id == node.flow_id,
                OrchestrationAcceptedPlan.superseded_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        require(current is not None and (current.id, current.version, current.plan_hash) == expected_plan, "plan_changed")
        node = await session.scalar(
            select(OrchestrationNode)
            .where(OrchestrationNode.id == node.id, OrchestrationNode.org_id == node.org_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        require(node.attempts == expected_attempt, "attempt_changed")
        fresh = await accepted_evaluation(session, node)
        require(fresh is not None and fresh[1] == spec and node.state == "ready" and flow.state in {"pending", "running"}, "scope_changed")
        require(await sources_for(session, node, current, spec, lock=True) == sources, "source_changed")
        require(await authorize(session, node, current, spec, binding, provider) == authority, "evaluation_authority_changed")
        receipt = receipt_model(spec)(
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            cycle=node.attempts + 1,
            accepted_plan_version=current.version,
            accepted_plan_hash=current.plan_hash,
            acceptance_decision_id=authority[0],
            evaluation_policy_hash=authority[1],
            specification_hash=spec_hash,
            harness_sha256=spec.runner.harness_sha256,
            source_snapshot_hash=snapshot_hash,
            observed_at=datetime.now(UTC),
            **observed,
        )
        before = node.state
        if receipt.mandatory_passed:
            for target in (NodeState.RUNNING, NodeState.PASSED):
                require(
                    transition(node.state, target, actor_kind=ActorKind.SERVICE, reason="Verified accepted repository evidence").allowed,
                    "transition_refused",
                )
                node.state = target.value
            node.attempts += 1
        await record(session, node, dict(action="repository_evaluation", receipt=receipt.model_dump(mode="json")), before=before)
        if receipt.mandatory_passed:
            from .tick import release_satisfied_successors

            await release_satisfied_successors(session, node)
        await settlement.commit()
        return True
    except Exception as error:
        if settlement is not None:
            await settlement.rollback()
            await session.refresh(node)
        reason = error.reason if isinstance(error, CycleBlockedError) else "repository_evaluation_provider_unverifiable"
        logger.warning("Repository evaluation refused node=%s reason=%s", node.id, reason, exc_info=not isinstance(error, CycleBlockedError))
        await record(
            session,
            node,
            dict(
                action="repository_evaluation_blocked",
                block_code=reason,
                owner="evidence-producer",
                required_input="Provide successful evidence matching the accepted repository evaluation specification.",
                next_action="Recheck authenticated repository evidence on the next engine tick.",
            ),
            rejection=True,
        )
        return True


def receipt_model(spec):
    if spec.evidence_schema == "cli-live-evaluation/v1":
        from .cli_live_contract import CliWorkflowReceipt

        class CliLiveEvaluationReceipt(RepositoryEvaluationReceipt):
            evidence_schema: Literal["cli-live-evaluation-receipt/v1"] = "cli-live-evaluation-receipt/v1"
            live_attestation: Literal[True] = True
            workflows: list[CliWorkflowReceipt]

        return CliLiveEvaluationReceipt
    return RepositoryEvaluationReceipt
