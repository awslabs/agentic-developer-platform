"""The pending-amendment lifecycle: request → authored draft → named human accept.

Issue #4529 (ENGINE-BRIDGE 3/3, EPIC #4191).

--------------------------------------------------------------------------------
The hole this closes
--------------------------------------------------------------------------------

Before this module, `@agent-engine replan: <what should change>` wrote a
`REPLAN_REQUESTED` decision and replied "the plan is unchanged until someone authors
an amendment." Nothing then authored one. `amend.amend_plan` existed and worked, but it
applies a plan a human is *already holding* — reached only from the dashboard's
`POST /orchestration/flows/{flow_id}/amendments`. So there was no place for "an agent
has proposed this, and no human has said yes yet" to live, and therefore no way for an
authoring agent's output to reach a human at all.

This module is that place, plus the two transitions either side of it:

    replan (human)   ->  record_replan_request     ->  one authoring assignment
    author (agent)   ->  register_amendment_draft  ->  a PENDING draft, inert
    human, by name   ->  accept_amendment          ->  amend_plan, one new version

--------------------------------------------------------------------------------
What a draft is NOT
--------------------------------------------------------------------------------

Writing a draft creates **no** node, edge, decision, work claim, gate or
accepted-plan version. It writes one row holding a proposal document. That is why
`register_amendment_draft` does not call `registration.transform_for_registration` and
does not go anywhere near `compile_proposal`: those create graph rows, and a graph row
is a thing the tick can read. A draft is data the graph does not reference at all, so
its inertness needs no filter anywhere to hold — unlike a `status` column on
`orchestration_accepted_plans`, where one missed filter would make an agent's proposal
executable.

Acceptance is the only path from a draft to graph state, it runs the existing
`amend_plan` (never a second implementation of it), and it runs it with the **accepting
human's** own resolved context.

--------------------------------------------------------------------------------
Why acceptance must name the draft
--------------------------------------------------------------------------------

`accept_amendment` is reachable only from `@agent-engine accept amendment <draft-id>`
and the operator plane's equivalent. Plain `@agent-engine accept` still answers the
acceptance gate and never touches this table.

That separation is deliberate and is not stylistic. An `accept` that resolved "the
latest pending amendment" would apply a plan on a command the human typed for something
else — and the something else (answering a gate) is the more common act, so the mistake
would be routine rather than rare. There is no "most recent draft" selector in this
module for the same reason: the only way to accept a draft is to have read its id.

--------------------------------------------------------------------------------
Why the base version and hash are compared exactly
--------------------------------------------------------------------------------

Every draft records the accepted version and hash that were in force when its request
was made. At acceptance those are compared against what is in force *now*, and a
mismatch is refused as a conflict that requires a fresh replan.

It is tempting to be helpful here and apply the proposal anyway, or to rebase it. Both
are wrong in the same way: the author read v3, someone accepted v4 in the meantime, and
applying the v3-derived document as v5 silently discards v4's changes while reporting
success. The human who accepted v4 gets no signal at all. Refusing costs one replan;
rebasing costs the plan of record.

That comparison is also what makes concurrent amendments safe without a distributed
lock: two drafts may both be `PENDING`, the first accepted wins, and the second's
recorded base no longer matches, so it can only be refused — and the acceptance
transaction marks it `SUPERSEDED` rather than leaving a draft that will fail
confusingly later.

--------------------------------------------------------------------------------
What an agent cannot do here
--------------------------------------------------------------------------------

* It cannot set `state`. `register_amendment_draft` writes `PENDING` unconditionally;
  the only writer of the other three states is `accept_amendment`, which is not
  reachable from any agent-facing route.
* It cannot set `accepted_by`, `accepted_by_decision_id` or `accepted_plan_version`.
  Those are written by the acceptance transaction from the human's resolved context and
  from `amend_plan`'s own result.
* It cannot choose which flow or request its draft attaches to.
  `resolve_authoring_request` requires the presented run id to equal the `author_run_id`
  the *server* wrote on the request row, and registration then uses the **request's**
  flow — so a draft cannot be filed against a flow the assignment did not name. That
  preserves #4556's target-ambiguity protection: the flow comes from server-written
  state, never from a document field or an issue lookup.
* It cannot accept its own draft. There is no self-accept and no agent-reachable
  acceptance route.

Nothing in this module commits. Callers own the transaction, exactly as `amend_plan` and
`compile_proposal` do, so a command's state change and its decision row land atomically
or not at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import utcnow

from .amend import AmendmentContext, AmendResult, FlowNotFoundError, amend_plan
from .compile import plan_hash
from .models import (
    AmendmentRequestState,
    OrchestrationAcceptedPlan,
    OrchestrationAmendmentRequest,
    OrchestrationFlow,
    OrchestrationPendingAmendment,
    PendingAmendmentState,
)
from .proposal import LoopProposal

logger = logging.getLogger("bedrockgateway.orchestration.pending_amendments")

__all__ = [
    "AmendmentAcceptResult",
    "AmendmentConflictError",
    "AmendmentDraftNotFoundError",
    "AmendmentRequest",
    "AmendmentRequestNotFoundError",
    "GateDiff",
    "PendingAmendmentDraft",
    "accept_amendment",
    "assign_author_run",
    "gate_diff",
    "in_force_plan",
    "mark_request_dispatched",
    "owed_authoring_requests",
    "record_replan_request",
    "register_amendment_draft",
    "resolve_authoring_request",
]


class AmendmentRequestNotFoundError(LookupError):
    """No open authoring assignment matches the presented run.

    Raised identically whether the request is absent, belongs to another tenant, or
    names a different authoring run — and the route maps all three to one status, for
    the reason `FlowNotFoundError` documents: distinguishing them would confirm the
    existence of another tenant's request to a caller probing ids.
    """


class AmendmentDraftNotFoundError(LookupError):
    """No such pending amendment **in this tenant and on this flow**.

    Raised identically for absent, cross-tenant and wrong-flow, and mapped to one
    answer. A distinguishable "wrong flow" would let a caller enumerate draft ids and
    learn which are real.
    """


class AmendmentConflictError(Exception):
    """The draft cannot be accepted as it stands. Carries a human-facing reason.

    Always a refusal that writes no plan version. `code` is stable and
    machine-readable so a reply can be worded for a human while a log stays greppable —
    "the base plan moved" and "this draft was already superseded" are the same refusal
    to the command pass and completely different situations to the person who typed the
    command.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class GateDiff:
    """Which gate addresses a draft adds, keeps and drops versus the in-force plan.

    Reported to the human *before* they accept, because gate placement is the one part
    of an amendment whose consequences are not visible from the diff of a plan document:
    a removed gate is a removed human decision point, and it reads as an ordinary edit.
    Computed from the two documents' gate nodes, never from the authoring agent's own
    summary of what it changed.

    Addresses are sorted, so two runs over the same pair of documents report the same
    diff — a set's iteration order would make the rendered comment unstable.
    """

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def changes_gating(self) -> bool:
        """Whether the amendment moves any human decision point at all."""
        return bool(self.added or self.removed)


