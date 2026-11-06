"""Operator-plane REST API for the amendment review loop (#5329).

The interaction this completes: **propose -> preview the graph change -> edit -> approve
-> a new plan version, with eligible work scheduled automatically.** #4529 built the two
ends (an author files a `PENDING` draft; a named human accepts it) and #5331 gave the CLI
its `adp flow` foundation. What had no HTTP surface at all was the middle — a reviewer
could not enumerate what was waiting, read one, see what it would do to the graph,
disagree with it, or decline it.

Eight routes on the operator plane. Human mutation requires
`Permission.PLAN_APPROVE`; list/show/preview additionally permit an authenticated,
assignment-bound author to read only its own draft:

    POST   /flows/{flow_id}/amendments/requests           request authored suggestion
    POST   /flows/{flow_id}/amendments/proposals          propose (human-originated)
    GET    /flows/{flow_id}/amendments/drafts             list
    GET    /flows/{flow_id}/amendments/drafts/{draft_id}  show
    POST   /flows/{flow_id}/amendments/drafts/{id}/preview  preview the graph change
    POST   /flows/{flow_id}/amendments/drafts/{id}/edit     new revision
    POST   /flows/{flow_id}/amendments/drafts/{id}/reject    decline
    POST   /flows/{flow_id}/amendments/drafts/{id}/accept    apply, exact revision bound

Why a separate module from `routes.py`
--------------------------------------
Not for a weaker mutation permission — every human write here is `PLAN_APPROVE`,
the same gate `create_amendment` uses. `routes.py` has a CI guard
(`test_internal_plane_guard.py::test_orchestration_routes_require_plan_approve`) that
demands the literal `Permission.PLAN_APPROVE` in every non-GET handler *and* in anything
reading acceptance records; these routes satisfy it, and they are registered onto that
router so the guard covers them. The split is for size: `routes.py` is already ~1900
lines, and this is one coherent feature with its own response models.

The line this module must not cross
-----------------------------------
`draft_routes.py`'s allowlist states it directly: **no acceptance endpoint may ever be
added to the `PLAN_DRAFT` router**, because an agent-reachable accept — even one
restricted to drafts that agent authored — is the self-approval the EPIC exists to
prevent. That is the reason accept, reject and edit are *here* rather than next to
`register_amendment_draft`. Editing is on this side too, which is worth being explicit
about: an edit chooses what a human will be asked to approve, so an agent that could edit
a draft could rewrite it after a reviewer read it.

Preview requires `PLAN_APPROVE` for ordinary callers. An assignment-bound author may
read only the draft registered by its own run under `PLAN_DRAFT`; tenant, flow, request,
and run binding are all checked together, and that exception grants no edit, reject, or
accept authority.

What this module does NOT decide
--------------------------------
Every refusal rule belongs to the layer that enforces it, and is imported rather than
restated:

  - **placement** -> `amendment_placement.resolve_placement`. Ambiguity comes back as
    named blockers with `document=None`, so an unresolved placement cannot be previewed
    into an approvable document.
  - **impact / blocking diagnostics** -> `amendment_impact.assess_amendment`, which asks
    `validate_proposal`, `amend.prerequisites_change_refused`,
    `amend.definition_change_refusal` and `state.transition` the same questions
    acceptance asks. A preview that computed its own answers would drift from the accept
    that follows it, and a preview nobody can rely on is worse than none.
  - **revision identity, staleness, self-approval, idempotency** ->
    `pending_amendments.accept_amendment`, which is `amend_plan` under the accepting
    human's own context.

Route prefix is `/orchestration`, NOT `/api/orchestration` — CloudFront strips the `/api`
prefix before the ALB, so a handler carrying it would be unreachable in production. See
`tests/test_route_prefix_convention.py`.
"""

import logging
from dataclasses import asdict, replace
from typing import Annotated
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.budget.run_binding import RunBindingResolver
from src.orchestration.amend import AmendmentContext, FlowNotFoundError
from src.orchestration.amendment_impact import AmendmentImpact, assess_amendment
from src.orchestration.amendment_placement import PlacementRequest, resolve_placement
from src.orchestration.compile import ProposalRejectedError, TenantMismatchError, plan_hash
from src.orchestration.draft_binding import DraftBindingError, resolve_draft_tenant
from src.orchestration.draft_routes import get_run_binding_resolver
from src.orchestration.execution_policy import PolicySummary
from src.orchestration.models import DecisionKind, OrchestrationDecision, OrchestrationFlow, PendingAmendmentState
from src.orchestration.pending_amendments import (
    AmendmentConflictError,
    AmendmentDraftNotFoundError,
    accept_amendment,
    assign_author_run,
    get_pending_amendment,
    in_force_plan,
    list_pending_amendments,
    publish_for_acceptance,
    record_replan_request,
    register_amendment_draft,
    reject_amendment,
    resolve_authoring_request,
    revise_amendment,
)
from src.orchestration.proposal import LoopProposal, Violation
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.state import ActorKind
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

RUN_ID_HEADER = "X-Agent-RunId"

logger = logging.getLogger("bedrockgateway.orchestration.amendment_review")

#: No prefix here: this router is included into `routes.py`'s, which supplies
#: `/orchestration`. Declaring it again would produce `/orchestration/orchestration/...`.
#: Same convention as `continuation_routes` and `evaluation_acceptance_routes`.
router = APIRouter()


async def get_access_control(db: Annotated[AsyncSession, Depends(get_db)]) -> AccessControl:
    """Get access control instance."""
    return AccessControl(db)


async def _resolve_actor_role(access: AccessControl, current_user: TokenContext) -> str:
    """The caller's role, resolved server-side by `routes.py`'s own helper.

    Imported inside the call rather than at module scope because `routes.py` imports this
    module's router to register it, so a top-level import would be circular. Same
    deferred-import pattern as `shared_amendment_routes.call`.
    """
    from src.orchestration.routes import _resolve_actor_role as resolve

    return await resolve(access, current_user)


# --- Response models ------------------------------------------------------------------


class ViolationModel(BaseModel):
    """A named reason acceptance is unavailable.

    One shape for placement blockers, validation violations and state-authority refusals,
    because to a reviewer they are the same class of answer. Two shapes would make the API
    and the CLI each decide which list to render first.
    """

    model_config = ConfigDict(extra="forbid")

    rule: str
    message: str
    where: str | None = None


