"""Operator-plane REST API for orchestration plan amendment.

Issue #4200 (EPIC #4191, intent #4120).

Endpoints (paths as registered on the app; browsers reach them under `/api/...`,
which CloudFront strips before the origin — see the prefix note below):
- POST /orchestration/flows — submit an approved plan, creating the flow
  (issue #4320). This is the engine's only authenticated ingress for plan state;
  without it the orchestration graph cannot be populated at all.
- POST /orchestration/flows/{flow_id}/amendments — supersede the accepted plan
- GET  /orchestration/flows/{flow_id}/plans — read plan versions, including
  superseded ones
- GET  /orchestration/flows/{flow_id}/cost — three-valued cost rolled up by
  graph address (issue #4207). Gated on `USAGE_READ`, not `PLAN_APPROVE`: it is a
  read of spend, and approval is a write authority over promotion state.
- GET  /orchestration/flows/{flow_id} — the whole flow for the graph view
  (issue #4212): every node including ones that have never run, every edge, and
  per-node plus rolled-up cost. Gated on `USAGE_READ` for the same reason as the
  cost route.

**This is the operator plane, not the internal plane.** The distinction is the
EPIC's central guarantee, not a routing detail. Agent pods can reach any
`/internal/v1/*` endpoint with any HTTP method, so an amendment route registered
there would hand agents write access to the record of what was approved with no
permission change and nothing to trigger a review. This router is
Cognito-authenticated via `get_current_user` and gated on `Permission.PLAN_APPROVE`
per request; `tests/orchestration/test_internal_plane_guard.py` asserts it is not
on the internal plane.

**Authorization is the same permission as gate approval**, never weaker —
amendment is *at least* as privileged as approval. `PLAN_APPROVE` is registered in
`_ORG_SCOPED_PERMISSIONS`, which is what makes a principal with an empty `org_id`
get denied instead of skipping the membership check and short-circuiting the
`target_org_id` scope check.

**Tenant isolation.** `target_org_id` is the caller's own authenticated
`org_id` — the flow is then resolved under that org inside `amend_plan`, so there
is no path where a `flow_id` from another tenant is read. A cross-org `flow_id`
returns 404, not 403: a 403 would confirm the id exists somewhere, letting a caller
enumerate flows by status code.

**The GET is deliberately part of this story.** "The superseded version is still
readable" is the guarantee amendment rests on, and a guarantee with no read path is
untestable end-to-end. It is a read, gated on the same permission.
"""

import logging
from collections import defaultdict
from dataclasses import asdict
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.orchestration.amend import AmendmentContext, FlowNotFoundError, amend_plan
from src.orchestration.compile import ApprovalContext, ProposalRejectedError, TenantMismatchError, compile_proposal
from src.orchestration.cost import (
    COST_SCOPE_LABEL,
    AggregateCost,
    CostStatus,
    NodeCost,
    UnknownReason,
    get_cost_by_address_prefixes,
    get_flow_cost,
)
from src.orchestration.dispatch_pass import resolve_installation_id
from src.orchestration.display_state import FlowStatus
from src.orchestration.models import DecisionKind
from src.orchestration.proposal import LoopProposal, split_address
from src.orchestration.repository import OrchestrationRepository, WaveAggregate
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger("bedrockgateway.orchestration")

# NOTE: prefix is "/orchestration", NOT "/api/orchestration". CloudFront fronts
# the gateway with an /api/* behavior whose viewer-request function
# (bedrockgw-<env>-strip-api-prefix) removes the first leading /api before
# forwarding to the origin. So the browser calls /api/orchestration/flows and the
# backend must serve /orchestration/flows. Issue #4330: registering this router
# under /api/orchestration made every operator-plane call 404 through the
# dashboard front door — the routes were only ever verified against the internal
# ALB, which does not run the strip function. Every other operator router follows
# this convention (/auth, /admin, /budgets); tests/test_route_prefix_convention.py
# guards it app-wide.
router = APIRouter(prefix="/orchestration", tags=["orchestration"])


async def get_access_control(db: Annotated[AsyncSession, Depends(get_db)]) -> AccessControl:
    """Get access control instance."""
    return AccessControl(db)


class AmendmentResponse(BaseModel):
    """The outcome of an amendment.

    `superseded_version` is null when the flow had no plan in force. `already_
    amended` is true when the identical document was already current and nothing
    was written — a client reporting "N nodes superseded" must not present a retry
    as a fresh amendment.
    """

    model_config = ConfigDict(extra="forbid")

    flow_id: str
    plan_version: int
    superseded_version: int | None
    decision_id: str
    plan_hash: str
    nodes_created: int
    nodes_superseded: int
    edges_created: int
    already_amended: bool


