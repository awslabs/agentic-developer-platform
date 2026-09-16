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
from src.orchestration.execution_policy import Action
from src.orchestration.genesis import resolve_engine_genesis
from src.orchestration.models import DecisionKind, NodeKind, OrchestrationDecision, OrchestrationFlow, OrchestrationNode
from src.orchestration.state import ActorKind, NodeState


def _claimed_issue(execution) -> int | None:
    """The issue an execution's work claim is held on, from its protected record.

    Engine-written (`issue_number` on the dispatch record), never request-supplied.
    `None` when unreadable, which leaves the claim lookup on its existing default
    rather than substituting a guess that could match another lane's claim.
    """
    try:
        return int((execution or {})["issue_number"]["N"])
    except (KeyError, TypeError, ValueError):
        return None


async def _authorize_dispatch_policy(*, session, service, command, grant, node, body, continuing, coordinates, parent=None, coordinator_node=None):
    """Admit this dispatch under the flow's accepted policy, or raise `PolicyError`.

    Three shapes reach here, and #5224 separates them deliberately:

    1. **An ordinary child dispatch** (`coordinates` False, no coordinator parent) —
       the node's own admission, unchanged.
    2. **Dispatching a coordinator** (`coordinates` True) — a wave launch. Admitted as
       `COORDINATE` at the wave's evaluation anchor, which is the address an accepted
       `CoordinationScope` names. Before this story every such request was refused
       outright with a blanket message, because the vocabulary had nothing to accept.
       A v1/v2 policy still refuses — now as a typed `action_not_permitted`, since
       absence of an accepted scope grants nothing.
    3. **A coordinator requesting a child** (`coordinates` False, `coordinator_node`
       present) — BOTH the coordinator's authority to ask (`authorize_child_request`,
       via `authorize_coordinator_child_request`) AND the child's own admission. The
       order matters: the coordinator check is a precondition, never a substitute. The
       child's own `authorize_node_dispatch` is what reserves budget and mints
       anything, so coordinator authority cannot stand in for a child's accepted
       action, live claim and bounded grant.
    """
    from src.orchestration.policy_admission import authorize_coordinator_child_request, authorize_node_dispatch, load_in_force_policy
    from src.shared.identity.resolver import UnresolvableUserEntityError, resolve_root_user_entity_id

    inputs = await load_in_force_policy(session, org_id=grant.tenant_id, flow_id=grant.flow_id)
    if inputs.refusal is not None:
        raise PolicyError(409, f"execution policy refused: {inputs.refusal.reason.value}")
    if inputs.policy is None:
        return
    execution = await run_in_threadpool(service.store._read, f"TENANT#{grant.tenant_id}", f"EXEC#{command['invocation_id']['S']}")
    if not execution:
        raise BootstrapRefusedError("policy dispatch binding unavailable")
    invocation = execution.get("work_claim_deferred_from", {}).get("S") or execution["invocation_id"]["S"]
    repository = execution.get("provider_repository_id", {}).get("N", "")
    try:
        principal = await resolve_root_user_entity_id(session, grant.tenant_id, grant.authority.human_id)
    except UnresolvableUserEntityError:
        raise PolicyError(409, "execution policy refused: membership_revoked") from None

    if coordinator_node is not None:
        # The requesting coordinator's own claim and repository, not the child's: the
        # child's are checked by its own admission below, and it does not own work yet.
        parent_repository = (parent or {}).get("provider_repository_id", {}).get("N", "")
        parent_invocation = (parent or {}).get("work_claim_deferred_from", {}).get("S") or (parent or {}).get("invocation_id", {}).get("S")
        coordinator = await authorize_coordinator_child_request(
            session,
            coordinator_node=coordinator_node,
            child_node=node,
            child_persona=body.persona,
            principal_user_id=principal,
            inputs=inputs,
            child_action_override=Action.REVIEW if body.persona == "reviewer" else None,
            continuing_child=continuing,
            expected_invocation_id=parent_invocation,
            provider_repository_id=int(parent_repository) if parent_repository.isdigit() else None,
            work_claim_issue=_claimed_issue(parent),
        )
        if not coordinator.permitted:
            raise PolicyError(409, f"execution policy refused: {coordinator.reason.value}")

    if coordinates:
        # A coordinator anchors to its wave's evaluation node but does not execute it —
        # the engine leaves that node `pending`, so `continuing_node` (which requires a
        # running node) would refuse every wave launch and its idempotent replay.
        decision = await authorize_node_dispatch(
            session,
            node=node,
            principal_user_id=principal,
            target_repository=body.target.repo,
            installation_resolved=int(execution.get("installation_id", {}).get("N", "0")) > 0,
            provider_repository_id=int(repository) if repository.isdigit() else None,
            expected_invocation_id=invocation,
            action_override=Action.COORDINATE,
            # The coordinator holds its claim on its launch issue; the evaluation
            # anchor's own `issue_ref` is a different issue (or unset). See
            # `resolve_authorization_context`'s `work_claim_issue`.
            work_claim_issue=_claimed_issue(execution),
        )
        if not decision.permitted:
            raise PolicyError(409, f"execution policy refused: {decision.reason.value}")
        return

    action = Action.REVIEW if body.persona == "reviewer" else Action.EVALUATE if body.persona == "operations" else None
    decision = await authorize_node_dispatch(
        session,
        node=node,
        principal_user_id=principal,
        target_repository=body.target.repo,
        installation_resolved=int(execution.get("installation_id", {}).get("N", "0")) > 0,
        provider_repository_id=int(repository) if repository.isdigit() else None,
        expected_invocation_id=invocation,
        action_override=action,
        continuing_node=continuing,
    )
    if not decision.permitted:
        raise PolicyError(409, f"execution policy refused: {decision.reason.value}")