class AddedEdgeModel(BaseModel):
    """An edge the placement added, with the rule that produced it.

    The reason is the payload. A reviewer can see *that* a gate now waits on a new node;
    what they cannot see from the graph is whether that was the intent or a side effect of
    the placement rule.
    """

    model_config = ConfigDict(extra="forbid")

    from_address: str
    to_address: str
    reason: str


class NodeDispositionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str
    change: str
    kind: str
    title: str
    #: None for an added node: it has no execution state yet.
    state: str | None = None
    attempts: int = 0
    concluded_by: str | None = None
    issue_ref: str | None = None
    proposal_identity: str | None = None
    repository_identity: str | None = None
    provider_repository_id: int | None = None
    execution_owner: str | None = None
    execution_adapter: str | None = None
    criteria: list[str] = Field(default_factory=list)
    required_inputs: list[dict] = Field(default_factory=list)
    evidence_expectations: list[str] = Field(default_factory=list)
    before: dict | None = None
    after: dict | None = None


class EdgeChangeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_address: str
    to_address: str
    change: str


class WaveMembershipChangeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wave: str
    added_addresses: list[str] = Field(default_factory=list)
    removed_addresses: list[str] = Field(default_factory=list)
    is_new: bool = False
    is_emptied: bool = False


class ExecutionStageModel(BaseModel):
    """One step of the proposed execution order.

    `stage=None` means the waves in it are mutually dependent and cannot be staged — a
    real answer, not missing data, and one a reviewer needs before approving.
    """

    model_config = ConfigDict(extra="forbid")

    stage: int | None = None
    waves: list[str] = Field(default_factory=list)


class NodeImpactModel(BaseModel):
    """A node whose prerequisites move. Only these are downstream-affected.

    A node whose prerequisites are unchanged is unaffected no matter how much of the graph
    around it moved, because the engine releases it on exactly the same condition as
    before. `newly_blocked` flags the direction that matters: gaining a prerequisite can
    park a node behind work nobody has scheduled.
    """

    model_config = ConfigDict(extra="forbid")

    address: str
    state: str
    added_prerequisites: list[str] = Field(default_factory=list)
    removed_prerequisites: list[str] = Field(default_factory=list)
    newly_blocked: bool = False


class EffectiveLimitsModel(BaseModel):
    """Limits dispatch would enforce on either side of an amendment, including carried receipts."""

    model_config = ConfigDict(extra="forbid")

    max_spend_usd: str
    max_attempts_per_node: int
    max_concurrent_actions: int
    max_wall_clock_seconds: int
    expires_at: str


class IssuePublicationModel(BaseModel):
    """New work acceptance will bind to an issue, and whether that issue is NEW.

    `create` means accepting will open a new issue in `repository`; `pending` means a
    publication was started earlier and acceptance will reconcile it (never a second
    issue); `reuse`/`update` mean the issue is already recorded (`update` refreshes its
    public title/body). Nothing is published before a human accepts.
    """

    model_config = ConfigDict(extra="forbid")

    address: str
    proposal_identity: str
    repository: str
    action: str
    creates_issue: bool
    issue_number: int | None = None
    url: str | None = None


class ImpactModel(BaseModel):
    """The whole proposed graph change, as a reviewer must see it before approving."""

    model_config = ConfigDict(extra="forbid")

    flow_id: str
    base_plan_version: int | None = None
    base_plan_hash: str | None = None
    product_before: str | None = None
    product_after: str | None = None
    nodes: list[NodeDispositionModel] = Field(default_factory=list)
    edges: list[EdgeChangeModel] = Field(default_factory=list)
    before_nodes: list[dict] = Field(default_factory=list)
    after_nodes: list[dict] = Field(default_factory=list)
    before_edges: list[dict] = Field(default_factory=list)
    after_edges: list[dict] = Field(default_factory=list)
    wave_changes: list[WaveMembershipChangeModel] = Field(default_factory=list)
    execution_order: list[ExecutionStageModel] = Field(default_factory=list)
    downstream: list[NodeImpactModel] = Field(default_factory=list)
    gates_added: list[str] = Field(default_factory=list)
    gates_removed: list[str] = Field(default_factory=list)
    policy_before: PolicySummary | None = None
    policy_after: PolicySummary | None = None
    policy_changed: bool = False
    #: Effective limits now and after accepting the proposed plan. A compatible
    #: amendment carries verified receipts; incompatible amendments list lost
    #: receipts and their effective before/after values separately.
    effective_limits_before: EffectiveLimitsModel | None = None
    effective_limits_after: EffectiveLimitsModel | None = None
    receipts_preserved: list[dict] = Field(default_factory=list)
    receipts_dropped: list[dict] = Field(default_factory=list)
    issue_publications: list[IssuePublicationModel] = Field(default_factory=list)
    #: Every reason acceptance would refuse, not just the first. `amend_plan` raises on
    #: the first problem it hits; a reviewer fixing one at a time learns the next only by
    #: trying again, so the preview reports them all.
    blocking: list[ViolationModel] = Field(default_factory=list)
    acceptable: bool
    is_noop: bool


