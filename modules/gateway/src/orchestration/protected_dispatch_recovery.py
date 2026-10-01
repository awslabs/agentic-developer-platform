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
        try:
            decision = await session.scalar(
                select(OrchestrationDecision)
                .where(
                    OrchestrationDecision.org_id == node.org_id,
                    OrchestrationDecision.node_id == node.id,
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
                pending_ids.add(node.id)
            if len(report.pending) >= config.max_dispatches_per_tick:
                break
        except Exception:
            logger.exception("Protected dispatch replay refused node=%s", node.id)
            report.record(node.org_id, "publish_failed")


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
        resolve_installation_id,
    )
    from .executor_assignment import accepted_executor_persona
    from .flow_execution import flow_is_paused
    from .genesis import resolve_engine_genesis
    from .policy_admission import authorize_node_dispatch
    from .tick import _predecessor_states, _unsatisfied

    if await flow_is_paused(session, org_id=node.org_id, flow_id=node.flow_id, lock=True):
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
        or source.get("repo") != config.repo
        or source.get("issue") != issue_number_for_dispatch(node.issue_ref)
        or source.get("installation_id") != dispatch.get("installation_id")
        or source.get("provider_repository_id") != dispatch.get("provider_repository_id")
        or await _latest_approval_decision_id(session, org_id=node.org_id, flow_id=node.flow_id) != root
    ):
        raise ValueError("protected_dispatch_assignment_changed")
    persona = await accepted_executor_persona(session, node, default=config.persona)
    if node.kind == "story" and envelope.get("persona") != persona:
        raise ValueError("protected_dispatch_persona_changed")
    raw = await run_in_threadpool(store._read, f"TENANT#{node.org_id}", f"EXEC#{run_id}")
    if raw:
        if raw.get("envelope_digest") != {"S": envelope_digest(envelope)}:
            raise ValueError("protected_dispatch_digest_changed")
        # Atomic bootstrap forbids a second pod binding. Never restart a bound,
        # terminal, revoked or cancelled invocation, even if queue ack was lost.
        if raw.get("status") != {"S": "pending"} or raw.get("workload_binding"):
            return None
    if _unsatisfied(await _predecessor_states(session, org_id=node.org_id, node_id=node.id)):
        return None
    genesis = await resolve_engine_genesis(session, org_id=node.org_id, decision_id=root)
    if genesis.flow_id != node.flow_id or await resolve_installation_id(session, org_id=node.org_id) != source.get("installation_id"):
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
        target_repository=config.repo,
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