class FlowCreatedResponse(BaseModel):
    """The outcome of submitting an approved plan.

    `already_compiled` is true when the identical document was already in force and
    nothing was written — the route returns 200 rather than 201 in that case, so a
    retried submission is distinguishable from a first one by status code alone.

    `dispatchable` / `dispatch_blocked_reason` are not decoration. A flow whose org
    has no single unambiguous GitHub installation compiles perfectly and then never
    dispatches: every node is counted `undispatchable` by the tick and the
    submitter is told nothing. That is the invisible-stall class this EPIC exists
    to remove, so the condition is surfaced here, at submission, where the person
    who can fix it is still watching. `dispatchable=False` does NOT mean the
    submission failed — the rows are committed and are exactly what a correct
    submission produces; it means the plan cannot yet be delivered.
    """

    model_config = ConfigDict(extra="forbid")

    flow_id: str
    plan_version: int
    decision_id: str
    plan_hash: str
    nodes_created: int
    edges_created: int
    already_compiled: bool
    dispatchable: bool
    dispatch_blocked_reason: str | None


class PlanVersionResponse(BaseModel):
    """One accepted-plan version. `superseded_at` null means currently in force."""

    model_config = ConfigDict(extra="forbid")

    version: int
    plan_hash: str
    plan_document: dict[str, Any]
    accepted_by_decision_id: str | None
    superseded_at: str | None
    created_at: str


async def _resolve_actor_role(access: AccessControl, current_user: TokenContext) -> str:
    """The caller's role, snapshotted for the decision record.

    Read from the same resolver the permission check uses, so the attributed role
    is the one authority was actually granted under. A copy, not a join: roles
    change, and `orchestration_decisions` must record the authority held at
    amendment time.
    """
    role, _, _ = await access.get_user_role(current_user)
    return role.value


@router.post("/flows", response_model=FlowCreatedResponse, status_code=201)
async def create_flow(
    proposal: LoopProposal,
    response: Response,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
    reason: Annotated[str | None, Query(max_length=2000)] = None,
) -> FlowCreatedResponse:
    """Submit an approved plan, creating the flow and its graph.

    The engine's single authenticated ingress for plan state. `compile_proposal` is
    the only code that creates flow, node and edge rows, and before this route it
    had no caller outside its own module — so the graph the tick sweeps was
    permanently empty and every capability reading it had nothing to operate on.

    The body is a **full** `LoopProposal`, re-validated authoritatively inside
    `compile_proposal` (AC-29) regardless of whether the advisory CLI ran.

    Returns 201 on a first compile, 200 when the identical document was already in
    force, 403 without `PLAN_APPROVE` (zero rows written), and 422 for a document
    that fails validation or declares a tenant other than the caller's.
    """
    # Gate first, before any read or write — same ordering as `create_amendment`,
    # so a denied caller cannot learn whether anything exists.
    await access.check_permission(
        current_user,
        Permission.PLAN_APPROVE,
        target_org_id=current_user.org_id,
    )

    # Server-resolved, every field. `org_id` is the caller's authenticated claim
    # and is what the plan lands under; the document's declared `org_id` is only
    # ever compared against it inside `compile_proposal` (its Gate 2). No tenant
    # logic belongs here — a second implementation could disagree with that one.
    # `actor_kind` is left at its `HUMAN` default: submitting a plan is a human act.
    actor = ApprovalContext(
        org_id=current_user.org_id,
        actor_id=current_user.user_id,
        actor_role=await _resolve_actor_role(access, current_user),
        reason=reason,
    )

    try:
        result = await compile_proposal(db, proposal, actor)
    except TenantMismatchError as exc:
        # Before `ProposalRejectedError`: TenantMismatchError subclasses it, so the
        # broader clause would swallow this one if the order were reversed.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProposalRejectedError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "violations": [{"rule": violation.rule, "message": violation.message, "where": violation.where} for violation in exc.violations],
            },
        ) from exc

    # `compile_proposal` does not commit — the caller owns the transaction, so the
    # request boundary is where it lands.
    await db.commit()

    # Resolved AFTER the commit: this is a report on the submission, not a
    # condition on it. A plan whose org has an ambiguous installation is still a
    # validly approved plan, and refusing it here would make an operational data
    # problem look like a rejected document.
    installation_id = await resolve_installation_id(db, org_id=actor.org_id)
    dispatch_blocked_reason = (
        None
        if installation_id is not None
        else (
            f"org {actor.org_id!r} does not resolve to exactly one GitHub installation, so no node in this flow can be "
            "dispatched; the engine will count every node undispatchable until exactly one installation is configured"
        )
    )

    # 200, not 201, for a resubmission: nothing was created, and a client
    # reporting "N nodes created" must not present a retry as a fresh submission.
    if result.already_compiled:
        response.status_code = 200

    logger.info(
        "plan_submitted flow=%s org=%s actor=%s v%s nodes=%s edges=%s idempotent=%s dispatchable=%s",
        result.flow_id,
        actor.org_id,
        actor.actor_id,
        result.plan_version,
        result.nodes_created,
        result.edges_created,
        result.already_compiled,
        installation_id is not None,
    )

    if dispatch_blocked_reason is not None:
        # Logged at warning as well as returned: the submitter sees the response,
        # but whoever is watching the engine wonder why nothing moved sees this.
        logger.warning("plan_submitted flow=%s is undispatchable: %s", result.flow_id, dispatch_blocked_reason)

    return FlowCreatedResponse(
        flow_id=result.flow_id,
        plan_version=result.plan_version,
        decision_id=result.decision_id,
        plan_hash=result.plan_hash,
        nodes_created=result.nodes_created,
        edges_created=result.edges_created,
        already_compiled=result.already_compiled,
        dispatchable=installation_id is not None,
        dispatch_blocked_reason=dispatch_blocked_reason,
    )