class GateDiffModel(BaseModel):
    """Gate placement changes — the consequence a document diff does not reveal.

    A removed gate is a removed human decision point and reads as an ordinary edit in a
    document diff, which is why this is computed from the two documents rather than from an
    author's summary of what it changed.

    `unchanged` is carried, not just the deltas: it is what distinguishes "this plan has no
    gates" from "this plan's gates are all still there", and a reviewer reading an empty
    `added`/`removed` cannot tell those apart otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    changes_gating: bool = False


class DraftSummary(BaseModel):
    """One pending amendment, without its document.

    Documents are whole plan proposals; a page of them is megabytes, so `list` omits them
    and `show` returns one.
    """

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    flow_id: str
    state: str
    proposal_hash: str
    base_plan_version: int | None = None
    base_plan_hash: str | None = None
    #: False means the plan moved since this draft was authored, so acceptance will refuse
    #: as stale. Reported here so a reviewer learns it before spending time on a review.
    base_is_current: bool
    requested_by: str | None = None
    request_text: str | None = None
    #: Set when this draft is a revision, naming the draft it was edited from.
    revision_of_draft_id: str | None = None
    gate_diff: GateDiffModel
    created_at: str | None = None
    decided_at: str | None = None
    decision_actor: str | None = None
    decision_id: str | None = None
    decision_reason: str | None = None
    accepted_plan_version: int | None = None
    approved_diff: dict | None = None


class DraftDetail(DraftSummary):
    """One pending amendment with its full proposal document."""

    proposal_document: dict | None = None


class ProposalCreatedResponse(BaseModel):
    """A human-originated proposal, filed as an inert pending draft.

    `resolved_edges` is what makes the placement auditable: the reviewer sees the exact
    incoming and outgoing edges the placement produced, with the rule behind each, rather
    than a wave number they have to trust. Wave membership or visual order alone would not
    let anyone confirm the work landed where they meant.
    """

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    flow_id: str
    proposal_hash: str
    base_plan_version: int | None = None
    added_nodes: list[str] = Field(default_factory=list)
    resolved_edges: list[AddedEdgeModel] = Field(default_factory=list)
    impact: ImpactModel


class AuthoringRequestResponse(BaseModel):
    """A durable text request waiting for the bounded amendment authoring path."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    flow_id: str
    state: str
    base_plan_version: int | None = None
    base_plan_hash: str | None = None
    already_requested: bool = False


class RevisionResponse(BaseModel):
    """The outcome of an edit.

    `unchanged` true means the edit produced an identical document and no new revision was
    written — a no-op edit must not churn the revision a reviewer is reading. In that case
    `draft_id` is the original's.
    """

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    flow_id: str
    proposal_hash: str
    superseded_draft_id: str | None = None
    unchanged: bool = False
    gate_diff: GateDiffModel
    impact: ImpactModel


class AcceptanceResponse(BaseModel):
    """The plan version an acceptance produced.

    `already_accepted` true means this draft was already applied and this call read back
    the original outcome rather than amending again — retries are idempotent, and a retry
    must not be reported as a second plan version.

    `superseded_draft_ids` names the sibling drafts this acceptance displaced. Concurrent
    acceptance yields one valid successor, and these are the proposals that lost; a
    reviewer who was reading one of them needs to know it is gone rather than discovering
    it at their own accept.
    """

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    flow_id: str
    plan_version: int
    superseded_version: int | None = None
    decision_id: str
    gate_diff: GateDiffModel
    superseded_draft_ids: list[str] = Field(default_factory=list)
    already_accepted: bool = False
    receipts_preserved: list[dict] = Field(default_factory=list)
    receipts_dropped: list[dict] = Field(default_factory=list)
    effective_limits_before: EffectiveLimitsModel | None = None
    effective_limits_after: EffectiveLimitsModel | None = None


# --- Request bodies -------------------------------------------------------------------


class ProposalBody(BaseModel):
    """A placement-based proposal from a human.

    The placement is the substance: `work` says what to add, `placement` says where it
    goes in terms of explicit node addresses. `request_text` is the human's own words,
    stored verbatim as data and carried to the reviewer as the stated ask.
    """

    model_config = ConfigDict(extra="forbid")

    placement: PlacementRequest
    request_text: str = Field(min_length=1, max_length=2000)


class AuthoringRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_text: str = Field(min_length=1, max_length=2000)


class EditBody(BaseModel):
    """A replacement document, whole.

    Not a patch: acceptance re-validates a full document, and a patch format here would
    need its own validator that could drift from it.
    """

    model_config = ConfigDict(extra="forbid")

    proposal: LoopProposal
    reason: str | None = Field(default=None, max_length=2000)


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)


class AcceptBody(BaseModel):
    """An approval, bound to the exact revision the human reviewed.

    The REST contract requires all three reviewed identities. Legacy GitHub comment
    compatibility remains isolated in the lower-level acceptance service, where the
    named draft is protected by revision supersession.
    """

    model_config = ConfigDict(extra="forbid")

    expected_proposal_hash: str = Field(min_length=64, max_length=64)
    expected_base_plan_version: int = Field(ge=1)
    expected_base_plan_hash: str = Field(min_length=64, max_length=64)
    reason: str | None = Field(default=None, max_length=2000)


# --- Serialisation --------------------------------------------------------------------


def _violations(items) -> list[ViolationModel]:
    return [ViolationModel(rule=item.rule, message=item.message, where=item.where) for item in items]


def _gate_diff(diff) -> GateDiffModel:
    return GateDiffModel(
        added=list(diff.added),
        removed=list(diff.removed),
        unchanged=list(diff.unchanged),
        changes_gating=diff.changes_gating,
    )


def _limits_model(limits) -> EffectiveLimitsModel | None:
    return EffectiveLimitsModel(**asdict(limits)) if limits is not None else None


def _impact_model(impact: AmendmentImpact) -> ImpactModel:
    return ImpactModel(
        flow_id=impact.flow_id,
        base_plan_version=impact.base_plan_version,
        base_plan_hash=impact.base_plan_hash,
        product_before=impact.product_before,
        product_after=impact.product_after,
        nodes=[
            NodeDispositionModel(
                address=node.address,
                change=node.change,
                kind=node.kind,
                title=node.title,
                state=node.state,
                attempts=node.attempts,
                concluded_by=node.concluded_by,
                issue_ref=node.issue_ref,
                proposal_identity=node.proposal_identity,
                repository_identity=node.repository_identity,
                provider_repository_id=node.provider_repository_id,
                execution_owner=node.execution_owner,
                execution_adapter=node.execution_adapter,
                criteria=list(node.criteria),
                required_inputs=list(node.required_inputs),
                evidence_expectations=list(node.evidence_expectations),
                before=node.before,
                after=node.after,
            )
            for node in impact.nodes
        ],
        edges=[EdgeChangeModel(from_address=edge.from_address, to_address=edge.to_address, change=edge.change) for edge in impact.edges],
        before_nodes=list(impact.before_nodes),
        after_nodes=list(impact.after_nodes),
        before_edges=list(impact.before_edges),
        after_edges=list(impact.after_edges),
        wave_changes=[
            WaveMembershipChangeModel(
                wave=change.wave,
                added_addresses=list(change.added_addresses),
                removed_addresses=list(change.removed_addresses),
                is_new=change.is_new,
                is_emptied=change.is_emptied,
            )
            for change in impact.wave_changes
        ],
        execution_order=[ExecutionStageModel(stage=stage.stage, waves=list(stage.waves)) for stage in impact.execution_order],
        downstream=[
            NodeImpactModel(
                address=item.address,
                state=item.state,
                added_prerequisites=list(item.added_prerequisites),
                removed_prerequisites=list(item.removed_prerequisites),
                newly_blocked=item.newly_blocked,
            )
            for item in impact.downstream
        ],
        gates_added=list(impact.gates_added),
        gates_removed=list(impact.gates_removed),
        policy_before=impact.policy_before,
        policy_after=impact.policy_after,
        policy_changed=impact.policy_changed,
        effective_limits_before=_limits_model(impact.effective_limits_before),
        effective_limits_after=_limits_model(impact.effective_limits_after),
        receipts_preserved=list(impact.receipts_preserved),
        receipts_dropped=list(impact.receipts_dropped),
        issue_publications=[
            IssuePublicationModel(
                address=plan.address,
                proposal_identity=plan.proposal_identity,
                repository=plan.repository,
                action=plan.action,
                creates_issue=plan.creates_issue,
                issue_number=plan.issue_number,
                url=plan.url,
            )
            for plan in impact.issue_publications
        ],
        blocking=_violations(impact.blocking),
        acceptable=impact.acceptable,
        is_noop=impact.is_noop,
    )


