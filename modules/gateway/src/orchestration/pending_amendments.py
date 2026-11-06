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
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import utcnow

from .amend import AmendmentContext, AmendResult, FlowNotFoundError, amend_plan
from .amendment_impact import assess_amendment, assignment_conflict_violations, new_work_issue_violations
from .amendment_issue_publisher import PlannedPublication, apply_publications, permitted_repositories, publish_unbound_work
from .compile import ProposalRejectedError, address_of, plan_hash
from .execution_policy import ExecutionPolicy, summarize_policy
from .models import (
    AmendmentRequestState,
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationAmendmentRequest,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationPendingAmendment,
    PendingAmendmentState,
)
from .proposal import LoopProposal
from .repository import OrchestrationRepository
from .state import ActorKind

logger = logging.getLogger("bedrockgateway.orchestration.pending_amendments")

__all__ = [
    "AmendmentAcceptResult",
    "AmendmentConflictError",
    "AmendmentDraftNotFoundError",
    "AmendmentRequest",
    "AmendmentRequestNotFoundError",
    "AmendmentRevision",
    "publish_for_acceptance",
    "GateDiff",
    "PendingAmendmentDraft",
    "PendingAmendmentView",
    "accept_amendment",
    "assign_author_run",
    "gate_diff",
    "get_pending_amendment",
    "in_force_plan",
    "list_pending_amendments",
    "mark_request_dispatched",
    "owed_authoring_requests",
    "record_replan_request",
    "register_amendment_draft",
    "reject_amendment",
    "resolve_authoring_request",
    "revise_amendment",
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
    authoring_assignment: dict | None = None


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
class PendingAmendmentView:
    """One draft as a reviewer sees it. Read-only projection, never the ORM row.

    A snapshot for the same reason `AmendmentRequest` is one: a route holding a live row
    could mutate draft state outside the three functions allowed to
    (`accept_amendment`, `reject_amendment`, `revise_amendment`), and draft state is what
    stands between a proposal and the graph.

    `proposal_hash` is the **revision identity** an acceptance binds to. Carried on every
    view so a client that previews, renders, then accepts can pass back the exact hash it
    showed a human — which is how editing a draft invalidates an approval of the version
    before it, without needing a separate approval token.
    """

    draft_id: str
    flow_id: str
    request_id: str
    state: str
    #: Canonical `plan_hash` of `proposal_document`: this revision's identity.
    proposal_hash: str
    base_plan_version: int | None
    base_plan_hash: str | None
    #: The human whose replan commissioned this, and their words. Verbatim **data**,
    #: never an instruction — carried so a reviewer can see what was asked for.
    requested_by: str
    request_text: str
    created_at: datetime
    #: True when the recorded base still describes what is in force. False means
    #: acceptance can only refuse, and the reviewer should be told before they try.
    base_is_current: bool
    gate_diff: GateDiff
    #: Present only on `list`/`show` with the document requested. Omitted from list
    #: responses by default: a flow's drafts are whole plan documents, and returning
    #: twenty of them turns a list call into a multi-megabyte response.
    proposal_document: dict | None = None
    accepted_plan_version: int | None = None
    accepted_by: str | None = None
    decision_id: str | None = None
    decision_reason: str | None = None
    author_run_id: str | None = None
    approved_diff: dict | None = None
    superseded_by_draft_id: str | None = None
    decided_at: datetime | None = None
    #: The draft this one revised, when it came from an edit rather than an author.
    revision_of_draft_id: str | None = None


@dataclass(frozen=True)
class AmendmentRevision:
    """What editing a draft produced: a new draft, and the one it replaced.

    Two ids rather than one mutated row, because a revision must **invalidate** any
    approval of what came before. An in-place edit cannot do that — a human who read
    revision 1 and typed `accept <id>` would apply revision 2 under the same id, which is
    precisely the "approve one thing, apply another" failure the whole preview/approve
    interaction exists to prevent. A new row means the prior hash is no longer
    acceptable, and the acceptance path's exact-hash check enforces it for free.
    """

    draft_id: str
    flow_id: str
    proposal_hash: str
    superseded_draft_id: str
    gate_diff: GateDiff
    #: True when the edit produced a document identical to the draft it edited, so no
    #: new row was written. A no-op edit must not churn the revision a human is reading.
    unchanged: bool = False


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
    receipts_preserved: list[dict] = field(default_factory=list)
    receipts_dropped: list[dict] = field(default_factory=list)
    effective_limits_before: dict | None = None
    effective_limits_after: dict | None = None


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
        authoring_assignment=row.authoring_assignment,
        created_at=row.created_at.replace(tzinfo=UTC) if row.created_at.tzinfo is None else row.created_at.astimezone(UTC),
        created=created,
    )