@router.post("/flows/{flow_id}/amendments", response_model=AmendmentResponse)
async def create_amendment(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    proposal: LoopProposal,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
    reason: Annotated[str | None, Query(max_length=2000)] = None,
) -> AmendmentResponse:
    """Supersede a flow's accepted plan with a new version.

    The body is a **full** `LoopProposal`, not a patch, and is re-validated by the
    same validator the original acceptance ran (AC-29).

    Returns 403 without the required permission (zero rows written), 404 for a
    flow outside the caller's tenant, and 422 for a document that fails validation
    or declares the wrong tenant or flow.
    """
    # Gate first: nothing is read or written before the permission check, so a
    # denied caller cannot even learn whether the flow exists.
    await access.check_permission(
        current_user,
        Permission.PLAN_APPROVE,
        target_org_id=current_user.org_id,
    )

    actor = AmendmentContext(
        org_id=current_user.org_id,
        actor_id=current_user.user_id,
        actor_role=await _resolve_actor_role(access, current_user),
        reason=reason,
    )

    try:
        result = await amend_plan(db, flow_id, proposal, actor)
    except FlowNotFoundError as exc:
        # 404 for both "absent" and "another tenant's" — see module docstring.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TenantMismatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProposalRejectedError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "violations": [{"rule": violation.rule, "message": violation.message, "where": violation.where} for violation in exc.violations],
            },
        ) from exc

    # `amend_plan` does not commit — the caller owns the transaction, so the
    # request boundary is where it lands.
    await db.commit()

    logger.info(
        "plan_amended flow=%s org=%s actor=%s v%s->v%s superseded_nodes=%s idempotent=%s",
        result.flow_id,
        actor.org_id,
        actor.actor_id,
        result.superseded_version,
        result.plan_version,
        result.nodes_superseded,
        result.already_amended,
    )

    return AmendmentResponse(
        flow_id=result.flow_id,
        plan_version=result.plan_version,
        superseded_version=result.superseded_version,
        decision_id=result.decision_id,
        plan_hash=result.plan_hash,
        nodes_created=result.nodes_created,
        nodes_superseded=result.nodes_superseded,
        edges_created=result.edges_created,
        already_amended=result.already_amended,
    )


@router.get("/flows/{flow_id}/plans", response_model=list[PlanVersionResponse])
async def list_plans(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
    version: Annotated[int | None, Query(ge=1)] = None,
) -> list[PlanVersionResponse]:
    """Read a flow's accepted-plan versions, superseded ones included.

    This is what makes amendment auditable: after an amendment, the version in
    force at any past gate is still readable here with its original document.
    Pass `version` to read one; omit it for all, ascending.
    """
    await access.check_permission(
        current_user,
        Permission.PLAN_APPROVE,
        target_org_id=current_user.org_id,
    )

    repo = OrchestrationRepository(db)

    # Org-filtered flow resolution before any plan read, so a cross-tenant flow_id
    # is a 404 rather than an empty list — an empty list would imply the flow
    # exists but has no plans.
    flow = await repo.get_flow(org_id=current_user.org_id, flow_id=flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail=f"no orchestration flow {flow_id!r} in this tenant")

    plans = await repo.list_plan_versions(org_id=current_user.org_id, flow_id=flow.id)
    if version is not None:
        plans = [plan for plan in plans if plan.version == version]
        if not plans:
            raise HTTPException(status_code=404, detail=f"flow {flow_id!r} has no plan version {version}")

    return [
        PlanVersionResponse(
            version=plan.version,
            plan_hash=plan.plan_hash,
            plan_document=plan.plan_document,
            accepted_by_decision_id=plan.accepted_by_decision_id,
            superseded_at=plan.superseded_at.isoformat() if plan.superseded_at else None,
            created_at=plan.created_at.isoformat(),
        )
        for plan in plans
    ]