def _summary_fields(view) -> dict:
    return {
        "draft_id": view.draft_id,
        "flow_id": view.flow_id,
        "state": view.state,
        "proposal_hash": view.proposal_hash,
        "base_plan_version": view.base_plan_version,
        "base_plan_hash": view.base_plan_hash,
        "base_is_current": view.base_is_current,
        "requested_by": view.requested_by,
        "request_text": view.request_text,
        "revision_of_draft_id": view.revision_of_draft_id,
        "gate_diff": _gate_diff(view.gate_diff),
        "created_at": view.created_at.isoformat() if view.created_at else None,
        "decided_at": view.decided_at.isoformat() if view.decided_at else None,
        "decision_actor": view.accepted_by,
        "decision_id": view.decision_id,
        "decision_reason": view.decision_reason,
        "accepted_plan_version": view.accepted_plan_version,
        "approved_diff": view.approved_diff,
    }


# --- Shared plumbing ------------------------------------------------------------------

#: Conflict codes a caller resolves by retrying with fresh information, versus ones that
#: are terminal for this draft. Surfaced so the CLI can exit non-zero with a message that
#: says which it is, rather than making the human guess from prose.
#:
#: Exactly one member, and it is checked against `pending_amendments`' real raise sites
#: rather than aspirational: `draft_already_accepted` is the only code that is terminal.
#: `draft_not_pending` is retryable in this sense — an edited draft has a live successor
#: revision to read — and `revision_mismatch`, `base_version_mismatch` and `stale_base`
#: all resolve by re-reading. A code listed here that nothing raises is worse than
#: useless: it reads as an enforced refusal to anyone auditing this file for the
#: no-self-approval rule, which is in fact enforced structurally — accept lives only on
#: this `PLAN_APPROVE` router, never on the agent-reachable `PLAN_DRAFT` one.
_TERMINAL_CONFLICTS = frozenset({"draft_already_accepted"})


def _conflict(error: AmendmentConflictError) -> HTTPException:
    """409 with a machine-readable code and the store's own human-facing message.

    The message is passed through rather than rewritten. `pending_amendments` composes it
    with the specific ids and versions involved and, for a superseded draft, decides
    whether to name a successor revision to read or to route the human to a fresh replan —
    a distinction this layer has no way to re-derive.

    The code goes under `detail["error"]`, which is not a cosmetic choice: `adp_common.Api`
    reads exactly `detail["error"]` (falling back to `detail["reason"]`) to turn an HTTP
    failure into a `CliError` code, and every other orchestration router — `draft_routes`,
    `intake_routes` — already spells it that way. Under `detail["code"]` the transport
    would find nothing, and `adp flow amendment accept` against a moved plan would surface
    as a generic `http_error` instead of the `stale_base` the CLI must exit nonzero on and
    name. `"code"` is kept as a duplicate for the dashboard, which reads that key.
    """
    return HTTPException(
        status_code=409,
        detail={
            "error": error.code,
            "code": error.code,
            "message": error.message,
            "retryable": error.code not in _TERMINAL_CONFLICTS,
        },
    )


async def _actor(access: AccessControl, current_user: TokenContext, reason: str | None) -> AmendmentContext:
    """The accepting human's server-resolved context.

    Every field comes from the verified token and the access-control lookup, never from the
    request body — an actor a caller could name is an actor a caller could forge, and this
    identity is what "no agent self-approval" is enforced against.
    """
    if current_user.account_type != "human" or current_user.auth_source != "jwt":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "human_approval_required",
                "code": "human_approval_required",
                "message": "Plan amendment decisions require a signed-in human identity; service and agent credentials cannot approve them.",
            },
        )
    return AmendmentContext(
        org_id=current_user.org_id,
        actor_id=current_user.user_id,
        actor_role=await _resolve_actor_role(access, current_user),
        actor_kind=ActorKind.HUMAN,
        username=current_user.cognito_username,
        reason=reason,
    )


def _not_found(code: str, message: str) -> HTTPException:
    """A 404 that says which kind of 404 it is.

    Load-bearing for the CLI, and the reason these carry a code at all. `adp flow
    amendment` must distinguish "this gateway does not serve the amendment API" — a
    bare routing 404, where the honest answer is "ask an administrator to upgrade, and
    note that applying a plan is NOT a fallback" — from "no such draft", where the
    answer is "check the id". With a plain-string detail both arrive as
    `adp_common.Api`'s generic `http_error` and the CLI has to guess, so a mistyped
    draft id would tell an operator their deployment is too old.

    Absent and another tenant's remain deliberately indistinguishable from each other;
    a distinguishable answer there would turn these routes into an existence oracle for
    another tenant's ids. The code separates *route* from *resource*, never tenant from
    tenant.
    """
    return HTTPException(status_code=404, detail={"error": code, "code": code, "message": message})


