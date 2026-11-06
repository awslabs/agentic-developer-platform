"""Resolve accepted evaluation nodes and their current D3 predecessors."""

import json
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import select

from .deployment_controller import DEPLOYMENT_KIND
from .deployment_runtime_contract import DeploymentReceipt
from .dispatch import graph_address
from .evaluation_contract import MULTI_REPOSITORY_SCHEMA, specification
from .execution_runner import RunnerContext
from .execution_state import ExecutionIdentity, OutcomeKind
from .execution_store import load_execution
from .merge_controller import MERGE_KIND, MergeReceipt
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationEdge, OrchestrationExecution, OrchestrationFlow, OrchestrationNode
from .plan_lineage import execution_plan_matches
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .repository import OrchestrationRepository
from .review_cycle import CycleBlockedError


def require(condition, reason):
    if not condition:
        raise CycleBlockedError(reason)


async def accepted_evaluation(session, node):
    if node.kind != "eval":
        return None
    plan = await OrchestrationRepository(session).get_accepted_plan(org_id=node.org_id, flow_id=node.flow_id)
    if plan is None:
        return None
    flow = await session.get(OrchestrationFlow, node.flow_id, populate_existing=True)
    require(flow is not None and flow.org_id == node.org_id, "evaluation_flow_missing")
    address = graph_address(node, flow_slug=flow.slug)
    rows = [item for item in (plan.plan_document or {}).get("nodes", []) if item.get("address") == address]
    require(len(rows) <= 1, "evaluation_accepted_node_ambiguous")
    if not rows:
        return None
    raw = rows[0].get("evaluation")
    attached = None
    if raw is None or raw.get("evidence_schema") in {
        "repository-evaluation/v1",
        "cli-live-evaluation/v1",
        "workflow-evaluation/v1",
        MULTI_REPOSITORY_SCHEMA,
    }:
        from .evaluation_acceptance import accepted_contract

        attached = await accepted_contract(session, node=node, plan=plan)
    if attached is None and raw is None:
        return None
    spec = attached[1] if attached is not None else specification(raw)
    policy = (plan.plan_document or {}).get("execution_policy") or {}
    if policy.get("evaluation_acceptance", {}).get(address) == "machine":
        require(spec.acceptance_mode == "machine", "evaluation_acceptance_mode_changed")
    return plan, spec, address


async def human_evaluation(session, node):
    if node.kind != "eval":
        return None
    plan = await OrchestrationRepository(session).get_accepted_plan(org_id=node.org_id, flow_id=node.flow_id)
    if plan is None or not (plan.plan_document or {}).get("execution_policy"):
        return None
    await session.refresh(plan)
    flow = await session.get(OrchestrationFlow, node.flow_id, populate_existing=True)
    require(flow is not None and flow.org_id == node.org_id, "evaluation_flow_missing")
    require(flow.state in {"pending", "running"}, "evaluation_flow_not_active")
    require(not flow.execution_paused, "evaluation_flow_paused")
    address = graph_address(node, flow_slug=flow.slug)
    rows = [item for item in plan.plan_document.get("nodes", []) if item.get("address") == address]
    require(len(rows) <= 1, "evaluation_accepted_node_ambiguous")
    if not rows or rows[0].get("kind") != "eval":
        return None
    if plan.plan_document["execution_policy"].get("evaluation_acceptance", {}).get(address) == "machine":
        return None
    accepted = await accepted_evaluation(session, node)
    if accepted is None:
        return (plan, None) if rows[0].get("evaluation") is None else None
    if accepted[1].acceptance_mode != "human":
        return None
    return plan, accepted[1]


async def latest_human_gate_evidence(session, node):
    row = await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == node.org_id,
            OrchestrationDecision.flow_id == node.flow_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.kind == "human_evaluation_evidence",
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    return row, json.loads(row.reason) if row is not None else None


async def legacy_worker_gate(session, node):
    """Only flows with no accepted policy history retain worker-evaluation gates."""
    from .policy_admission import load_in_force_policy

    inputs = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
    recorded, _ = await latest_human_gate_evidence(session, node)
    return inputs.policy is None and inputs.refusal is None and recorded is None