def _approved_diff(base: dict | None, proposed: dict) -> dict:
    before_by_address = {node["address"]: node for node in (base or {}).get("nodes", [])}
    after_by_address = {node["address"]: node for node in proposed.get("nodes", [])}
    before_nodes = set(before_by_address)
    after_nodes = set(after_by_address)
    before_edges = {(edge["from_address"], edge["to_address"]) for edge in (base or {}).get("edges", [])}
    after_edges = {(edge["from_address"], edge["to_address"]) for edge in proposed.get("edges", [])}
    return {
        "nodes_added": sorted(after_nodes - before_nodes),
        "nodes_removed": sorted(before_nodes - after_nodes),
        "nodes_modified": [
            {"address": address, "before": before_by_address[address], "after": after_by_address[address]}
            for address in sorted(before_nodes & after_nodes)
            if before_by_address[address] != after_by_address[address]
        ],
        "edges_added": [{"from_address": start, "to_address": end} for start, end in sorted(after_edges - before_edges)],
        "edges_removed": [{"from_address": start, "to_address": end} for start, end in sorted(before_edges - after_edges)],
        "execution_policy_before": _policy_summary((base or {}).get("execution_policy")),
        "execution_policy_after": _policy_summary(proposed.get("execution_policy")),
    }


def _policy_summary(value: object) -> dict | None:
    if value is None:
        return None
    return summarize_policy(ExecutionPolicy.model_validate(value)).model_dump(mode="json")


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
    await session.scalar(select(OrchestrationFlow.id).where(OrchestrationFlow.org_id == org_id, OrchestrationFlow.id == flow_id).with_for_update())
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


