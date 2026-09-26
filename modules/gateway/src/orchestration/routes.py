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
- GET  /orchestration/flows/{flow_id}/execution — execution progress and blocks
  from the delivery ledger (issue #5145): per node and cycle, the phase, whether
  it is runnable, the next scheduled check, the last real progress and — when
  stuck — the typed block naming its owner, the required input and the gates still
  outstanding. Read-only and gated on `USAGE_READ`: it adds no control and no
  acceptance authority, and `controls.py` remains the sole human approval and
  recovery surface. The projection lives in `execution_read.py`, which documents
  why an operator read is org-scoped rather than presenting a work claim it does
  not hold.

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

import json
import logging
import os
import re
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.orchestration.amend import AmendmentContext, FlowNotFoundError, amend_plan
from src.orchestration.compile import ApprovalContext, ProposalRejectedError, TenantMismatchError, compile_proposal
from src.orchestration.continuation_routes import router as continuation_router
from src.orchestration.cost import (
    COST_SCOPE_LABEL,
    AggregateCost,
    CostStatus,
    NodeCost,
    UnknownReason,
    get_cost_by_address_prefixes,
    get_flow_cost,
)
from src.orchestration.dispatch_pass import (
    REPO_ENV,
    RoutingBlocker,
    resolve_installation_id,
    routing_blocker_for_node,
)
from src.orchestration.display_state import FlowStatus
from src.orchestration.draft_revision_routes import router as draft_revision_router
from src.orchestration.evaluation_acceptance_routes import router as evaluation_acceptance_router
from src.orchestration.evaluation_waiver_routes import router as evaluation_waiver_router
from src.orchestration.execution_policy import PolicySummary, summarize_policy
from src.orchestration.execution_read import MAX_EXECUTIONS_PER_PAGE, load_flow_execution_view
from src.orchestration.flow_controls import router as flow_controls_router
from src.orchestration.models import DecisionKind, NodeState
from src.orchestration.node_activity import NodeActivity, StoryExecution, load_story_execution
from src.orchestration.policy_admission import load_in_force_policy
from src.orchestration.pr_bindings import (
    BindingError,
    BindingRefusal,
    active_bindings_for_flow,
    binding_summary,
    completion_candidate,
    hold_explanation,
    recover_binding,
)
from src.orchestration.pr_identity import PrIdentityError, resolve_pr_identity
from src.orchestration.proposal import EpicMetadata, LoopProposal, WaveMetadata, split_address
from src.orchestration.repository import OrchestrationRepository, WaveAggregate
from src.orchestration.run_report_read import MAX_REPORTS_PER_PAGE, FlowRunReportsResponse, load_flow_run_reports
from src.orchestration.shared_amendment_routes import router as shared_amendment_router
from src.orchestration.shared_budget_routes import router as shared_budget_router
from src.orchestration.shared_concurrency_routes import router as shared_concurrency_router
from src.orchestration.shared_retry_routes import router as shared_retry_router
from src.orchestration.shared_window_routes import router as shared_window_router
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .delivery_progress import DeliveryProgress, current_executions, is_preserved_execution, node_progress

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


@router.get("/flows/{flow_id}/run-reports", response_model=FlowRunReportsResponse)
async def get_flow_run_reports(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=MAX_REPORTS_PER_PAGE)] = MAX_REPORTS_PER_PAGE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FlowRunReportsResponse:
    """Read acknowledged worker reports; neither dispatch success nor merge approval."""
    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=current_user.org_id)
    flow = await OrchestrationRepository(db).get_flow(org_id=current_user.org_id, flow_id=flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="orchestration flow not found")
    return await load_flow_run_reports(db, org_id=current_user.org_id, flow_id=flow_id, limit=limit, offset=offset)


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


class DispatchBlockedCause(BaseModel):
    """One reason a submitted plan cannot be delivered.

    `cause` is a stable id a client may key off; `detail` is human-readable prose
    that stays free to be reworded. Split that way because the two have different
    consumers — a dashboard branches on the id, an operator reads the detail.

    `detail` names only the submitter's own nodes, by their tenant-local
    `node_ref`. Never another tenant's data and never an installation id: the
    ambiguous-installation cause reports how many installations resolved, which
    is what the submitter needs to fix it, and not which ones.
    """

    model_config = ConfigDict(extra="forbid")

    cause: str
    detail: str


