"""Operator-plane ingress for draft plan registration.

Issues #4528 and #4529 (EPIC #4191, intent #4120).

- POST /orchestration/flows/drafts — register a compiled loop proposal as an inert
  draft. Gated on `Permission.PLAN_DRAFT`.
- POST /orchestration/flows/{flow_id}/amendments/drafts — register an authored
  **amendment** to an already-accepted plan as a pending draft awaiting one named
  human accept (#4529). Same permission, same tenant resolution, and inert in a
  stronger sense: it writes no graph rows at all.

Both routes exist because an authoring agent must be able to make a plan *visible*
without being able to make it *run*. They differ only in what the author is
amending: nothing (a new flow) or an accepted plan (an amendment). The second one
additionally requires the server to have commissioned the authoring run — see
`register_amendment_draft` — because unlike a new flow, an amendment names an
existing plan of record, so "which plan" cannot be left to the caller.

--------------------------------------------------------------------------------
Why this is a separate router from `routes.py`
--------------------------------------------------------------------------------

`routes.py` is the acceptance surface, and `test_internal_plane_guard.py` requires
the literal `Permission.PLAN_APPROVE` in **every** non-GET handler registered
there. That rule is correct and must not be relaxed: everything on that router
writes the record of what a human approved.

Draft registration is the one write into the graph that is *not* that. Its caller
is an authoring agent, and an agent must never hold approval authority — a plan
compiled under `PLAN_APPROVE` by the agent that wrote it is precisely the
self-approval the EPIC exists to prevent. So the route needs a weaker permission,
and a weaker permission cannot live on a router whose guarantee is "nothing here is
reachable below approval authority". Two routers, two guarantees, each asserted for
equality by the guard test.

**The internal plane was not an option.** Agent pods can call any `/internal/v1/*`
route with any method, so a registration endpoint there would need no permission at
all — and the guard test forbids `src/internal/` from even naming an orchestration
symbol. `docs/design-notes/4303-engine-genesis-transport.md` rejects the internal
plane for orchestration explicitly, calling that boundary the single highest-value
control in the EPIC. This router is on the operator plane, behind
`get_current_user`, exactly like the acceptance surface; the difference is which
permission it demands, not how strongly it authenticates.

--------------------------------------------------------------------------------
Why the weaker permission is safe
--------------------------------------------------------------------------------

Not because registration is trusted, but because everything it can produce is
inert *by construction* (see `registration.py`): the graph sits behind an
acceptance gate whose only progress edges are human-only, and the decision row it
writes is not of an approval kind, so dispatch has nothing to root a chain in.
`PLAN_DRAFT` therefore authorises "make a plan visible", and no reachable
composition of it authorises "make a plan run".

The blast radius of the *weakest* holder matters, because a registry-resolved agent
principal lands on `AdminRole.MEMBER`. What a MEMBER can do with this route is:
create graph rows in **its own** tenant (`target_org_id` is the caller's resolved
`org_id`, and `PLAN_DRAFT` is org-scoped so an empty `org_id` is denied outright),
all of which a `PLAN_APPROVE` human must then accept before anything executes.

**Tenant isolation.** The document's declared `org_id` is compared against the
actor's inside `compile_proposal` (its Gate 2) and a mismatch is rejected, never
re-homed. That comparison is the isolation boundary and this module does not touch
it — only the *right-hand side* of it, i.e. where the actor's tenant comes from.

For a human caller that is the authenticated `org_id`, unchanged. For an
internal-scope service caller it is resolved server-side from the run's ingress row
(Issue #4597; see `draft_binding.py`), because the shared worker every hosted run
authenticates as has the registry `org_id` `__platform__`, which equals no real
tenant — so before #4597 *every* real-tenant registration was refused with a 422 and
the engine bridge could not complete its own happy path. `attributed_org_id` is
never read here: it is caller-influenced and the #4132 invariant forbids it gating
access.

Route prefix is `/orchestration`, NOT `/api/orchestration` — CloudFront strips the
first `/api` before the origin. Guarded app-wide by
`tests/test_route_prefix_convention.py`.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.budget.run_binding import RunBindingResolver
from src.orchestration.amend import FlowNotFoundError
from src.orchestration.compile import ApprovalContext, NonApprovalSupersedeError, ProposalRejectedError, TenantMismatchError
from src.orchestration.draft_binding import DraftBindingError, resolve_draft_tenant
from src.orchestration.pending_amendments import (
    AmendmentRequestNotFoundError,
    register_amendment_draft,
    resolve_authoring_request,
)
from src.orchestration.proposal import LoopProposal
from src.orchestration.registration import DraftFlowConflictError, register_draft_proposal
from src.orchestration.state import ActorKind
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger("bedrockgateway.orchestration.drafts")

router = APIRouter(prefix="/orchestration", tags=["orchestration"])


async def get_access_control(db: Annotated[AsyncSession, Depends(get_db)]) -> AccessControl:
    """Get access control instance."""
    return AccessControl(db)


async def get_run_binding_resolver() -> RunBindingResolver:
    """The `webhook-events` row resolver used to establish a run's owning tenant.

    A FastAPI dependency rather than a module global so tests can inject a stub
    table without patching boto3, matching how `get_access_control` is overridden.
    Built per request; the underlying Redis cache means the DDB Query happens once
    per run, and a draft registration happens once per run anyway.
    """
    from src.shared.config import get_settings

    settings = get_settings()
    return RunBindingResolver(
        table_name=settings.webhook_events_table,
        aws_region=settings.aws_region,
        redis_url=settings.redis_url,
    )


class DraftRegisteredResponse(BaseModel):
    """The outcome of registering a draft.

    `accept_command` is returned rather than left to the caller to compose. The
    caller is a worker pod that puts this string in a GitHub comment a human then
    types back, so if the wording drifts from what `engine_commands.py` parses, the
    human follows a working instruction that does nothing. One server-side source
    for it means the bridge's two halves cannot disagree.

    `already_registered` is true when the identical document was already in force
    and nothing was written — a fail-soft caller retries, and a retry must not be
    reported as a second plan.
    """

    model_config = ConfigDict(extra="forbid")

    flow_id: str
    plan_version: int
    plan_hash: str
    decision_id: str
    nodes_created: int
    edges_created: int
    already_registered: bool
    # The synthesised gate a human answers to make the plan live.
    acceptance_gate_address: str
    accept_command: str
    # The flows-list URL for this flow (#4885), composed server-side for the same
    # reason `accept_command` is: the caller puts it in a GitHub comment an
    # operator clicks, and it is how they actually find their graph. The worker
    # cannot build it — its `ADP_GATEWAY_ENDPOINT` is the API Gateway invoke URL,
    # not the user-facing origin, and pasting that would hand an operator a link
    # to the machine plane. `None` when `gateway_base_url` is unconfigured, so
    # the comment omits the link rather than rendering a broken relative one.
    flow_url: str | None


# The exact comment body story 1/3's parser recognises. Spelled once, here.
ACCEPT_COMMAND = "@agent-engine accept"

# The one caller scope whose tenant is resolved from the run's ingress row rather
# than from its authenticated `org_id` (Issue #4597).
#
# Spelled here as the single literal `"internal"` rather than imported from
# `internal/auth_deps.INTERNAL_PLANE_SCOPES`, for two reasons. First, that
# frozenset also carries `"platform"`, and widening the server-side-tenant path to
# a scope nobody has audited for it is exactly the "widen who can register
# cross-tenant too far" failure mode this issue's own impact table names. Second,
# `test_internal_plane_guard.py` exists to keep the orchestration package and the
# internal plane from taking a dependency on each other; importing an internal-plane
# constant here would be the first such edge, in the direction the guard does not
# yet check. This module's rule is narrower than that constant and should stay
# stated independently of it.
_SERVER_RESOLVED_TENANT_SCOPE = "internal"

# The run-reference header, spelled exactly as the platform sends it: one word,
# `RunId`, matching `proxy/routes.py`'s `x-agent-runid` read and the worker's
# `engine_registration.py`. Header matching is case-insensitive, hyphenation is not.
RUN_ID_HEADER = "X-Agent-RunId"


async def _resolve_owning_tenant(
    current_user: TokenContext,
    *,
    run_bindings: RunBindingResolver,
    run_id: str | None,
    target: str,
) -> str:
    """Which tenant this registration's rows land in. Server-written state only.

    For a human caller the authenticated org is the tenant, exactly as before — a
    human registering a draft sends no run id, and both `X-Agent-RunId` and
    `X-Agent-OrgId` are ignored for tenant purposes (the #4132 pin).

    For an internal-scope service caller the authenticated org is `__platform__`,
    which equals no real tenant, so the tenant is resolved from the run's ingress
    row instead. See `draft_binding.py` for why that row and not the `X-Agent-OrgId`
    header the worker's intent is also available in.

    Shared by both draft routes deliberately. Two copies of this would be two
    chances for one of them to grow a fallback to `attributed_org_id`, and a
    fallback is the whole bypass — reachable by any caller who can make the lookup
    fail.

    Args:
        target: What is being registered against, for the refusal log only. Never an
            input to the decision.

    Raises:
        HTTPException: 403 with a machine-readable `error` code when an
            internal-scope caller's run does not bind to a tenant.
    """
    if current_user.scope != _SERVER_RESOLVED_TENANT_SCOPE:
        return current_user.org_id
    try:
        return await resolve_draft_tenant(run_id=run_id, resolver=run_bindings)
    except DraftBindingError as exc:
        # 403 and not 422: a 422 here would be indistinguishable, in the worker's
        # closing-comment warning, from the tenant-mismatch refusal a document can
        # also earn — which is a different problem with a different fix. The
        # machine-readable `error` code says which arm fired.
        logger.warning(
            "draft registration refused, run did not bind to a tenant: target=%s actor=%s reason=%s",
            target,
            current_user.user_id,
            exc.code,
        )
        raise HTTPException(status_code=403, detail={"error": exc.code, "message": exc.message}) from exc


@router.post("/flows/drafts", response_model=DraftRegisteredResponse, status_code=201)
async def register_draft(
    proposal: LoopProposal,
    response: Response,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
    run_bindings: Annotated[RunBindingResolver, Depends(get_run_binding_resolver)],
    reason: Annotated[str | None, Query(max_length=2000)] = None,
    # `alias` is REQUIRED and is not cosmetic. FastAPI derives a header name from the
    # parameter name by replacing underscores with hyphens, so `x_agent_run_id` would
    # bind `X-Agent-Run-Id` — which is not the header the platform sends. The wire
    # name is `X-Agent-RunId` (one word, `proxy/routes.py` reads
    # `request.headers.get("x-agent-runid")`), so without the alias this parameter is
    # silently always `None`: every internal caller is refused `missing_run_id` while
    # the worker is demonstrably sending the header. Caught in test, and the reason
    # `RUN_ID_HEADER` below is spelled once.
    x_agent_run_id: Annotated[str | None, Header(alias=RUN_ID_HEADER)] = None,
) -> DraftRegisteredResponse:
    """Register a loop proposal as an inert draft plan.

    The plan is visible in the graph UI immediately and executes nothing. A human
    answering the returned acceptance gate — by commenting `@agent-engine accept`
    or clicking accept in the dashboard — is the only thing that starts it.

    Returns 201 on a first registration, 200 when the identical document was
    already registered, 403 without `PLAN_DRAFT` (zero rows written) or when an
    internal-scope caller's run does not resolve to an owning tenant, 409 when the
    target flow has already been approved (registration creates flows, it never
    extends one — see `registration.py`), and 422 for a document that fails
    validation or declares a tenant other than the run's.
    """
    # Gate first, before any read or write, so a denied caller cannot learn whether
    # anything exists. Same ordering as the acceptance routes.
    #
    # ------------------------------------------------------------------------
    # `target_org_id` is the AUTHENTICATED org, and must stay that way (#4597)
    # ------------------------------------------------------------------------
    # Two different questions are being answered on this route, and conflating
    # them into one field is how this fix fails:
    #
    #   1. MAY this caller register drafts at all?  -> answered here, against the
    #      authenticated identity.
    #   2. WHICH tenant do the rows land in?        -> answered below, from
    #      server-written ground truth only.
    #
    # It is tempting to "fix" the line below by passing the run's tenant, since
    # that is the tenant the rows land in. Do not. `get_user_role` finds no
    # membership row for the shared worker principal (its `user_id` is the registry
    # agent name, which matches neither `cognito_sub` nor `users.id`), so it takes
    # the no-row fallback and its `allowed_org_id` is the caller's own
    # `__platform__`. Passing the run's tenant as `target_org_id` would then trip
    # the org-scope arm in `access_control.check_permission` — `"aws-e" !=
    # "__platform__"` — and raise `InvalidScopeError`, turning today's 422 into a
    # 403 and leaving the feature exactly as inert as it was. `PLAN_DRAFT` is
    # org-scoped (`_ORG_SCOPED_PERMISSIONS`, equality-pinned) and that arm must
    # stay live, so the two values disagree here BY DESIGN.
    await access.check_permission(
        current_user,
        Permission.PLAN_DRAFT,
        target_org_id=current_user.org_id,
    )

    # Question 2. See `_resolve_owning_tenant`.
    owning_org_id = await _resolve_owning_tenant(
        current_user,
        run_bindings=run_bindings,
        run_id=x_agent_run_id,
        target=proposal.flow_slug,
    )

    # Server-resolved, every field. `actor_kind` is SERVICE and stated explicitly:
    # the default is HUMAN because the overwhelming majority of compiles are a
    # human accepting a plan, and letting a registering agent inherit that default
    # would put a human's `actor_kind` on a row no human made — which is exactly
    # what `resolve_engine_genesis` reads to decide whether a decision can root a
    # dispatch.
    #
    # `actor_id` is the SERVICE principal, never a human from the run's lineage:
    # `genesis.py` sets `root_human_id=decision.actor_id`, so a human's id here
    # would manufacture a dispatch-rootable decision attributed to a human who
    # approved nothing.
    actor = ApprovalContext(
        org_id=owning_org_id,
        actor_id=current_user.user_id,
        actor_role=(await access.get_user_role(current_user))[0].value,
        actor_kind=ActorKind.SERVICE,
        reason=reason,
    )

    try:
        result, gate_address = await register_draft_proposal(db, proposal, actor)
    except DraftFlowConflictError as exc:
        # 409, not 422: the document is fine, the *target* is not available. Also
        # before `ProposalRejectedError`, which it subclasses. A conflict means
        # someone tried to register into an already-approved flow — the escalation
        # route found in review (PR #4558) — so it is logged at warning, unlike a
        # routine validation failure.
        logger.warning(
            "draft registration refused: flow=%s org=%s actor=%s reason=%s",
            proposal.flow_slug,
            current_user.org_id,
            current_user.user_id,
            exc,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NonApprovalSupersedeError as exc:
        # The same class of refusal as the conflict above, raised one layer deeper by
        # `compile_proposal` itself, so it gets the same 409 and the same warning.
        # Unreachable while `register_draft_proposal`'s pre-flight stands — that
        # refuses an approved flow before compiling — and handled anyway, because the
        # whole point of moving the invariant into the primitive is that it holds when
        # the outer guard does not. Silently mapping it to a 422 would report a
        # blocked privilege escalation as an author's malformed document.
        logger.warning(
            "draft registration refused at the primitive: flow=%s org=%s actor=%s reason=%s",
            proposal.flow_slug,
            current_user.org_id,
            current_user.user_id,
            exc,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TenantMismatchError as exc:
        # Before `ProposalRejectedError`: it subclasses it, so the broader clause
        # would swallow this one if the order were reversed.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProposalRejectedError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "violations": [{"rule": violation.rule, "message": violation.message, "where": violation.where} for violation in exc.violations],
            },
        ) from exc

    # `register_draft_proposal` does not commit — the caller owns the transaction.
    await db.commit()

    if result.already_compiled:
        response.status_code = 200

    return DraftRegisteredResponse(
        flow_id=result.flow_id,
        plan_version=result.plan_version,
        plan_hash=result.plan_hash,
        decision_id=result.decision_id,
        nodes_created=result.nodes_created,
        edges_created=result.edges_created,
        already_registered=result.already_compiled,
        acceptance_gate_address=gate_address,
        accept_command=ACCEPT_COMMAND,
        flow_url=_flow_url(result.flow_id),
    )


# --- Amendment drafts (#4529) -------------------------------------------------


class GateDiffResponse(BaseModel):
    """Which gate addresses an amendment adds, keeps and drops.

    Returned so the worker's comment can tell a human what the amendment does to
    their *decision points*, which is the one consequence not visible in a diff of
    the plan document: a removed gate reads as an ordinary edit and is a removed
    human decision. Computed server-side from the two documents, never taken from
    the authoring agent's own summary of what it changed — an author describing its
    own gate removals is exactly the claim that should not be trusted.
    """

    model_config = ConfigDict(extra="forbid")

    added: list[str]
    removed: list[str]
    unchanged: list[str]
    changes_gating: bool


class AmendmentDraftRegisteredResponse(BaseModel):
    """The outcome of registering an authored amendment. Nothing is accepted yet.

    Fresh drafts report `pending_human_accept`. Replays report the stored state,
    so an accepted or superseded draft never appears to be awaiting a new decision.

    `accept_command` is composed server-side for the same reason as
    `DraftRegisteredResponse.accept_command`: the worker puts this string in a GitHub
    comment a human types back, so if the wording drifts from what
    `engine_commands.py` parses, the human follows a working instruction that does
    nothing. Here it must also carry the draft id, because acceptance names the
    draft — there is deliberately no "accept the latest amendment" form.
    """

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    flow_id: str
    request_id: str
    # The accepted version this amendment was authored against. Compared exactly at
    # acceptance, so it is reported here: a human who sees a base older than the
    # current version knows the answer will be a conflict before they type it.
    base_plan_version: int | None
    proposal_hash: str
    gate_diff: GateDiffResponse
    already_registered: bool
    status: str
    accept_command: str
    flow_url: str | None


# What the human types to accept ONE named amendment. Spelled here, once, as a
# format rather than a constant, because the id is not optional: a bare
# `@agent-engine accept` answers the acceptance gate and must never select an
# amendment.
ACCEPT_AMENDMENT_COMMAND = "@agent-engine accept amendment {draft_id}"

# The status every fresh registration reports. A literal, so no code path can
# report an amendment as applied.
PENDING_HUMAN_ACCEPT = "pending_human_accept"


@router.post(
    "/flows/{flow_id}/amendments/drafts",
    response_model=AmendmentDraftRegisteredResponse,
    status_code=201,
)
async def register_amendment(
    flow_id: str,
    proposal: LoopProposal,
    response: Response,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
    run_bindings: Annotated[RunBindingResolver, Depends(get_run_binding_resolver)],
    request_id: Annotated[str, Query(max_length=36, min_length=1)],
    x_agent_run_id: Annotated[str | None, Header(alias=RUN_ID_HEADER)] = None,
) -> AmendmentDraftRegisteredResponse:
    """File an authored amendment as a pending draft. Accepts nothing.

    Writes one row holding a proposal document, and **no** node, edge, decision, work
    claim, gate or accepted-plan version — see `pending_amendments.py`. A human
    commenting `@agent-engine accept amendment <draft-id>` is the only thing that
    applies it, and that path runs the existing `amend_plan` with the accepting
    human's own context.

    `request_id` is required and is the authorization, not a hint. It names the
    authoring assignment the server created when a verified human commented
    `replan:`, and `resolve_authoring_request` refuses unless the presented
    `X-Agent-RunId` equals the `author_run_id` the server wrote on that assignment.
    So holding `PLAN_DRAFT` is not sufficient to file an amendment: the server must
    have commissioned this run for this request. That is what stops an authoring
    agent from proposing amendments to plans nobody asked it to touch, and it is why
    the flow the draft attaches to comes from the assignment rather than from
    `flow_id` or from the document (#4556's target-ambiguity protection).

    `flow_id` in the path is therefore a *check*, not the source of truth: it must
    agree with the assignment's flow, and a mismatch is refused.

    Returns 201 on a first registration, 200 when the identical document was already
    on file (a fail-soft author retries, and a retry is not a second draft), 403
    without `PLAN_DRAFT` or when the run does not bind to a tenant, 404 when no open
    assignment matches this run, and 422 for a document that fails validation.
    """
    # Gate first, before any read or write, so a denied caller cannot learn whether
    # anything exists. `target_org_id` is the AUTHENTICATED org for the reason spelled
    # out at length in `register_draft` — the two values disagree here by design.
    await access.check_permission(
        current_user,
        Permission.PLAN_DRAFT,
        target_org_id=current_user.org_id,
    )

    owning_org_id = await _resolve_owning_tenant(
        current_user,
        run_bindings=run_bindings,
        run_id=x_agent_run_id,
        target=flow_id,
    )

    # The binding. Refused unless the server itself commissioned this run for this
    # assignment. `(run_id or "")` so an absent header cannot match a NULL
    # `author_run_id` — `resolve_authoring_request` also refuses that, and this keeps
    # the type honest at the boundary.
    try:
        request = await resolve_authoring_request(
            db,
            org_id=owning_org_id,
            request_id=request_id,
            author_run_id=(x_agent_run_id or ""),
        )
    except AmendmentRequestNotFoundError as exc:
        # 404 and one message for absent, cross-tenant, wrong-run and
        # already-answered. Distinguishing them would let a caller enumerate request
        # ids and learn which are real.
        logger.warning(
            "amendment registration refused, no open assignment: request=%s flow=%s actor=%s",
            request_id,
            flow_id,
            current_user.user_id,
        )
        raise HTTPException(
            status_code=404,
            detail={"error": "no_open_authoring_request", "message": str(exc)},
        ) from exc

    # The path's flow must be the assignment's flow. Checked rather than trusted, and
    # reported as the same 404: an author asking to amend a flow it was not
    # commissioned for should learn nothing about whether that flow exists.
    if request.flow_id != flow_id:
        logger.warning(
            "amendment registration refused, flow does not match the assignment: request=%s asked=%s assigned=%s",
            request_id,
            flow_id,
            request.flow_id,
        )
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no_open_authoring_request",
                "message": f"no authoring assignment {request_id!r} is open for this run on flow {flow_id!r}",
            },
        )

    try:
        draft = await register_amendment_draft(
            db,
            org_id=owning_org_id,
            request=request,
            author_run_id=(x_agent_run_id or ""),
            proposal=proposal,
        )
    except FlowNotFoundError as exc:
        # The assignment's flow has been deleted since it was created. 404 for the
        # same reason as above, and the draft is not written: an amendment to a
        # nonexistent flow could never be accepted.
        raise HTTPException(status_code=404, detail={"error": "flow_not_found", "message": str(exc)}) from exc

    # `register_amendment_draft` does not commit — the caller owns the transaction.
    await db.commit()

    if draft.already_registered:
        response.status_code = 200

    return AmendmentDraftRegisteredResponse(
        draft_id=draft.draft_id,
        flow_id=draft.flow_id,
        request_id=draft.request_id,
        base_plan_version=draft.base_plan_version,
        proposal_hash=draft.proposal_hash,
        gate_diff=GateDiffResponse(
            added=draft.gate_diff.added,
            removed=draft.gate_diff.removed,
            unchanged=draft.gate_diff.unchanged,
            changes_gating=draft.gate_diff.changes_gating,
        ),
        already_registered=draft.already_registered,
        status=PENDING_HUMAN_ACCEPT if draft.state == "pending" else draft.state,
        accept_command=ACCEPT_AMENDMENT_COMMAND.format(draft_id=draft.draft_id) if draft.state == "pending" else "",
        flow_url=_flow_url(draft.flow_id),
    )


def _flow_url(flow_id: str) -> str | None:
    """The user-facing URL for a flow, or None when no origin is configured.

    `gateway_base_url` is the CloudFront/custom-domain origin (`BG_GATEWAY_BASE_URL`,
    set by `gateway-deploy.yml`), the same setting the magic-link builders use. The
    path matches the SPA route `/flows/:flowId` registered for the #4869 list page.

    Returns `None` rather than a bare path when the setting is empty: a relative
    URL in a GitHub comment resolves against github.com and 404s, which is worse
    than no link at all because it looks like the feature is broken.
    """
    from src.shared.config import get_settings

    base = get_settings().gateway_base_url.rstrip("/")
    return f"{base}/flows/{flow_id}" if base else None