@dataclass(frozen=True)
class AmendmentRequest:
    """One human replan's standing claim on an authoring job.

    Returned as a plain snapshot rather than the ORM row so callers — the command pass
    and the post-commit publisher — cannot quietly mutate request state from outside
    this module. The two legitimate mutations have named functions
    (`assign_author_run`, `mark_request_dispatched`).

    `created` is False when an equivalent request already existed, which is how a
    duplicated delivery reconciles onto one assignment instead of queueing a second
    author for one human ask.
    """

    id: str
    flow_id: str
    #: The committed `REPLAN_REQUESTED` decision this request was born from. Carried
    #: on the snapshot (#4529) because it is the authoring assignment's attribution
    #: root and its FIFO deduplication input — `authoring_dispatch` must derive both
    #: from server-written state rather than re-reading the row it was handed.
    replan_decision_id: str
    requested_by: str
    request_text: str
    base_plan_version: int | None
    base_plan_hash: str | None
    state: str
    author_run_id: str | None
    created_at: datetime
    created: bool = False


@dataclass(frozen=True)
class PendingAmendmentDraft:
    """What registering an authored amendment produced.

    `already_registered` is True when the identical document was already on file and
    nothing was written. The authoring worker is fail-soft and retries, and a retry must
    not be reported as a second draft — a human offered two ids for one proposal has to
    work out which to accept, and either choice is the same plan.
    """

    draft_id: str
    flow_id: str
    request_id: str
    base_plan_version: int | None
    proposal_hash: str
    gate_diff: GateDiff
    already_registered: bool
    state: str = PendingAmendmentState.PENDING.value