async def _require_flow(db: AsyncSession, *, org_id: str, flow_id: str) -> None:
    """404 for a flow that is absent or another tenant's — the two are indistinguishable."""
    found = await db.scalar(select(OrchestrationFlow.id).where(OrchestrationFlow.org_id == org_id, OrchestrationFlow.id == flow_id))
    if found is None:
        raise _not_found("flow_not_found", f"flow {flow_id!r} not found")


async def _read_scope(
    *,
    current_user: TokenContext,
    access: AccessControl,
    run_bindings: RunBindingResolver,
    run_id: str | None,
) -> tuple[str, str | None]:
    if current_user.account_type == "human":
        await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
        return current_user.org_id, None
    await access.check_permission(current_user, Permission.PLAN_DRAFT, target_org_id=current_user.org_id)
    if not run_id:
        raise _not_found("draft_not_found", "no amendment draft is authorized for this run")
    org_id = current_user.org_id
    if current_user.scope == "internal":
        try:
            org_id = await resolve_draft_tenant(run_id=run_id, resolver=run_bindings)
        except DraftBindingError as error:
            raise _not_found("draft_not_found", "no amendment draft is authorized for this run") from error
    return org_id, run_id


async def _impact_of(
    db: AsyncSession, *, org_id: str, flow_id: str, document: dict, draft=None, actor_id: str | None = None, actor_username: str = ""
) -> ImpactModel:
    """Assess a stored draft document. Read-only.

    `actor_kind=HUMAN` because the reviewer reading this preview is the one who would
    accept, and supersede authority differs by actor kind: `passed -> superseded` is
    human-only in `LEGAL_TRANSITIONS`, so the same document can be acceptable for a human
    and refused for a SERVICE actor. Assessing as anything else here would show a reviewer
    blockers that do not apply to them.
    """
    impact = await assess_amendment(
        db,
        org_id=org_id,
        flow_id=flow_id,
        proposal=LoopProposal(**document),
        actor_kind=ActorKind.HUMAN,
        actor_id=actor_id,
        actor_username=actor_username,
    )
    lifecycle = []
    if draft is not None:
        if draft.state != PendingAmendmentState.PENDING.value:
            lifecycle.append(
                Violation(
                    rule="draft_not_pending",
                    message=f"This amendment is {draft.state} and cannot be accepted. Read the current revision or request a fresh replan.",
                    where=draft.draft_id,
                )
            )
        if not draft.base_is_current:
            lifecycle.append(
                Violation(
                    rule="stale_base",
                    message=(
                        "The accepted plan changed after this amendment was authored. Refresh and re-author it against "
                        "the current plan; it will not be rebased silently."
                    ),
                    where=draft.draft_id,
                )
            )
    if lifecycle:
        impact = replace(impact, blocking=[*impact.blocking, *lifecycle])
    return _impact_model(impact)


# --- Routes ---------------------------------------------------------------------------


@router.post("/flows/{flow_id}/amendments/requests", response_model=AuthoringRequestResponse, status_code=202)
async def request_authored_amendment(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: AuthoringRequestBody,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuthoringRequestResponse:
    """Record a text-only amendment request for the existing bounded authoring path."""
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    actor = await _actor(access, current_user, body.request_text)
    await _require_flow(db, org_id=actor.org_id, flow_id=flow_id)
    in_force = await in_force_plan(db, org_id=actor.org_id, flow_id=flow_id)
    if in_force is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_plan_in_force",
                "code": "no_plan_in_force",
                "message": f"flow {flow_id!r} has no accepted plan to amend.",
                "retryable": False,
            },
        )

    decision_id = str(
        uuid5(
            NAMESPACE_URL,
            f"adp:amendment-request:{actor.org_id}:{flow_id}:{actor.actor_id}:{in_force.plan_hash}:{body.request_text}",
        )
    )
    decision = await db.scalar(
        select(OrchestrationDecision).where(
            OrchestrationDecision.org_id == actor.org_id,
            OrchestrationDecision.id == decision_id,
        )
    )
    if decision is None:
        decision = await OrchestrationRepository(db).append_decision(
            org_id=actor.org_id,
            flow_id=flow_id,
            decision_id=decision_id,
            kind=DecisionKind.REPLAN_REQUESTED.value,
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
            actor_kind=ActorKind.HUMAN.value,
            reason=body.request_text,
        )
    request = await record_replan_request(
        db,
        org_id=actor.org_id,
        flow_id=flow_id,
        replan_decision_id=decision.id,
        requested_by=actor.actor_id,
        request_text=body.request_text,
    )
    await db.commit()
    return AuthoringRequestResponse(
        request_id=request.id,
        flow_id=request.flow_id,
        state=request.state,
        base_plan_version=request.base_plan_version,
        base_plan_hash=request.base_plan_hash,
        already_requested=not request.created,
    )