class NodeCostResponse(BaseModel):
    """Cost for one graph address, three-valued.

    `amount_usd` is null for any status but `known`, and it is a **string**, not a
    float: `Numeric(10, 6)` through a float loses the sub-cent precision that is
    the majority of an individual agent call's cost. Serialising the Decimal as a
    string is what keeps the wire value exact.
    """

    model_config = ConfigDict(extra="forbid")

    address: str
    status: str
    amount_usd: str | None
    total_tokens: int
    call_count: int
    reason: str | None
    scope: str


class FlowCostResponse(BaseModel):
    """A flow's rolled-up cost, with the labels that stop it being misread.

    `partial` and `scope` are not decoration. A partial total is a **lower
    bound** — some node's contribution was never measured — and `scope` says the
    figure covers agent-run Bedrock spend only. A client that renders the number
    without either one presents a lower bound of one cost category as the total
    cost, which is the misreading this story exists to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    flow_id: str
    address: str
    status: str
    amount_usd: str | None
    total_tokens: int
    call_count: int
    node_count: int
    unknown_node_count: int
    partial: bool
    reason: str | None
    scope: str
    nodes: list[NodeCostResponse]


def _node_cost_response(node: NodeCost) -> NodeCostResponse:
    """Serialise one node's cost. Shared by the cost route and the graph route.

    One function rather than the same six-line projection in both places: the
    `amount_usd`-as-string rule exists so sub-cent precision survives JSON, and a
    second copy is a second chance for someone to "simplify" it into a float.
    """
    return NodeCostResponse(
        address=node.address,
        status=node.status.value,
        amount_usd=str(node.amount_usd) if node.amount_usd is not None else None,
        total_tokens=node.total_tokens,
        call_count=node.call_count,
        reason=node.reason.value if node.reason else None,
        scope=node.scope,
    )


def _flow_cost_response(flow_id: str, aggregate: AggregateCost) -> FlowCostResponse:
    """Serialise a flow's rolled-up cost, `partial` and `scope` included.

    Both labels travel with the figure by construction here, because a total
    rendered without them is a lower bound of one cost category presented as the
    total cost.
    """
    return FlowCostResponse(
        flow_id=flow_id,
        address=aggregate.address,
        status=aggregate.status.value,
        amount_usd=str(aggregate.amount_usd) if aggregate.amount_usd is not None else None,
        total_tokens=aggregate.total_tokens,
        call_count=aggregate.call_count,
        node_count=aggregate.node_count,
        unknown_node_count=aggregate.unknown_node_count,
        partial=aggregate.partial,
        reason=aggregate.reason.value if aggregate.reason else None,
        scope=COST_SCOPE_LABEL,
        nodes=[_node_cost_response(node) for node in aggregate.nodes],
    )


async def _stalled_node_ids(repo: OrchestrationRepository, *, org_id: str, flow_id: str) -> set[str]:
    """Node ids whose latest stall-or-halt decision was a **stall**.

    Why this is needed at all: stall detection transitions a stalled node to
    `failed`, not to a state of its own (`stall.py`), while halting moves it to
    `halted`. So `halted` is readable from `state` but "stalled" is not — a stalled
    node and a node whose work simply failed are indistinguishable by state, and
    AC-3 requires the view to distinguish them.

    Latest-wins rather than any-match: a node that stalled, was resumed, and then
    halted must not still read as stalled. `list_decisions` returns in `created_at`
    order, so the last of the two kinds seen for a node is the current one.
    Decisions are append-only, which is what makes this reduction sound — no row
    is ever rewritten behind it.
    """
    latest: dict[str, str] = {}
    for decision in await repo.list_decisions(org_id=org_id, flow_id=flow_id):
        if decision.node_id and decision.kind in (DecisionKind.NODE_STALLED.value, DecisionKind.NODE_HALTED.value):
            latest[decision.node_id] = decision.kind
    return {node_id for node_id, kind in latest.items() if kind == DecisionKind.NODE_STALLED.value}


@router.get("/flows/{flow_id}/cost", response_model=FlowCostResponse)
async def get_flow_cost_route(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FlowCostResponse:
    """A flow's cost, rolled up from its nodes by graph address.

    Gated on `USAGE_READ` rather than `PLAN_APPROVE`: this is a read of spend, and
    approval authority is a *write* permission over promotion state. Requiring the
    stronger one would mean nobody could see costs without also being able to
    accept plans — authority creep in the direction that grants more than the
    operation needs. `USAGE_READ` is already in `_ORG_SCOPED_PERMISSIONS`, so a
    principal with an empty `org_id` is denied rather than skipping the scope
    check.

    A `flow_id` from another tenant returns 404, consistent with the rest of this
    router: a 403 would confirm the id exists somewhere and let a caller enumerate
    flows by status code.

    Every figure in the response carries `scope`, and aggregates carry `partial`.
    A node with no ledger row is `unknown` — never `0`.
    """
    await access.check_permission(
        current_user,
        Permission.USAGE_READ,
        target_org_id=current_user.org_id,
    )

    repo = OrchestrationRepository(db)

    # Org-filtered flow resolution before any ledger read, so a cross-tenant
    # flow_id can never reach the cost query.
    flow = await repo.get_flow(org_id=current_user.org_id, flow_id=flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail=f"no orchestration flow {flow_id!r} in this tenant")

    nodes = await repo.list_nodes(org_id=current_user.org_id, flow_id=flow.id)
    aggregate = await get_flow_cost(db, org_id=current_user.org_id, flow=flow, nodes=nodes)

    return _flow_cost_response(flow.id, aggregate)


class FlowDisplayCountsResponse(BaseModel):
    """Node counts in the five-value display vocabulary (#4212's §1.3 projection).

    Five keys, always all five, including zeroes: the rollup bar renders segments
    from these and a missing key would silently drop a segment rather than draw an
    empty one. `superseded` has no key because it is in no bucket — see
    `display_state.py`.
    """

    model_config = ConfigDict(extra="forbid")

    queued: int
    in_progress: int
    gate: int
    stalled: int
    complete: int


class WaveSummaryResponse(BaseModel):
    """One wave's rollup, for the rail on a flow card.

    Ordered by first appearance (`MIN(node.created_at)`) in the enclosing list —
    **not** by `wave_ref`, which sorts `wave-10` before `wave-2`. The order of the
    array is the order the rail renders, so it is part of the contract.
    """

    model_config = ConfigDict(extra="forbid")

    epic_ref: str
    wave_ref: str
    total: int
    done: int
    display_counts: FlowDisplayCountsResponse


class FlowSummaryResponse(BaseModel):
    """One flow as the list page reads it: identity plus everything derived.

    **`OrchestrationFlow.state` is deliberately absent.** It defaults to
    `pending`, has no writer anywhere in `src/`, and is therefore permanently
    `"pending"` for every flow that exists — publishing it would put a meaningless
    word where operators look for status. `status` below is derived from the
    flow's nodes instead, consistent with container state being derived and never
    stored.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    slug: str
    title: str
    intent_ref: str | None
    # --- The design loop's story (#4885) -----------------------------------
    # Both ride the flow row already fetched by the page query: no new query, no
    # change to the join, no change to the statement-count bound #4869 pins.
    #
    # `null` means "we do not know" and MUST reach the client as `null`. It is the
    # honest value for every flow registered before #4885, and the card renders no
    # stage strip at all for it — an empty or all-pending strip would assert
    # design gates that never happened. Neither field is defaulted to `""`/`{}`
    # here for exactly that reason.
    description: str | None
    design_history: dict[str, Any] | None
    # One of the six `FlowStatus` values, derived by `derive_flow_status`.
    status: str
    # Surfaced alongside `status` because `status` is first-match-wins: a flow that
    # is both stalled and gated reports `attention_needed`, and the card still has
    # to be able to say "1 waiting on you".
    awaiting_gate_count: int
    # Decision-derived (latest `node_stalled` wins), NOT the count of `failed`
    # nodes — a stall and a plain failure share an engine state.
    stalled_count: int
    display_counts: FlowDisplayCountsResponse
    total_nodes: int
    epic_count: int
    wave_count: int
    current_wave_ref: str | None
    waves: list[WaveSummaryResponse]
    # Agent-run Bedrock spend under this flow's address prefix, three-valued.
    # `unknown` carries no amount, so a client cannot render absence as $0.00.
    delivery_cost: NodeCostResponse
    created_at: str
    updated_at: str | None


class FlowListResponse(BaseModel):
    """A page of flows, the filtered total, and the unfiltered status chips.

    `total` counts rows matching the **filters across all pages** — it is not
    `len(flows)`. A client showing "Showing 3 of 5" needs both numbers, and a
    `total` that only described the current page would make the pager lie about
    how much is there.

    `status_counts` is **unfiltered and tenant-wide**, which is why it is a
    separate number from `total`: the chips describe the population the operator
    is choosing among, so they still total 5 while a `needs_me` filter shows 3.
    Every status is present including zeroes — "nothing is stalled" is information.
    """

    model_config = ConfigDict(extra="forbid")

    flows: list[FlowSummaryResponse]
    total: int
    limit: int
    offset: int
    status_counts: dict[str, int]


def _wave_summary(wave: WaveAggregate) -> WaveSummaryResponse:
    return WaveSummaryResponse(
        epic_ref=wave.epic_ref,
        wave_ref=wave.wave_ref,
        total=wave.total,
        done=wave.done,
        display_counts=FlowDisplayCountsResponse(**asdict(wave.display_counts)),
    )


@router.get("/flows", response_model=FlowListResponse)
async def list_flows_route(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str | None, Query(max_length=256)] = None,
    status: Annotated[FlowStatus | None, Query()] = None,
    needs_me: Annotated[bool, Query()] = False,
    sort: Annotated[Literal["created", "updated", "stalled"], Query()] = "created",
) -> FlowListResponse:
    """The tenant's delivery flows, with derived status, wave rollups and cost.

    Without this route the orchestration engine has no entry point in the UI at
    all: the graph view is reachable only by knowing a flow id, so a flow nobody
    has the id for is invisible along with every gate waiting on a human.

    Gated on `USAGE_READ`, identical to the detail and cost routes — seeing where
    delivery stands must not require `PLAN_APPROVE`, which is *write* authority
    over promotion state. The check runs before any read, so a denied caller
    learns nothing about what exists.

    **An empty tenant is `200 {"flows": [], "total": 0}`, never 404.** Unlike the
    detail route, where "no such flow" and "a flow with no work" are genuinely
    different answers, "this org has no flows" is a true and complete answer to
    the question asked.

    `limit` over 100 is a **422**, not a silent clamp: a client that asked for 500
    and received 100 would page as though it had 500 and skip four fifths of the
    list.

    Every aggregate filters `org_id` in SQL; the page's flow ids are never used as
    the only scoping. Query count is constant in page size (three, or four with
    the chips) — see `list_flows_page_with_aggregates`.
    """
    await access.check_permission(
        current_user,
        Permission.USAGE_READ,
        target_org_id=current_user.org_id,
    )

    repo = OrchestrationRepository(db)

    page = await repo.list_flows_page_with_aggregates(
        org_id=current_user.org_id,
        limit=limit,
        offset=offset,
        q=q,
        status=status,
        needs_me=needs_me,
        sort=sort,
    )

    # ONE grouped ledger query for the whole page: a flow's slug is its graph
    # address prefix (`compile.address_of`), so every flow's spend comes back from
    # a single `or_()` of prefix predicates. A per-flow cost call here would be the
    # N+1 the address-keyed cost model exists to avoid.
    slugs = [aggregate.flow.slug for aggregate in page.flows]
    ledger = await get_cost_by_address_prefixes(db, org_id=current_user.org_id, address_prefixes=slugs)

    # Attribute each measured address back to its flow by its FIRST segment, which
    # is the flow slug. Summed per slug rather than per node: the card shows one
    # figure per flow.
    #
    # Known limitation (#4885): `orchestration_flows` has no `uq(org_id, slug)`,
    # so two flows in one tenant may share a slug and their spend is then
    # indistinguishable by address. Noted rather than worked around — the fix is a
    # constraint, which is a migration.
    measured: dict[str, list[NodeCost]] = defaultdict(list)
    for node_cost in ledger:
        try:
            flow_slug, _, _, _ = split_address(node_cost.address)
        except ValueError:
            # A malformed address predates or bypassed validation. Skipped rather
            # than guessed at: attributing it to a flow by string-slicing would
            # put someone else's spend on this card.
            logger.warning("skipping malformed graph_address in cost rollup: %r", node_cost.address)
            continue
        measured[flow_slug].append(node_cost)

    flows: list[FlowSummaryResponse] = []
    for aggregate in page.flows:
        flows.append(
            FlowSummaryResponse(
                id=aggregate.flow.id,
                slug=aggregate.flow.slug,
                title=aggregate.flow.title,
                intent_ref=aggregate.flow.intent_ref,
                description=aggregate.flow.description,
                design_history=aggregate.flow.design_history,
                status=aggregate.status.value,
                awaiting_gate_count=aggregate.awaiting_gate_count,
                stalled_count=aggregate.stalled_count,
                display_counts=FlowDisplayCountsResponse(**asdict(aggregate.display_counts)),
                total_nodes=aggregate.display_counts.total,
                epic_count=aggregate.epic_count,
                wave_count=len(aggregate.waves),
                current_wave_ref=aggregate.current_wave_ref,
                waves=[_wave_summary(wave) for wave in aggregate.waves],
                delivery_cost=_node_cost_response(_roll_up_delivery_cost(aggregate.flow.slug, measured.get(aggregate.flow.slug, []))),
                created_at=aggregate.flow.created_at.isoformat(),
                updated_at=aggregate.flow.updated_at.isoformat() if aggregate.flow.updated_at else None,
            )
        )

    status_counts = await repo.count_flows_by_status(org_id=current_user.org_id)

    return FlowListResponse(
        flows=flows,
        total=page.total,
        limit=limit,
        offset=offset,
        status_counts={flow_status.value: count for flow_status, count in status_counts.items()},
    )