@dataclass(frozen=True)
class AmendmentAcceptResult:
    """What accepting a draft produced.

    `replayed` is True when the draft was already accepted and this call returned the
    original outcome rather than amending again. A repeated `accept amendment` — a human
    re-sending the comment, or a duplicated delivery — must report what it reports the
    first time, not a conflict and not a second version.
    """

    draft_id: str
    flow_id: str
    plan_version: int
    superseded_version: int | None
    decision_id: str
    gate_diff: GateDiff
    superseded_draft_ids: list[str] = field(default_factory=list)
    replayed: bool = False


def _snapshot(row: OrchestrationAmendmentRequest, *, created: bool) -> AmendmentRequest:
    return AmendmentRequest(
        id=row.id,
        flow_id=row.flow_id,
        replan_decision_id=row.replan_decision_id,
        requested_by=row.requested_by,
        request_text=row.request_text,
        base_plan_version=row.base_plan_version,
        base_plan_hash=row.base_plan_hash,
        state=row.state,
        author_run_id=row.author_run_id,
        created_at=row.created_at.replace(tzinfo=UTC) if row.created_at.tzinfo is None else row.created_at.astimezone(UTC),
        created=created,
    )


def _gate_addresses(document: dict) -> set[str]:
    """The gate node addresses in a plan document, defensively.

    Reads the stored JSON rather than re-parsing it into a `LoopProposal`, because this
    runs over the *in-force* document too, and that document was written by a possibly
    older schema. A field that has since changed shape must degrade to "no gates found"
    in a report, not raise on the acceptance path — the acceptance itself is
    authoritative and re-validates the proposal through `amend_plan`.
    """
    nodes = document.get("nodes") if isinstance(document, dict) else None
    if not isinstance(nodes, list):
        return set()
    return {str(node.get("address")) for node in nodes if isinstance(node, dict) and node.get("kind") == "gate" and node.get("address")}


def gate_diff(*, base_document: dict | None, proposed_document: dict) -> GateDiff:
    """Compare gate placement between the plan in force and a proposal.

    `base_document` is None for a flow with no accepted plan yet, in which case every
    proposed gate is `added` — which is the honest reading: nothing was gated before,
    because there was no plan.
    """
    base = _gate_addresses(base_document or {})
    proposed = _gate_addresses(proposed_document)
    return GateDiff(
        added=sorted(proposed - base),
        removed=sorted(base - proposed),
        unchanged=sorted(base & proposed),
    )


