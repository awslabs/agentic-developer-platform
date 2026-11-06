"""Replay protected deliveries through the existing commit/prepare/publish pass.

The immutable envelope lives in the original SQL dispatch decision, before any
credential provisioning or queue call. It contains no worker credential. A
replay retains the invocation, claim, attempt, model and FIFO identity. Protected
pod binding remains the start-once authority, including a lost queue response.
"""

import json
import logging
import os

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from .models import DecisionKind, OrchestrationDecision, OrchestrationFlow, OrchestrationNode

logger = logging.getLogger(__name__)


async def recover_pending_protected(session, *, config, report):
    if os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() != "true" or not config.configured:
        return
    from src.agentauth.engine import get_engine_authority_writer

    pending_ids = {p.node_id for p in report.pending}
    if len(report.pending) >= config.max_dispatches_per_tick:
        return
    # Existing dispatch identity and decision are the outbox, not advisory
    # activity or a newly fabricated worker assignment.
    nodes = (
        await session.scalars(
            select(OrchestrationNode)
            .join(OrchestrationFlow, (OrchestrationFlow.id == OrchestrationNode.flow_id) & (OrchestrationFlow.org_id == OrchestrationNode.org_id))
            .where(OrchestrationFlow.execution_paused.is_(False), OrchestrationNode.state == "running")
            .order_by(OrchestrationNode.id)
            .limit(1000)
            .with_for_update(of=OrchestrationNode, skip_locked=True)
        )
    ).all()
    writer = None
    for node in nodes:
        if node.id in pending_ids:
            continue
        node_id, org_id = node.id, node.org_id
        try:
            # A claim-lock deadlock must not poison later candidates or discard
            # earlier recoveries. Only publish a replay after its savepoint exits.
            async with session.begin_nested():
                decision = await session.scalar(
                    select(OrchestrationDecision)
                    .where(
                        OrchestrationDecision.org_id == org_id,
                        OrchestrationDecision.node_id == node_id,
                        OrchestrationDecision.kind == DecisionKind.NODE_DISPATCHED.value,
                    )
                    .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
                    .limit(1)
                )
                if decision is None:
                    continue
                dispatch = json.loads(decision.reason or "{}")
                envelope = dispatch.get("dispatch_envelope")
                if not envelope:  # Historical dispatches have no reproducible envelope.
                    continue
                if writer is None:
                    writer = await run_in_threadpool(get_engine_authority_writer)
                pending = await recover_one(session, node=node, dispatch=dispatch, envelope=envelope, config=config, store=writer.store)
            if pending is not None:
                report.pending.append(pending)
                pending_ids.add(node_id)
            if len(report.pending) >= config.max_dispatches_per_tick:
                break
        except Exception:
            logger.exception("Protected dispatch replay refused node=%s", node_id)
            report.record(org_id, "publish_failed")


