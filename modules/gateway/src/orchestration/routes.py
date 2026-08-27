"""Operator-plane REST API for orchestration plan amendment.

Issue #4200 (EPIC #4191, intent #4120).

Endpoints:
- POST /api/orchestration/flows/{flow_id}/amendments — supersede the accepted plan
- GET  /api/orchestration/flows/{flow_id}/plans — read plan versions, including
  superseded ones
- GET  /api/orchestration/flows/{flow_id}/cost — three-valued cost rolled up by
  graph address (issue #4207). Gated on `USAGE_READ`, not `PLAN_APPROVE`: it is a
  read of spend, and approval is a write authority over promotion state.

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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.orchestration.amend import AmendmentContext, FlowNotFoundError, amend_plan
from src.orchestration.compile import ProposalRejectedError, TenantMismatchError
from src.orchestration.cost import COST_SCOPE_LABEL, get_flow_cost
from src.orchestration.proposal import LoopProposal
from src.orchestration.repository import OrchestrationRepository
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger("bedrockgateway.orchestration")

router = APIRouter(prefix="/api/orchestration", tags=["orchestration"])


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

    return FlowCostResponse(
        flow_id=flow.id,
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
        nodes=[
            NodeCostResponse(
                address=node.address,
                status=node.status.value,
                amount_usd=str(node.amount_usd) if node.amount_usd is not None else None,
                total_tokens=node.total_tokens,
                call_count=node.call_count,
                reason=node.reason.value if node.reason else None,
                scope=node.scope,
            )
            for node in aggregate.nodes
        ],
    )