async def in_force_plan(session: AsyncSession, *, org_id: str, flow_id: str) -> OrchestrationAcceptedPlan | None:
    """The flow's currently accepted plan, or None if it has never been accepted.

    `superseded_at IS NULL` is the single-row invariant `amend_plan` maintains; this is
    the same read `repository.get_accepted_plan` performs, spelled locally because the
    acceptance path needs it inside its own lock.

    Exported (#4529) because the authoring-authority validator in `agentauth/engine.py`
    must compare a request's recorded base against what is in force *by the same read*
    the acceptance path uses. Two spellings of "the plan in force" is how an authoring
    run gets admitted against a base that acceptance will then reject.
    """
    return (
        await session.execute(
            select(OrchestrationAcceptedPlan).where(
                OrchestrationAcceptedPlan.org_id == org_id,
                OrchestrationAcceptedPlan.flow_id == flow_id,
                OrchestrationAcceptedPlan.superseded_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def record_replan_request(
    session: AsyncSession,
    *,
    org_id: str,
    flow_id: str,
    replan_decision_id: str,
    requested_by: str,
    request_text: str,
) -> AmendmentRequest:
    """Record the authoring job one human replan owes.

    Idempotent on `(org_id, replan_decision_id)`: called twice for the same replan
    decision, the second call finds the first's row and reports `created=False`. That is
    what makes a duplicated webhook delivery reconcile to one authoring assignment.

    The existence check runs first and the unique index is the backstop, not the other
    way round: two concurrent ticks can both pass the read, so the `IntegrityError` arm
    below is a real path rather than a defensive gesture. The INSERT is wrapped in a
    savepoint so a lost race does not poison the caller's transaction.

    Args:
        session: Caller-owned. **Not committed here** — the request row must land in the
            same transaction as the `REPLAN_REQUESTED` decision it is born from, or a
            crash between them leaves an assignment with no attribution root.
        org_id: Server-resolved tenant.
        flow_id: The flow the replan named, resolved server-side by the command pass.
        replan_decision_id: The committed `REPLAN_REQUESTED` decision. The authoring
            run's attribution root and the idempotency key.
        requested_by: The human who asked, snapshotted.
        request_text: The human's bounded words. Stored verbatim and carried to the
            author as the request to consider. **Data, never an instruction** — nothing
            in this platform executes it.
    """
    # Serialize new authoring obligations with graph amendments. A request must
    # either be visible to the quiescence check or capture the new accepted base.
    await session.scalar(
        select(OrchestrationFlow.id)
        .where(OrchestrationFlow.org_id == org_id, OrchestrationFlow.id == flow_id)
        .with_for_update()
    )
    existing = (
        await session.execute(
            select(OrchestrationAmendmentRequest).where(
                OrchestrationAmendmentRequest.org_id == org_id,
                OrchestrationAmendmentRequest.replan_decision_id == replan_decision_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _snapshot(existing, created=False)

    # What was in force when the human asked. Recorded now rather than at authoring
    # time: the request is the moment the human's intent was fixed, and a base read
    # later would silently re-target the request at whatever landed in between.
    in_force = await in_force_plan(session, org_id=org_id, flow_id=flow_id)

    request = OrchestrationAmendmentRequest(
        org_id=org_id,
        flow_id=flow_id,
        replan_decision_id=replan_decision_id,
        requested_by=requested_by,
        base_plan_version=in_force.version if in_force is not None else None,
        base_plan_hash=in_force.plan_hash if in_force is not None else None,
        request_text=request_text,
        state=AmendmentRequestState.QUEUED.value,
    )

    try:
        async with session.begin_nested():
            session.add(request)
            await session.flush()
    except IntegrityError:
        # A concurrent pass won the unique index. Its row is the assignment; this call
        # reports it rather than raising, because both callers are handling the same
        # human ask and exactly one authoring job is owed for it.
        found = (
            await session.execute(
                select(OrchestrationAmendmentRequest).where(
                    OrchestrationAmendmentRequest.org_id == org_id,
                    OrchestrationAmendmentRequest.replan_decision_id == replan_decision_id,
                )
            )
        ).scalar_one_or_none()
        if found is None:  # pragma: no cover - the unique index is the only constraint that can fire
            raise
        logger.info(
            "amendment_request_reconciled flow=%s org=%s decision=%s request=%s",
            flow_id,
            org_id,
            replan_decision_id,
            found.id,
        )
        return _snapshot(found, created=False)

    logger.info(
        "amendment_requested flow=%s org=%s decision=%s request=%s by=%s base=v%s",
        flow_id,
        org_id,
        replan_decision_id,
        request.id,
        requested_by,
        request.base_plan_version,
    )
    return _snapshot(request, created=True)


async def assign_author_run(session: AsyncSession, *, org_id: str, request_id: str, author_run_id: str) -> None:
    """Bind an authoring run id to a request, in the DB pass, before publishing.

    This write is what `resolve_authoring_request` later checks, so it is the whole
    binding between an assignment and the run allowed to answer it. It must land in the
    same transaction that records the request and *before* the envelope is published: if
    it landed after, an authoring run could reach the registration route while the server
    had no record of having commissioned it.

    Conditional on the row still having no run assigned, so a retried pass cannot
    re-point an existing assignment at a second run and thereby admit two authors for one
    human ask.
    """
    await session.execute(
        update(OrchestrationAmendmentRequest)
        .where(
            OrchestrationAmendmentRequest.org_id == org_id,
            OrchestrationAmendmentRequest.id == request_id,
            OrchestrationAmendmentRequest.author_run_id.is_(None),
        )
        .values(author_run_id=author_run_id)
    )


async def mark_request_dispatched(session: AsyncSession, *, org_id: str, request_id: str) -> None:
    """Record that the authoring envelope was published. Called after a successful send.

    Conditional on `QUEUED`, so this is a one-way transition and a duplicated publisher
    pass cannot rewrite a dispatched request's timestamp.

    `QUEUED` is deliberately the retryable state and there is no `FAILED`: a publish whose
    ack was lost leaves the row `QUEUED`, so the next pass sees an assignment still owed
    and re-publishes under the same deduplication id. A `FAILED` state would have to be
    distinguished from "not yet tried" by something, and the only honest something is "try
    again" — which is what `QUEUED` already means.
    """
    await session.execute(
        update(OrchestrationAmendmentRequest)
        .where(
            OrchestrationAmendmentRequest.org_id == org_id,
            OrchestrationAmendmentRequest.id == request_id,
            OrchestrationAmendmentRequest.state == AmendmentRequestState.QUEUED.value,
        )
        .values(state=AmendmentRequestState.DISPATCHED.value, dispatched_at=utcnow())
    )


async def owed_authoring_requests(
    session: AsyncSession,
    *,
    limit: int,
    older_than: datetime,
) -> list[tuple[str, AmendmentRequest, str | None]]:
    """Requests still owed an authoring job. **Read-only; the recovery pass's input.**

    The gap this closes. `mark_request_dispatched` is conditional on `QUEUED` and there
    is deliberately no `FAILED` state, so a publish that did not land leaves the row
    `QUEUED` — the retryable state. But nothing read that state back. The engine command
    pass reacts to *pending comment markers*, and the marker for a replan is consumed by
    the same flush whose publish failed, so the next pass saw nothing and reconstructed
    nothing. The row sat `QUEUED` forever while the human had already been told an author
    was assigned. `authoring_dispatch`'s own docstring asserted "a later pass re-publishes
    it"; this function is what makes that sentence true rather than aspirational.

    Deliberately **not tenant-scoped**, unlike every other read in this module. The
    recovery pass runs on the tick, which has no tenant of its own — it is the engine
    finishing work it already accepted on every tenant's behalf. `org_id` is therefore
    *returned* per row rather than filtered on, and every downstream step (installation
    resolution, identity resolution, the authority row) re-derives its tenant from it.

    `older_than` is a grace boundary, not an optimisation. The pass that creates a
    request publishes it moments later in its own post-commit flush, and a recovery
    running concurrently would otherwise race that first attempt: both would publish,
    and while the derived deduplication id collapses them in SQS, relying on that for
    the *ordinary* path would make the dedup window load-bearing for something it should
    never see. Skipping requests younger than the boundary means recovery only ever sees
    genuinely stuck work.

    `limit` bounds the pass. A tenant that somehow accrues thousands of unpublishable
    requests must not make the tick unbounded; the remainder is simply picked up next
    wake, because `QUEUED` does not expire.

    Ordered oldest-first so the longest-waiting human is answered first, and so the cap
    cannot indefinitely starve one request behind newer arrivals.

    Returns:
        `(org_id, request_snapshot, flow_intent_ref)` per owed request. The flow's
        `intent_ref` is joined in because a rebuilt envelope has to be *addressed* — the
        request row carries no issue number, and the recovery pass must not invent one.
    """
    rows = (
        await session.execute(
            select(OrchestrationAmendmentRequest, OrchestrationFlow.intent_ref)
            .join(OrchestrationFlow, OrchestrationFlow.id == OrchestrationAmendmentRequest.flow_id)
            .where(
                OrchestrationAmendmentRequest.state == AmendmentRequestState.QUEUED.value,
                OrchestrationAmendmentRequest.created_at < older_than,
            )
            .order_by(OrchestrationAmendmentRequest.created_at.asc())
            .limit(limit)
        )
    ).all()
    return [(row[0].org_id, _snapshot(row[0], created=False), row[1]) for row in rows]


async def resolve_authoring_request(session: AsyncSession, *, org_id: str, request_id: str, author_run_id: str) -> AmendmentRequest:
    """The open assignment a presented run is registering against, or refuse.

    This is the binding: the run id is *presented* by the caller, and it must equal the
    `author_run_id` the server wrote onto the request row when it built the envelope. So
    an authoring agent cannot file a draft against an assignment it was not given, even
    holding the route's permission.

    The tenant filter is applied in the query rather than checked afterwards, so a
    cross-tenant request id is indistinguishable from an absent one.

    Raises:
        AmendmentRequestNotFoundError: Absent, another tenant's, or not this run's.
    """
    request = (
        await session.execute(
            select(OrchestrationAmendmentRequest).where(
                OrchestrationAmendmentRequest.org_id == org_id,
                OrchestrationAmendmentRequest.id == request_id,
            )
        )
    ).scalar_one_or_none()
    if request is None or not request.author_run_id or request.author_run_id != author_run_id:
        raise AmendmentRequestNotFoundError(f"no authoring assignment {request_id!r} is open for this run in this tenant")
    return _snapshot(request, created=False)


async def register_amendment_draft(
    session: AsyncSession,
    *,
    org_id: str,
    request: AmendmentRequest,
    author_run_id: str,
    proposal: LoopProposal,
) -> PendingAmendmentDraft:
    """File an authored amendment as a PENDING draft. Writes no graph state.

    Deliberately does NOT: transform the document, insert an acceptance gate, compile it,
    create a flow, or write a decision. See the module docstring — a draft is data the
    graph does not reference, and that is what makes it inert without a filter.

    The flow comes from `request.flow_id`, never from the document or an issue lookup.
    That is #4556's target-ambiguity protection carried onto this path: the assignment
    named the flow, so the author cannot re-aim its output at another one.

    Idempotent on `(org_id, flow_id, request_id, proposal_hash)`: a fail-soft author's retry converges
    on its own row. The concurrent case is handled through the unique index for the same
    reason as in `record_replan_request`.

    Args:
        session: Caller-owned. Not committed here.
        org_id: Server-resolved tenant, from the run's ingress row (see
            `draft_binding.resolve_draft_tenant`), never a caller header.
        request: The open assignment, from `resolve_authoring_request`.
        author_run_id: The presented run id, already checked against the request.
        proposal: The authored replacement document.

    Raises:
        FlowNotFoundError: The request's flow no longer exists in this tenant.
    """
    flow = (
        await session.execute(
            select(OrchestrationFlow).where(
                OrchestrationFlow.org_id == org_id,
                OrchestrationFlow.id == request.flow_id,
            )
        )
    ).scalar_one_or_none()
    if flow is None:
        raise FlowNotFoundError(f"no orchestration flow {request.flow_id!r} in this tenant")

    document = proposal.model_dump(mode="json")
    # The same canonical hash the accepted-plan store uses, so a draft whose document
    # equals what is already in force is recognisable as such rather than looking like a
    # novel proposal.
    document_hash = plan_hash(proposal)

    in_force = await in_force_plan(session, org_id=org_id, flow_id=request.flow_id)
    diff = gate_diff(
        base_document=in_force.plan_document if in_force is not None else None,
        proposed_document=document,
    )

    existing = (
        await session.execute(
            select(OrchestrationPendingAmendment).where(
                OrchestrationPendingAmendment.org_id == org_id,
                OrchestrationPendingAmendment.flow_id == request.flow_id,
                OrchestrationPendingAmendment.request_id == request.id,
                OrchestrationPendingAmendment.proposal_hash == document_hash,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return PendingAmendmentDraft(
            draft_id=existing.id,
            flow_id=existing.flow_id,
            request_id=existing.request_id,
            base_plan_version=existing.base_plan_version,
            proposal_hash=existing.proposal_hash,
            gate_diff=diff,
            already_registered=True,
            state=existing.state,
        )

    # The base is the request's, not a fresh read of what is in force. The author was
    # commissioned against that version, and acceptance compares the recorded base to
    # current state — so re-reading here would quietly "refresh" a stale draft into one
    # that passes the conflict check it exists to fail.
    draft = OrchestrationPendingAmendment(
        org_id=org_id,
        flow_id=request.flow_id,
        request_id=request.id,
        author_run_id=author_run_id,
        base_plan_version=request.base_plan_version,
        base_plan_hash=request.base_plan_hash,
        proposal_document=document,
        proposal_hash=document_hash,
        # Unconditional, and the only place a draft's state is set on this path. An
        # author has no way to file anything other than a pending proposal.
        state=PendingAmendmentState.PENDING.value,
    )

    try:
        async with session.begin_nested():
            session.add(draft)
            await session.flush()
    except IntegrityError:
        found = (
            await session.execute(
                select(OrchestrationPendingAmendment).where(
                    OrchestrationPendingAmendment.org_id == org_id,
                    OrchestrationPendingAmendment.flow_id == request.flow_id,
                    OrchestrationPendingAmendment.request_id == request.id,
                    OrchestrationPendingAmendment.proposal_hash == document_hash,
                )
            )
        ).scalar_one_or_none()
        if found is None:  # pragma: no cover - the unique index is the only constraint that can fire
            raise
        return PendingAmendmentDraft(
            draft_id=found.id,
            flow_id=found.flow_id,
            request_id=found.request_id,
            base_plan_version=found.base_plan_version,
            proposal_hash=found.proposal_hash,
            gate_diff=diff,
            already_registered=True,
            state=found.state,
        )

    logger.info(
        "amendment_drafted flow=%s org=%s draft=%s request=%s run=%s base=v%s gates_added=%s gates_removed=%s",
        request.flow_id,
        org_id,
        draft.id,
        request.id,
        author_run_id,
        draft.base_plan_version,
        len(diff.added),
        len(diff.removed),
    )
    return PendingAmendmentDraft(
        draft_id=draft.id,
        flow_id=draft.flow_id,
        request_id=draft.request_id,
        base_plan_version=draft.base_plan_version,
        proposal_hash=draft.proposal_hash,
        gate_diff=diff,
        already_registered=False,
    )


async def accept_amendment(
    session: AsyncSession,
    *,
    draft_id: str,
    actor: AmendmentContext,
    flow_id: str | None = None,
) -> AmendmentAcceptResult:
    """Apply one named pending amendment as the accepting human. One transaction.

    The only path from a draft to graph state, and it delegates the actual work to
    `amend_plan` — the same call the dashboard's amendment route makes, with an
    `AmendmentContext` built from the accepting human's resolved identity. There is no
    second amendment implementation here, deliberately: a second one would be free to
    accept a document `amend_plan` refuses, and this is the *easier* path to reach.

    Order of checks is the security property. Tenant and flow scope are applied in the
    query; pending status and the exact base version + hash are checked under the flow's
    row lock before anything is written. So a refusal writes no plan version, and two
    concurrent accepts on one flow serialize on the same row `amend_plan` locks.

    Args:
        session: Caller-owned. **Not committed here** — the amendment, the draft's status
            and whatever else the request writes land together or not at all.
        draft_id: The draft the human named. There is no "latest" selector.
        actor: The accepting human's server-resolved context. `actor.org_id` is
            authoritative and `actor.actor_id` is the human, never an agent.
        flow_id: When given, the draft must belong to this flow. Passed by callers that
            already resolved a flow (the command pass resolves one from the issue), so a
            draft id belonging to another flow in the same tenant cannot be applied to the
            flow the human was talking about.

    Returns:
        An `AmendmentAcceptResult`. `replayed=True` means the draft was already accepted
        and the original outcome is being reported again.

    Raises:
        AmendmentDraftNotFoundError: Absent, another tenant's, or another flow's.
        AmendmentConflictError: Not pending, or its recorded base no longer describes what
            is in force. Nothing written.
        ProposalRejectedError / TenantMismatchError / FlowNotFoundError: Raised by
            `amend_plan`, unchanged and uncaught — a draft whose document fails
            authoritative validation must fail exactly as a hand-submitted one does.
    """
    conditions = [
        OrchestrationPendingAmendment.org_id == actor.org_id,
        OrchestrationPendingAmendment.id == draft_id,
    ]
    if flow_id is not None:
        conditions.append(OrchestrationPendingAmendment.flow_id == flow_id)
    draft = (await session.execute(select(OrchestrationPendingAmendment).where(*conditions))).scalar_one_or_none()
    if draft is None:
        raise AmendmentDraftNotFoundError(f"no pending amendment {draft_id!r} in this tenant")

    async with session.begin_nested():
        # Serialize against other acceptances on this flow before reading what is in
        # force. Same row and same order as `amend_plan`'s own lock, so the two nest
        # without deadlocking.
        await session.execute(
            select(OrchestrationFlow).where(OrchestrationFlow.org_id == actor.org_id, OrchestrationFlow.id == draft.flow_id).with_for_update()
        )
        # Another acceptance may have committed while this transaction waited
        # for the flow lock. Refresh the identity-map row before deciding replay
        # or terminal status; its original pending snapshot is no longer current.
        await session.refresh(draft)

        # --- Replay before refusal ---------------------------------------------------
        # Checked FIRST, and before the base comparison, because an already-accepted draft's
        # base is *guaranteed* not to match current state — it superseded it. A base check
        # ordered first would answer every repeated accept with a conflict, which is precisely
        # the "repeated accept returns the original result" property inverted. A human
        # re-sending a comment, or a duplicated delivery, must see what they saw the first
        # time.
        if draft.state == PendingAmendmentState.ACCEPTED.value:
            return AmendmentAcceptResult(
                draft_id=draft.id,
                flow_id=draft.flow_id,
                plan_version=draft.accepted_plan_version or 0,
                superseded_version=draft.base_plan_version,
                decision_id=draft.accepted_by_decision_id or "",
                gate_diff=GateDiff(),
                replayed=True,
            )

        if draft.state != PendingAmendmentState.PENDING.value:
            # Rejected or superseded. Terminal, and not re-openable: a superseded draft's base
            # describes a plan that is no longer in force, so "accept it anyway" is the
            # discard-someone-else's-amendment failure by another route.
            raise AmendmentConflictError(
                "draft_not_pending",
                f"amendment `{draft_id}` is `{draft.state}` and can no longer be accepted. Comment "
                "`@agent-engine replan: <what should change>` to request a fresh one.",
            )

        in_force = await in_force_plan(session, org_id=actor.org_id, flow_id=draft.flow_id)

        current_version = in_force.version if in_force is not None else None
        current_hash = in_force.plan_hash if in_force is not None else None
        if (draft.base_plan_version, draft.base_plan_hash) != (current_version, current_hash):
            # Refused, never rebased. See the module docstring: applying a document
            # authored against an older version discards the intervening amendment while
            # reporting success.
            logger.warning(
                "amendment_stale flow=%s org=%s draft=%s base=v%s current=v%s",
                draft.flow_id,
                actor.org_id,
                draft.id,
                draft.base_plan_version,
                current_version,
            )
            raise AmendmentConflictError(
                "stale_base",
                f"amendment `{draft_id}` was authored against plan version "
                f"{draft.base_plan_version if draft.base_plan_version is not None else 'none'}, but version "
                f"{current_version if current_version is not None else 'none'} is now in force. Accepting it "
                "would discard the amendment that landed in between. Comment `@agent-engine replan: <what "
                "should change>` to have it re-authored against the current plan.",
            )

        diff = gate_diff(
            base_document=in_force.plan_document if in_force is not None else None,
            proposed_document=draft.proposal_document,
        )

        # Re-parsed from the stored document, so the amendment applied is exactly what was
        # on file and reviewable — not a re-serialisation of something held in memory
        # since authoring. A document that no longer parses fails here, which is the
        # correct outcome: it is not acceptable.
        proposal = LoopProposal.model_validate(draft.proposal_document)

        # The existing transaction, with the HUMAN's context. Uncaught: a draft that fails
        # authoritative validation must be refused exactly as a hand-submitted document
        # is, and `amend_plan` owns those refusals.
        result: AmendResult = await amend_plan(session, draft.flow_id, proposal, actor)

        now: datetime = utcnow()
        draft.state = PendingAmendmentState.ACCEPTED.value
        # Server-written from the human's resolved context and from `amend_plan`'s own
        # result. Nothing an author supplied reaches these four columns.
        draft.accepted_by = actor.actor_id
        draft.accepted_by_decision_id = result.decision_id
        draft.accepted_plan_version = result.plan_version
        draft.decided_at = now

        # Every other pending draft on this flow is now unacceptable: its recorded base
        # describes the version this acceptance just superseded. Marked here rather than
        # left to fail later, so an operator reading the flow sees why, and so a human is
        # not offered a draft that can only be refused. NOT `rejected` — nobody declined
        # these.
        siblings = list(
            (
                await session.execute(
                    select(OrchestrationPendingAmendment).where(
                        OrchestrationPendingAmendment.org_id == actor.org_id,
                        OrchestrationPendingAmendment.flow_id == draft.flow_id,
                        OrchestrationPendingAmendment.state == PendingAmendmentState.PENDING.value,
                        OrchestrationPendingAmendment.id != draft.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for sibling in siblings:
            sibling.state = PendingAmendmentState.SUPERSEDED.value
            sibling.superseded_by_draft_id = draft.id
            sibling.decided_at = now

        await session.flush()

    logger.info(
        "amendment_accepted flow=%s org=%s draft=%s by=%s v%s->v%s gates_added=%s gates_removed=%s superseded=%s",
        draft.flow_id,
        actor.org_id,
        draft.id,
        actor.actor_id,
        result.superseded_version,
        result.plan_version,
        len(diff.added),
        len(diff.removed),
        len(siblings),
    )

    return AmendmentAcceptResult(
        draft_id=draft.id,
        flow_id=draft.flow_id,
        plan_version=result.plan_version,
        superseded_version=result.superseded_version,
        decision_id=result.decision_id,
        gate_diff=diff,
        superseded_draft_ids=[sibling.id for sibling in siblings],
        replayed=False,
    )