async def recover_one(session, *, node, dispatch, envelope, config, store):
    from src.agentauth.bootstrap import envelope_digest
    from src.shared.identity.resolver import resolve_root_user_entity_id

    from .dispatch_pass import (
        PendingPublish,
        _latest_approval_decision_id,
        attempt_run_id,
        issue_number_for_dispatch,
        message_deduplication_id,
        message_group_id,
        product_binding_hold,
    )
    from .executor_assignment import accepted_executor_persona
    from .flow_execution import flow_is_paused
    from .genesis import resolve_engine_genesis
    from .policy_admission import authorize_node_dispatch
    from .tick import _predecessor_states, _unsatisfied

    if await flow_is_paused(session, org_id=node.org_id, flow_id=node.flow_id, lock=True):
        return None
    if await product_binding_hold(session, org_id=node.org_id, flow_id=node.flow_id):
        return None
    run_id = attempt_run_id(node.id, node.attempts)
    graph, source = envelope.get("orchestration", {}), envelope.get("source_ref", {})
    root = dispatch.get("root_decision_id")
    if (
        node.state != "running"
        or envelope.get("message_id") != run_id
        or dispatch.get("run_id") != run_id
        or dispatch.get("attempt") != node.attempts
        or envelope.get("tenant_id") != node.org_id
        or graph.get("flow_id") != node.flow_id
        or graph.get("node_id") != node.id
        or graph.get("attempt") != node.attempts
        or graph.get("root_decision_id") != root
        or envelope.get("run_report")
        or envelope.get("execution_continuation")
        or source.get("repo") != dispatch.get("repo")
        or source.get("issue") != issue_number_for_dispatch(node.issue_ref)
        or source.get("installation_id") != dispatch.get("installation_id")
        or source.get("provider_repository_id") != dispatch.get("provider_repository_id")
    ):
        raise ValueError("protected_dispatch_assignment_changed")
    assignment = dispatch.get("assignment")
    if assignment is not None:
        expected_assignment = {
            "tenant_id": node.org_id,
            "flow_id": node.flow_id,
            "node_id": node.id,
            "graph_address": graph.get("graph_address"),
            "invocation_id": run_id,
            "attempt": node.attempts,
            "issue": source.get("issue"),
            "repo": source.get("repo"),
            "provider_repository_id": source.get("provider_repository_id"),
            "installation_id": source.get("installation_id"),
        }
        if any(assignment.get(key) != value for key, value in expected_assignment.items()):
            raise ValueError("protected_dispatch_assignment_changed")
        plan_version = assignment.get("accepted_plan_version")
        plan_hash = assignment.get("accepted_plan_hash")
        if (plan_version is None) != (plan_hash is None):
            raise ValueError("protected_dispatch_plan_changed")
        if plan_version is not None:
            from .models import OrchestrationAcceptedPlan

            original_plan = await session.scalar(
                select(OrchestrationAcceptedPlan).where(
                    OrchestrationAcceptedPlan.org_id == node.org_id,
                    OrchestrationAcceptedPlan.flow_id == node.flow_id,
                    OrchestrationAcceptedPlan.version == plan_version,
                    OrchestrationAcceptedPlan.plan_hash == plan_hash,
                )
            )
            if original_plan is None:
                raise ValueError("protected_dispatch_plan_changed")
            original_nodes = [
                item
                for item in (original_plan.plan_document or {}).get("nodes", [])
                if isinstance(item, dict) and item.get("address") == graph.get("graph_address")
            ]
            product_id = (original_plan.plan_document or {}).get("product_id")
            if product_id is not None:
                current_product = await session.scalar(
                    select(OrchestrationFlow.product_id).where(OrchestrationFlow.org_id == node.org_id, OrchestrationFlow.id == node.flow_id)
                )
                if current_product != product_id or len(original_nodes) != 1:
                    raise ValueError("protected_dispatch_plan_changed")
            if len(original_nodes) > 1 or (
                original_nodes
                and original_nodes[0].get("provider_repository_id") is not None
                and (
                    original_nodes[0].get("provider_repository_id") != source.get("provider_repository_id")
                    or original_nodes[0].get("issue_ref", "").lstrip("#") != str(source.get("issue"))
                    or (
                        (product_id is not None or original_nodes[0].get("repository_connection") is not None)
                        and (original_nodes[0].get("repository_connection") or {}).get("installation_id") != source.get("installation_id")
                    )
                )
            ):
                raise ValueError("protected_dispatch_plan_changed")
        handoff = envelope.get("handoff_expect") or {}
        if assignment.get("execution_id") != handoff.get("execution_id") or (
            handoff
            and (
                assignment.get("accepted_plan_version") != handoff.get("accepted_plan_version")
                or assignment.get("claim_id") != handoff.get("claim_id")
                or assignment.get("claim_generation") != handoff.get("claim_generation")
            )
        ):
            raise ValueError("protected_dispatch_execution_changed")
        if bool(assignment.get("claim_id")) != bool(envelope.get("work_claim_required")):
            raise ValueError("protected_dispatch_claim_changed")
    controller_transfer = False
    expected = envelope.get("handoff_expect") or {}
    if expected.get("execution_id"):
        from .models import OrchestrationExecution
        from .operations_continuation_authority import PREFIX, transfer
        from .run_reports import OrchestrationRunReport

        execution = await session.get(OrchestrationExecution, expected["execution_id"])
        if (
            execution is not None
            and execution.org_id == node.org_id
            and execution.node_id == node.id
            and execution.flow_id == node.flow_id
            and (execution.handoff_receipt_ref or "").startswith(PREFIX)
        ):
            controller_transfer = True
            report = await session.get(OrchestrationRunReport, run_id)
            if report is not None and await transfer(session, node=node, execution=execution, report=report) is not None:
                # The failed worker's claim now belongs to a validated service
                # continuation. It has no pending worker delivery to replay.
                # Unvalidated service transfers still require ownership checks.
                return None
    if not controller_transfer and graph.get("release_binding") is None and dispatch.get("conclusion_authority") != "evidence":
        # Ordinary workers that started have no delivery left to replay. Their
        # claim may now belong to a reviewer. Service continuations retain their
        # ownership checks before protected I/O; unstarted workers retain every
        # admission check and the final digest check below.
        raw = await run_in_threadpool(store._read, f"TENANT#{node.org_id}", f"EXEC#{run_id}")
        if raw and (raw.get("status") != {"S": "pending"} or raw.get("workload_binding")):
            return None
    if assignment is not None:
        if assignment.get("claim_id") is not None:
            from .models import OrchestrationWorkClaim

            claim = await session.get(OrchestrationWorkClaim, assignment["claim_id"], with_for_update=True)
            if (
                claim is None
                or claim.org_id != node.org_id
                or claim.provider_repository_id != source.get("provider_repository_id")
                or claim.issue_number != source.get("issue")
                or claim.owner_ref != node.flow_id
                or claim.owner_kind != "engine_flow"
                or claim.active_run_id != run_id
                or claim.generation != assignment.get("claim_generation")
                or claim.state != "held"
            ):
                raise ValueError("protected_dispatch_claim_changed")
    # The approval recorded at dispatch stays valid while the node's lineage keeps
    # it (#7535); a newer approval from an unrelated amendment is not a change of
    # assignment.
    from .plan_lineage import approval_authorizes_node

    if await _latest_approval_decision_id(session, org_id=node.org_id, flow_id=node.flow_id) != root and not await approval_authorizes_node(
        session, org_id=node.org_id, flow_id=node.flow_id, node_id=node.id, decision_id=root
    ):
        raise ValueError("protected_dispatch_assignment_changed")
    persona = await accepted_executor_persona(session, node, default=config.persona)
    if node.kind == "story" and envelope.get("persona") != persona:
        raise ValueError("protected_dispatch_persona_changed")
    if graph.get("release_binding") is not None or dispatch.get("conclusion_authority") == "evidence":
        from .accepted_node import accepted_node_contract
        from .models import OrchestrationExecution, OrchestrationWorkClaim
        from .operations_release_resolution import recorded_operations_release

        contract = await accepted_node_contract(session, node)
        if contract is None:
            raise ValueError("protected_dispatch_release_changed")
        release = getattr(contract.node, "operational_release_binding", None)
        if graph.get("release_binding") is not None or release is not None:
            expected = envelope.get("handoff_expect") or {}
            execution = await session.scalar(
                select(OrchestrationExecution).where(
                    OrchestrationExecution.org_id == node.org_id,
                    OrchestrationExecution.flow_id == node.flow_id,
                    OrchestrationExecution.node_id == node.id,
                    OrchestrationExecution.cycle == node.attempts,
                    OrchestrationExecution.accepted_plan_version == expected.get("accepted_plan_version"),
                )
            )
            if (
                release is None
                or execution is None
                or execution.id != expected.get("execution_id")
                or execution.status in {"concluded", "superseded"}
                or execution.claim_id != expected.get("claim_id")
                or execution.claim_generation != expected.get("claim_generation")
                or source.get("repo") != release.repository
                or await recorded_operations_release(session, node=node, execution=execution, contract=contract, metadata=graph) is None
            ):
                raise ValueError("protected_dispatch_release_changed")
            claim = await session.scalar(
                select(OrchestrationWorkClaim)
                .where(
                    OrchestrationWorkClaim.id == execution.claim_id,
                    OrchestrationWorkClaim.org_id == node.org_id,
                    OrchestrationWorkClaim.provider_repository_id == source.get("provider_repository_id"),
                    OrchestrationWorkClaim.issue_number == source.get("issue"),
                    OrchestrationWorkClaim.owner_kind == "engine_flow",
                    OrchestrationWorkClaim.owner_ref == node.flow_id,
                    OrchestrationWorkClaim.active_run_id == run_id,
                    OrchestrationWorkClaim.state == "held",
                )
                .with_for_update()
            )
            if claim is None or claim.generation != execution.claim_generation:
                raise ValueError("protected_dispatch_claim_changed")
    raw = await run_in_threadpool(store._read, f"TENANT#{node.org_id}", f"EXEC#{run_id}")
    if raw:
        # Atomic bootstrap forbids a second pod binding. Never restart a bound,
        # terminal, revoked or cancelled invocation, even if queue ack was lost.
        if raw.get("status") != {"S": "pending"} or raw.get("workload_binding"):
            return None
    from .protected_report_replay import restore_report_envelope

    envelope = await restore_report_envelope(session, envelope)
    if envelope is None:
        return None
    if raw and raw.get("envelope_digest") != {"S": envelope_digest(envelope)}:
        raise ValueError("protected_dispatch_digest_changed")
    if _unsatisfied(await _predecessor_states(session, org_id=node.org_id, node_id=node.id)):
        return None
    genesis = await resolve_engine_genesis(session, org_id=node.org_id, decision_id=root)
    if genesis.flow_id != node.flow_id:
        raise ValueError("protected_dispatch_authority_changed")
    from .accepted_node import accepted_node_contract
    from .work_admission import resolve_accepted_repository

    contract = await accepted_node_contract(session, node)
    installation_id, repository_id, observed_name = await resolve_accepted_repository(session, node=node, contract=contract, repo=source["repo"])
    if (installation_id, repository_id, observed_name) != (source.get("installation_id"), source.get("provider_repository_id"), source.get("repo")):
        raise ValueError("protected_dispatch_authority_changed")
    if envelope.get("handoff_required"):
        from .execution_state import ExecutionIdentity, OutcomeKind
        from .execution_store import load_execution

        expected = envelope.get("handoff_expect", {})
        identity = ExecutionIdentity(
            **{key: expected[key] for key in ("org_id", "node_id", "cycle", "accepted_plan_version", "claim_id", "claim_generation")}
        )
        loaded = await load_execution(session, identity=identity)
        if (
            loaded is None
            or loaded.kind != OutcomeKind.APPLIED
            or loaded.record is None
            or loaded.record.id != expected.get("execution_id")
            or loaded.record.status in {"concluded", "superseded"}
        ):
            raise ValueError("protected_dispatch_execution_changed")
    user_id = await resolve_root_user_entity_id(session, node.org_id, genesis.root_human_id)
    if envelope.get("actor", {}).get("user_id") != user_id:
        raise ValueError("protected_dispatch_principal_changed")
    permit = await authorize_node_dispatch(
        session,
        node=node,
        principal_user_id=user_id,
        target_repository=source["repo"],
        installation_resolved=True,
        provider_repository_id=source.get("provider_repository_id"),
        expected_invocation_id=run_id,
        continuing_node=True,
        work_claim_issue=source.get("issue"),
    )
    if not permit.permitted:
        raise ValueError(f"protected_dispatch_replay_refused:{permit.reason}")
    return PendingPublish(
        node_id=node.id,
        org_id=node.org_id,
        envelope=envelope,
        genesis=genesis,
        node_attempt=node.attempts,
        node_kind=node.kind,
        group_id=message_group_id(org_id=node.org_id, node_id=node.id),
        deduplication_id=message_deduplication_id(node_id=node.id, decision_id=root, attempt=node.attempts),
    )