async def human_gate_evidence(session, node, plan, spec, *, now, lock=False):
    if spec is not None and spec.evidence_schema == MULTI_REPOSITORY_SCHEMA:
        combined = await human_multi_repository_evidence(session, node, plan, spec, now=now, lock=lock)
        return dict(contract="human-evaluation-gate/v1", plan_version=plan.version, plan_hash=plan.plan_hash, repository_receipt=combined)
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
    require(len(parents) <= 128 and bool(parents), "evaluation_predecessors_missing")
    require(all(parent.state == "passed" for parent in parents), "evaluation_predecessors_not_passed")
    deployment = None
    policy = (plan.plan_document or {}).get("execution_policy") or {}
    deployment_required = spec is not None and spec.target is not None
    if spec is None:
        deployment_required = bool(policy.get("environment_connection_ids")) or "deploy" in set(policy.get("allowed_actions", [])).union(
            policy.get("human_gates", [])
        )
    if deployment_required:
        deployment = await predecessor_deployments(session, node, plan.version, now=now)
        require(deployment is not None, "verified_deployment_required")
    sources = []
    for parent in parents:
        decision = await session.scalar(
            select(OrchestrationDecision)
            .where(
                OrchestrationDecision.org_id == node.org_id,
                OrchestrationDecision.flow_id == node.flow_id,
                OrchestrationDecision.node_id == parent.id,
                OrchestrationDecision.to_state == "passed",
            )
            .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
            .limit(1)
        )
        require(decision is not None, "evaluation_predecessor_evidence_missing")
        source = dict(node_id=parent.id, attempt=parent.attempts, decision_id=decision.id)
        if parent.kind == "story":
            binding = await active_binding_for_node(session, org_id=node.org_id, node_id=parent.id, attempt=parent.attempts)
            if binding is not None:
                if lock:
                    await session.refresh(binding, with_for_update=True)
                require(binding.state == "active" and binding_scope_matches(binding, parent), "evaluation_predecessor_binding_changed")
                actions = list(
                    await session.execute(
                        select(OrchestrationAction, OrchestrationExecution)
                        .join(OrchestrationExecution, OrchestrationExecution.id == OrchestrationAction.execution_id)
                        .where(
                            OrchestrationAction.org_id == node.org_id,
                            OrchestrationAction.kind == MERGE_KIND,
                            OrchestrationAction.status == "succeeded",
                            OrchestrationExecution.org_id == node.org_id,
                            OrchestrationExecution.node_id == parent.id,
                            OrchestrationExecution.cycle == parent.attempts,
                        )
                        .limit(101)
                    )
                )
                require(len(actions) <= 100, "evaluation_merge_history_limit")
                matches = []
                for action, execution in actions:
                    raw = (action.detail or {}).get("merge_receipt")
                    if raw is None:
                        continue
                    receipt = MergeReceipt.model_validate(raw)
                    if (receipt.execution_id, receipt.operation_key, receipt.reviewed_head_sha) != (
                        execution.id,
                        action.operation_key,
                        binding.head_sha,
                    ):
                        continue
                    if (receipt.org_id, receipt.flow_id, receipt.node_id, receipt.cycle, receipt.repo, receipt.pr_number) != (
                        node.org_id,
                        node.flow_id,
                        parent.id,
                        parent.attempts,
                        binding.repo,
                        binding.pr_number,
                    ):
                        continue
                    require(
                        await execution_plan_matches(
                            session,
                            org_id=node.org_id,
                            flow_id=node.flow_id,
                            node_id=parent.id,
                            version=receipt.accepted_plan_version,
                            current_version=plan.version,
                        ),
                        "evaluation_merge_plan_changed",
                    )
                    matches.append((action, receipt))
                require(len(matches) == 1, "evaluation_merge_evidence_missing_or_ambiguous")
                action, receipt = matches[0]
                checks = (action.detail or {}).get("post_merge_verification") or (action.detail or {}).get("eligibility") or {}
                source.update(
                    binding_id=binding.id,
                    binding_revision=binding.revision,
                    head_sha=receipt.reviewed_head_sha,
                    merge_sha=receipt.merge_sha,
                    merge_operation_key=receipt.operation_key,
                    checks=checks.get("checks", []),
                    check_requirements=checks.get("check_requirements", checks.get("requirements", {})),
                )
        sources.append(source)
    return dict(
        contract="human-evaluation-gate/v1",
        plan_version=plan.version,
        plan_hash=plan.plan_hash,
        predecessors=sources,
        deployments=sorted(
            (
                dict(node_id=parent.id, operation_key=receipt.operation_key, actual_revision=receipt.actual_revision)
                for _, parent, receipt in (deployment or [])
            ),
            key=lambda item: item["node_id"],
        ),
    )