async def _coordinator_anchor(session, *, grant, node_id):
    """Re-read the coordinator's assigned anchor in the current session.

    The second authorization pass runs in a fresh session, so the node object from
    the first pass is detached. Re-reading by the id already validated against the
    coordinator's committed dispatch receipt keeps the tenant/flow scoping explicit,
    and a vanished anchor refuses rather than silently skipping the coordinator check.
    """
    if node_id is None:
        return None
    anchor = await session.scalar(
        select(OrchestrationNode).where(
            OrchestrationNode.id == node_id,
            OrchestrationNode.org_id == grant.tenant_id,
            OrchestrationNode.flow_id == grant.flow_id,
        )
    )
    if anchor is None:
        raise BootstrapRefusedError("coordinator assignment is unavailable")
    return anchor


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
        coordinator_anchor_id = None
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
            elif body.persona in {"developer", "reviewer"}:
                # A live coordinator is asking for a child inside its own wave. Its
                # accepted coordination scope names the evaluation anchor this receipt
                # already binds, so policy admission checks the coordinator's authority
                # to ask at that address in addition to the child's own admission.
                #
                # Restricted to the two `ChildPersona` members deliberately. The other
                # dispatch a coordinator makes inside its own wave is its wave's
                # EVALUATION, and that is not a delegated child request: it is admitted
                # on its own `EVALUATE` action and additionally requires the address to
                # be marked for machine acceptance. Routing it through the coordination
                # scope would both refuse it as an unrecognised child persona and, worse,
                # imply coordination authority is what governs concluding an evaluation.
                coordinator_anchor_id = own_eval.id
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
        try:
            await _authorize_dispatch_policy(
                session=session,
                service=service,
                command=command,
                grant=grant,
                node=node,
                body=body,
                continuing=bool(receipt) or body.persona == "reviewer",
                coordinates=coordinates,
                parent=parent,
                coordinator_node=await _coordinator_anchor(session, grant=grant, node_id=coordinator_anchor_id),
            )
        except PolicyError:
            if not receipt:
                await run_in_threadpool(service._refuse_unpublished, command, caller)
            raise
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
        await _authorize_dispatch_policy(
            session=session,
            service=service,
            command=command,
            grant=grant,
            node=node,
            body=body,
            continuing=True,
            coordinates=coordinates,
            parent=parent,
            coordinator_node=await _coordinator_anchor(session, grant=grant, node_id=coordinator_anchor_id),
        )
        return await run_in_threadpool(service._publish, command, grant, caller)
