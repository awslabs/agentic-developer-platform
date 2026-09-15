"""Commit the workflow transition before publishing a delegated worker.

DynamoDB first reserves the exact command and child budget. SQL then commits
the guarded node transition and an append-only receipt. Publication follows
that commit. A lost response retries the same reservation and receipt; a child
cannot bootstrap without the committed receipt and current node attempt.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.dispatch import GraphAssignment
from src.agentauth.engine import validate_engine_authority
from src.agentauth.policy import PolicyError
from src.agentauth.waves import load_issue_wave, successor_ready, validate_wave_coordinator, wave_key
from src.orchestration.dispatch import dispatch_node, graph_address
from src.orchestration.genesis import resolve_engine_genesis
from src.orchestration.models import DecisionKind, NodeKind, OrchestrationDecision, OrchestrationFlow, OrchestrationNode
from src.orchestration.state import ActorKind, NodeState


async def _lock_assignment(session, *, grant, issue):
    flow = (
        await session.execute(
            select(OrchestrationFlow)
            .where(OrchestrationFlow.org_id == grant.tenant_id, OrchestrationFlow.id == grant.flow_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if flow is None or flow.state not in {NodeState.PENDING.value, NodeState.READY.value, NodeState.RUNNING.value}:
        raise BootstrapRefusedError("workflow is no longer active")
    nodes = list(
        (
            await session.execute(
                select(OrchestrationNode)
                .where(
                    OrchestrationNode.org_id == grant.tenant_id,
                    OrchestrationNode.flow_id == grant.flow_id,
                    OrchestrationNode.issue_ref.in_([str(issue), f"#{issue}"]),
                    OrchestrationNode.kind == NodeKind.STORY.value,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    if len(nodes) != 1:
        raise BootstrapRefusedError("no unique eligible story assignment")
    return flow, nodes[0]


async def dispatch_graph(*, service, session_factory, body, credential_token, workload_binding, configured_repo):
    caller = service.policy.resolve_caller(credential_token)
    grant = await run_in_threadpool(
        service.store.live_grant, invocation_id=caller.invocation_id, tenant_id=caller.tenant_id, attempt=caller.attempt, now=service.now()
    )
    if not configured_repo or body.target.repo != configured_repo or configured_repo not in grant.repo_scope:
        raise BootstrapRefusedError("workflow repository is not configured for this dispatch")
    parent = await run_in_threadpool(service.store._read, f"TENANT#{caller.tenant_id}", f"EXEC#{caller.invocation_id}")
    request_key = envelope_digest({"principal": caller.principal, "request_id": body.request_id})
    receipt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"adp-graph-dispatch:{caller.tenant_id}:{request_key}"))
    intent = envelope_digest(body.model_dump())
    async with session_factory() as session:
        mapping = None
        coordinates = False
        if body.persona == "operations":
            loaded = await load_issue_wave(
                service=service, session=session, tenant_id=grant.tenant_id, flow_id=grant.flow_id, repo=body.target.repo, issue=body.target.issue
            )
            if loaded is None or not parent.get("coordinator_flow_id"):
                raise BootstrapRefusedError("no approved operations assignment")
            mapping, role, nodes, node = loaded
            if mapping["authority_reference_id"] != grant.authority.reference_id:
                raise BootstrapRefusedError("wave approval changed")
            flow = await session.get(OrchestrationFlow, grant.flow_id)
            coordinates = role == "orchestrator"
            if coordinates:
                receipt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"adp-wave-launch:{grant.tenant_id}:{mapping['receipt_id']}"))
        elif body.persona in {"developer", "reviewer"}:
            flow, node = await _lock_assignment(session, grant=grant, issue=body.target.issue)
        else:
            raise BootstrapRefusedError("unsupported workflow persona")
        await validate_engine_authority(session=session, execution=parent or {}, grant=grant, store=service.store)
        if parent.get("wave_coordinator") == {"BOOL": True}:
            own, _, own_nodes, own_eval = await validate_wave_coordinator(session=session, execution=parent, grant=grant, store=service.store)
            if coordinates:
                if mapping["receipt_id"] == own["receipt_id"] or not await successor_ready(
                    session, grant=grant, source_eval=own_eval, target_nodes=nodes
                ):
                    raise BootstrapRefusedError("successor wave is not eligible")
            elif node.id not in {n.id for n in own_nodes}:
                raise BootstrapRefusedError("dispatch is outside the assigned wave")
        if not parent.get("coordinator_flow_id") and (body.persona != "reviewer" or parent.get("orchestration_node_id") != {"S": node.id}):
            raise BootstrapRefusedError("dispatch is outside the assigned story")
        receipt = (
            await session.execute(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.id == receipt_id,
                    OrchestrationDecision.org_id == caller.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if receipt:
            metadata = json.loads(receipt.reason or "{}")
            if (
                receipt.kind != (DecisionKind.WAVE_COORDINATOR_DISPATCHED.value if coordinates else DecisionKind.AGENT_DISPATCHED.value)
                or receipt.actor_kind != ActorKind.SERVICE.value
                or receipt.actor_id != f"agent:{caller.principal}"
                or receipt.node_id != node.id
                or metadata.get("intent_digest") != intent
                or (not coordinates and metadata.get("node_attempt") != node.attempts)
                or (not coordinates and node.state != NodeState.RUNNING.value)
            ):
                raise PolicyError(409, "dispatch receipt no longer matches this workflow attempt")
            attempt = metadata["node_attempt"]
        else:
            if coordinates:
                if not any(n.kind == NodeKind.STORY.value and n.state == NodeState.READY.value for n in nodes):
                    raise BootstrapRefusedError("wave has no eligible work")
            elif node.state != (NodeState.RUNNING.value if body.persona == "reviewer" else NodeState.READY.value):
                raise BootstrapRefusedError("node is not eligible for this persona")
            attempt = node.attempts + (not coordinates and body.persona != "reviewer")
        graph = GraphAssignment(
            node.id,
            attempt,
            receipt_id,
            graph_address(node, flow_slug=flow.slug),
            wave_key(grant.flow_id, mapping["epic_ref"], mapping["wave_ref"]) if mapping else None,
            coordinates,
        )
        command, grant, caller = await run_in_threadpool(
            service.prepare, body=body, credential_token=credential_token, workload_binding=workload_binding, graph=graph
        )
        from src.orchestration.work_admission import admit_pending, enabled
        from src.orchestration.work_claims import WorkClaimError

        if enabled() and not receipt:
            try:
                await admit_pending(service.store, command["invocation_id"]["S"], session=session, allow_defer=True)
            except WorkClaimError as exc:
                await run_in_threadpool(service._refuse_unpublished, command, caller)
                raise PolicyError(409, f"work ownership refused: {exc.code}") from None
        if not receipt:
            if not coordinates and body.persona != "reviewer":
                genesis = await resolve_engine_genesis(session, org_id=grant.tenant_id, decision_id=grant.authority.reference_id)
                outcome = await dispatch_node(session, node.id, genesis)
                if not outcome.dispatched:
                    raise PolicyError(409, "workflow node was not dispatched")
            session.add(
                OrchestrationDecision(
                    id=receipt_id,
                    org_id=caller.tenant_id,
                    flow_id=flow.id,
                    node_id=node.id,
                    kind=DecisionKind.WAVE_COORDINATOR_DISPATCHED.value if coordinates else DecisionKind.AGENT_DISPATCHED.value,
                    actor_id=f"agent:{caller.principal}",
                    actor_kind=ActorKind.SERVICE.value,
                    actor_role=parent["persona"]["S"],
                    from_state=node.state if coordinates else (NodeState.RUNNING.value if body.persona == "reviewer" else NodeState.READY.value),
                    to_state=node.state if coordinates else NodeState.RUNNING.value,
                    reason=json.dumps(
                        {
                            "intent_digest": intent,
                            "node_attempt": attempt,
                            "invocation_id": command["invocation_id"]["S"],
                            "authority_reference_id": grant.authority.reference_id,
                            "mapping_digest": envelope_digest(mapping) if mapping else None,
                        },
                        sort_keys=True,
                    ),
                )
            )
            await session.commit()
    # Lock current workflow state again after commit. Cancellation cannot commit
    # between this validation and the queue send. Bootstrap revalidates on arrival.
    async with session_factory() as session:
        if mapping:
            await load_issue_wave(
                service=service, session=session, tenant_id=grant.tenant_id, flow_id=grant.flow_id, repo=body.target.repo, issue=body.target.issue
            )
        else:
            _, node = await _lock_assignment(session, grant=grant, issue=body.target.issue)
        execution = await run_in_threadpool(service.store._read, f"TENANT#{caller.tenant_id}", f"EXEC#{command['invocation_id']['S']}")
        await validate_engine_authority(session=session, execution=execution, grant=grant, store=service.store)
        command, grant, caller = await run_in_threadpool(
            service.prepare, body=body, credential_token=credential_token, workload_binding=workload_binding, graph=graph
        )
        return await run_in_threadpool(service._publish, command, grant, caller)