async def human_multi_repository_evidence(session, node, plan, spec, *, now, lock=False):
    from .evaluation_acceptance import accepted_contract
    from .evaluation_contract import verify_multi_repository_receipt
    from .repository_evaluation import multi_repository_sources, multi_source_receipts
    from .repository_producer import MULTI_CONTEXT_KIND, digest

    require(node.state == "awaiting_gate", "evaluation_repository_receipt_not_collected")
    row = await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == node.org_id,
            OrchestrationDecision.flow_id == node.flow_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.kind == "result_observed",
            OrchestrationDecision.actor_id == "system:repository-evaluation",
            OrchestrationDecision.actor_kind == "service",
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    require(row is not None, "evaluation_repository_receipt_not_collected")
    try:
        data = json.loads(row.reason)
        require(data.get("action") == "multi_repository_completed", "evaluation_repository_receipt_not_collected")
        receipt = data["receipt"]
        accepted = await accepted_contract(session, node=node, plan=plan)
        require(accepted is not None and accepted[1] == spec, "evaluation_human_contract_changed")
        execution = await session.get(OrchestrationExecution, data["producer_execution_id"])
        require(
            execution is not None
            and execution.id == receipt["execution_id"]
            and execution.org_id == node.org_id
            and execution.flow_id == node.flow_id
            and execution.node_id == node.id
            and execution.cycle == node.attempts
            and execution.claim_generation == receipt["claim_generation"]
            and execution.accepted_plan_version == plan.version,
            "evaluation_repository_execution_changed",
        )
        sources, _, _ = await multi_repository_sources(session, node, plan, spec, lock=lock)
        contexts = list(
            await session.scalars(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.execution_id == execution.id,
                    OrchestrationAction.kind == MULTI_CONTEXT_KIND,
                    OrchestrationAction.status == "succeeded",
                )
                .limit(2)
            )
        )
        require(len(contexts) == 1, "evaluation_repository_context_changed")
        context = contexts[0].detail or {}
        require(
            context.get("acceptance_decision_id") == accepted[0].id
            and context.get("evaluation_policy_hash") == accepted[2].policy_hash
            and context.get("specification_hash") == digest(spec.model_dump(mode="json"))
            and (context.get("settled_source_snapshot_hash") or context.get("source_snapshot_hash")) == digest(sources)
            and context.get("plan_id") == plan.id
            and context.get("plan_hash") == plan.plan_hash,
            "evaluation_repository_context_changed",
        )
        observations = [{"receipts": source["checks"]} for source in receipt["sources"]]
        resolved = await multi_source_receipts(session, node, plan, spec, sources, observations)
        verified = verify_multi_repository_receipt(spec.model_dump(mode="json"), receipt, resolved, now=now)
        require(verified.verdict == "pass", "evaluation_repository_verdict_not_passed")
    except (KeyError, TypeError, ValueError):
        raise CycleBlockedError("evaluation_repository_receipt_unverifiable") from None
    return {"decision_id": row.id, "input_set_digest": verified.input_set_digest, "receipt": verified.model_dump(mode="json")}


async def record_human_gate_evidence(session, node, evidence, *, actor_id):
    row = OrchestrationDecision(
        org_id=node.org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        kind="human_evaluation_evidence",
        actor_id=actor_id,
        actor_role="engine",
        actor_kind="service",
        reason=json.dumps(evidence, sort_keys=True),
    )
    session.add(row)
    await session.flush()
    return row


