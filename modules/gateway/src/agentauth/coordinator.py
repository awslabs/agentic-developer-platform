"""Bind a human-launched Operations run to its specifically approved intent flow.

The worker supplies no flow or approval claims. Its protected launch issue and
repository must match an accepted flow reference and a real human gate approval.
This grants explicit FLOW_NODE scope, not shared-root scope.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select

from src.agentauth.bootstrap import BootstrapRefusedError, BootstrapStore, _iso, _key
from src.agentauth.engine import ensure_engine_authority
from src.agentauth.grants import AUTHORITY_GITHUB_EVENT, AgentAction, AuthorityReference, TargetRelationship
from src.agentauth.store import AuthorityStoreError
from src.orchestration.dispatch_pass import _latest_approval_decision_id
from src.orchestration.genesis import EngineGenesis, resolve_engine_genesis
from src.orchestration.models import OrchestrationFlow, OrchestrationNode
from src.orchestration.state import NodeState


@dataclass(frozen=True)
class CoordinatorAssignment:
    genesis: EngineGenesis
    repo: str
    intent_issue: int


async def resolve_repository_fan_out(*, session, execution, grant, target_repo: str, target_issue: int) -> bool:
    """May this root coordinator dispatch to `target_issue` in its own repo? (#5365)

    The capability marker on the grant (written only by the HMAC-verified webhook
    path) says the *coordinator* is allowed repository fan-out. This resolves the
    remaining question the marker cannot answer: whether this particular target is
    work the orchestration graph owns.

    Both refusals below protect the approval gate, and both were found by
    measurement rather than argument. Accepting a dispatch here creates no
    orchestration receipt and moves no node, so this is not a way to *advance* a
    flow — but without these two checks it would still be a way to start an agent
    on graph-governed work without the approval that governs it:

    * **The target must not be a node of any flow.** A node's issue is sequenced
      by the graph; starting a second agent on it out-of-band is the double
      dispatch the graph exists to prevent.
    * **The coordinator's own launch issue must not be any flow's intent.** A
      flow approval does not mutate an already-issued ``github_event`` grant.
      Letting that stale grant widen after approval would therefore bypass
      ``dispatch_graph``. The coordinator must re-enrol so
      ``resolve_coordinator_assignment`` can bind it to ``gate_decision`` and
      keep every later dispatch on the graph path.

    Ownership is resolved from SQL and server-written execution state. The requested target is
    checked again against the signed repository scope by the dispatch gate.
    """
    try:
        launch_issue = int(execution["issue_number"]["N"])
    except (KeyError, TypeError, ValueError):
        return False
    matching_nodes = (
        await session.scalars(
            select(OrchestrationNode)
            .join(OrchestrationFlow, OrchestrationFlow.id == OrchestrationNode.flow_id)
            .where(
                OrchestrationFlow.org_id == grant.tenant_id,
                OrchestrationNode.issue_ref.in_([str(target_issue), f"#{target_issue}"]),
            )
            .limit(257)
        )
    ).all()
    if len(matching_nodes) > 256:
        return False
    from src.orchestration.accepted_node import accepted_node_contract

    for node in matching_nodes:
        contract = await accepted_node_contract(session, node)
        if (
            contract is None
            or contract.node.repository_identity != node.repository_identity
            or contract.node.provider_repository_id != node.provider_repository_id
            or (contract.node.repository_connection.model_dump(mode="json") if contract.node.repository_connection else None)
            != node.repository_connection
            or not node.provider_repository_id
            or not node.repository_identity
            or node.repository_identity.casefold() == target_repo.casefold()
        ):
            return False
    flow_owned_launch = await session.scalar(
        select(OrchestrationFlow.id)
        .where(
            OrchestrationFlow.org_id == grant.tenant_id,
            OrchestrationFlow.intent_ref.in_([str(launch_issue), f"#{launch_issue}"]),
        )
        .limit(1)
    )
    return not bool(flow_owned_launch)


async def resolve_coordinator_assignment(*, session, execution, grant) -> CoordinatorAssignment | None:
    if (
        execution.get("persona", {}).get("S") not in {"operations", "aidlc"}
        or execution.get("parent_principal")
        or grant.authority.kind != AUTHORITY_GITHUB_EVENT
        or AgentAction.DISPATCH not in grant.allowed_actions
    ):
        return None
    repo = execution.get("repo", {}).get("S")
    if not repo or repo not in grant.repo_scope:
        return None
    try:
        issue = int(execution["issue_number"]["N"])
    except (KeyError, ValueError, TypeError):
        raise BootstrapRefusedError("coordinator launch is unavailable") from None
    flows = list(
        (
            await session.execute(
                select(OrchestrationFlow)
                .where(
                    OrchestrationFlow.org_id == grant.tenant_id,
                    OrchestrationFlow.intent_ref.in_([str(issue), f"#{issue}"]),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    if not flows:
        return None  # Ordinary issue-scoped Operations keeps its existing grant.
    if len(flows) != 1 or flows[0].state not in {NodeState.PENDING.value, NodeState.READY.value, NodeState.RUNNING.value}:
        raise BootstrapRefusedError("coordinator flow is unavailable")
    flow = flows[0]
    decision = await _latest_approval_decision_id(session, org_id=grant.tenant_id, flow_id=flow.id)
    if not decision:
        return None  # No approval means no widening beyond the launch issue.
    from src.orchestration.proposal import PlanningIssueReference
    from src.orchestration.repository import OrchestrationRepository

    plan = await OrchestrationRepository(session).get_accepted_plan(org_id=grant.tenant_id, flow_id=flow.id)
    if plan is None or flow.origin_ref != plan.plan_document.get("origin_ref") or flow.tracker_refs != plan.plan_document.get("tracker_refs"):
        return None
    references = [plan.plan_document.get("origin_ref")]
    references.extend(item.get("issue") for item in plan.plan_document.get("tracker_refs") or [])
    try:
        observed_id = execution.get("provider_repository_id", {}).get("N")
        repository_id = int(observed_id) if observed_id is not None else None
        installation_id = int(execution["installation_id"]["N"])
    except (KeyError, ValueError, TypeError):
        return None
    for raw in references:
        try:
            reference = PlanningIssueReference.model_validate(raw)
        except ValueError:
            continue
        if (
            reference.issue_number == issue
            and reference.repository.casefold() == repo.casefold()
            and (repository_id is None or reference.repository_id == repository_id)
            and reference.installation_id == installation_id
        ):
            break
    else:
        return None
    genesis = await resolve_engine_genesis(session, org_id=grant.tenant_id, decision_id=decision)
    if genesis.flow_id != flow.id:
        raise BootstrapRefusedError("coordinator approval does not name this flow")
    return CoordinatorAssignment(genesis=genesis, repo=repo, intent_issue=issue)


def bind_coordinator(*, store: BootstrapStore, record, grant, assignment: CoordinatorAssignment, now=None) -> None:
    now = now or datetime.now(UTC)
    if (
        record.tenant_id != assignment.genesis.org_id
        or grant.tenant_id != record.tenant_id
        or grant.principal != record.principal
        or grant.authority.kind != AUTHORITY_GITHUB_EVENT
        or not record.workload_binding
        or assignment.repo not in grant.repo_scope
    ):
        raise BootstrapRefusedError("coordinator assignment refused")
    expiry, _ = ensure_engine_authority(store=store, genesis=assignment.genesis, now=now)
    if grant.expires_at is None:
        raise BootstrapRefusedError("coordinator launch has no expiry")
    expiry = min(expiry, grant.expires_at)
    updated = replace(
        grant,
        authority=AuthorityReference("gate_decision", assignment.genesis.decision_id, assignment.genesis.root_human_id, grant.tenant_id),
        flow_id=assignment.genesis.flow_id,
        target_relationships=grant.target_relationships | {TargetRelationship.FLOW_NODE},
        expires_at=expiry,
        revocation_epoch=grant.revocation_epoch + 1,
    )
    pk = f"TENANT#{record.tenant_id}"
    raw_grant = store._read(pk, f"GRANT#{record.principal}") or {}
    if store.authority.load_grant(principal=record.principal, tenant_id=record.tenant_id) != grant:
        raise BootstrapRefusedError("coordinator launch changed")
    new_grant = {
        **raw_grant,
        **store._grant_item(updated),
        "launch_authority_reference_id": {"S": grant.authority.reference_id},
        "launch_authority_human_id": {"S": grant.authority.human_id},
        "launch_authority_kind": {"S": grant.authority.kind},
        "coordinator_flow_id": {"S": assignment.genesis.flow_id},
    }
    # Compare the complete old grant, including ceilings and dispatch metadata.
    # A racing reduction must never be overwritten by this scope assignment.
    names, values, conditions = {}, {}, []
    for index, (name, value) in enumerate(sorted(raw_grant.items())):
        if name in {"pk", "sk"}:
            continue
        names[f"#g{index}"], values[f":g{index}"] = name, value
        conditions.append(f"#g{index} = :g{index}")
    transaction = [
        {
            "Update": {
                "TableName": store.table,
                "Key": _key(pk, f"EXEC#{record.invocation_id}"),
                "UpdateExpression": "SET flow_id = :flow, coordinator_flow_id = :flow, coordinator_intent_issue = :issue",
                "ConditionExpression": (
                    "#s = :active AND current_attempt = :attempt AND current_credential_epoch = :epoch "
                    "AND workload_binding = :pod AND attribute_not_exists(coordinator_flow_id) "
                    "AND attribute_not_exists(parent_principal) AND repo = :repo AND issue_number = :issue "
                    "AND persona IN (:operations, :aidlc)"
                ),
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {
                    ":active": {"S": "active"},
                    ":attempt": {"N": str(record.current_attempt)},
                    ":epoch": {"N": str(record.current_credential_epoch)},
                    ":pod": {"S": record.workload_binding},
                    ":flow": {"S": assignment.genesis.flow_id},
                    ":issue": {"N": str(assignment.intent_issue)},
                    ":repo": {"S": assignment.repo},
                    ":operations": {"S": "operations"},
                    ":aidlc": {"S": "aidlc"},
                },
            }
        },
        {
            "Put": {
                "TableName": store.table,
                "Item": new_grant,
                "ConditionExpression": " AND ".join(conditions) + " AND expires_at > :now AND revoked = :false",
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": {**values, ":now": {"S": _iso(now)}, ":false": {"BOOL": False}},
            }
        },
        {
            "ConditionCheck": {
                "TableName": store.table,
                "Key": _key(pk, f"RESV#{grant.grant_id}"),
                "ConditionExpression": "attribute_not_exists(total_dispatched) OR total_dispatched = :zero",
                "ExpressionAttributeValues": {":zero": {"N": "0"}},
            }
        },
        store._authority_check(grant),
        store._authority_check(updated),
    ]
    try:
        store.client.transact_write_items(TransactItems=transaction)
    except (ClientError, BotoCoreError):
        current = store.authority.load_grant(principal=record.principal, tenant_id=record.tenant_id)
        execution = store._read(pk, f"EXEC#{record.invocation_id}") or {}
        if (
            current == updated
            and execution.get("coordinator_flow_id") == {"S": assignment.genesis.flow_id}
            and execution.get("current_attempt") == {"N": str(record.current_attempt)}
            and execution.get("current_credential_epoch") == {"N": str(record.current_credential_epoch)}
            and execution.get("workload_binding") == {"S": record.workload_binding}
            and execution.get("status") == {"S": "active"}
        ):
            store.live_grant(invocation_id=record.invocation_id, tenant_id=record.tenant_id, attempt=record.current_attempt, now=now)
            return
        raise AuthorityStoreError("coordinator assignment was not committed") from None
