"""Operator-plane ingress for draft plan registration.

Issue #4528 (EPIC #4191, intent #4120).

- POST /orchestration/flows/drafts — register a compiled loop proposal as an inert
  draft. Gated on `Permission.PLAN_DRAFT`.

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
from src.orchestration.compile import ApprovalContext, NonApprovalSupersedeError, ProposalRejectedError, TenantMismatchError
from src.orchestration.draft_binding import DraftBindingError, resolve_draft_tenant
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

    # Question 2. For a human caller the authenticated org is the tenant, exactly as
    # before — a human registering a draft sends no run id and both `X-Agent-RunId`
    # and `X-Agent-OrgId` are ignored for tenant purposes (the #4132 pin).
    #
    # For an internal-scope service caller the authenticated org is `__platform__`,
    # so the tenant is resolved from the run's ingress row instead. See
    # `draft_binding.py` for why that row and not the `X-Agent-OrgId` header the
    # worker's intent is also available in.
    owning_org_id = current_user.org_id
    if current_user.scope == _SERVER_RESOLVED_TENANT_SCOPE:
        try:
            owning_org_id = await resolve_draft_tenant(run_id=x_agent_run_id, resolver=run_bindings)
        except DraftBindingError as exc:
            # 403 and not 422: a 422 here would be indistinguishable, in the
            # worker's closing-comment warning, from the tenant-mismatch refusal
            # below — which is a different problem with a different fix. The
            # machine-readable `error` code says which arm fired.
            logger.warning(
                "draft registration refused, run did not bind to a tenant: flow=%s actor=%s reason=%s",
                proposal.flow_slug,
                current_user.user_id,
                exc.code,
            )
            raise HTTPException(status_code=403, detail={"error": exc.code, "message": exc.message}) from exc

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