async def validate_human_gate_evidence(session, node, evidence_id):
    # Legacy evaluations reach this gate through a completed worker result. They
    # have no native human-evaluation contract or evidence row to review. Removed
    # or unreadable policy, and any retained native evidence, cannot downgrade.
    if await legacy_worker_gate(session, node):
        return None
    recorded, bound = await latest_human_gate_evidence(session, node)
    require(recorded is not None and recorded.id == evidence_id, "evaluation_gate_evidence_not_reviewed")
    from .policy_admission import load_in_force_policy

    inputs = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
    require(inputs.refusal is None, "evaluation_policy_unverifiable")
    accepted = await human_evaluation(session, node)
    require(accepted is not None, "evaluation_human_contract_changed")
    current = await human_gate_evidence(session, node, *accepted, now=datetime.now(UTC), lock=True)
    require(current == bound, "evaluation_gate_evidence_changed")
    return current


async def managed_evaluation(session, node):
    if node.kind != "eval":
        return False
    accepted = await accepted_evaluation(session, node)
    if accepted and (accepted[0].plan_document or {}).get("execution_policy"):
        return True
    # A missing specification is a blocked machine evaluation, never permission
    # to dispatch a legacy worker whose result would require a human gate.
    if await human_evaluation(session, node):
        return True
    plan = await OrchestrationRepository(session).get_accepted_plan(org_id=node.org_id, flow_id=node.flow_id)
    policy = (plan.plan_document or {}).get("execution_policy") if plan else None
    flow = await session.get(OrchestrationFlow, node.flow_id)
    return bool(
        policy
        and flow is not None
        and flow.org_id == node.org_id
        and policy.get("evaluation_acceptance", {}).get(graph_address(node, flow_slug=flow.slug)) == "machine"
    )


def identity_for(record):
    return ExecutionIdentity(record.org_id, record.node_id, record.cycle, record.accepted_plan_version, record.claim_id, record.claim_generation)


async def predecessor_deployments(session, node, plan_version, *, now):
    parents = list(
        (
            await session.scalars(
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
            )
        ).all()
    )
    require(len(parents) <= 128, "evaluation_predecessor_limit")
    if not parents or any(parent.state != "passed" for parent in parents):
        return None
    deployments = []
    for parent in parents:
        if parent.kind != "story":
            continue
        current = await current_deployment(session, parent, plan_version, now=now)
        if current is None:
            return None
        deployments.append(current)
    return deployments or None


async def current_deployment(session, parent, plan_version, *, now):
    records = list(
        (
            await session.scalars(
                select(OrchestrationExecution)
                .where(
                    OrchestrationExecution.org_id == parent.org_id,
                    OrchestrationExecution.node_id == parent.id,
                    OrchestrationExecution.cycle == parent.attempts,
                    OrchestrationExecution.accepted_plan_version == plan_version,
                )
                .limit(2)
            )
        ).all()
    )
    if not records:
        return None
    require(len(records) == 1, "evaluation_predecessor_execution_ambiguous")
    identity = identity_for(records[0])
    loaded = await load_execution(session, identity=identity)
    require(loaded is not None and loaded.kind is OutcomeKind.APPLIED, "evaluation_predecessor_authority_changed")
    actions = list(
        (
            await session.scalars(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.org_id == parent.org_id,
                    OrchestrationAction.execution_id == records[0].id,
                    OrchestrationAction.kind == DEPLOYMENT_KIND,
                    OrchestrationAction.status == "succeeded",
                )
                .order_by(OrchestrationAction.created_at.desc())
                .limit(101)
            )
        ).all()
    )
    require(len(actions) <= 100, "evaluation_deployment_history_limit")
    final = []
    for action in actions:
        raw = (action.detail or {}).get("deployment_receipt")
        if not raw or raw.get("delivery_complete") is not True:
            continue
        receipt = DeploymentReceipt.model_validate(raw)
        require(all(getattr(receipt, key) == value for key, value in asdict(identity).items()), "evaluation_deployment_scope_changed")
        require(
            receipt.execution_id == records[0].id and receipt.flow_id == parent.flow_id and receipt.operation_key == action.operation_key,
            "evaluation_deployment_reference_changed",
        )
        final.append(receipt)
    if not final:
        return None
    require(len(final) == 1, "evaluation_final_deployment_ambiguous")
    return RunnerContext(identity, loaded.record, now), parent, final[0]
