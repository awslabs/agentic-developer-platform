"""Trusted engine dispatch and live PostgreSQL flow validation for agent requests."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select

from src.agentauth.bootstrap import BootstrapRefusedError, BootstrapStore, _iso, _key
from src.agentauth.grants import AgentAction, AuthorityReference, DelegatedGrant, TargetRelationship
from src.orchestration.genesis import EngineGenesis, resolve_engine_genesis
from src.orchestration.models import DecisionKind, NodeKind, OrchestrationDecision, OrchestrationFlow, OrchestrationNode
from src.orchestration.run_store import EngineRunStore
from src.orchestration.state import NodeState


def ensure_engine_authority(*, store: BootstrapStore, genesis: EngineGenesis, now: datetime) -> tuple[datetime, str]:
    pk, sk = f"TENANT#{genesis.org_id}", f"AUTHORITY#{genesis.decision_id}"
    authority = store._read(pk, sk)
    if authority is None:
        item = {
            **_key(pk, sk),
            "status": {"S": "active"},
            "authority_kind": {"S": "gate_decision"},
            "human_id": {"S": genesis.root_human_id},
            "flow_id": {"S": genesis.flow_id},
            "created_at": {"S": _iso(now)},
            "expires_at": {"S": _iso(now + timedelta(days=7))},
        }
        try:
            store.client.put_item(TableName=store.table, Item=item, ConditionExpression="attribute_not_exists(pk)")
        except (ClientError, BotoCoreError):
            pass  # A lost response/race must resolve to the same live authority.
        authority = store._read(pk, sk)
    if (
        not authority
        or authority.get("human_id") != {"S": genesis.root_human_id}
        or authority.get("flow_id") != {"S": genesis.flow_id}
        or authority.get("status") != {"S": "active"}
        or authority.get("authority_kind") != {"S": "gate_decision"}
    ):
        raise BootstrapRefusedError("engine authority refused")
    try:
        expiry = datetime.strptime(authority["expires_at"]["S"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        created_at = authority["created_at"]["S"]
        datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError):
        raise BootstrapRefusedError("engine authority unavailable") from None
    if expiry <= now:
        raise BootstrapRefusedError("engine authority expired")
    return expiry, created_at


class EngineAuthorityWriter:
    def __init__(self, *, store: BootstrapStore, events_table: str):
        self.store, self.events_table = store, events_table

    def provision(self, pending) -> dict:
        """Only the committed engine dispatch pass supplies this trusted context."""
        from src.orchestration.dispatch_pass import attempt_run_id

        genesis = pending.genesis
        if (
            not isinstance(genesis, EngineGenesis)
            or genesis.org_id != pending.org_id
            or pending.envelope.get("tenant_id") != genesis.org_id
            or pending.node_attempt < 1
            or not self.events_table
        ):
            raise BootstrapRefusedError("verified engine genesis required")
        invocation = attempt_run_id(pending.node_id, pending.node_attempt)
        envelope = dict(pending.envelope)
        graph = envelope.get("orchestration", {})
        if (
            envelope.get("message_id") != invocation
            or graph.get("node_id") != pending.node_id
            or graph.get("attempt") != pending.node_attempt
            or graph.get("flow_id") != genesis.flow_id
            or graph.get("root_decision_id") != genesis.decision_id
        ):
            raise BootstrapRefusedError("engine dispatch identity mismatch")
        persona = envelope["persona"]
        allowed = {"operations"} if pending.node_kind == NodeKind.EVAL.value else {"developer", "reviewer"}
        if pending.node_kind not in {NodeKind.STORY.value, NodeKind.EVAL.value} or persona not in allowed:
            raise BootstrapRefusedError("unsupported engine persona")
        now = datetime.now(UTC)
        expiry, _ = ensure_engine_authority(store=self.store, genesis=genesis, now=now)
        source = envelope["source_ref"]
        actions = {AgentAction.MONITOR, AgentAction.DISPATCH} if persona == "developer" else {AgentAction.MONITOR}
        grant = DelegatedGrant(
            grant_id=f"grant:{invocation}:1",
            tenant_id=genesis.org_id,
            principal=f"{invocation}#1",
            authority=AuthorityReference("gate_decision", genesis.decision_id, genesis.root_human_id, genesis.org_id),
            allowed_actions=frozenset(actions),
            delegable_actions=frozenset({AgentAction.MONITOR}),
            target_relationships=frozenset({TargetRelationship.SELF, TargetRelationship.DESCENDANT}),
            flow_id=genesis.flow_id,
            repo_scope=frozenset({source["repo"]}),
            expires_at=expiry,
            max_dispatch_concurrency=1,
            max_chain_depth=8,
        )
        metadata = {"work_item_issue": {"N": str(source["issue"])}, "max_total_dispatches": {"N": "1"}}
        if persona == "developer":
            metadata["dispatch_personas"] = {"SS": ["reviewer"]}
        event = EngineRunStore.build_item(envelope)
        event.update(actor_kind="service", actor_user_id="system:orchestration-dispatch")
        serializer = TypeSerializer()
        self.store.provision_pending(
            envelope=envelope,
            grant=grant,
            now=now,
            execution_metadata={
                "issue_number": {"N": str(source["issue"])},
                "installation_id": {"N": str(source["installation_id"])},
                "chain_depth": {"N": "0"},
                "orchestration_node_id": {"S": pending.node_id},
                "orchestration_node_attempt": {"N": str(pending.node_attempt)},
            },
            grant_metadata=metadata,
            events_table=self.events_table,
            event_item={key: serializer.serialize(value) for key, value in event.items()},
        )
        return envelope


@lru_cache(maxsize=1)
def get_engine_authority_writer() -> EngineAuthorityWriter:
    return EngineAuthorityWriter(
        store=BootstrapStore(
            table_name=os.environ.get("AGENT_AUTHORITY_TABLE", ""),
            dynamodb_client=boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")),
        ),
        events_table=os.environ.get("WEBHOOK_EVENTS_TABLE", ""),
    )


async def validate_engine_authority(*, session, execution: dict, grant: DelegatedGrant, store=None) -> None:
    """Read current flow/node/approval state, rather than cached SQS claims."""
    try:
        genesis = await resolve_engine_genesis(session, org_id=grant.tenant_id, decision_id=grant.authority.reference_id)
        approval = await session.get(OrchestrationDecision, genesis.decision_id)
        newer_plan = (
            await session.execute(
                select(OrchestrationDecision.id)
                .where(
                    OrchestrationDecision.org_id == grant.tenant_id,
                    OrchestrationDecision.flow_id == grant.flow_id,
                    OrchestrationDecision.kind.in_([DecisionKind.PLAN_ACCEPTED.value, DecisionKind.PLAN_AMENDED.value]),
                    OrchestrationDecision.created_at > approval.created_at,
                )
                .limit(1)
            )
        ).first()
        if newer_plan:
            raise BootstrapRefusedError("workflow approval has been superseded")
        flow = (
            await session.execute(
                select(OrchestrationFlow)
                .where(OrchestrationFlow.id == grant.flow_id, OrchestrationFlow.org_id == grant.tenant_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            flow is None
            or flow.state not in {NodeState.PENDING.value, NodeState.READY.value, NodeState.RUNNING.value}
            or genesis.flow_id != grant.flow_id
            or genesis.root_human_id != grant.authority.human_id
        ):
            raise BootstrapRefusedError("engine flow is no longer authorized")
        if "coordinator_flow_id" in execution:
            if execution.get("wave_coordinator") == {"BOOL": True}:
                from src.agentauth.waves import validate_wave_coordinator

                await validate_wave_coordinator(session=session, execution=execution, grant=grant, store=store)
                return
            if (
                execution["coordinator_flow_id"] != {"S": grant.flow_id}
                or execution.get("persona", {}).get("S") not in {"operations", "aidlc"}
                or execution.get("parent_principal")
                or flow.intent_ref not in {execution["coordinator_intent_issue"]["N"], "#" + execution["coordinator_intent_issue"]["N"]}
            ):
                raise BootstrapRefusedError("coordinator flow is no longer assigned")
            return
        node_id = execution["orchestration_node_id"]["S"]
        attempt = int(execution["orchestration_node_attempt"]["N"])
        node = (
            await session.execute(
                select(OrchestrationNode)
                .where(OrchestrationNode.id == node_id, OrchestrationNode.org_id == grant.tenant_id, OrchestrationNode.flow_id == grant.flow_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if node is None or node.state != NodeState.RUNNING.value or node.attempts != attempt:
            raise BootstrapRefusedError("engine node is no longer authorized")
        if "orchestration_dispatch_receipt" in execution:
            receipt = (
                await session.execute(
                    select(OrchestrationDecision).where(
                        OrchestrationDecision.id == execution["orchestration_dispatch_receipt"]["S"],
                        OrchestrationDecision.org_id == grant.tenant_id,
                        OrchestrationDecision.flow_id == grant.flow_id,
                        OrchestrationDecision.node_id == node_id,
                    )
                )
            ).scalar_one_or_none()
            metadata = json.loads(receipt.reason or "{}") if receipt else {}
            if (
                receipt is None
                or receipt.kind != DecisionKind.AGENT_DISPATCHED.value
                or receipt.actor_kind != "service"
                or receipt.actor_id != f"agent:{execution['parent_principal']['S']}"
                or metadata.get("node_attempt") != attempt
                or metadata.get("invocation_id") != execution["invocation_id"]["S"]
                or metadata.get("authority_reference_id") != grant.authority.reference_id
            ):
                raise BootstrapRefusedError("workflow dispatch is not committed")
    except BootstrapRefusedError:
        raise
    except Exception:
        raise BootstrapRefusedError("engine authority unavailable") from None