class FlowCreatedResponse(BaseModel):
    """The outcome of submitting an approved plan.

    `already_compiled` is true when the identical document was already in force and
    nothing was written — the route returns 200 rather than 201 in that case, so a
    retried submission is distinguishable from a first one by status code alone.

    `dispatchable` / `dispatch_blocked_reason` / `dispatch_blocked_causes` are not
    decoration. A flow that trips any dispatch precondition compiles perfectly and
    then never dispatches: every node is counted `undispatchable` by the tick and
    the submitter is told nothing. That is the invisible-stall class this EPIC
    exists to remove, so the conditions are surfaced here, at submission, where
    the person who can fix them is still watching.

    **All causes, not the first (#4334).** The engine enforces several independent
    preconditions and checks issue routing *before* the installation, so a report
    naming one of them sends the submitter to fix that one, resubmit, and hit the
    same silent stall. `dispatch_blocked_causes` enumerates every cause; the
    single `dispatch_blocked_reason` string remains as their joined prose, with
    the installation cause keeping its original wording so existing consumers of
    that string are unaffected.

    `dispatchable=False` does NOT mean the submission failed — the rows are
    committed and are exactly what a correct submission produces; it means the
    plan cannot yet be delivered. Nor does `dispatchable=True` promise immediate
    execution: it means the *routing* prerequisites hold at submission time.
    Policy admission, gates, ownership and capacity are all evaluated later, by
    the tick, and none of them are reported here.
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
    dispatch_blocked_causes: list[DispatchBlockedCause] = []


class PlanVersionResponse(BaseModel):
    """One accepted-plan version. `superseded_at` null means currently in force."""

    model_config = ConfigDict(extra="forbid")

    version: int
    plan_hash: str
    plan_document: dict[str, Any]
    accepted_by_decision_id: str | None
    superseded_at: str | None
    created_at: str


# Cause ids for the preconditions that are NOT per-node. The per-node ones come
# from `RoutingBlocker` in `dispatch_pass`, so the two vocabularies are declared
# where their rule lives rather than restated as one list here.
_CAUSE_AMBIGUOUS_INSTALLATION = "ambiguous_installation"
_CAUSE_UNKNOWN_DISPATCH_REPO = "unknown_dispatch_repo"

# The sentinel Terraform writes to SSM when no dispatch repository is configured
# (`agent-authority-coordinator.tf`), which reaches the pod as this literal
# rather than as an empty string. Treated as unconfigured, exactly as an empty
# value is.
_DISPATCH_REPO_DISABLED = "disabled"


async def _dispatch_blocked_causes(
    db: AsyncSession,
    *,
    org_id: str,
    flow_id: str,
    installation_id: int | None,
) -> list[DispatchBlockedCause]:
    """Every reason this flow cannot be delivered, in the order dispatch checks them.

    Enumerated rather than short-circuited at the first: a submitter told one of
    three causes fixes it, resubmits, and gets the same silent stall (#4334).

    Each cause is evaluated by the same code dispatch enforces, never a restatement
    of it — `routing_blocker_for_node` for the per-node issue-routing rule and
    `resolve_installation_id` for the tenant installation (resolved by the caller
    and passed in, since it is also needed for the `dispatchable` flag). A second
    implementation of a fail-closed check would be free to drift, and the drift is
    invisible in the worst direction.

    Bounded: one read of the just-compiled flow's nodes, scoped to `org_id`, with
    no per-node query. `installation_id` is resolved once for the whole flow
    because it is a property of the org, not of a node.
    """
    causes: list[DispatchBlockedCause] = []

    # --- Configuration: is there a repository to dispatch into at all? ---
    #
    # Reported as *unknown* rather than as a confirmed block. The gateway pod and
    # the scheduled tick are configured from separate places (the pod's configmap
    # versus the tick's own Terraform-stamped environment), so this process's view
    # is evidence about the gateway, not proof about the deployed tick. Naming it
    # `unknown_dispatch_repo` keeps the honest reading available; claiming the
    # tick is misconfigured from here would be a guess, and claiming the plan is
    # fine would hide a real and common cause.
    if (os.environ.get(REPO_ENV) or "").strip() in ("", _DISPATCH_REPO_DISABLED):
        causes.append(
            DispatchBlockedCause(
                cause=_CAUSE_UNKNOWN_DISPATCH_REPO,
                detail=(
                    "no dispatch target repository is configured for this gateway, so the engine may have no repository to "
                    "deliver into; confirm the deployed engine's dispatch configuration"
                ),
            )
        )

    # --- Per-node: story and evaluation nodes need a routable issue. ---
    #
    # Checked FIRST by dispatch, which is why reporting only the installation
    # cause was insufficient. Gate nodes are correctly exempt — they are presented
    # by the tick and never consume a worker — and that exemption comes from the
    # shared predicate rather than a kind check written here.
    repo = OrchestrationRepository(db)
    missing: list[str] = []
    malformed: list[str] = []
    for node in await repo.list_nodes(org_id=org_id, flow_id=flow_id):
        blocker = routing_blocker_for_node(kind=node.kind, issue_ref=node.issue_ref)
        if blocker is RoutingBlocker.MISSING_ISSUE_REF:
            missing.append(f"{node.kind} node {node.node_ref!r}")
        elif blocker is RoutingBlocker.MALFORMED_ISSUE_REF:
            malformed.append(f"{node.kind} node {node.node_ref!r} (issue_ref {node.issue_ref!r})")

    # One cause per blocker kind rather than per node: a plan with forty
    # issue-less stories has one problem to fix, not forty. The node references
    # are listed in the detail so the submitter still knows which ones, and they
    # are `node_ref`s — tenant-local addresses from the flow just compiled under
    # the caller's own org.
    if missing:
        causes.append(
            DispatchBlockedCause(
                cause=RoutingBlocker.MISSING_ISSUE_REF.value,
                detail=f"{len(missing)} node(s) have no issue_ref and cannot be dispatched: {', '.join(sorted(missing))}",
            )
        )
    if malformed:
        causes.append(
            DispatchBlockedCause(
                cause=RoutingBlocker.MALFORMED_ISSUE_REF.value,
                detail=f"{len(malformed)} node(s) have an issue_ref that is not an issue number: {', '.join(sorted(malformed))}",
            )
        )

    # --- Tenant: exactly one GitHub installation. ---
    #
    # Last because dispatch checks it last, and the ordering is what makes the
    # joined reason string read in the order an operator would hit the causes.
    if installation_id is None:
        causes.append(
            DispatchBlockedCause(
                cause=_CAUSE_AMBIGUOUS_INSTALLATION,
                # Deliberately does not report *which* installations resolved, or
                # how many: the count is another org's-shape detail that the
                # submitter does not need in order to fix it, and this string is
                # logged and rendered widely.
                detail=f"org {org_id!r} does not resolve to exactly one GitHub installation",
            )
        )

    return causes


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
    causes = await _dispatch_blocked_causes(
        db,
        org_id=actor.org_id,
        flow_id=result.flow_id,
        installation_id=installation_id,
    )

    # The installation cause keeps its ORIGINAL wording verbatim when it is the
    # cause, so a consumer matching on that string is unaffected by this change
    # (#4334 names it as a regression check). Other causes are joined onto it in
    # the order dispatch checks them.
    reasons = [
        (
            f"org {actor.org_id!r} does not resolve to exactly one GitHub installation, so no node in this flow can be "
            "dispatched; the engine will count every node undispatchable until exactly one installation is configured"
        )
        if cause.cause == _CAUSE_AMBIGUOUS_INSTALLATION
        else cause.detail
        for cause in causes
    ]
    dispatch_blocked_reason = "; ".join(reasons) if reasons else None
    dispatchable = not causes

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
        dispatchable,
    )

    if dispatch_blocked_reason is not None:
        # Logged at warning as well as returned: the submitter sees the response,
        # but whoever is watching the engine wonder why nothing moved sees this.
        # The cause ids are logged alongside the prose so a log search can find
        # every plan blocked by one cause without matching on wording.
        logger.warning(
            "plan_submitted flow=%s is undispatchable causes=%s: %s",
            result.flow_id,
            ",".join(cause.cause for cause in causes),
            dispatch_blocked_reason,
        )

    return FlowCreatedResponse(
        flow_id=result.flow_id,
        plan_version=result.plan_version,
        decision_id=result.decision_id,
        plan_hash=result.plan_hash,
        nodes_created=result.nodes_created,
        edges_created=result.edges_created,
        already_compiled=result.already_compiled,
        dispatchable=dispatchable,
        dispatch_blocked_reason=dispatch_blocked_reason,
        dispatch_blocked_causes=causes,
    )


class RecoverBindingRequest(BaseModel):
    """An operator attesting which pull request delivered a historical story (#5301).

    The operator names the PR and the basis for recovery. Immutable identity and
    current head are verified against GitHub; optional identity assertions support
    older callers and must agree with provider truth.
    """

    model_config = ConfigDict(extra="forbid")

    expected_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    provider_repository_id: int | None = Field(default=None, gt=0)
    provider_pr_node_id: str | None = Field(default=None, min_length=1, max_length=255)
    repo: str = Field(min_length=3, max_length=255, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    pr_number: int = Field(gt=0)
    head_sha: str | None = Field(default=None, min_length=7, max_length=64, pattern=r"^[0-9a-fA-F]+$")
    reason: str = Field(min_length=10, max_length=2000)
    replaces_reason: str | None = Field(default=None, min_length=10, max_length=2000)
    adopt_delivery: bool = False


class RecoverBindingResponse(BaseModel):
    """The recovered association, plus whatever still stands between it and passing."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    bound_pull_request: dict
    # Non-None when the recovery alone does not make the story completable. Recording
    # an association is not the same as satisfying the evidence, and conflating the
    # two is how a backfill quietly becomes an approval.
    remaining_hold: str | None = None


@router.post("/flows/{flow_id}/nodes/{node_id}/pull-request-recovery", response_model=RecoverBindingResponse)
async def recover_story_binding(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    node_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: RecoverBindingRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RecoverBindingResponse:
    """Attributed recovery of a story delivered before pull-request binding existed.

    For the stories this issue's fix leaves stranded: their PR merged, but no binding
    was ever registered because the contract did not exist when they ran, so
    reconciliation now holds them with `NO_BINDING`.

    Deliberately *not* automatic, and the alternatives were considered and refused.
    Searching titles or branch names for a candidate and adopting it is what #5301
    names as not-the-fix — it is a guess, and a guess that completes a story is worse
    than a hold. So a named human with approval authority states which PR delivered
    the work and why, and the row records both (`registered_by`,
    `registered_by_kind=HUMAN`, `recovery_reason`).

    Gated on `PLAN_APPROVE`, matching every other route here that changes what the
    engine will act on. `USAGE_READ` would be wrong in the other direction: this is a
    write that can let a story pass, so it belongs with approval authority rather
    than with reads.

    What it does **not** do: assert that the PR is merged, green or reviewed. The
    recovery establishes only the association, and reconciliation then verifies the
    same four requirements against the provider that a self-registered binding
    faces. So this cannot complete a story whose evidence is missing — it returns the
    remaining hold instead. That is what keeps the path from becoming a way to pass
    work by asserting it.
    """
    await access.check_permission(
        current_user,
        Permission.PLAN_APPROVE,
        target_org_id=current_user.org_id,
    )

    repo_reader = OrchestrationRepository(db)
    # Org-filtered before any node read, so a cross-tenant flow_id cannot reach it,
    # and 404 rather than 403 for the same reason `get_flow_graph` gives: a 403
    # confirms the id exists somewhere.
    flow = await repo_reader.get_flow(org_id=current_user.org_id, flow_id=flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail=f"no orchestration flow {flow_id!r} in this tenant")

    node = await repo_reader.get_node(org_id=current_user.org_id, node_id=node_id)
    if node is None or node.flow_id != flow.id or node.kind != "story":
        raise HTTPException(status_code=404, detail="story not found in this flow")

    if body.expected_revision is not None:
        from .recovery_snapshot import require_snapshot

        node = await require_snapshot(db, org_id=current_user.org_id, node_id=node_id, flow_id=flow_id, expected_revision=body.expected_revision)

    adoption_scope = None
    if body.adopt_delivery:
        from .dispatch_pass import DispatchPassConfig
        from .pr_bindings import _accepted_scope

        if node.attempts != 0 or node.state not in {"pending", "ready", "awaiting_merge", "passed"}:
            raise HTTPException(status_code=409, detail="historical adoption requires a never-dispatched story; recover its current run instead")
        if body.replaces_reason:
            raise HTTPException(status_code=409, detail="historical adoption cannot replace an existing implementation")
        if body.repo.lower() != DispatchPassConfig.from_env().repo.lower():
            raise HTTPException(status_code=409, detail="historical delivery must use the configured engine repository")
        adoption_scope = await _accepted_scope(db, node)

    # A historical dispatch may lack immutable IDs; preserve any authority it
    # does contain instead of allowing recovery to silently change repository.
    dispatch = {}
    for decision in await repo_reader.list_decisions(org_id=current_user.org_id, flow_id=flow.id):
        if decision.node_id != node.id or decision.kind != DecisionKind.NODE_DISPATCHED.value:
            continue
        try:
            detail = json.loads(decision.reason or "{}")
        except (ValueError, TypeError):
            continue
        if isinstance(detail, dict) and detail.get("attempt") == node.attempts:
            dispatch = detail
    if dispatch.get("repo") and dispatch["repo"].lower() != body.repo.lower():
        raise HTTPException(status_code=409, detail="pull request repository does not match the story dispatch")

    installation_id = await resolve_installation_id(db, org_id=current_user.org_id)
    if not installation_id:
        # Without an installation the binding could never be verified against the
        # provider, so recording it would produce a permanent hold with a confusing
        # reason. Refused up front with the actual cause.
        raise HTTPException(
            status_code=409,
            detail="this tenant has no usable GitHub installation, so a recovered binding could not be verified",
        )

    try:
        identity = await resolve_pr_identity(org_id=current_user.org_id, installation_id=installation_id, repo=body.repo, pr_number=body.pr_number)
    except PrIdentityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if (
        (body.provider_repository_id is not None and body.provider_repository_id != identity.provider_repository_id)
        or (body.provider_pr_node_id is not None and body.provider_pr_node_id != identity.provider_pr_node_id)
        or (body.head_sha is not None and body.head_sha.lower() != identity.head_sha.lower())
        or (dispatch.get("provider_repository_id") is not None and dispatch["provider_repository_id"] != identity.provider_repository_id)
    ):
        raise HTTPException(status_code=409, detail="pull request identity does not match GitHub or the story dispatch")

    if body.adopt_delivery:
        from .delivery_adoption import adopt_delivery
        from .merge_evidence import GitHubEvidenceSource

        try:
            evidence = await GitHubEvidenceSource().bound_pull_request(
                org_id=current_user.org_id,
                installation_id=installation_id,
                repo=body.repo,
                pr_number=body.pr_number,
            )
            binding = await adopt_delivery(
                db,
                org_id=current_user.org_id,
                node_id=node_id,
                pr=identity,
                installation_id=installation_id,
                actor_id=current_user.user_id,
                reason=body.reason,
                evidence=evidence,
                expected_scope=adoption_scope,
            )
        except BindingError as exc:
            raise HTTPException(status_code=409, detail=exc.message) from exc
        except Exception:
            logger.exception("Historical delivery evidence unavailable node=%s", node_id)
            raise HTTPException(status_code=409, detail="historical delivery evidence could not be verified") from None
        await db.commit()
        return RecoverBindingResponse(
            node_id=node_id,
            bound_pull_request=binding_summary(binding),
            remaining_hold=None
            if node.state == "passed"
            else "Historical delivery verified; waiting for predecessor gates and final reconciliation.",
        )

    try:
        binding = await recover_binding(
            db,
            org_id=current_user.org_id,
            node_id=node_id,
            pr=identity,
            installation_id=installation_id,
            actor_id=current_user.user_id,
            reason=body.reason,
            replaces_reason=body.replaces_reason,
        )
    except BindingError as exc:
        # UNKNOWN_RUN here means "no such story in this tenant" — 404, and the same
        # answer a cross-tenant node id gets, so neither reveals the other.
        if exc.code in (BindingRefusal.UNKNOWN_RUN, BindingRefusal.NOT_A_STORY):
            raise HTTPException(status_code=404, detail=exc.message) from exc
        raise HTTPException(status_code=409, detail=exc.message) from exc

    if binding.flow_id != flow.id:
        # The node exists in this tenant but under a different flow. Refused after
        # the fact rather than trusting the path pair, so a mismatched flow_id cannot
        # file a binding against a story the caller did not name.
        raise HTTPException(status_code=404, detail=f"story {node_id!r} is not part of flow {flow_id!r}")

    await db.commit()
    logger.info(
        "pr_binding_recovered flow=%s node=%s pr=%s#%s by=%s",
        flow.id,
        node_id,
        body.repo,
        body.pr_number,
        current_user.user_id,
    )

    refusal = completion_candidate(binding)
    return RecoverBindingResponse(
        node_id=node_id,
        bound_pull_request=binding_summary(binding),
        remaining_hold=hold_explanation(refusal)
        if refusal
        else "Pull request registered; merge, checks and current-head review verification are pending.",
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
    title: str | None = None
    description: str | None = None
    total: int
    done: int
    story_count: int
    gate_count: int
    eval_count: int
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
    execution_paused: bool = True
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
    # Current attention count, identical to display_counts.stalled.
    stalled_count: int
    display_counts: FlowDisplayCountsResponse
    total_nodes: int
    story_count: int
    gate_count: int
    eval_count: int
    changes_requested_count: int
    completed_story_count: int
    eval_story_count: int
    completed_eval_story_count: int
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


def _wave_summary(wave: WaveAggregate, metadata: dict | None = None) -> WaveSummaryResponse:
    return WaveSummaryResponse(
        epic_ref=wave.epic_ref,
        wave_ref=wave.wave_ref,
        title=(metadata or {}).get("title"),
        description=(metadata or {}).get("description"),
        total=wave.total,
        done=wave.done,
        story_count=wave.story_count,
        gate_count=wave.gate_count,
        eval_count=wave.eval_count,
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

    display_metadata = await repo.display_metadata_for_flows(org_id=current_user.org_id, flow_ids=[aggregate.flow.id for aggregate in page.flows])
    flows: list[FlowSummaryResponse] = []
    for aggregate in page.flows:
        wave_metadata = {(item["epic_ref"], item["wave_ref"]): item for item in display_metadata.get(aggregate.flow.id, {}).get("wave_metadata", [])}
        flows.append(
            FlowSummaryResponse(
                id=aggregate.flow.id,
                execution_paused=aggregate.flow.execution_paused,
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
                story_count=aggregate.story_count,
                gate_count=aggregate.gate_count,
                eval_count=aggregate.eval_count,
                changes_requested_count=aggregate.changes_requested_count,
                completed_story_count=aggregate.completed_story_count,
                eval_story_count=aggregate.eval_story_count,
                completed_eval_story_count=aggregate.completed_eval_story_count,
                epic_count=aggregate.epic_count,
                wave_count=len(aggregate.waves),
                current_wave_ref=aggregate.current_wave_ref,
                waves=[_wave_summary(wave, wave_metadata.get((wave.epic_ref, wave.wave_ref))) for wave in aggregate.waves],
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


class GateDecisionSummary(BaseModel):
    action: Literal["approved", "changes_requested"]
    reason: str | None
    created_at: str


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
    display_state: str | None = None
    title: str
    issue_ref: str | None
    attempts: int
    run_id: str | None = None
    activity: NodeActivity | None = None
    execution_history: StoryExecution | None = None
    issue_url: str | None = None
    result_summary: str | None = None
    delivery_progress: DeliveryProgress | None = None
    configuration_problem: str | None = None
    last_gate_decision: GateDecisionSummary | None = None
    # True when the most recent stall/halt decision for this node was a stall.
    #
    # Load-bearing for AC-3, and not inferable from `state`: stall detection moves
    # a stalled node to `failed` (`stall.py`), so a stall and an ordinary failure
    # are the same state. Without this flag "stalled" and "failed" cannot be told
    # apart, and the contract requires them to look different — a stall means "go
    # find out why this is wedged", a failure means the work itself failed.
    stalled: bool
    # The pull request bound to this story, and why it is not completing (#5301).
    #
    # Load-bearing for diagnosis, and the reason the original failure was expensive:
    # a story in `awaiting_merge` said only "waiting for the issue to be completed by
    # a merged pull request", which was true, unactionable, and describing something
    # that could never happen. Surfacing the bound PR answers "which PR is this
    # waiting on"; `binding_hold` answers "and what is missing" in terms an operator
    # can act on.
    #
    # Both are None for a legacy dispatch, which has no binding and keeps the old
    # generic message — so this never claims a binding exists where one does not.
    bound_pull_request: dict | None = None
    binding_hold: str | None = None
    evaluation_waiver: dict | None = None
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
    execution_paused: bool = True
    slug: str
    title: str
    intent_ref: str | None
    state: str
    created_at: str
    updated_at: str | None
    nodes: list[GraphNodeResponse]
    edges: list[GraphEdgeResponse]
    wave_metadata: list[WaveMetadata] = Field(default_factory=list)
    epic_metadata: list[EpicMetadata] = Field(default_factory=list)
    cost: FlowCostResponse
    # What the owner authorized for this delivery, or `None` when no policy is in
    # force (#5128). `None` is a real and permanent state, not a transitional one:
    # every flow accepted before policies existed has no policy and never will, and
    # those flows run with legacy semantics. So the client must render nothing rather
    # than an empty or zeroed policy — a summary reading "0 autonomous actions,
    # $0.00" describes a policy that authorizes nothing, which is the opposite of
    # what an unpolicied flow does.
    execution_policy: PolicySummary | None = None


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
    # The same loader admission uses, so the view describes the policy that is
    # actually deciding rather than a second reading of the plan document. It resolves
    # the version currently in force, which is what makes an amendment show up here
    # without a superseded version ever being shown as current.
    policy_inputs = await load_in_force_policy(db, org_id=current_user.org_id, flow_id=flow.id)
    display_metadata = await repo.display_metadata_for_flows(org_id=current_user.org_id, flow_ids=[flow.id])
    aggregate = await get_flow_cost(db, org_id=current_user.org_id, flow=flow, nodes=nodes)
    display_states = await repo.node_display_states(org_id=current_user.org_id, flow_id=flow.id)
    stalled_node_ids = await _stalled_node_ids(repo, org_id=current_user.org_id, flow_id=flow.id)

    # Only committed dispatch records can produce run links. Older attempts
    # remain in the decision log but must not masquerade as the current result.
    dispatches: dict[str, dict] = {}
    result_summaries: dict[str, dict] = {}
    gate_decisions: dict[str, GateDecisionSummary] = {}
    waivers: dict[str, dict] = {}
    observed_at: dict[str, tuple[int, str]] = {}
    admission_refusals: dict[str, dict] = {}
    from .admission_diagnostics import ACTOR as ADMISSION_ACTOR
    from .admission_diagnostics import CONTRACT as ADMISSION_CONTRACT

    for decision in await repo.list_decisions(org_id=current_user.org_id, flow_id=flow.id):
        if decision.kind == "evaluation_waived" and decision.actor_kind == "human":
            try:
                content = json.loads(decision.reason or "{}")
                waivers[decision.node_id] = dict(
                    decision_id=decision.id,
                    actor_id=decision.actor_id,
                    created_at=decision.created_at.isoformat(),
                    reason=content["reason"],
                    criterion_ids=content["criterion_ids"],
                    plan_version=content["plan_version"],
                )
            except (ValueError, KeyError, TypeError):
                pass
        if decision.kind == DecisionKind.TRANSITION_REJECTED.value and decision.actor_id == ADMISSION_ACTOR and decision.actor_kind == "service":
            try:
                refusal = json.loads(decision.rejection_reason or "{}")
                if isinstance(refusal, dict) and refusal.get("contract") == ADMISSION_CONTRACT:
                    admission_refusals[decision.node_id] = {**refusal, "observed_at": decision.created_at.isoformat()}
            except (ValueError, TypeError):
                pass
        if decision.kind == DecisionKind.NODE_DISPATCHED.value:
            admission_refusals.pop(decision.node_id, None)
        if decision.kind in (DecisionKind.GATE_APPROVED.value, DecisionKind.GATE_REJECTED.value) and decision.node_id:
            gate_decisions[decision.node_id] = GateDecisionSummary(
                action="approved" if decision.kind == DecisionKind.GATE_APPROVED.value else "changes_requested",
                reason=decision.reason,
                created_at=decision.created_at.isoformat(),
            )
        if decision.kind not in (DecisionKind.NODE_DISPATCHED.value, DecisionKind.RESULT_OBSERVED.value):
            continue
        try:
            detail = json.loads(decision.reason or "{}")
            if not isinstance(detail, dict):
                continue
            target = dispatches if decision.kind == DecisionKind.NODE_DISPATCHED.value else result_summaries
            target[decision.node_id] = detail
            if decision.kind == DecisionKind.RESULT_OBSERVED.value and isinstance(detail.get("attempt"), int):
                observed_at[decision.node_id] = (detail["attempt"], decision.created_at.isoformat())
        except (ValueError, TypeError):
            continue

    # One query for every story's bound PR (#5301), so the journey view can say which
    # pull request a waiting story is waiting on and what is missing from it.
    bindings = await active_bindings_for_flow(db, org_id=current_user.org_id, flow_id=flow.id)
    executions_by_node = await current_executions(db, org_id=current_user.org_id, flow_id=flow.id)

    # Keyed by address because that is what `get_flow_cost` returns them under.
    # Built once rather than searched per node: a linear scan inside the node loop
    # would make this quadratic in node count for no benefit.
    cost_by_address = {node_cost.address: node_cost for node_cost in aggregate.nodes}

    graph_nodes: list[GraphNodeResponse] = []
    from .plan_lineage import preserved_execution_pairs

    preserved = set(await preserved_execution_pairs(db, org_id=current_user.org_id, flow_ids=[flow_id]))
    for node in nodes:
        address = f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"
        node_cost = cost_by_address.get(address)
        dispatch = dispatches.get(node.id, {})
        if dispatch.get("attempt") != node.attempts:
            dispatch = {}
        result = result_summaries.get(node.id, {})
        if result.get("attempt") != node.attempts:
            result = {}
        # Recovered historical and completed stories retain their delivery PR.
        # Evidence is current only for the exact binding revision it observed.
        bound_pull_request: dict | None = None
        binding_hold: str | None = None
        candidate = bindings.get(node.id)
        has_current_binding = candidate is not None and not isinstance(candidate, BindingRefusal) and candidate.attempt == node.attempts
        if has_current_binding:
            bound_pull_request = binding_summary(candidate)
        if (dispatch.get("pr_binding_required") or candidate is not None) and node.state in (NodeState.RUNNING.value, NodeState.AWAITING_MERGE.value):
            if isinstance(candidate, BindingRefusal):
                binding_hold = hold_explanation(candidate)
            elif has_current_binding:
                refusal = completion_candidate(candidate)
                if refusal:
                    binding_hold = hold_explanation(refusal)
                else:
                    observed_binding = result.get("binding") or {}
                    current_observation = (
                        isinstance(observed_binding, dict)
                        and observed_binding.get("id") == candidate.id
                        and observed_binding.get("revision") == candidate.revision
                    )
                    binding_hold = (
                        result.get("evidence")
                        if current_observation
                        else "Pull request registered; merge, checks and current-head review verification are pending."
                    )
            else:
                binding_hold = hold_explanation(BindingRefusal.NO_BINDING)

        source_repo = dispatch.get("repo", "")
        source_issue = dispatch.get("issue")
        issue_url = (
            f"https://github.com/{source_repo}/issues/{source_issue}"
            if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source_repo) and isinstance(source_issue, int) and source_issue > 0
            else None
        )
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
                run_id=dispatch.get("run_id"),
                issue_url=issue_url,
                result_summary=result.get("evidence"),
                delivery_progress=node_progress(
                    node=node,
                    binding=candidate if has_current_binding else None,
                    dispatch=dispatch,
                    result=result,
                    execution=executions_by_node.get(node.id),
                    policy_enabled=policy_inputs.policy is not None or policy_inputs.refusal is not None,
                    plan_version=policy_inputs.plan_version,
                    preserved_execution=is_preserved_execution(executions_by_node.get(node.id), preserved),
                    policy_hash=policy_inputs.policy.policy_hash if policy_inputs.policy else None,
                    admission_refusal=admission_refusals.get(node.id),
                    observed_at=observed_at[node.id][1] if node.id in observed_at and observed_at[node.id][0] == node.attempts else None,
                ),
                last_gate_decision=gate_decisions.get(node.id),
                evaluation_waiver=waivers.get(node.id) if node.state == "waived" else None,
                configuration_problem=(
                    "Link an evaluation issue in the plan before this evaluation can run." if node.kind == "eval" and not node.issue_ref else None
                ),
                display_state=display_states[node.id],
                stalled=node.state == "failed" and node.id in stalled_node_ids,
                bound_pull_request=bound_pull_request,
                binding_hold=binding_hold,
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

    # Retain observed history after merge or interruption. Queued/replaced
    # stories must not borrow a previous attempt's activity.
    history_nodes = [node for node in graph_nodes if node.kind == "story" and node.run_id and node.state not in ("pending", "ready", "superseded")]
    executions = await load_story_execution(org_id=current_user.org_id, run_ids=[node.run_id for node in history_nodes])
    for node in history_nodes:
        execution = executions.get(node.run_id)
        node.execution_history = execution
        if node.state in ("running", "awaiting_merge"):
            node.activity = execution.activity if execution else None

    return FlowGraphResponse(
        flow_id=flow.id,
        execution_paused=flow.execution_paused,
        slug=flow.slug,
        title=flow.title,
        intent_ref=flow.intent_ref,
        state=flow.state,
        created_at=flow.created_at.isoformat(),
        updated_at=flow.updated_at.isoformat() if flow.updated_at else None,
        nodes=graph_nodes,
        wave_metadata=display_metadata.get(flow.id, {}).get("wave_metadata", []),
        epic_metadata=display_metadata.get(flow.id, {}).get("epic_metadata", []),
        edges=[GraphEdgeResponse(from_node_id=edge.from_node_id, to_node_id=edge.to_node_id) for edge in edges],
        cost=_flow_cost_response(flow.id, aggregate),
        execution_policy=summarize_policy(policy_inputs.policy) if policy_inputs.policy is not None else None,
    )


# ---------------------------------------------------------------------------
# Issue #5145 (ENGINE-K4): execution progress and blocks, read-only.
# ---------------------------------------------------------------------------


class ExecutionBlockResponse(BaseModel):
    """Why delivery stopped, who clears it, and what they must supply.

    Present only when the execution is actually blocked. `code` is a stable
    `BlockCode` a client may branch on; `owner` and `required_input` are what turn
    a status into a next step, which is the whole point — a bare "blocked" flag
    sends an operator to logs that expire.

    `remaining_gates` is informational. The gates themselves stay with the existing
    `controls.py`/graph state, and this route neither approves nor bypasses one.

    `progressed_at` is the last *real* progress, not the moment of blocking: the
    store deliberately does not reset it when a row blocks, because it is the clock
    that distinguishes "stuck for a minute" from "stuck since Tuesday".
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    owner: str
    required_input: str
    remaining_gates: list[str]
    progressed_at: str | None
    detail: str | None


class ExecutionActionResponse(BaseModel):
    """One externally-visible step an execution took.

    `resolved` is served explicitly rather than left to the client, because the
    derivation has a trap: `unknown` is a settled record of an *unsettled* fact, so
    a client testing `status != "prepared"` would treat an outcome nobody observed
    as resolved evidence and could show delivery as complete on the strength of it.

    `receipt_ref` null means the provider's own identifier is not recorded yet —
    **pending**, not "nothing happened". A reference that failed sanitisation also
    arrives null, which is the fail-closed direction.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    operation_key: str
    kind: str
    status: str
    attempt: int
    resolved: bool
    artifact_ref: str | None
    receipt_ref: str | None
    created_at: str | None
    observed_at: str | None


class ExecutionSummaryResponse(BaseModel):
    """One node's delivery cycle: where it is, whether it is moving, why not if not.

    Keyed by `node_id` + `cycle` because that is the ledger's own identity — a
    repair cycle is separate work with its own attempts and actions, and collapsing
    cycles would present a retry as the original attempt.

    `revision` is here so a client can reject a stale poll response: it advances by
    exactly one per applied write, making "is this older than what I already show?"
    a comparison rather than a guess about arrival order.

    **Deliberately absent: the whole authority binding** — `claim_id`,
    `claim_generation` and `accepted_plan_version`.

    The first two are what the store's authority fence tests; publishing them would
    put the values that satisfy the next authority check into a browser payload.

    `accepted_plan_version` is absent for a different reason, which
    `test_internal_plane_guard.py` caught in an earlier draft that served it: it is
    an **acceptance record** — it names which approved plan authorized this
    delivery. This router requires `PLAN_APPROVE` of any handler touching those
    records, because reading "what was approved" under a spend-read permission is an
    escalation. Both escapes were wrong: relaxing the guard, or promoting this route
    so that viewing delivery *progress* would demand approval authority. Nothing
    here needs the field — "why is delivery waiting and who acts next" is answered
    by the phase, the block and the next check, and the authorizing plan is already
    on the plans route under the permission that governs it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    node_id: str
    cycle: int
    phase: str
    status: str
    revision: int
    attempts: int
    next_check_at: str | None
    deadline_at: str | None
    progressed_at: str | None
    progress_note: str | None
    block: ExecutionBlockResponse | None
    pending_action_key: str | None
    notification_receipt_ref: str | None
    handoff_receipt_ref: str | None
    created_at: str | None
    updated_at: str | None
    actions: list[ExecutionActionResponse]
    action_overflow: bool


class FlowExecutionResponse(BaseModel):
    """Execution progress and blocks for one flow (#5145).

    `server_time` is what makes every other instant interpretable. A client
    computing "stuck for three hours" against its own clock is computing against a
    clock that may be wrong or in another zone; against this field it subtracts two
    values from the same source.

    `legacy` is true when the flow has **no execution rows at all**. That is a real
    and permanent state — every flow delivered before this ledger existed has none —
    and it means *no durable execution record*, which is emphatically not success.
    The response is still 200: the flow exists, and a 404 would say otherwise.

    `total` is the flow's whole execution count, so a client showing a page can say
    how many it is not showing.
    """

    model_config = ConfigDict(extra="forbid")

    flow_id: str
    server_time: str
    executions: list[ExecutionSummaryResponse]
    total: int
    limit: int
    offset: int
    legacy: bool


def _iso(moment: datetime | None) -> str | None:
    """Serialize an instant, or None. Always with an offset — see `_as_aware`."""
    return moment.isoformat() if moment is not None else None


@router.get("/flows/{flow_id}/execution", response_model=FlowExecutionResponse)
async def get_flow_execution(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=MAX_EXECUTIONS_PER_PAGE)] = MAX_EXECUTIONS_PER_PAGE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FlowExecutionResponse:
    """Execution progress and blocks for one flow. Read-only (#5145).

    Answers the question the graph view cannot: *why* is delivery waiting, and who
    acts next. The graph's `state` says a story is running; this says it has been
    blocked for three hours on a human gate, names who must approve it and what
    they must supply.

    **Gated on `USAGE_READ`**, the same permission as the flow-graph and cost reads,
    and checked before any database read so a denied caller cannot learn whether the
    flow exists. No new permission is introduced: this adds no control and no
    acceptance authority, and existing `controls.py` remains the sole human
    approval/recovery surface.

    **Tenant isolation.** The flow is resolved under the caller's own authenticated
    `org_id` before any ledger read, and the ledger query carries `org_id` in its
    own predicate as well. An unknown or cross-tenant `flow_id` returns the same
    **404** every other route in this router gives — never 403, which would confirm
    the id exists somewhere and let a caller enumerate flows by status code.

    **Why this does not call `execution_store.load_execution`.** That entry point
    requires the `claim_id`/`claim_generation` of the work claim a caller holds, and
    an operator holds none; a read presenting a claim it does not own reaches the
    store's `claim_mismatch` arm, which withholds the record by design so a refusal
    cannot disclose the binding that would satisfy it. The correct scoping is the
    caller's own org — the access path `ix_orchestration_executions_flow_id` exists
    for — and the store's fence is left untouched. See `execution_read.py`.

    **An empty ledger is 200 with `legacy=true`**, not 404 and not an implied
    success: the flow exists, and "no execution record" is the honest answer for
    every flow delivered before this ledger existed.
    """
    await access.check_permission(
        current_user,
        Permission.USAGE_READ,
        target_org_id=current_user.org_id,
    )

    repo = OrchestrationRepository(db)

    # Org-filtered resolution BEFORE any ledger read, so a cross-tenant flow_id can
    # never reach it. Same 404 as the rest of the router.
    flow = await repo.get_flow(org_id=current_user.org_id, flow_id=flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail=f"no orchestration flow {flow_id!r} in this tenant")

    view = await load_flow_execution_view(
        db,
        org_id=current_user.org_id,
        flow_id=flow.id,
        limit=limit,
        offset=offset,
    )

    return FlowExecutionResponse(
        flow_id=view.flow_id,
        server_time=view.server_time.isoformat(),
        total=view.total,
        limit=view.limit,
        offset=view.offset,
        legacy=view.legacy,
        executions=[
            ExecutionSummaryResponse(
                id=execution.id,
                node_id=execution.node_id,
                cycle=execution.cycle,
                # `.value` on every enum: a `StrEnum` serializes as its value anyway,
                # but being explicit keeps the wire format independent of that.
                phase=execution.phase.value,
                status=execution.status.value,
                revision=execution.revision,
                attempts=execution.attempts,
                next_check_at=_iso(execution.next_check_at),
                deadline_at=_iso(execution.deadline_at),
                progressed_at=_iso(execution.progressed_at),
                progress_note=execution.progress_note,
                block=(
                    ExecutionBlockResponse(
                        code=execution.block.code.value,
                        owner=execution.block.owner,
                        required_input=execution.block.required_input,
                        remaining_gates=list(execution.block.remaining_gates),
                        progressed_at=_iso(execution.block.progressed_at),
                        detail=execution.block.detail,
                    )
                    if execution.block is not None
                    else None
                ),
                pending_action_key=execution.pending_action_key,
                notification_receipt_ref=execution.notification_receipt_ref,
                handoff_receipt_ref=execution.handoff_receipt_ref,
                created_at=_iso(execution.created_at),
                updated_at=_iso(execution.updated_at),
                action_overflow=execution.action_overflow,
                actions=[
                    ExecutionActionResponse(
                        id=action.id,
                        operation_key=action.operation_key,
                        kind=action.kind,
                        status=action.status.value,
                        attempt=action.attempt,
                        resolved=action.resolved,
                        artifact_ref=action.artifact_ref,
                        receipt_ref=action.receipt_ref,
                        created_at=_iso(action.created_at),
                        observed_at=_iso(action.observed_at),
                    )
                    for action in execution.actions
                ],
            )
            for execution in view.executions
        ],
    )


router.include_router(continuation_router)
router.include_router(flow_controls_router)
router.include_router(shared_amendment_router)
router.include_router(shared_budget_router)
router.include_router(shared_concurrency_router)
router.include_router(shared_retry_router)
router.include_router(shared_window_router)
router.include_router(evaluation_acceptance_router)
router.include_router(evaluation_waiver_router)

router.include_router(draft_revision_router)