def _roll_up_delivery_cost(slug: str, node_costs: list[NodeCost]) -> NodeCost:
    """Sum a flow's measured addresses into one figure, three-valued.

    A flow with no ledger rows at all is `UNKNOWN` with a reason — **never
    `$0.00`**. That distinction is the whole point of the cost model: `$0.00`
    asserts the work was free, while `unknown` says nobody measured it. A flow
    that has not started yet and a flow that genuinely cost nothing must not read
    the same.

    Unlike `get_flow_cost` this does not mark the total `partial`, because it has
    no node list to know how many addresses *should* have rows. The figure is
    therefore "what the ledger holds for this flow", and the card labels it as
    spend so far.
    """
    if not node_costs:
        return NodeCost(
            address=slug,
            status=CostStatus.UNKNOWN,
            reason=UnknownReason.NO_USAGE_ROWS,
        )

    total = sum((cost.amount_usd or Decimal(0) for cost in node_costs), Decimal(0))
    return NodeCost(
        address=slug,
        # Rows exist, so this is a measurement either way: > 0 is known, == 0 is a
        # verified zero. Neither is `unknown`.
        status=CostStatus.KNOWN if total > 0 else CostStatus.NONE_INCURRED,
        amount_usd=total,
        total_tokens=sum(cost.total_tokens for cost in node_costs),
        call_count=sum(cost.call_count for cost in node_costs),
    )