@router.post("/flows/{flow_id}/amendments/proposals", response_model=ProposalCreatedResponse, status_code=201)
async def propose_amendment(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: ProposalBody,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProposalCreatedResponse:
    """Propose work with an explicit graph placement. Writes no graph state.

    The placement resolves to concrete node addresses and edges via
    `resolve_placement`, which is strictly additive: it never removes an existing
    dependency when inserting work. An ambiguous or unresolvable placement comes back as
    422 with named blockers and **no draft is filed** — presenting alternatives or an
    unresolved prerequisite before anything is approvable is the requirement, and a draft
    holding a placement guessed one way is the input to an approval nobody reviewed.

    The result is a `PENDING` draft, inert in exactly the sense #4529 established: one row
    holding a document, referenced by no graph query. It becomes real only through
    `accept`, which is a separate act by a named human.

    Returns 403 without `PLAN_APPROVE`, 404 for a flow outside the caller's tenant, 409
    when no plan is in force to amend, and 422 for an unresolvable placement or a document
    that fails validation.
    """
    # Gate first, before any read or write, so a denied caller cannot learn whether the
    # flow exists. Same ordering as `create_amendment`.
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    actor = await _actor(access, current_user, body.request_text)
    org_id = actor.org_id
    actor_role = actor.actor_role
    await _require_flow(db, org_id=org_id, flow_id=flow_id)

    in_force = await in_force_plan(db, org_id=org_id, flow_id=flow_id)
    if in_force is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_plan_in_force",
                "code": "no_plan_in_force",
                "message": f"flow {flow_id!r} has no accepted plan to amend.",
                "retryable": False,
            },
        )

    base = LoopProposal(**in_force.plan_document)
    if base.execution_policy is not None:
        # Only this gateway-owned accepted document may shed its old stamp.
        # A new human acceptance will supply its own authenticated principal;
        # edit/accept still reject stamped fields supplied by clients.
        from .execution_policy import _STAMPED_FIELDS

        base = base.model_copy(update={"execution_policy": base.execution_policy.model_copy(update=dict.fromkeys(_STAMPED_FIELDS))})
    resolution = resolve_placement(base=base, request=body.placement)
    if not resolution.resolved:
        # 422 with the blockers, and nothing written. The blockers name what a human must
        # decide — which of two anchors, or a prerequisite that does not exist yet.
        raise HTTPException(
            status_code=422,
            detail={
                "error": "placement_unresolved",
                "code": "placement_unresolved",
                "message": "the placement does not resolve to an unambiguous graph position.",
                "violations": [violation.model_dump() for violation in _violations(resolution.blockers)],
            },
        )

    # A human-originated proposal still goes through #4529's request/assignment chain: the
    # draft's base version is captured on the REQUEST, and the request is the attribution
    # root. Reusing it rather than adding a second way to file a draft is what keeps one
    # amendment store and one authoring coordinator.
    #
    # The decision id is derived from the flow, actor and resolved document so a retried
    # POST reconciles onto the same request and the same draft instead of filing a
    # duplicate. `REPLAN_REQUESTED` is absent from `genesis.APPROVAL_DECISION_KINDS`, so
    # this roots no executing work — a proposal is not an approval.
    document = resolution.document
    document_hash = plan_hash(document)
    decision_id = str(uuid5(NAMESPACE_URL, f"adp:proposal:{org_id}:{flow_id}:{current_user.user_id}:{document_hash}"))

    existing_decision = await db.scalar(
        select(OrchestrationDecision).where(OrchestrationDecision.org_id == org_id, OrchestrationDecision.id == decision_id)
    )
    if existing_decision is None:
        await OrchestrationRepository(db).append_decision(
            org_id=org_id,
            flow_id=flow_id,
            decision_id=decision_id,
            kind=DecisionKind.REPLAN_REQUESTED.value,
            actor_id=current_user.user_id,
            actor_role=actor_role,
            actor_kind=ActorKind.HUMAN.value,
            reason=body.request_text,
        )

    request = await record_replan_request(
        db,
        org_id=org_id,
        flow_id=flow_id,
        replan_decision_id=decision_id,
        requested_by=current_user.user_id,
        # The human's own words, verbatim, as DATA. Nothing executes this.
        request_text=body.request_text,
    )

    # The proposing human is also the author of record for this document: there is no
    # agent run behind it. The run id names the route rather than a run so the
    # provenance reads honestly.
    author_run_id = f"operator:{current_user.user_id}"[:255]
    await assign_author_run(db, org_id=org_id, request_id=request.id, author_run_id=author_run_id)
    assignment = await resolve_authoring_request(db, org_id=org_id, request_id=request.id, author_run_id=author_run_id)

    # New work stays unbound here: nothing is published before a human accepts this
    # exact revision. The preview lists what acceptance will publish, and where.
    try:
        draft = await register_amendment_draft(
            db,
            org_id=org_id,
            request=assignment,
            author_run_id=author_run_id,
            proposal=document,
        )
    except ProposalRejectedError as error:
        await db.rollback()
        raise HTTPException(
            status_code=422,
            detail={
                "error": "proposal_rejected",
                "code": "proposal_rejected",
                "message": str(error),
                "violations": [{"rule": v.rule, "message": v.message, "where": v.where} for v in error.violations],
            },
        ) from error
    except TenantMismatchError as error:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from error

    impact = await _impact_of(
        db,
        org_id=org_id,
        flow_id=flow_id,
        document=document.model_dump(mode="json"),
        actor_id=current_user.user_id if current_user.account_type == "human" else None,
        actor_username=current_user.cognito_username,
    )
    await db.commit()

    logger.info(
        "amendment_proposed flow=%s org=%s actor=%s draft=%s kind=%s added=%s acceptable=%s",
        flow_id,
        org_id,
        current_user.user_id,
        draft.draft_id,
        body.placement.placement.kind,
        len(resolution.added_nodes),
        impact.acceptable,
    )

    return ProposalCreatedResponse(
        draft_id=draft.draft_id,
        flow_id=flow_id,
        proposal_hash=draft.proposal_hash,
        base_plan_version=draft.base_plan_version,
        added_nodes=list(resolution.added_nodes),
        resolved_edges=[
            AddedEdgeModel(from_address=edge.from_address, to_address=edge.to_address, reason=edge.reason) for edge in resolution.added_edges
        ],
        impact=impact,
    )


@router.get("/flows/{flow_id}/amendments/drafts", response_model=list[DraftSummary])
async def list_drafts(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
    run_bindings: Annotated[RunBindingResolver, Depends(get_run_binding_resolver)],
    state: Annotated[list[str] | None, Query()] = None,
    x_agent_run_id: Annotated[str | None, Header(alias=RUN_ID_HEADER)] = None,
) -> list[DraftSummary]:
    """A flow's amendment drafts, newest first. Reads only.

    All states by default, including terminal ones: a reviewer asking "what happened to
    the one I rejected" needs those rows, and a default that hid them would be
    indistinguishable from the store having lost the draft. Pass `state` to narrow.

    Documents are omitted — they are whole plan proposals. Use `show` for one.

    Returns 403 without `PLAN_APPROVE` and 404 for a flow outside the caller's tenant.
    Reads acceptance records, so `Permission.PLAN_APPROVE` is required here as on every
    other route in this module.
    """
    if current_user.account_type == "human":
        await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
        org_id, author_run_id = current_user.org_id, None
    else:
        org_id, author_run_id = await _read_scope(
            current_user=current_user,
            access=access,
            run_bindings=run_bindings,
            run_id=x_agent_run_id,
        )
    await _require_flow(db, org_id=org_id, flow_id=flow_id)

    if state:
        allowed = {item.value for item in PendingAmendmentState}
        unknown = sorted(set(state) - allowed)
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown state(s): {', '.join(unknown)}; expected one of {sorted(allowed)}")

    views = await list_pending_amendments(db, org_id=org_id, flow_id=flow_id, states=state, author_run_id=author_run_id)
    return [DraftSummary(**_summary_fields(view)) for view in views]