async def assign_author_run(session: AsyncSession, *, org_id: str, request_id: str, author_run_id: str, assignment: dict | None = None) -> None:
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
        .values(author_run_id=author_run_id, authoring_assignment=assignment)
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

    A stored draft satisfies authoring even if a publish acknowledgement was lost or
    the proposal was supplied directly by a human. Reviewing or deciding that draft
    must not trigger another authoring dispatch, regardless of its current state.

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
        `(org_id, request_snapshot, flow_intent_ref)` per owed request. The flow join
        retains the existing read contract, but its mutable `intent_ref` does not
        authorize publication; the request's saved authoring assignment does.
    """
    rows = (
        await session.execute(
            select(OrchestrationAmendmentRequest, OrchestrationFlow.intent_ref)
            .join(OrchestrationFlow, OrchestrationFlow.id == OrchestrationAmendmentRequest.flow_id)
            .where(
                OrchestrationAmendmentRequest.state == AmendmentRequestState.QUEUED.value,
                OrchestrationAmendmentRequest.created_at < older_than,
                ~select(OrchestrationPendingAmendment.id)
                .where(
                    OrchestrationPendingAmendment.org_id == OrchestrationAmendmentRequest.org_id,
                    OrchestrationPendingAmendment.flow_id == OrchestrationAmendmentRequest.flow_id,
                    OrchestrationPendingAmendment.request_id == OrchestrationAmendmentRequest.id,
                )
                .exists(),
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


async def _view_of(
    session: AsyncSession,
    row: OrchestrationPendingAmendment,
    *,
    org_id: str,
    include_document: bool,
    in_force: OrchestrationAcceptedPlan | None,
    request: OrchestrationAmendmentRequest | None,
) -> PendingAmendmentView:
    """Project one draft row for reading, with its staleness and gate diff resolved.

    `in_force` and `request` are passed in rather than read here so a list call does one
    plan read and one request read for the whole page instead of one pair per draft.

    `base_is_current` is computed with the **same** pair comparison `accept_amendment`
    makes, not a version-only check. A hash difference at the same version means the plan
    in force was rewritten — rarer, but exactly the case a version-only check would call
    acceptable and acceptance would then refuse.
    """
    current_version = in_force.version if in_force is not None else None
    current_hash = in_force.plan_hash if in_force is not None else None
    approved_diff = None
    decision_reason = None
    if row.accepted_by_decision_id:
        decision_reason = await session.scalar(
            select(OrchestrationDecision.reason).where(
                OrchestrationDecision.org_id == org_id,
                OrchestrationDecision.id == row.accepted_by_decision_id,
            )
        )
    if row.state == PendingAmendmentState.ACCEPTED.value:
        base = await session.scalar(
            select(OrchestrationAcceptedPlan).where(
                OrchestrationAcceptedPlan.org_id == org_id,
                OrchestrationAcceptedPlan.flow_id == row.flow_id,
                OrchestrationAcceptedPlan.version == row.base_plan_version,
            )
        )
        approved_diff = _approved_diff(base.plan_document if base is not None else None, row.proposal_document)
    return PendingAmendmentView(
        draft_id=row.id,
        flow_id=row.flow_id,
        request_id=row.request_id,
        state=row.state,
        proposal_hash=row.proposal_hash,
        base_plan_version=row.base_plan_version,
        base_plan_hash=row.base_plan_hash,
        requested_by=request.requested_by if request is not None else "",
        request_text=request.request_text if request is not None else "",
        created_at=row.created_at.replace(tzinfo=UTC) if row.created_at.tzinfo is None else row.created_at.astimezone(UTC),
        base_is_current=(row.base_plan_version, row.base_plan_hash) == (current_version, current_hash),
        gate_diff=gate_diff(
            base_document=in_force.plan_document if in_force is not None else None,
            proposed_document=row.proposal_document,
        ),
        proposal_document=row.proposal_document if include_document else None,
        accepted_plan_version=row.accepted_plan_version,
        accepted_by=row.accepted_by,
        decision_id=row.accepted_by_decision_id,
        decision_reason=decision_reason,
        author_run_id=row.author_run_id,
        approved_diff=approved_diff,
        superseded_by_draft_id=row.superseded_by_draft_id,
        decided_at=(
            None
            if row.decided_at is None
            else (row.decided_at.replace(tzinfo=UTC) if row.decided_at.tzinfo is None else row.decided_at.astimezone(UTC))
        ),
        revision_of_draft_id=_revision_of(row),
    )


#: Marker prefix for the edit provenance a revision carries. Encoded in the existing
#: `author_run_id` column rather than in a new one: #5329 is explicitly not supposed to
#: need a migration, and this column's purpose — "which run produced this document" — is
#: exactly what an edit also answers, with the editor's identity in place of a run id.
_REVISION_MARKER = "revision-of:"


def _revision_of(row: OrchestrationPendingAmendment) -> str | None:
    """The draft this row revised, or None if an author produced it directly."""
    marker = row.author_run_id or ""
    return marker[len(_REVISION_MARKER) :].split("/", 1)[0] or None if marker.startswith(_REVISION_MARKER) else None


async def _revision_successor(session: AsyncSession, *, org_id: str, draft: OrchestrationPendingAmendment) -> str | None:
    """The id of the still-pending revision that replaced `draft`, if that is why it went.

    `superseded_by_draft_id` alone cannot answer this. It is set by two different causes:
    `revise_amendment` names the edit that replaced this draft, and `accept_amendment`
    names the *sibling whose acceptance* displaced it. Only the first has a successor a
    human can go and read; the second names a draft that is already in force, and sending
    someone to "review" it would be telling them to re-approve the plan of record.

    Resolved by loading the named draft and checking it is still `PENDING`, rather than by
    storing a cause column: the distinction is derivable, and a second column recording
    *why* a draft was superseded would be a fact that could disagree with the states.
    """
    if not draft.superseded_by_draft_id:
        return None
    successor = (
        await session.execute(
            select(OrchestrationPendingAmendment).where(
                OrchestrationPendingAmendment.org_id == org_id,
                OrchestrationPendingAmendment.id == draft.superseded_by_draft_id,
            )
        )
    ).scalar_one_or_none()
    return successor.id if successor is not None and successor.state == PendingAmendmentState.PENDING.value else None


async def _load_request(session: AsyncSession, *, org_id: str, request_id: str) -> OrchestrationAmendmentRequest | None:
    return (
        await session.execute(
            select(OrchestrationAmendmentRequest).where(
                OrchestrationAmendmentRequest.org_id == org_id,
                OrchestrationAmendmentRequest.id == request_id,
            )
        )
    ).scalar_one_or_none()


async def list_pending_amendments(
    session: AsyncSession,
    *,
    org_id: str,
    flow_id: str,
    states: list[str] | None = None,
    author_run_id: str | None = None,
) -> list[PendingAmendmentView]:
    """A flow's amendment drafts, newest first. Reads only.

    Newest first because the question a reviewer opens this with is "what am I being
    asked to decide", and that is the recent end. Deterministic within one timestamp by
    breaking ties on id, so a paging client cannot see one draft twice.

    Args:
        session: Caller-owned. No lock and no write — listing is advisory.
        org_id: Server-resolved tenant, applied in the query.
        flow_id: The flow whose drafts to list. Not optional: there is no cross-flow
            listing, because a draft is only meaningful against the plan it amends.
        states: Restrict to these `PendingAmendmentState` values. `None` returns all
            states rather than only pending ones — a reviewer asking "what happened to
            the amendment I rejected" needs the terminal rows too, and a default that
            hid them would make the CLI's `list` look like the draft vanished.

    Returns:
        Views **without** documents. A caller that needs the document asks for one draft
        by id; returning twenty plan documents would make a list call cost megabytes.
    """
    conditions = [
        OrchestrationPendingAmendment.org_id == org_id,
        OrchestrationPendingAmendment.flow_id == flow_id,
    ]
    if states:
        conditions.append(OrchestrationPendingAmendment.state.in_(states))
    if author_run_id is not None:
        conditions.append(OrchestrationPendingAmendment.author_run_id == author_run_id)

    rows = list(
        (
            await session.execute(
                select(OrchestrationPendingAmendment)
                .where(*conditions)
                .order_by(OrchestrationPendingAmendment.created_at.desc(), OrchestrationPendingAmendment.id.desc())
            )
        )
        .scalars()
        .all()
    )

    in_force = await in_force_plan(session, org_id=org_id, flow_id=flow_id)
    requests = {}
    for request_id in {row.request_id for row in rows}:
        requests[request_id] = await _load_request(session, org_id=org_id, request_id=request_id)

    return [
        await _view_of(
            session,
            row,
            org_id=org_id,
            include_document=False,
            in_force=in_force,
            request=requests.get(row.request_id),
        )
        for row in rows
    ]


async def get_pending_amendment(
    session: AsyncSession,
    *,
    org_id: str,
    draft_id: str,
    flow_id: str | None = None,
    include_document: bool = True,
    author_run_id: str | None = None,
) -> PendingAmendmentView:
    """One draft by id, with its document. Reads only.

    Scoped exactly as `accept_amendment` scopes its lookup, and raising the same error
    for absent, cross-tenant and wrong-flow. A `show` that distinguished them would be
    the enumeration oracle `accept` is careful not to be — and `show` is the *easier*
    call to make in a loop.

    Raises:
        AmendmentDraftNotFoundError: Absent, another tenant's, or another flow's.
    """
    conditions = [
        OrchestrationPendingAmendment.org_id == org_id,
        OrchestrationPendingAmendment.id == draft_id,
    ]
    if flow_id is not None:
        conditions.append(OrchestrationPendingAmendment.flow_id == flow_id)
    if author_run_id is not None:
        conditions.append(OrchestrationPendingAmendment.author_run_id == author_run_id)

    row = (await session.execute(select(OrchestrationPendingAmendment).where(*conditions))).scalar_one_or_none()
    if row is None:
        raise AmendmentDraftNotFoundError(f"no pending amendment {draft_id!r} in this tenant")

    return await _view_of(
        session,
        row,
        org_id=org_id,
        include_document=include_document,
        in_force=await in_force_plan(session, org_id=org_id, flow_id=row.flow_id),
        request=await _load_request(session, org_id=org_id, request_id=row.request_id),
    )


async def reject_amendment(
    session: AsyncSession,
    *,
    org_id: str,
    draft_id: str,
    rejected_by: str,
    reason: str | None = None,
    flow_id: str | None = None,
    decision: AmendmentContext | None = None,
) -> PendingAmendmentView:
    """Decline a draft. The first writer of `PendingAmendmentState.REJECTED`.

    #4529 declared the state and left it unwritten, so a reviewer who disagreed with a
    proposal had no way to say so: the draft stayed `PENDING` and kept appearing as work
    owed, indistinguishable from one nobody had looked at. Refusing to decide is not a
    decision, and a queue that cannot be emptied stops being read.

    Writes **no graph state and no plan version** — that is the whole point of a
    rejection. It sets the draft's state and time and appends an immutable attributed
    rejection decision. The compatibility-named decision columns point at that record;
    `accepted_plan_version` remains NULL.

    Deliberately **not** idempotent-by-replay the way `accept_amendment` is: rejecting an
    already-rejected draft returns the existing row unchanged rather than re-stamping the
    decider, so a duplicated request cannot rewrite who declined it or when.

    Args:
        session: Caller-owned. Not committed here.
        org_id: Server-resolved tenant.
        draft_id: The draft being declined, by name. No "latest" selector, for the same
            reason acceptance has none.
        rejected_by: The deciding human's resolved identity. Server-supplied.
        reason: Their words, bounded by the caller. Data, never an instruction.
        flow_id: When given, the draft must belong to this flow.

    Raises:
        AmendmentDraftNotFoundError: Absent, another tenant's, or another flow's.
        AmendmentConflictError: The draft was already accepted — its content is in force
            and rejecting it now would report a decision that did not happen. Undoing an
            applied amendment is a fresh replan, not a late rejection.
    """
    conditions = [
        OrchestrationPendingAmendment.org_id == org_id,
        OrchestrationPendingAmendment.id == draft_id,
    ]
    if flow_id is not None:
        conditions.append(OrchestrationPendingAmendment.flow_id == flow_id)

    row = (await session.execute(select(OrchestrationPendingAmendment).where(*conditions))).scalar_one_or_none()
    if row is None:
        raise AmendmentDraftNotFoundError(f"no pending amendment {draft_id!r} in this tenant")

    async with session.begin_nested():
        # Same flow row and same order as the acceptance path, so a concurrent
        # accept/reject on one draft serializes rather than interleaving.
        await session.execute(
            select(OrchestrationFlow).where(OrchestrationFlow.org_id == org_id, OrchestrationFlow.id == row.flow_id).with_for_update()
        )
        await session.refresh(row)

        if row.state == PendingAmendmentState.ACCEPTED.value:
            raise AmendmentConflictError(
                "draft_already_accepted",
                f"amendment `{draft_id}` was already accepted as plan version "
                f"{row.accepted_plan_version if row.accepted_plan_version is not None else 'unknown'} and is in force. "
                "Request a fresh replan to change it back.",
            )

        if row.state == PendingAmendmentState.PENDING.value:
            row.state = PendingAmendmentState.REJECTED.value
            row.decided_at = utcnow()
            if decision is not None:
                decision_id = str(uuid5(NAMESPACE_URL, f"adp:amendment-rejected:{org_id}:{row.id}"))
                session.add(
                    OrchestrationDecision(
                        id=decision_id,
                        org_id=org_id,
                        flow_id=row.flow_id,
                        node_id=None,
                        kind=DecisionKind.AMENDMENT_REJECTED.value,
                        actor_id=decision.actor_id,
                        actor_role=decision.actor_role,
                        actor_kind=decision.actor_kind.value,
                        reason=reason,
                    )
                )
                row.accepted_by = decision.actor_id
                row.accepted_by_decision_id = decision_id
            # The append-only decision captures the rejecting actor and reason. Existing
            # nullable decision columns are reused for compatibility; no graph state or
            # accepted plan version is written.
            logger.info(
                "amendment_rejected flow=%s org=%s draft=%s by=%s reason=%s",
                row.flow_id,
                org_id,
                row.id,
                rejected_by,
                (reason or "")[:200],
            )
            await session.flush()

    return await _view_of(
        session,
        row,
        org_id=org_id,
        include_document=False,
        in_force=await in_force_plan(session, org_id=org_id, flow_id=row.flow_id),
        request=await _load_request(session, org_id=org_id, request_id=row.request_id),
    )


async def revise_amendment(
    session: AsyncSession,
    *,
    org_id: str,
    draft_id: str,
    proposal: LoopProposal,
    revised_by: str,
    flow_id: str | None = None,
) -> AmendmentRevision:
    """Replace a pending draft with an edited one, as a NEW revision. Writes no plan.

    Editing produces a new row and supersedes the old one; it never mutates the document
    in place. That is what makes an earlier approval invalid: a human who read revision 1
    and typed `accept <old-id>` gets "that draft is superseded" rather than silently
    applying revision 2 under the id they read. An in-place edit would make
    "preview, then approve" unverifiable, because the thing approved could change between
    the two steps with no trace.

    The new draft inherits the **original's** recorded base version and hash, not a fresh
    read. An edit changes what is proposed, not what it was proposed against — and
    re-reading the base here would "refresh" a stale draft into one that passes the
    conflict check it exists to fail, which is the same mistake `register_amendment_draft`
    documents refusing.

    Args:
        session: Caller-owned. Not committed here.
        org_id: Server-resolved tenant.
        draft_id: The pending draft being edited.
        proposal: The edited replacement document, whole. Not a patch: acceptance
            re-validates a full document, and a patch format here would need its own
            validator that could drift from it.
        revised_by: The editing human's resolved identity, recorded as this revision's
            provenance in place of an authoring run id.
        flow_id: When given, the draft must belong to this flow.

    Returns:
        An `AmendmentRevision`. `unchanged=True` means the edit produced an identical
        document and no new row was written — a no-op edit must not churn the revision a
        reviewer is currently reading.

    Raises:
        AmendmentDraftNotFoundError: Absent, another tenant's, or another flow's.
        AmendmentConflictError: The draft is not pending. An accepted draft's content is
            in force and editing it would misrepresent the plan of record; a rejected or
            superseded one is terminal.
    """
    conditions = [
        OrchestrationPendingAmendment.org_id == org_id,
        OrchestrationPendingAmendment.id == draft_id,
    ]
    if flow_id is not None:
        conditions.append(OrchestrationPendingAmendment.flow_id == flow_id)

    row = (await session.execute(select(OrchestrationPendingAmendment).where(*conditions))).scalar_one_or_none()
    if row is None:
        raise AmendmentDraftNotFoundError(f"no pending amendment {draft_id!r} in this tenant")

    document = proposal.model_dump(mode="json")
    document_hash = plan_hash(proposal)

    async with session.begin_nested():
        await session.execute(
            select(OrchestrationFlow).where(OrchestrationFlow.org_id == org_id, OrchestrationFlow.id == row.flow_id).with_for_update()
        )
        await session.refresh(row)

        if row.state != PendingAmendmentState.PENDING.value:
            raise AmendmentConflictError(
                "draft_not_pending",
                f"amendment `{draft_id}` is `{row.state}` and can no longer be edited. Comment "
                "`@agent-engine replan: <what should change>` to request a fresh one.",
            )

        in_force = await in_force_plan(session, org_id=org_id, flow_id=row.flow_id)
        diff = gate_diff(
            base_document=in_force.plan_document if in_force is not None else None,
            proposed_document=document,
        )

        if row.proposal_hash == document_hash:
            return AmendmentRevision(
                draft_id=row.id,
                flow_id=row.flow_id,
                proposal_hash=row.proposal_hash,
                superseded_draft_id=row.id,
                gate_diff=diff,
                unchanged=True,
            )

        revision = OrchestrationPendingAmendment(
            org_id=org_id,
            flow_id=row.flow_id,
            # The same request: an edit refines the answer to one human ask, it does not
            # create a second assignment. Keeping the request id is also what keeps the
            # `(org, flow, request, hash)` uniqueness meaningful across revisions — two
            # edits converging on the same document reconcile instead of forking.
            request_id=row.request_id,
            author_run_id=f"{_REVISION_MARKER}{row.id}/{revised_by}"[:255],
            base_plan_version=row.base_plan_version,
            base_plan_hash=row.base_plan_hash,
            proposal_document=document,
            proposal_hash=document_hash,
            state=PendingAmendmentState.PENDING.value,
        )

        try:
            async with session.begin_nested():
                session.add(revision)
                await session.flush()
        except IntegrityError:
            found = (
                await session.execute(
                    select(OrchestrationPendingAmendment).where(
                        OrchestrationPendingAmendment.org_id == org_id,
                        OrchestrationPendingAmendment.flow_id == row.flow_id,
                        OrchestrationPendingAmendment.request_id == row.request_id,
                        OrchestrationPendingAmendment.proposal_hash == document_hash,
                    )
                )
            ).scalar_one_or_none()
            if found is None:  # pragma: no cover - the unique index is the only constraint that can fire
                raise
            if found.state != PendingAmendmentState.PENDING.value:
                raise AmendmentConflictError(
                    "draft_not_pending",
                    f"This content already belongs to {found.state} revision `{found.id}`. "
                    "It cannot be reopened by editing. Your current pending revision is unchanged; "
                    "request a fresh replan to propose that content again.",
                )
            revision = found

        # The edited draft is no longer acceptable: an approval of it must not apply the
        # revision. `SUPERSEDED` rather than `REJECTED` — nobody declined this content,
        # it was replaced, and `superseded_by_draft_id` names what replaced it.
        now = utcnow()
        row.state = PendingAmendmentState.SUPERSEDED.value
        row.superseded_by_draft_id = revision.id
        row.decided_at = now
        await session.flush()

    logger.info(
        "amendment_revised flow=%s org=%s draft=%s superseded=%s by=%s gates_added=%s gates_removed=%s",
        row.flow_id,
        org_id,
        revision.id,
        row.id,
        revised_by,
        len(diff.added),
        len(diff.removed),
    )

    return AmendmentRevision(
        draft_id=revision.id,
        flow_id=revision.flow_id,
        proposal_hash=revision.proposal_hash,
        superseded_draft_id=row.id,
        gate_diff=diff,
    )


async def publish_for_acceptance(
    session: AsyncSession,
    *,
    draft_id: str,
    actor: AmendmentContext,
    flow_id: str,
    expected_proposal_hash: str,
    expected_base_plan_version: int,
    expected_base_plan_hash: str,
    publisher=None,
) -> list[PlannedPublication]:
    """Publish a draft's unbound new work, only if this exact acceptance could succeed.

    Called by the human REST accept immediately before `accept_amendment`. It publishes
    nothing unless every identity check `accept_amendment` makes would pass (pending
    draft, exact reviewed revision, exact base version and hash, base still in force)
    and the full impact assessment has no blocker. When any of those fails it returns
    without side effects and `accept_amendment` raises the precise refusal.

    COMMITS the session when it publishes: each binding row is committed before its
    provider call (see `amendment_issue_publisher`). Nothing else has been written yet.
    """
    draft = (
        await session.execute(
            select(OrchestrationPendingAmendment).where(
                OrchestrationPendingAmendment.org_id == actor.org_id,
                OrchestrationPendingAmendment.id == draft_id,
                OrchestrationPendingAmendment.flow_id == flow_id,
            )
        )
    ).scalar_one_or_none()
    if (
        draft is None
        or ActorKind(actor.actor_kind) is not ActorKind.HUMAN
        or draft.state != PendingAmendmentState.PENDING.value
        or (draft.proposal_hash, draft.base_plan_version, draft.base_plan_hash)
        != (expected_proposal_hash, expected_base_plan_version, expected_base_plan_hash)
    ):
        return []
    in_force = await in_force_plan(session, org_id=actor.org_id, flow_id=flow_id)
    if in_force is None or (in_force.version, in_force.plan_hash) != (draft.base_plan_version, draft.base_plan_hash):
        return []
    try:
        proposal = LoopProposal.model_validate(draft.proposal_document)
    except ValueError:
        return []
    impact = await assess_amendment(
        session,
        org_id=actor.org_id,
        flow_id=flow_id,
        proposal=proposal,
        actor_kind=ActorKind.HUMAN,
        actor_id=actor.actor_id,
    )
    if impact.blocking or all(plan.action == "reuse" and plan.parent_repository is None for plan in impact.issue_publications):
        return []
    repo = OrchestrationRepository(session)
    flow = await repo.get_flow(org_id=actor.org_id, flow_id=flow_id)
    existing = {address_of(flow.slug, node) for node in await repo.list_nodes(org_id=actor.org_id, flow_id=flow_id)}

    async def require_current_revision() -> None:
        await session.execute(
            select(OrchestrationFlow).where(OrchestrationFlow.org_id == actor.org_id, OrchestrationFlow.id == flow_id).with_for_update()
        )
        await session.refresh(draft)
        if draft.state != PendingAmendmentState.PENDING.value:
            raise AmendmentConflictError("draft_not_pending", "The amendment changed before issue publication; refresh the draft and preview again.")
        if (draft.proposal_hash, draft.base_plan_version, draft.base_plan_hash) != (
            expected_proposal_hash,
            expected_base_plan_version,
            expected_base_plan_hash,
        ):
            raise AmendmentConflictError("revision_mismatch", "The amendment no longer matches the reviewed revision; preview again.")
        current = await in_force_plan(session, org_id=actor.org_id, flow_id=flow_id)
        if current is None or (current.version, current.plan_hash) != (expected_base_plan_version, expected_base_plan_hash):
            raise AmendmentConflictError("stale_base", "The accepted plan changed before issue publication; request a fresh replan.")

    return await publish_unbound_work(
        session,
        org_id=actor.org_id,
        proposal=proposal,
        existing_addresses=existing,
        permitted=permitted_repositories(in_force.plan_document, proposal),
        publisher=publisher,
        before_claim=require_current_revision,
    )


async def accept_amendment(
    session: AsyncSession,
    *,
    draft_id: str,
    actor: AmendmentContext,
    flow_id: str | None = None,
    expected_proposal_hash: str | None = None,
    expected_base_plan_version: int | None = None,
    expected_base_plan_hash: str | None = None,
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
        expected_proposal_hash: The revision the accepting human actually reviewed
            (#5329). When given, a draft whose document has since been revised is refused
            as a conflict rather than applied. This is what binds an approval to a
            *revision* rather than to an id: without it, an edit between preview and
            accept would apply content nobody read. Optional because the GitHub comment
            path has no way to carry a hash — that path is protected instead by
            `revise_amendment` superseding the draft the human named, so the id they typed
            is no longer acceptable.
        expected_base_plan_version: The plan version the human was told this amendment
            would supersede (#5329), compared against the draft's own recorded base. This
            is not a duplicate of the in-force check below: that one asks "has the plan
            moved since authoring", this one asks "is this the draft I previewed". A
            mistyped id that happens to name a real draft on the same flow passes every
            other check and applies a plan nobody reviewed; this is what catches it.

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
            if expected_proposal_hash is not None and draft.proposal_hash != expected_proposal_hash:
                raise AmendmentConflictError("revision_mismatch", "The retry does not name the revision that was accepted.")
            if expected_base_plan_version is not None and draft.base_plan_version != expected_base_plan_version:
                raise AmendmentConflictError("base_version_mismatch", "The retry does not name the base version that was accepted.")
            if expected_base_plan_hash is not None and draft.base_plan_hash != expected_base_plan_hash:
                raise AmendmentConflictError("base_hash_mismatch", "The retry does not name the base hash that was accepted.")
            from .amend import _saved_receipt_dispositions

            preserved, dropped, limits_before, limits_after = await _saved_receipt_dispositions(session, draft.accepted_by_decision_id)
            return AmendmentAcceptResult(
                draft_id=draft.id,
                flow_id=draft.flow_id,
                plan_version=draft.accepted_plan_version or 0,
                superseded_version=draft.base_plan_version,
                decision_id=draft.accepted_by_decision_id or "",
                gate_diff=GateDiff(),
                replayed=True,
                receipts_preserved=preserved,
                receipts_dropped=dropped,
                effective_limits_before=limits_before,
                effective_limits_after=limits_after,
            )

        if draft.state != PendingAmendmentState.PENDING.value:
            # Rejected or superseded. Terminal, and not re-openable: a superseded draft's base
            # describes a plan that is no longer in force, so "accept it anyway" is the
            # discard-someone-else's-amendment failure by another route.
            #
            # A draft superseded by an *edit* (#5329) lands here too, and that is exactly
            # the intended effect: the human who read the pre-edit revision and typed its
            # id is refused rather than silently applying the revision that replaced it.
            #
            # The two supersede causes need different advice, so they are distinguished
            # rather than given one message. An edited draft has a live successor to read,
            # and telling that human to replan would throw away an edit that is sitting
            # there pending. A draft displaced by someone else's *acceptance* has no such
            # successor — that plan is already in force — and pointing at it would send the
            # human to "review" something already applied. Only replan resolves that one.
            successor = await _revision_successor(session, org_id=actor.org_id, draft=draft)
            raise AmendmentConflictError(
                "draft_not_pending",
                f"amendment `{draft_id}` is `{draft.state}` and can no longer be accepted."
                + (
                    f" It was replaced by revision `{successor}`; review that one and accept it instead."
                    if successor is not None
                    else " Comment `@agent-engine replan: <what should change>` to request a fresh one."
                ),
            )

        # --- The reviewed revision, bound exactly (#5329) -----------------------------
        # Checked before the in-force comparison because it is the more specific question:
        # "this is not the document you reviewed" is actionable in a way that "the plan
        # moved" is not, and reporting the latter for an edited draft would send the human
        # to request a replan when all they need is to re-read the current revision.
        if expected_proposal_hash is not None and draft.proposal_hash != expected_proposal_hash:
            logger.warning(
                "amendment_revision_mismatch flow=%s org=%s draft=%s reviewed=%s current=%s",
                draft.flow_id,
                actor.org_id,
                draft.id,
                expected_proposal_hash[:12],
                draft.proposal_hash[:12],
            )
            raise AmendmentConflictError(
                "revision_mismatch",
                f"amendment `{draft_id}` is now revision `{draft.proposal_hash[:12]}` but the approval names "
                f"`{expected_proposal_hash[:12]}`. It was edited after you reviewed it. Preview the current "
                "revision and accept that one — an approval never carries over to a revision it did not see.",
            )

        if expected_base_plan_version is not None and draft.base_plan_version != expected_base_plan_version:
            raise AmendmentConflictError(
                "base_version_mismatch",
                f"amendment `{draft_id}` was authored against plan version "
                f"{draft.base_plan_version if draft.base_plan_version is not None else 'none'}, but the approval "
                f"names version {expected_base_plan_version}. Nothing was applied; confirm which amendment you "
                "meant to accept.",
            )

        if expected_base_plan_hash is not None and draft.base_plan_hash != expected_base_plan_hash:
            raise AmendmentConflictError(
                "base_hash_mismatch",
                f"amendment `{draft_id}` was authored against plan hash "
                f"`{(draft.base_plan_hash or 'none')[:12]}`, but the approval names "
                f"`{expected_base_plan_hash[:12]}`. Nothing was applied; preview the draft again and approve "
                "the exact base revision shown there.",
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

        # The same assessment the preview showed, now under the flow lock: a preview
        # saying "acceptable" and an accept that applies something else cannot differ.
        # `require_published` turns any new work still without a recorded issue into a
        # blocker; the REST accept publishes it just before this call.
        impact = await assess_amendment(
            session,
            org_id=actor.org_id,
            flow_id=draft.flow_id,
            proposal=proposal,
            actor_kind=ActorKind(actor.actor_kind),
            actor_id=actor.actor_id,
            require_published=True,
        )
        if impact.blocking:
            raise ProposalRejectedError(
                f"amendment acceptance is blocked by {len(impact.blocking)} diagnostic(s): "
                + "; ".join(f"{item.rule}: {item.message}" for item in impact.blocking),
                violations=list(impact.blocking),
            )
        # Hash semantics: the human approved `draft.proposal_hash`, the stored document
        # WITHOUT the issue refs of work published at acceptance. Those refs come only
        # from the durable binding rows keyed by (tenant, repository, proposal identity)
        # for exactly the nodes the preview listed, so the applied document is a
        # deterministic function of the approved one. The accepted plan's own hash
        # covers the bound refs.
        proposal = apply_publications(proposal, impact.issue_publications)
        existing_addresses = {
            node["address"]
            for node in (in_force.plan_document.get("nodes", []) if in_force is not None else [])
            if isinstance(node, dict) and isinstance(node.get("address"), str)
        }
        issue_violations = new_work_issue_violations(existing_addresses=existing_addresses, proposal=proposal)
        if issue_violations:
            raise ProposalRejectedError(
                "new amendment work has no durable issue binding",
                violations=issue_violations,
            )
        claim_violations = await assignment_conflict_violations(
            session,
            org_id=actor.org_id,
            flow_id=draft.flow_id,
            proposal=proposal,
            existing_addresses=existing_addresses,
        )
        if claim_violations:
            raise ProposalRejectedError(
                "new amendment work conflicts with an active assignment",
                violations=claim_violations,
            )

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
        receipts_preserved=result.receipts_preserved,
        receipts_dropped=result.receipts_dropped,
        effective_limits_before=result.effective_limits_before,
        effective_limits_after=result.effective_limits_after,
    )