class GraphNodeResponse(BaseModel):
    """One executable node — story, eval, or gate — as the graph view reads it.

    Carries the address components rather than the joined address string. The
    view groups by EPIC and wave to derive its containers (§8.2 of the design
    contract: container state is derived, never stored), and per item 7.2 the
    joined address is internal and must never be rendered, so shipping the
    components is both what the client needs and the shape that does not invite
    displaying a path.

    `cost` is inlined per node rather than left to the `/cost` route. Correlating
    the two responses client-side would mean the SPA rebuilding the internal
    address string as a join key — a second implementation of the address format,
    in the layer least able to notice when it drifts.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    epic_ref: str
    wave_ref: str
    node_ref: str
    kind: str
    state: str
    title: str
    issue_ref: str | None
    attempts: int
    # True when the most recent stall/halt decision for this node was a stall.
    #
    # Load-bearing for AC-3, and not inferable from `state`: stall detection moves
    # a stalled node to `failed` (`stall.py`), so a stall and an ordinary failure
    # are the same state. Without this flag "stalled" and "failed" cannot be told
    # apart, and the contract requires them to look different — a stall means "go
    # find out why this is wedged", a failure means the work itself failed.
    stalled: bool
    cost: NodeCostResponse
    created_at: str
    updated_at: str | None


class GraphEdgeResponse(BaseModel):
    """A dependency edge. What makes look-ahead and parallel branches renderable.

    Node ids, not addresses: the client already has every node keyed by id, and
    resolving edges by id avoids reconstructing the address string (see
    `GraphNodeResponse`).
    """

    model_config = ConfigDict(extra="forbid")

    from_node_id: str
    to_node_id: str


class FlowGraphResponse(BaseModel):
    """A whole flow: its nodes, its edges, and what it has cost.

    **Containers are deliberately absent.** No wave, EPIC or flow-level state is
    returned beyond the flow's own row, because §8.2 of the design contract makes
    container state derived, never stored. Returning a computed container state
    here would publish it as authoritative and create a second source of truth for
    a value its children already imply.

    **Nodes that have never run are included, and that is the point.** A response
    holding only what has already executed cannot answer "how much is left", which
    is the question the view exists to answer (AC-1).
    """

    model_config = ConfigDict(extra="forbid")

    flow_id: str
    slug: str
    title: str
    intent_ref: str | None
    state: str
    created_at: str
    updated_at: str | None
    nodes: list[GraphNodeResponse]
    edges: list[GraphEdgeResponse]
    cost: FlowCostResponse


@router.get("/flows/{flow_id}", response_model=FlowGraphResponse)
async def get_flow_graph(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FlowGraphResponse:
    """The whole journey for one flow: nodes, edges, and three-valued cost.

    Gated on `USAGE_READ` rather than `PLAN_APPROVE`, for the same reason the cost
    route is: this is a read, and approval authority is a *write* permission over
    promotion state. Requiring the stronger one would mean nobody could see where
    delivery stands without also being able to accept plans.

    The permission check runs before any read, so a denied caller cannot learn
    whether the flow exists. A `flow_id` from another tenant returns **404**, not
    403 and not an empty graph: a 403 confirms the id exists somewhere and lets a
    caller enumerate flows by status code, while an empty graph would read as "no
    work", which is a different and more misleading answer than "not found".
    """
    await access.check_permission(
        current_user,
        Permission.USAGE_READ,
        target_org_id=current_user.org_id,
    )

    repo = OrchestrationRepository(db)

    # Org-filtered resolution before any node, edge or ledger read, so a
    # cross-tenant flow_id can never reach them.
    flow = await repo.get_flow(org_id=current_user.org_id, flow_id=flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail=f"no orchestration flow {flow_id!r} in this tenant")

    nodes = await repo.list_nodes(org_id=current_user.org_id, flow_id=flow.id)
    edges = await repo.list_edges(org_id=current_user.org_id, flow_id=flow.id)
    aggregate = await get_flow_cost(db, org_id=current_user.org_id, flow=flow, nodes=nodes)
    stalled_node_ids = await _stalled_node_ids(repo, org_id=current_user.org_id, flow_id=flow.id)

    # Keyed by address because that is what `get_flow_cost` returns them under.
    # Built once rather than searched per node: a linear scan inside the node loop
    # would make this quadratic in node count for no benefit.
    cost_by_address = {node_cost.address: node_cost for node_cost in aggregate.nodes}

    graph_nodes: list[GraphNodeResponse] = []
    for node in nodes:
        address = f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"
        node_cost = cost_by_address.get(address)
        graph_nodes.append(
            GraphNodeResponse(
                id=node.id,
                epic_ref=node.epic_ref,
                wave_ref=node.wave_ref,
                node_ref=node.node_ref,
                kind=node.kind,
                state=node.state,
                title=node.title,
                issue_ref=node.issue_ref,
                attempts=node.attempts,
                stalled=node.id in stalled_node_ids,
                # `get_flow_cost` returns one entry per node passed in, so the
                # fallback is unreachable today. It is UNKNOWN rather than a zero
                # anyway: if that ever stops holding, the honest answer is "we do
                # not know", and `$0.00` is the exact lie this whole cost model
                # exists to prevent.
                cost=(
                    _node_cost_response(node_cost)
                    if node_cost is not None
                    else NodeCostResponse(
                        address=address,
                        status=CostStatus.UNKNOWN.value,
                        amount_usd=None,
                        total_tokens=0,
                        call_count=0,
                        reason=UnknownReason.NO_USAGE_ROWS.value,
                        scope=COST_SCOPE_LABEL,
                    )
                ),
                created_at=node.created_at.isoformat(),
                updated_at=node.updated_at.isoformat() if node.updated_at else None,
            )
        )

    return FlowGraphResponse(
        flow_id=flow.id,
        slug=flow.slug,
        title=flow.title,
        intent_ref=flow.intent_ref,
        state=flow.state,
        created_at=flow.created_at.isoformat(),
        updated_at=flow.updated_at.isoformat() if flow.updated_at else None,
        nodes=graph_nodes,
        edges=[GraphEdgeResponse(from_node_id=edge.from_node_id, to_node_id=edge.to_node_id) for edge in edges],
        cost=_flow_cost_response(flow.id, aggregate),
    )
