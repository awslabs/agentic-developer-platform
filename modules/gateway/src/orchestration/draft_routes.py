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

**Tenant isolation.** `org_id` comes from the authenticated context. The document's
declared `org_id` is compared against it inside `compile_proposal` (its Gate 2) and
a mismatch is rejected, never re-homed. No tenant logic lives in this module.

Route prefix is `/orchestration`, NOT `/api/orchestration` — CloudFront strips the
first `/api` before the origin. Guarded app-wide by
`tests/test_route_prefix_convention.py`.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.orchestration.compile import ApprovalContext, NonApprovalSupersedeError, ProposalRejectedError, TenantMismatchError
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


# The exact comment body story 1/3's parser recognises. Spelled once, here.
ACCEPT_COMMAND = "@agent-engine accept"


@router.post("/flows/drafts", response_model=DraftRegisteredResponse, status_code=201)
async def register_draft(
    proposal: LoopProposal,
    response: Response,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
    reason: Annotated[str | None, Query(max_length=2000)] = None,
) -> DraftRegisteredResponse:
    """Register a loop proposal as an inert draft plan.

    The plan is visible in the graph UI immediately and executes nothing. A human
    answering the returned acceptance gate — by commenting `@agent-engine accept`
    or clicking accept in the dashboard — is the only thing that starts it.

    Returns 201 on a first registration, 200 when the identical document was
    already registered, 403 without `PLAN_DRAFT` (zero rows written), 409 when the
    target flow has already been approved (registration creates flows, it never
    extends one — see `registration.py`), and 422 for a document that fails
    validation or declares a tenant other than the caller's.
    """
    # Gate first, before any read or write, so a denied caller cannot learn whether
    # anything exists. Same ordering as the acceptance routes.
    await access.check_permission(
        current_user,
        Permission.PLAN_DRAFT,
        target_org_id=current_user.org_id,
    )

    # Server-resolved, every field. `actor_kind` is SERVICE and stated explicitly:
    # the default is HUMAN because the overwhelming majority of compiles are a
    # human accepting a plan, and letting a registering agent inherit that default
    # would put a human's `actor_kind` on a row no human made — which is exactly
    # what `resolve_engine_genesis` reads to decide whether a decision can root a
    # dispatch.
    actor = ApprovalContext(
        org_id=current_user.org_id,
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
    )
