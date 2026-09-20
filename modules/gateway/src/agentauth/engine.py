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
from src.agentauth.grants import (
    AUTHORITY_GATE_DECISION,
    AUTHORITY_REPLAN_REQUEST,
    AgentAction,
    AuthorityReference,
    DelegatedGrant,
    TargetRelationship,
)
from src.orchestration.dispatch import GraphAttribution, graph_address
from src.orchestration.genesis import EngineGenesis, resolve_engine_genesis
from src.orchestration.models import (
    AmendmentRequestState,
    DecisionKind,
    NodeKind,
    OrchestrationAmendmentRequest,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.run_store import EngineRunStore
from src.orchestration.state import NodeState

#: How long a minted authority record stays usable. Shared by both kinds so an
#: authoring authority cannot outlive a gate-rooted one.
_AUTHORITY_TTL = timedelta(days=7)

#: The execution attribute carrying the amendment request an authoring run answers.
#: Server-written at provisioning time and read back by `validate_authoring_authority`
#: on every call, so the run cannot name a different assignment later.
AUTHORING_REQUEST_ATTRIBUTE = "orchestration_amendment_request_id"

#: The only persona an authoring assignment may be issued to. AI-DLC authoring is
#: `aidlc`'s job; a `developer` run under this kind would be an execution persona
#: holding a non-executing authority, which is a shape nothing should produce.
AUTHORING_PERSONA = "aidlc"


def _ensure_authority(
    *,
    store: BootstrapStore,
    org_id: str,
    decision_id: str,
    human_id: str,
    flow_id: str,
    kind: str,
    now: datetime,
) -> tuple[datetime, str]:
    """Write-once the authority record one grant derives from, then re-read it.

    Shared by the gate-rooted and authoring paths so there is exactly one writer of
    `AUTHORITY#` records and exactly one place that decides what a live one looks
    like. `kind` is recorded on the row AND re-read from it, which is what makes
    `bootstrap.live_grant`'s cross-check meaningful: the grant's kind and the
    authority's kind are written from one value here, so a later disagreement
    between them is a real inconsistency rather than a provisioning artefact.
    """
    pk, sk = f"TENANT#{org_id}", f"AUTHORITY#{decision_id}"
    authority = store._read(pk, sk)
    if authority is None:
        item = {
            **_key(pk, sk),
            "status": {"S": "active"},
            "authority_kind": {"S": kind},
            "human_id": {"S": human_id},
            "flow_id": {"S": flow_id},
            "created_at": {"S": _iso(now)},
            "expires_at": {"S": _iso(now + _AUTHORITY_TTL)},
        }
        try:
            store.client.put_item(TableName=store.table, Item=item, ConditionExpression="attribute_not_exists(pk)")
        except (ClientError, BotoCoreError):
            pass  # A lost response/race must resolve to the same live authority.
        authority = store._read(pk, sk)
    if (
        not authority
        or authority.get("human_id") != {"S": human_id}
        or authority.get("flow_id") != {"S": flow_id}
        or authority.get("status") != {"S": "active"}
        # Compared, not defaulted. An existing record for this decision under a
        # DIFFERENT kind must refuse rather than be reused: that is the only thing
        # stopping one recorded human act from being re-read as a stronger kind of
        # authority than the one it was minted under.
        or authority.get("authority_kind") != {"S": kind}
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


def ensure_engine_authority(*, store: BootstrapStore, genesis: EngineGenesis, now: datetime) -> tuple[datetime, str]:
    """The gate-rooted authority record. Unchanged behaviour; see `_ensure_authority`."""
    return _ensure_authority(
        store=store,
        org_id=genesis.org_id,
        decision_id=genesis.decision_id,
        human_id=genesis.root_human_id,
        flow_id=genesis.flow_id,
        kind=AUTHORITY_GATE_DECISION,
        now=now,
    )


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
        # Policy handoff assigns continuation to K2. The developer must not
        # create a competing reviewer while the durable controller does the same.
        worker_dispatch = persona == "developer" and envelope.get("handoff_required") is not True
        actions = {AgentAction.MONITOR, AgentAction.DISPATCH} if worker_dispatch else {AgentAction.MONITOR}
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
        if worker_dispatch:
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
                **({"provider_repository_id": {"N": str(source["provider_repository_id"])}} if "provider_repository_id" in source else {}),
            },
            grant_metadata=metadata,
            events_table=self.events_table,
            event_item={key: serializer.serialize(value) for key, value in event.items()},
        )
        return envelope

    def provision_authoring(self, pending) -> dict:
        """Mint the bounded authority for ONE AI-DLC amendment-authoring run (#4529).

        A sibling of :meth:`provision` rather than a mode of it, because the two
        differ in what they are rooted in and in what they may do, and collapsing
        them would mean relaxing checks that must keep holding for executing
        dispatch. `provision` demands a graph node and an attempt number and grants
        `DISPATCH` to a developer; an authoring run has no node — it reads a request
        and files a proposal — so reusing that path would have required making
        `node_id`/`attempt` optional on the one function that roots real plan
        execution.

        **What this grants.** `MONITOR` only, on `SELF` only, scoped to the
        assignment's tenant and flow. Deliberately:

        * no `DISPATCH`, so an authoring assignment cannot spawn executing work —
          an amendment that could dispatch would be a proposal applying itself.
          `dispatch.py` refuses this kind a second time, independently, so neither
          fence alone is load-bearing.
        * no `DESCENDANT` relationship, because with no dispatch there are no
          descendants, and a relationship that can never resolve is authority
          waiting to be misread.
        * nothing delegable, for the same reason.

        **What roots it.** The committed `REPLAN_REQUESTED` decision, carried as
        `pending.replan_decision_id` and recorded as the authority reference. That
        decision is deliberately absent from `genesis.APPROVAL_DECISION_KINDS`, so
        this kind cannot root executing graph work no matter what reads it later —
        the exclusion is enforced where approvals are resolved, not here.

        **Why the request id is written onto the execution.** `validate_flow` must
        re-prove on every call that the assignment is still open, still this run's,
        and still at the base revision the human asked against. It reads the request
        id from the execution record the server wrote, never from the request — so a
        run cannot present a different assignment after the fact.

        Args:
            pending: A committed :class:`~src.orchestration.authoring_dispatch.PendingAuthoring`.
                Only the post-commit publisher supplies this, and only after the
                request row and its decision are durable.

        Returns:
            The envelope to publish, unchanged.

        Raises:
            BootstrapRefusedError: The assignment's identity does not hold together,
                or the persona is not the authoring one.
        """
        if (
            not pending.request_id
            or not pending.replan_decision_id
            or not pending.flow_id
            or not pending.requested_by
            or not pending.org_id
            or pending.envelope.get("tenant_id") != pending.org_id
            or not self.events_table
        ):
            raise BootstrapRefusedError("verified authoring assignment required")

        envelope = dict(pending.envelope)
        invocation = envelope.get("message_id")
        assignment = envelope.get("orchestration", {})
        # The envelope is rebuilt from the request row by the producer, so these
        # must already agree. Checked rather than assumed: this is the last point
        # before a credential-bearing execution row exists, and an envelope whose
        # assignment disagrees with the row it came from is the one shape that could
        # bind a run to a flow the human never named.
        if (
            not invocation
            or invocation != pending.author_run_id
            or assignment.get("flow_id") != pending.flow_id
            or assignment.get("request_id") != pending.request_id
            or assignment.get("root_decision_id") != pending.replan_decision_id
            or assignment.get("base_plan_version") != pending.base_plan_version
        ):
            raise BootstrapRefusedError("authoring assignment identity mismatch")
        if envelope.get("persona") != AUTHORING_PERSONA:
            raise BootstrapRefusedError("unsupported authoring persona")

        now = datetime.now(UTC)
        expiry, _ = _ensure_authority(
            store=self.store,
            org_id=pending.org_id,
            decision_id=pending.replan_decision_id,
            human_id=pending.requested_by,
            flow_id=pending.flow_id,
            kind=AUTHORITY_REPLAN_REQUEST,
            now=now,
        )
        source = envelope["source_ref"]
        grant = DelegatedGrant(
            grant_id=f"grant:{invocation}:1",
            tenant_id=pending.org_id,
            principal=f"{invocation}#1",
            authority=AuthorityReference(
                AUTHORITY_REPLAN_REQUEST,
                pending.replan_decision_id,
                pending.requested_by,
                pending.org_id,
            ),
            # MONITOR only. See the docstring: the omissions are the point.
            allowed_actions=frozenset({AgentAction.MONITOR}),
            delegable_actions=frozenset(),
            target_relationships=frozenset({TargetRelationship.SELF}),
            flow_id=pending.flow_id,
            repo_scope=frozenset({source["repo"]}),
            expires_at=expiry,
            max_dispatch_concurrency=0,
            max_chain_depth=0,
        )
        # `build_authoring_item`, not `build_item`: this envelope carries no
        # `graph_address`/`node_id`/`attempt`, and that absence is the point — see
        # that method's docstring.
        event = EngineRunStore.build_authoring_item(envelope)
        event.update(actor_kind="service", actor_user_id="system:orchestration-replan")
        serializer = TypeSerializer()
        self.store.provision_pending(
            envelope=envelope,
            grant=grant,
            now=now,
            execution_metadata={
                "issue_number": {"N": str(source["issue"])},
                "installation_id": {"N": str(source["installation_id"])},
                "chain_depth": {"N": "0"},
                # The assignment this run answers, server-written. Re-read on every
                # call by `validate_authoring_authority`.
                AUTHORING_REQUEST_ATTRIBUTE: {"S": pending.request_id},
            },
            # No `dispatch_personas` and no `max_total_dispatches`: there is nothing
            # to cap because there is no DISPATCH to spend.
            grant_metadata={"work_item_issue": {"N": str(source["issue"])}},
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


async def validate_engine_authority(*, session, execution: dict, grant: DelegatedGrant, store=None) -> GraphAttribution | None:
    """Read current flow/node/approval state, rather than cached SQS claims.

    Returns the verified graph assignment (issue #4898) when this execution is
    bound to one specific node, so a model call can be attributed to that node in
    `usage_logs.graph_address` without re-deriving — or trusting — anything. The
    flow and node rows needed to compose the address are already loaded and
    already proven current here; before this they were simply discarded.

    Returns None where there is no single owning node, and that is a real answer
    rather than a gap: a flow-level or wave coordinator must not be charged to one
    of its children. Every caller that only needs the authorization outcome can
    keep ignoring the return value — a refusal is still an exception, never a
    None.
    """
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
        if "orchestration_continuation_receipt" in execution:
            from src.orchestration.review_cycle_dispatch import validate_continuation_assignment

            await validate_continuation_assignment(session, execution=execution, grant=grant, node=node)
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
        # Issue #4898. Reached only after every check above passed, so the address
        # reports a node this caller is *currently* authorized for at *this*
        # attempt — not one it was dispatched for at some point. Composed here,
        # inside the validated scope, from the same `graph_address` helper the
        # dispatch write and the cost read use, so all three agree by construction.
        return GraphAttribution(
            org_id=grant.tenant_id,
            flow_id=grant.flow_id,
            node_id=node_id,
            node_attempt=attempt,
            address=graph_address(node, flow_slug=flow.slug),
            # `.get` rather than `[...]`, deliberately: the outer `except
            # Exception` below converts ANY exception into a refusal, so an
            # unexpected shape here would turn a request this function just
            # authorized into a 403. Attribution must never be able to deny a
            # call. Absent resolves to "" and the middleware then declines to
            # attach the attribution, so the effect is a NULL address — the
            # honest degradation — instead of a denial.
            run_id=execution.get("invocation_id", {}).get("S", ""),
        )
    except BootstrapRefusedError:
        raise
    except Exception:
        raise BootstrapRefusedError("engine authority unavailable") from None


async def validate_authoring_authority(*, session, execution: dict, grant: DelegatedGrant) -> None:
    """Re-prove one amendment-authoring assignment against live state (#4529).

    The `replan_request` counterpart of :func:`validate_engine_authority`, and it
    returns None rather than a :class:`GraphAttribution` because there is genuinely
    no owning node: an authoring run reads a request and files a proposal, so there
    is nothing to charge its model spend to. That is an answer, not a gap — charging
    an authoring run to a node it did not execute would misattribute the cost.

    Every check reads the database now rather than trusting the grant, because the
    grant was minted when the assignment was created and a lot can change before the
    run calls: the request may have been answered, the flow deleted, or the plan
    amended by someone else. The four properties re-proved:

    * **Tenant.** The request is loaded with the grant's tenant in the WHERE clause,
      so a cross-tenant request id is indistinguishable from an absent one.
    * **Flow.** The request's flow must equal the grant's flow. A grant naming one
      flow and an assignment naming another is refused rather than reconciled.
    * **Run.** The request's `author_run_id` must be the run this grant was issued
      to. This is what makes a *stolen* or replayed assignment id useless: the id is
      not secret, but the server wrote which run may answer it.
    * **Base revision.** The plan in force must still be the version and hash the
      request recorded. An authoring run whose base moved is refused here, before it
      can spend anything producing a proposal that `accept_amendment` would refuse
      as a conflict anyway.

    The request id comes from the *execution record* the server wrote at
    provisioning, never from the caller, so a run cannot re-point itself at a
    different assignment.

    Raises:
        BootstrapRefusedError: Any of the above does not hold, or the state needed to
            decide is unavailable. Deliberately one message for all of them: a
            caller must not be able to tell "no such request" from "not your
            request" from "your base moved".
    """
    from src.orchestration.pending_amendments import in_force_plan

    try:
        request_id = execution.get(AUTHORING_REQUEST_ATTRIBUTE, {}).get("S", "")
        if not request_id:
            # No server-written assignment on this execution. The missing-request
            # case, and it must refuse: an authoring grant with nothing to author
            # against is an authority with unbounded scope.
            raise BootstrapRefusedError("authoring assignment is no longer authorized")
        request = (
            await session.execute(
                select(OrchestrationAmendmentRequest)
                .where(
                    OrchestrationAmendmentRequest.id == request_id,
                    OrchestrationAmendmentRequest.org_id == grant.tenant_id,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            request is None
            or request.flow_id != grant.flow_id
            or request.author_run_id != grant.principal.rsplit("#", 1)[0]
            or request.replan_decision_id != grant.authority.reference_id
            or request.requested_by != grant.authority.human_id
            or request.state not in {AmendmentRequestState.QUEUED.value, AmendmentRequestState.DISPATCHED.value}
        ):
            raise BootstrapRefusedError("authoring assignment is no longer authorized")

        flow = (
            await session.execute(
                select(OrchestrationFlow)
                .where(OrchestrationFlow.id == grant.flow_id, OrchestrationFlow.org_id == grant.tenant_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if flow is None:
            raise BootstrapRefusedError("authoring assignment is no longer authorized")

        # The base revision, compared exactly. Both-NULL is legitimate (a flow with
        # no accepted plan yet) and compares equal, so a first-plan authoring run is
        # not refused for having no base.
        in_force = await in_force_plan(session, org_id=grant.tenant_id, flow_id=grant.flow_id)
        current_version = in_force.version if in_force is not None else None
        current_hash = in_force.plan_hash if in_force is not None else None
        if current_version != request.base_plan_version or current_hash != request.base_plan_hash:
            raise BootstrapRefusedError("authoring assignment is no longer authorized")
    except BootstrapRefusedError:
        raise
    except Exception:
        raise BootstrapRefusedError("authoring authority unavailable") from None
