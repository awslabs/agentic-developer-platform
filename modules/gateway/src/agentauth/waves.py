"""Gateway-owned issue bindings for already approved delivery waves.

Bindings cannot create approval, nodes, edges or transitions. A committed SQL
receipt and a snapshot of node IDs prevent uncommitted or widened assignments.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.agentauth.bootstrap import BootstrapRefusedError, _key, envelope_digest
from src.agentauth.grants import AgentAction
from src.agentauth.policy import PolicyError
from src.orchestration.models import DecisionKind, NodeKind, OrchestrationDecision, OrchestrationFlow, OrchestrationNode
from src.orchestration.state import NodeState


class WaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    epic_ref: str = Field(pattern=r"^epic-[1-9][0-9]*$")
    wave_ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    orchestrator_issue: int = Field(gt=0, strict=True)
    evaluation_issue: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def distinct_issues(self):
        if self.orchestrator_issue == self.evaluation_issue:
            raise ValueError("orchestrator and evaluation require different issues")
        return self


def wave_key(flow_id, epic_ref, wave_ref):
    return "WAVE#" + envelope_digest({"flow": flow_id, "epic": epic_ref, "wave": wave_ref})


def issue_key(repo, issue):
    return f"WAVEISSUE#{repo}#{issue}"


async def verify_materialized_issues(*, tenant_id, installation_id, body):
    """Read native GitHub parent relationships with a tenant-scoped App token."""
    from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

    try:
        app_id, key = await resolve_tenant_app_credentials(tenant_id)
        token, _ = await mint_installation_token_with_expiry(
            app_id, key, installation_id, repositories=[body.repo.split("/", 1)[1]], permissions={"issues": "read", "metadata": "read"}
        )
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            timeout=10,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        ) as http:
            for issue in (body.orchestrator_issue, body.evaluation_issue):
                response = await http.get(f"/repos/{body.repo}/issues/{issue}/parent")
                response.raise_for_status()
                parent = response.json()
                if (
                    parent.get("number") != int(body.epic_ref.removeprefix("epic-"))
                    or parent.get("repository_url") != f"https://api.github.com/repos/{body.repo}"
                ):
                    raise BootstrapRefusedError("delivery issue is outside the approved epic")
    except BootstrapRefusedError:
        raise
    except Exception:
        raise BootstrapRefusedError("delivery issue membership could not be verified") from None


async def lock_wave(session, *, tenant_id, flow_id, epic_ref, wave_ref):
    flow = (
        await session.execute(
            select(OrchestrationFlow)
            .where(OrchestrationFlow.id == flow_id, OrchestrationFlow.org_id == tenant_id)
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
                    OrchestrationNode.org_id == tenant_id,
                    OrchestrationNode.flow_id == flow_id,
                    OrchestrationNode.epic_ref == epic_ref,
                    OrchestrationNode.wave_ref == wave_ref,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    stories = [node for node in nodes if node.kind == NodeKind.STORY.value]
    evaluations = [node for node in nodes if node.kind == NodeKind.EVAL.value]
    if not stories or len(evaluations) != 1 or any(node.state == NodeState.SUPERSEDED.value for node in nodes):
        raise BootstrapRefusedError("no unique approved delivery wave")
    return flow, nodes, evaluations[0]


def _reserve_mapping(service, *, mapping, caller, grant):
    pk = f"TENANT#{caller.tenant_id}"
    key = wave_key(grant.flow_id, mapping["epic_ref"], mapping["wave_ref"])
    item = {**_key(pk, key), "mapping_json": {"S": json.dumps(mapping, sort_keys=True)}, "digest": {"S": envelope_digest(mapping)}}
    aliases = [
        {**_key(pk, issue_key(mapping["repo"], mapping[role + "_issue"])), "wave_key": {"S": key}, "role": {"S": role}, "digest": item["digest"]}
        for role in ("orchestrator", "evaluation")
    ]
    transaction = [service.store._put(row) for row in [item, *aliases]] + [
        service.store._grant_check(grant, service.now()),
        service.store._authority_check(grant),
        {
            "ConditionCheck": {
                "TableName": service.store.table,
                "Key": _key(pk, f"EXEC#{caller.invocation_id}"),
                "ConditionExpression": (
                    "#s = :active AND current_attempt = :attempt AND current_credential_epoch = :epoch "
                    "AND workload_binding = :binding AND coordinator_flow_id = :flow"
                ),
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {
                    ":active": {"S": "active"},
                    ":attempt": {"N": str(caller.attempt)},
                    ":epoch": {"N": str(caller.credential_epoch)},
                    ":binding": {"S": mapping["creator_binding"]},
                    ":flow": {"S": grant.flow_id},
                },
            }
        },
    ]
    try:
        service.store.client.transact_write_items(TransactItems=transaction)
    except (ClientError, BotoCoreError):
        if any(service.store._read(pk, row["sk"]["S"]) != row for row in [item, *aliases]):
            raise PolicyError(409, "wave binding conflicts or could not be confirmed") from None


async def register_wave(*, service, session_factory, body, credential_token, workload_binding, configured_repo, verifier=None):
    from src.agentauth.engine import validate_engine_authority

    caller = service.policy.resolve_caller(credential_token)
    authorized = await run_in_threadpool(
        service.policy.authorize,
        credential_token=credential_token,
        action=AgentAction.DISPATCH,
        target_run_id=caller.invocation_id,
        presented_workload_binding=workload_binding,
        dispatch_replay=True,
    )
    grant = authorized.grant
    parent = await run_in_threadpool(service.store._read, f"TENANT#{caller.tenant_id}", f"EXEC#{caller.invocation_id}")
    if (
        not parent
        or grant.authority.kind != "gate_decision"
        or parent.get("parent_principal")
        or parent.get("coordinator_flow_id") != {"S": grant.flow_id}
        or body.repo != configured_repo
        or body.repo not in grant.repo_scope
    ):
        raise BootstrapRefusedError("wave binding requires the approved flow coordinator")
    current = await run_in_threadpool(
        service.store.live_grant, invocation_id=caller.invocation_id, tenant_id=caller.tenant_id, attempt=caller.attempt, now=service.now()
    )
    if current != grant:
        raise BootstrapRefusedError("coordinator authority changed")
    async with session_factory() as session:
        flow, nodes, evaluation = await lock_wave(
            session, tenant_id=caller.tenant_id, flow_id=grant.flow_id, epic_ref=body.epic_ref, wave_ref=body.wave_ref
        )
        await validate_engine_authority(session=session, execution=parent, grant=grant, store=service.store)
        numbers = {str(body.orchestrator_issue), str(body.evaluation_issue)}
        refs = numbers | {"#" + number for number in numbers}
        conflicts = list(
            (
                await session.execute(
                    select(OrchestrationNode).where(OrchestrationNode.org_id == caller.tenant_id, OrchestrationNode.issue_ref.in_(refs))
                )
            ).scalars()
        )
        intent = (
            await session.execute(
                select(OrchestrationFlow.id).where(OrchestrationFlow.org_id == caller.tenant_id, OrchestrationFlow.intent_ref.in_(refs))
            )
        ).first()
        if intent or any(
            node.id != evaluation.id or node.issue_ref not in {str(body.evaluation_issue), f"#{body.evaluation_issue}"} for node in conflicts
        ):
            raise BootstrapRefusedError("delivery issue is already assigned to different work")
        if evaluation.issue_ref and evaluation.issue_ref not in {str(body.evaluation_issue), f"#{body.evaluation_issue}"}:
            raise BootstrapRefusedError("evaluation already has a different issue")
        await (verifier or verify_materialized_issues)(tenant_id=caller.tenant_id, installation_id=int(parent["installation_id"]["N"]), body=body)
        mapping = {
            **body.model_dump(),
            "flow_id": flow.id,
            "tenant_id": caller.tenant_id,
            "node_ids": sorted(node.id for node in nodes),
            "node_snapshot": node_snapshot(nodes),
            "evaluation_node_id": evaluation.id,
            "creator": caller.principal,
            "creator_binding": workload_binding,
            "authority_reference_id": grant.authority.reference_id,
        }
        receipt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"adp-wave:{caller.tenant_id}:{wave_key(flow.id, body.epic_ref, body.wave_ref)}"))
        mapping["receipt_id"] = receipt_id
        await run_in_threadpool(_reserve_mapping, service, mapping=mapping, caller=caller, grant=grant)
        # Lost transaction replies cannot stand in for current authority.
        await run_in_threadpool(
            service.policy.authorize,
            credential_token=credential_token,
            action=AgentAction.DISPATCH,
            target_run_id=caller.invocation_id,
            presented_workload_binding=workload_binding,
            dispatch_replay=True,
        )
        live = await run_in_threadpool(
            service.store.live_grant, invocation_id=caller.invocation_id, tenant_id=caller.tenant_id, attempt=caller.attempt, now=service.now()
        )
        if live != grant:
            raise BootstrapRefusedError("coordinator authority changed")
        receipt = await session.get(OrchestrationDecision, receipt_id)
        if receipt is None:
            session.add(
                OrchestrationDecision(
                    id=receipt_id,
                    org_id=caller.tenant_id,
                    flow_id=flow.id,
                    node_id=evaluation.id,
                    kind=DecisionKind.WAVE_MATERIALIZED.value,
                    actor_id=f"agent:{caller.principal}",
                    actor_kind="service",
                    actor_role=parent["persona"]["S"],
                    reason=json.dumps({"mapping_digest": envelope_digest(mapping)}),
                )
            )
            await session.commit()
        elif receipt.reason != json.dumps({"mapping_digest": envelope_digest(mapping)}) or receipt.actor_id != f"agent:{caller.principal}":
            raise PolicyError(409, "wave materialization receipt conflicts")
    return {
        "status": "registered",
        "flow_id": flow.id,
        "epic_ref": body.epic_ref,
        "wave_ref": body.wave_ref,
        "orchestrator_issue": body.orchestrator_issue,
        "evaluation_issue": body.evaluation_issue,
    }


async def load_issue_wave(*, service, session, tenant_id, flow_id, repo, issue):
    alias = await run_in_threadpool(service.store._read, f"TENANT#{tenant_id}", issue_key(repo, issue))
    if not alias:
        return None
    try:
        row = await run_in_threadpool(service.store._read, f"TENANT#{tenant_id}", alias["wave_key"]["S"])
        mapping = json.loads(row["mapping_json"]["S"])
        digest = envelope_digest(mapping)
        if (
            mapping["tenant_id"] != tenant_id
            or mapping["flow_id"] != flow_id
            or mapping["repo"] != repo
            or row["digest"] != {"S": digest}
            or alias["digest"] != row["digest"]
            or mapping[alias["role"]["S"] + "_issue"] != issue
        ):
            raise ValueError("scope mismatch")
        receipt = await session.get(OrchestrationDecision, mapping["receipt_id"])
        if (
            receipt is None
            or receipt.org_id != tenant_id
            or receipt.flow_id != flow_id
            or receipt.kind != DecisionKind.WAVE_MATERIALIZED.value
            or receipt.actor_kind != "service"
            or receipt.actor_id != f"agent:{mapping['creator']}"
            or receipt.reason != json.dumps({"mapping_digest": digest})
        ):
            raise ValueError("uncommitted mapping")
        _, nodes, evaluation = await lock_wave(
            session, tenant_id=tenant_id, flow_id=flow_id, epic_ref=mapping["epic_ref"], wave_ref=mapping["wave_ref"]
        )
        if node_snapshot(nodes) != mapping["node_snapshot"] or evaluation.id != mapping["evaluation_node_id"]:
            raise ValueError("wave changed")
        return mapping, alias["role"]["S"], nodes, evaluation
    except (KeyError, ValueError, TypeError):
        raise BootstrapRefusedError("wave assignment is unavailable") from None


def node_snapshot(nodes):
    return sorted([node.id, node.kind, node.epic_ref, node.wave_ref, node.node_ref, node.issue_ref] for node in nodes)


async def validate_wave_coordinator(*, session, execution, grant, store):
    if store is None or execution.get("persona") != {"S": "operations"} or not execution.get("parent_principal"):
        raise BootstrapRefusedError("wave coordinator is not assigned")
    loaded = await load_issue_wave(
        service=SimpleNamespace(store=store),
        session=session,
        tenant_id=grant.tenant_id,
        flow_id=grant.flow_id,
        repo=execution["repo"]["S"],
        issue=int(execution["issue_number"]["N"]),
    )
    if loaded is None:
        raise BootstrapRefusedError("wave binding is missing")
    mapping, role, _, evaluation = loaded
    receipt = await session.get(OrchestrationDecision, execution["orchestration_dispatch_receipt"]["S"])
    metadata = json.loads(receipt.reason or "{}") if receipt else {}
    if (
        role != "orchestrator"
        or execution.get("coordinator_flow_id") != {"S": grant.flow_id}
        or execution.get("wave_key") != {"S": wave_key(grant.flow_id, mapping["epic_ref"], mapping["wave_ref"])}
        or mapping["authority_reference_id"] != grant.authority.reference_id
        or evaluation.state in {NodeState.HALTED.value, NodeState.FAILED.value, NodeState.SUPERSEDED.value}
        or receipt is None
        or receipt.org_id != grant.tenant_id
        or receipt.flow_id != grant.flow_id
        or receipt.node_id != evaluation.id
        or receipt.kind != DecisionKind.WAVE_COORDINATOR_DISPATCHED.value
        or receipt.actor_kind != "service"
        or receipt.actor_id != f"agent:{execution['parent_principal']['S']}"
        or metadata.get("invocation_id") != execution["invocation_id"]["S"]
        or metadata.get("authority_reference_id") != grant.authority.reference_id
        or metadata.get("mapping_digest") != envelope_digest(mapping)
    ):
        raise BootstrapRefusedError("wave coordinator dispatch is not committed")
    return loaded


async def successor_ready(session, *, grant, source_eval, target_nodes):
    """Only approved outgoing dependencies, including passed human gates."""
    from src.orchestration.models import OrchestrationEdge

    if source_eval.state != NodeState.PASSED.value:
        return False
    ready = {node.id for node in target_nodes if node.kind == NodeKind.STORY.value and node.state == NodeState.READY.value}
    frontier, visited = {source_eval.id}, set()
    while frontier:
        visited.update(frontier)
        successors = list(
            (
                await session.execute(
                    select(OrchestrationNode)
                    .join(OrchestrationEdge, OrchestrationEdge.to_node_id == OrchestrationNode.id)
                    .where(
                        OrchestrationEdge.org_id == grant.tenant_id,
                        OrchestrationEdge.flow_id == grant.flow_id,
                        OrchestrationEdge.from_node_id.in_(frontier),
                        OrchestrationNode.org_id == grant.tenant_id,
                        OrchestrationNode.flow_id == grant.flow_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        if any(node.id in ready for node in successors):
            return True
        frontier = {
            node.id for node in successors if node.id not in visited and node.kind == NodeKind.GATE.value and node.state == NodeState.PASSED.value
        }
    return False