@router.get("/flows/{flow_id}/amendments/drafts/{draft_id}", response_model=DraftDetail)
async def show_draft(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    draft_id: Annotated[str, Path(min_length=1, max_length=36)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
    run_bindings: Annotated[RunBindingResolver, Depends(get_run_binding_resolver)],
    x_agent_run_id: Annotated[str | None, Header(alias=RUN_ID_HEADER)] = None,
) -> DraftDetail:
    """One draft in full, with its proposal document. Reads only.

    `base_is_current` false means the plan moved since this draft was authored and
    acceptance will refuse it as stale — worth knowing before reading the document rather
    than after.

    Returns 403 without `PLAN_APPROVE`, and 404 when the draft is absent, another
    tenant's, or on a different flow. All three are the same answer deliberately: `show`
    is the easy call to make in an enumeration loop, so a distinguishable response would
    be an oracle for draft ids. Reads acceptance records, so `Permission.PLAN_APPROVE`.
    """
    if current_user.account_type == "human":
        await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
        org_id, author_run_id = current_user.org_id, None
    else:
        org_id, author_run_id = await _read_scope(
            current_user=current_user,
            access=access,
            run_bindings=run_bindings,
            run_id=x_agent_run_id,
        )

    try:
        view = await get_pending_amendment(db, org_id=org_id, draft_id=draft_id, flow_id=flow_id, include_document=True, author_run_id=author_run_id)
    except AmendmentDraftNotFoundError as error:
        raise _not_found("draft_not_found", str(error)) from error

    return DraftDetail(**_summary_fields(view), proposal_document=view.proposal_document)


@router.post("/flows/{flow_id}/amendments/drafts/{draft_id}/preview", response_model=ImpactModel)
async def preview_draft(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    draft_id: Annotated[str, Path(min_length=1, max_length=36)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
    run_bindings: Annotated[RunBindingResolver, Depends(get_run_binding_resolver)],
    x_agent_run_id: Annotated[str | None, Header(alias=RUN_ID_HEADER)] = None,
) -> ImpactModel:
    """What accepting this draft would do to the graph. Writes nothing.

    The whole proposed change: nodes and edges added and removed, wave membership changes,
    downstream impact with current states, gate and policy changes, the proposed execution
    order and parallelism, and every blocking diagnostic — not just the first, which is all
    `amend_plan` would tell you by raising.

    `acceptable` is computed from the same predicates acceptance enforces
    (`prerequisites_change_refused`, `definition_change_refusal`, `state.transition`,
    `validate_proposal`), imported rather than restated, so a preview saying "acceptable"
    and an accept that refuses cannot disagree.

    A POST because it is a computation over live execution state, not a cacheable read.
    Returns 403 without `PLAN_APPROVE` and 404 for an absent, cross-tenant or wrong-flow
    draft.
    """
    if current_user.account_type == "human":
        await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
        org_id, author_run_id = current_user.org_id, None
    else:
        org_id, author_run_id = await _read_scope(
            current_user=current_user,
            access=access,
            run_bindings=run_bindings,
            run_id=x_agent_run_id,
        )

    try:
        view = await get_pending_amendment(db, org_id=org_id, draft_id=draft_id, flow_id=flow_id, include_document=True, author_run_id=author_run_id)
    except AmendmentDraftNotFoundError as error:
        raise _not_found("draft_not_found", str(error)) from error

    return await _impact_of(
        db,
        org_id=org_id,
        flow_id=view.flow_id,
        document=view.proposal_document,
        draft=view,
        actor_id=current_user.user_id if current_user.account_type == "human" else None,
        actor_username=current_user.cognito_username,
    )


@router.post("/flows/{flow_id}/amendments/drafts/{draft_id}/edit", response_model=RevisionResponse)
async def edit_draft(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    draft_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: EditBody,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RevisionResponse:
    """Replace a pending draft with an edited revision. Writes no plan.

    The edit creates a NEW draft and supersedes the old one; it never mutates the document
    in place. That is what invalidates an earlier approval: a human who reviewed revision 1
    and then accepts its id gets "that draft is superseded" rather than silently applying
    revision 2 under the id they read. An in-place edit would make "preview, then approve"
    unverifiable, because the approved thing could change between the two steps with no
    trace.

    The revision inherits the original's recorded base version, not a fresh read: an edit
    changes what is proposed, not what it was proposed against.

    `PLAN_APPROVE` rather than `PLAN_DRAFT` because editing chooses what a human will be
    asked to approve. An agent that could edit a pending draft could rewrite it after a
    reviewer read it.

    Returns 403 without `PLAN_APPROVE`, 404 for an absent or cross-tenant draft, 409 when
    the draft is no longer pending, and 422 for a document that fails validation.
    """
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    await _actor(access, current_user, body.reason)
    org_id = current_user.org_id

    try:
        # Unbound new work stays unbound; acceptance publishes it (see `accept_draft`).
        proposal = body.proposal
        revision = await revise_amendment(
            db,
            org_id=org_id,
            draft_id=draft_id,
            proposal=proposal,
            revised_by=current_user.user_id,
            flow_id=flow_id,
        )
    except AmendmentDraftNotFoundError as error:
        raise _not_found("draft_not_found", str(error)) from error
    except AmendmentConflictError as error:
        await db.rollback()
        raise _conflict(error) from error
    except ProposalRejectedError as error:
        await db.rollback()
        raise HTTPException(
            status_code=422,
            detail={
                "error": "proposal_rejected",
                "code": "proposal_rejected",
                "message": str(error),
                "violations": [{"rule": v.rule, "message": v.message, "where": v.where} for v in error.violations],
            },
        ) from error

    impact = await _impact_of(
        db,
        org_id=org_id,
        flow_id=revision.flow_id,
        document=proposal.model_dump(mode="json"),
        actor_id=current_user.user_id if current_user.account_type == "human" else None,
        actor_username=current_user.cognito_username,
    )
    await db.commit()

    logger.info(
        "amendment_revised flow=%s org=%s actor=%s from=%s to=%s unchanged=%s",
        revision.flow_id,
        org_id,
        current_user.user_id,
        draft_id,
        revision.draft_id,
        revision.unchanged,
    )

    return RevisionResponse(
        draft_id=revision.draft_id,
        flow_id=revision.flow_id,
        proposal_hash=revision.proposal_hash,
        superseded_draft_id=revision.superseded_draft_id,
        unchanged=revision.unchanged,
        gate_diff=_gate_diff(revision.gate_diff),
        impact=impact,
    )


@router.post("/flows/{flow_id}/amendments/drafts/{draft_id}/reject", response_model=DraftSummary)
async def reject_draft(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    draft_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: RejectBody,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DraftSummary:
    """Decline a pending amendment. Writes no graph state.

    Terminal and attributable: the draft becomes `REJECTED` with a decision time, and no
    acceptance actor is stamped — a draft that was never applied must not claim an
    accepting human. Re-rejecting is idempotent and does not rewrite who declined it.

    Returns 403 without `PLAN_APPROVE`, 404 for an absent or cross-tenant draft, and 409
    when the draft was already accepted (its content is in force; a late rejection would
    record a decision that did not happen).
    """
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    actor = await _actor(access, current_user, body.reason)
    org_id = current_user.org_id

    try:
        view = await reject_amendment(
            db,
            org_id=org_id,
            draft_id=draft_id,
            rejected_by=current_user.user_id,
            reason=body.reason,
            flow_id=flow_id,
            decision=actor,
        )
    except AmendmentDraftNotFoundError as error:
        raise _not_found("draft_not_found", str(error)) from error
    except AmendmentConflictError as error:
        await db.rollback()
        raise _conflict(error) from error

    await db.commit()

    logger.info(
        "amendment_rejected_via_api flow=%s org=%s actor=%s draft=%s",
        view.flow_id,
        org_id,
        current_user.user_id,
        draft_id,
    )

    return DraftSummary(**_summary_fields(view))


@router.post("/flows/{flow_id}/amendments/drafts/{draft_id}/accept", response_model=AcceptanceResponse)
async def accept_draft(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    draft_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: AcceptBody,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AcceptanceResponse:
    """Apply a reviewed amendment as a new plan version.

    Delegates to `pending_amendments.accept_amendment`, which is `amend_plan` under this
    human's own resolved context — so one implementation owns amendment, and acceptance
    adds only the review bookkeeping around it. Eligible work is scheduled by the normal
    post-amendment path; nothing here decides readiness.

    `expected_proposal_hash` binds the approval to the exact revision reviewed. Send it:
    it is what makes preview-then-approve meaningful, and it is checked BEFORE the
    staleness comparison because "you are reading an old revision" and "the plan moved"
    send a human to two different places.

    Concurrent acceptances yield one valid successor, and a retried accept reads back the
    original outcome rather than amending twice (`already_accepted`).

    Returns 403 without `PLAN_APPROVE` or for an agent attempting to accept, 404 for an
    absent or cross-tenant draft, 409 for a stale base / superseded draft / revision
    mismatch, and 422 for a document that no longer validates.
    """
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    org_id = current_user.org_id
    actor = await _actor(access, current_user, body.reason)

    try:
        # Unbound new work is published HERE, and only here: after the human's exact
        # revision/base binding and every acceptance blocker pass for this document,
        # and before `amend_plan`. `accept_amendment` then binds the recorded issues
        # deterministically under the flow lock. A publication failure writes no plan.
        await publish_for_acceptance(
            db,
            draft_id=draft_id,
            actor=actor,
            flow_id=flow_id,
            expected_proposal_hash=body.expected_proposal_hash,
            expected_base_plan_version=body.expected_base_plan_version,
            expected_base_plan_hash=body.expected_base_plan_hash,
        )
        result = await accept_amendment(
            db,
            draft_id=draft_id,
            actor=actor,
            flow_id=flow_id,
            expected_proposal_hash=body.expected_proposal_hash,
            expected_base_plan_version=body.expected_base_plan_version,
            expected_base_plan_hash=body.expected_base_plan_hash,
        )
    except AmendmentDraftNotFoundError as error:
        raise _not_found("draft_not_found", str(error)) from error
    except AmendmentConflictError as error:
        await db.rollback()
        raise _conflict(error) from error
    except FlowNotFoundError as error:
        await db.rollback()
        raise _not_found("draft_not_found", str(error)) from error
    except CycleBlockedError as error:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "error": str(error),
                "code": str(error),
                "message": (
                    "An earlier issue write has an unproven outcome. Acceptance remains blocked until readback "
                    "observes that exact attempt. Retry this exact acceptance for reconciliation; "
                    "do not reset the claim or create a replacement issue."
                    if str(error) == "amendment_issue_write_uncertain"
                    else "New work could not be published and bound idempotently; no plan version was written. "
                    "Retrying reuses any issue already recorded and never creates a second one."
                ),
                "retryable": True,
            },
        ) from error
    except TenantMismatchError as error:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ProposalRejectedError as error:
        await db.rollback()
        raise HTTPException(
            status_code=422,
            detail={
                "error": "proposal_rejected",
                "code": "proposal_rejected",
                "message": str(error),
                "violations": [{"rule": v.rule, "message": v.message, "where": v.where} for v in error.violations],
            },
        ) from error

    # `accept_amendment` does not commit — the caller owns the transaction, so the request
    # boundary is where it lands.
    await db.commit()

    logger.info(
        "amendment_accepted_via_api flow=%s org=%s actor=%s draft=%s v%s->v%s replay=%s",
        result.flow_id,
        org_id,
        actor.actor_id,
        draft_id,
        result.superseded_version,
        result.plan_version,
        result.replayed,
    )

    return AcceptanceResponse(
        draft_id=result.draft_id,
        flow_id=result.flow_id,
        plan_version=result.plan_version,
        superseded_version=result.superseded_version,
        decision_id=result.decision_id,
        gate_diff=_gate_diff(result.gate_diff),
        superseded_draft_ids=list(result.superseded_draft_ids),
        already_accepted=result.replayed,
        receipts_preserved=list(result.receipts_preserved),
        receipts_dropped=list(result.receipts_dropped),
        effective_limits_before=EffectiveLimitsModel(**result.effective_limits_before) if result.effective_limits_before else None,
        effective_limits_after=EffectiveLimitsModel(**result.effective_limits_after) if result.effective_limits_after else None,
    )
