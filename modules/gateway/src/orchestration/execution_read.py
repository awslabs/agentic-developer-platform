"""Operator-plane read model over the execution/action delivery ledger.

Issue #5145 (ENGINE-K4, parent #5122). The ledger itself — the tables, the writes
and the authority fence — belongs to `execution_store.py` (#5142). This module is
the *read* side: it answers "where is delivery, and who has to act next?" for one
flow, for an operator looking at the Delivery Flows screen.

## Why this does not call `execution_store.load_execution`

That would be the obvious reuse, and it is wrong here. Every store entry point
takes an `ExecutionIdentity` carrying the `claim_id` and `claim_generation` of the
work claim the caller holds (#5127). A human operator holds no claim, and a read
presenting one it does not own lands on `_binding_conflict`'s `claim_mismatch`
arm — which deliberately **withholds the record**, because returning it would
disclose the `claim_id`/`claim_generation` binding that
satisfies the next authority check. A refusal must not hand over what would
satisfy it.

So the answer is not to fabricate an identity and not to loosen the store. It is
to scope the read the way the operator plane already scopes every other read: by
the caller's own authenticated `org_id`, resolved through the flow. That is the
access path the merged migration already provisioned — the
`ix_orchestration_executions_flow_id` index on `(org_id, flow_id)`, whose comment
in `models.py` names it the operator/read-model path. The store's claim fence
stays exactly as it is, and nothing here can write.

## Truthfulness is the whole feature

The failure this read exists to remove is a screen that says "running" while an
agent has been stopped for a day waiting on a human. Several of the ways to get
that wrong look like reasonable code, so they are named here and asserted in
`tests/orchestration/test_execution_read.py`:

- **Blocked is not failed.** `ExecutionStatus.BLOCKED` is its own status meaning
  "someone must supply something". Mapping it onto an error would send an operator
  hunting a crash that never happened, and mapping it onto progress would hide the
  person who has to act. It is projected as itself, with its typed `BlockCode`,
  the owner, the required input and any outstanding gates.
- **`progressed_at` is not reset when a row blocks**, by the store's deliberate
  choice, and this module does not paper over that. It is the clock that
  distinguishes "stuck for a minute" from "stuck since Tuesday", so it is reported
  as stored alongside `server_time` — the client computes an age from two explicit
  instants rather than from its own clock, which may be wrong.
- **An unobserved action stays unknown.** `ActionStatus.UNKNOWN` means an observer
  looked and could not tell. It is neither success nor failure and is projected as
  neither; `resolved` on the record already encodes that distinction and is
  surfaced rather than recomputed.
- **A missing receipt is pending, not absent-therefore-fine.** An action with no
  `receipt_ref` is reported with a null reference so the client can say "pending"
  instead of silently rendering nothing.
- **No row at all means no durable record, not success.** A flow that ran before
  the ledger existed has no executions. The route answers 200 with an empty list
  and `legacy=True`, never a 404 (the flow does exist) and never an implied
  completion.

## Bounded by construction

`load_flow_execution_view` takes a `limit`/`offset` and fetches actions for the
returned executions only, in ONE grouped query. The alternative — every historical
action per node — grows without bound on a long-running flow and is explicitly
out of scope for this issue. Per execution the action list is capped at
`MAX_ACTIONS_PER_EXECUTION` most-recent rows, with `action_overflow` telling the
truth when older ones were withheld rather than pretending the list is complete.

That cap is enforced **in the database, per execution**, by a `row_number()`
partitioned on `execution_id` — not by slicing in Python after the fact, which
would leave the *fetch* unbounded while the *response* looked bounded, and not by
a global `LIMIT`, which would let one busy execution starve a quiet sibling to
zero rows while reporting `action_overflow=False` about it. See the comment at the
query itself; both wrong shapes pass a naive single-execution regression, so the
test for this is deliberately asymmetric.

## References only, validated on the way out

`artifact_ref`/`receipt_ref` hold provider or storage identifiers. `_safe_ref`
admits only reference-shaped values and drops anything else, so a column that was
somehow written with prose, a credential-looking blob or a `javascript:` payload
cannot reach an operator's browser as a link. Dropping is the fail-closed
direction: a withheld reference shows as pending, while a rendered hostile one has
no revocation path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.shared.logging import get_logger

from .deployment_controller import bounded_deployment_summary
from .deployment_workflows import bounded_workflow_summary
from .execution_state import (
    ActionStatus,
    BlockCode,
    ExecutionPhase,
    ExecutionStatus,
)

# `_decode_gates` is imported from the store rather than reimplemented. It is
# private to that module, and reaching for it is deliberate: it decodes a storage
# format (`block_remaining_gates` as JSON text) that the store owns, and a second
# decoder here would be free to disagree with the writer about what a stored value
# means — including about a malformed value, which the store answers with "no
# gates" rather than an exception so a cosmetic storage problem cannot make a
# record unreadable to the operator diagnosing it.
from .execution_store import _decode_gates
from .merge_controller import bounded_receipt_summary
from .merge_evidence import bounded_merge_summary
from .models import OrchestrationAction, OrchestrationExecution

logger = get_logger(__name__)

__all__ = [
    "MAX_ACTIONS_PER_EXECUTION",
    "MAX_EXECUTIONS_PER_PAGE",
    "ActionView",
    "BlockView",
    "ExecutionView",
    "FlowExecutionView",
    "load_flow_execution_view",
]

# The page bound for executions. A flow's execution count grows with nodes ×
# repair cycles, so an unbounded read is a query whose cost is set by the oldest
# flow in the tenant. 200 covers every real flow in one page while keeping the
# ceiling explicit; the route exposes it as a 422 bound rather than clamping
# silently, so a caller cannot believe it received everything.
MAX_EXECUTIONS_PER_PAGE = 200

# Per-execution action cap. Actions accumulate per attempt, and the read model
# needs the *recent* ones (what is pending, what was last observed) rather than
# the full history — which is what "never fetch every historical action per node"
# forbids. When rows are withheld, `action_overflow` says so.
MAX_ACTIONS_PER_EXECUTION = 20

# What a reference may look like on the way out. Deliberately conservative: an S3
# key, a provider node id, a receipt id, a `kind/id` pair — no whitespace and
# nothing that could become an executable URL. Anything else is dropped.
#
# `:` is inside the character class because real references use it
# (`pr:node-7:cycle-1`), which is also why `_SCHEME_PATTERN` below exists: a
# colon is only dangerous in the *leading* position where a browser reads it as a
# URL scheme.
# Match the ledger's 512-character columns. Content-addressed review receipts
# include tenant/run hashes and an integrity fragment, which can exceed 256.
_REF_PATTERN = re.compile(r"^(?:s3://)?[A-Za-z0-9][A-Za-z0-9._:/#@=-]{0,511}$")

# A leading URL scheme, which `_REF_PATTERN` alone would admit: `javascript:alert`
# is made entirely of characters a legitimate reference also uses, so the pattern
# cannot tell it apart. Matched separately and refused unless it is the documented
# `s3://` form, because this is the one shape that turns a reference rendered as a
# link into script execution in an operator's browser.
_SCHEME_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_ALLOWED_SCHEMES = frozenset({"s3"})

# A reference long enough to be a transcript or a blob is refused outright,
# before the pattern, so the pattern never has to reason about length.
_MAX_REF_LENGTH = 512


def _safe_ref(raw: str | None) -> str | None:
    """Admit a reference-shaped value, or None.

    Fail-closed on purpose. These columns are documented to hold references only,
    so a value that is not reference-shaped means either a writer bug or something
    hostile, and in both cases the honest render is "nothing here" rather than a
    link an operator might click. A dropped reference degrades to `pending` in the
    UI, which is recoverable; a rendered `javascript:` or credential-bearing string
    is not.
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value or len(value) > _MAX_REF_LENGTH:
        return None
    if not _REF_PATTERN.fullmatch(value):
        logger.warning("execution read: dropping a reference that is not reference-shaped (length %s)", len(value))
        return None
    # Checked after the shape, because a value that already failed the pattern needs
    # no scheme reasoning. `_SCHEME_PATTERN` only matches a colon reached without
    # crossing a `/`, so a path-like reference that contains a colon later
    # (`pr/PR_x:1`) is unaffected — only a *leading* scheme is considered.
    #
    # Known cost, accepted deliberately: this also refuses a hypothetical
    # colon-prefixed reference such as `pr:PR_kwDO123`. That form appears nowhere in
    # the stored vocabulary (the documented and fixture shapes are `s3://…`,
    # `pr/PR_kwDO…`, `issue-comment/5142#c9`, `ses/0190a7…` — the colon-delimited
    # form is used for `operation_key`, which is a key rather than a reference and is
    # not passed through here). An allowlist that fails closed on an unknown scheme
    # is the right direction when the alternative is a blocklist that fails OPEN on
    # whatever scheme it has not heard of.
    scheme = _SCHEME_PATTERN.match(value)
    if scheme is not None and scheme.group(1).lower() not in _ALLOWED_SCHEMES:
        logger.warning("execution read: dropping a reference with a disallowed URL scheme %r", scheme.group(1).lower())
        return None
    return value


def _as_aware(moment: datetime | None) -> datetime | None:
    """Normalize a stored timestamp to timezone-aware UTC.

    Same reasoning as `execution_store._as_aware`: the columns are
    `DateTime(timezone=True)` and PostgreSQL returns aware values, but SQLite drops
    the offset. Every writer is UTC, so a naive read-back is a UTC value that lost
    its label in transit. Normalized here because this module serializes these
    instants next to `server_time`, and a naive one would serialize without an
    offset — leaving the client to guess a zone on the exact fields an operator
    uses to judge how long something has been stuck.
    """
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


@dataclass(frozen=True)
class BlockView:
    """Why delivery stopped, who clears it, and what they must supply.

    Every field is present because an operator reading only this must be able to
    act. `code` is the stable machine-readable `BlockCode`; `owner` and
    `required_input` are what turn it from a status into a next step.

    `progressed_at` is the last real progress — NOT the moment of blocking, which
    the store deliberately does not record here. Carried on the block so "stuck
    for a minute" and "stuck since Tuesday" are distinguishable without
    reconstructing a timeline.

    `remaining_gates` is informational. The gates live in the existing
    `controls.py`/graph state and nothing in this read path approves or bypasses
    one.
    """

    code: BlockCode
    owner: str
    required_input: str
    remaining_gates: tuple[str, ...]
    progressed_at: datetime | None
    detail: str | None


@dataclass(frozen=True)
class ActionView:
    """One externally-visible step, as an operator sees it.

    `resolved` is carried explicitly rather than left for a client to derive from
    `status`, because the derivation has a trap: `UNKNOWN` is a *settled record of
    an unsettled fact*, so a client testing `status != PREPARED` would treat an
    outcome nobody observed as resolved evidence. The record already knows the
    answer, so it is reported rather than recomputed downstream.

    `receipt_ref` null means the provider's own identifier is not (yet) recorded —
    pending, not "nothing happened". A reference that failed `_safe_ref` also
    arrives as null, which is the fail-closed direction.
    """

    id: str
    operation_key: str
    kind: str
    status: ActionStatus
    attempt: int
    resolved: bool
    artifact_ref: str | None
    receipt_ref: str | None
    created_at: datetime | None
    observed_at: datetime | None
    evidence_summary: dict | None = None


@dataclass(frozen=True)
class ExecutionView:
    """One node's delivery cycle, projected for display.

    Keyed by `node_id` + `cycle`, which is the ledger's own identity: a repair
    cycle is separate work with its own attempts and actions, so collapsing cycles
    would present a retry as the original attempt.

    `revision` is included because the client needs it to reject a stale poll
    response. It advances by exactly one per applied write, so "is this response
    older than what I already show?" is a comparison rather than a guess about
    arrival order.

    **Deliberately absent: the whole authority binding** — `claim_id`,
    `claim_generation` and `accepted_plan_version`.

    The first two are what the store's fence tests, and publishing them would put
    the values that satisfy the next authority check into a browser payload.

    `accepted_plan_version` is excluded for a different and initially
    counter-intuitive reason, which `test_internal_plane_guard.py` caught in an
    earlier draft of this module that did serve it. It is an *acceptance record*: it
    names which approved plan authorized this delivery. That router's guard requires
    `PLAN_APPROVE` of any handler touching acceptance records, on the rule that
    reading "what was approved" under a spend-read permission is an escalation. Both
    ways out were wrong — relaxing the guard to admit this route, or promoting the
    route to `PLAN_APPROVE` so that merely *viewing delivery progress* would demand
    approval authority. So the field goes. Nothing in this feature needs it: an
    operator asking "why is delivery waiting and who acts next" is answered by the
    phase, the block and the next check, and the plan a delivery was authorized
    under is already available on the plans route, under the permission that
    governs it.
    """

    id: str
    node_id: str
    cycle: int
    phase: ExecutionPhase
    status: ExecutionStatus
    revision: int
    attempts: int
    next_check_at: datetime | None
    deadline_at: datetime | None
    progressed_at: datetime | None
    progress_note: str | None
    block: BlockView | None
    pending_action_key: str | None
    notification_receipt_ref: str | None
    handoff_receipt_ref: str | None
    created_at: datetime | None
    updated_at: datetime | None
    actions: tuple[ActionView, ...]
    # True when older actions exist that this page withheld. Reported rather than
    # silently truncated: a capped list presented as complete would let an operator
    # conclude a step never happened.
    action_overflow: bool


@dataclass(frozen=True)
class FlowExecutionView:
    """Every execution this page covers for one flow, plus the honest framing.

    `server_time` is stamped once for the whole response and is what makes every
    other instant interpretable. A client computing "stuck for 3 hours" against its
    own clock is computing against a clock that may be wrong or in another zone;
    against this field it is subtracting two values from the same source.

    `legacy` is True when the flow has no execution rows at all. That is a real and
    permanent state — every flow delivered before the ledger existed has none — and
    it means *no durable execution record*, which is emphatically not success. The
    route still answers 200: the flow exists, and a 404 would say otherwise.

    `total` is the count of executions for the flow, so a client showing a page can
    say how many it is not showing.
    """

    flow_id: str
    server_time: datetime
    executions: tuple[ExecutionView, ...]
    total: int
    limit: int
    offset: int
    legacy: bool


def _block_view(row: OrchestrationExecution) -> BlockView | None:
    """Rebuild the block from its columns, or None when the row is not blocked.

    Keyed on `block_code` alone, matching `execution_store._block_from_row`: it is
    the one column a block cannot lack, so a row with a code and (through some
    earlier bug) no owner still reports as blocked rather than reading as runnable.

    An unrecognised code is reported as `AUTHORITY_UNVERIFIABLE` rather than
    dropped, again matching the store. A newer writer's block member must not read
    as "not blocked" to an older pod — that would show work as proceeding that was
    deliberately stopped.
    """
    if not row.block_code:
        return None
    try:
        code = BlockCode(row.block_code)
    except ValueError:
        logger.warning("execution read: unrecognised block code %r; reporting as authority-unverifiable", row.block_code)
        code = BlockCode.AUTHORITY_UNVERIFIABLE
    return BlockView(
        code=code,
        owner=row.block_owner or "unknown",
        required_input=row.block_required_input or "unspecified",
        remaining_gates=_decode_gates(row.block_remaining_gates),
        progressed_at=_as_aware(row.progressed_at),
        detail=row.block_detail,
    )


def _action_view(row: OrchestrationAction) -> ActionView | None:
    """Project one action, or None when its status is not in this build's vocabulary.

    An unrecognised status is dropped from the view rather than raised on, and that
    choice is specific to the read path: the store raises `ExecutionStoreError`
    because a writer must fail closed on a value it cannot reason about, while an
    operator asking "where is delivery?" must not get a 500 for the whole flow
    because one action row came from a newer build. The execution's own phase and
    status — the fields the operator is actually looking at — are unaffected.
    """
    try:
        status = ActionStatus(row.status)
    except ValueError:
        logger.warning("execution read: dropping action %s with unrecognised status %r", row.id, row.status)
        return None
    return ActionView(
        id=row.id,
        operation_key=row.operation_key,
        kind=row.kind,
        status=status,
        attempt=row.attempt,
        # From the enum set the record itself defines, so "unknown is not resolved"
        # cannot drift between here and `ActionRecord.resolved`.
        resolved=status not in (ActionStatus.PREPARED, ActionStatus.DISPATCHED, ActionStatus.UNKNOWN),
        artifact_ref=_safe_ref(row.artifact_ref),
        receipt_ref=_safe_ref(row.receipt_ref),
        created_at=_as_aware(row.created_at),
        observed_at=_as_aware(row.observed_at),
        evidence_summary=(
            bounded_merge_summary(row.detail)
            if row.kind == "merge_eligibility"
            else bounded_receipt_summary(row.detail)
            if row.kind == "merge_pull_request"
            else bounded_deployment_summary(row.detail)
            if row.kind == "deployment_verification"
            else bounded_workflow_summary(row.detail)
            if row.kind == "deployment_workflow"
            else None
        ),
    )


def _execution_view(row: OrchestrationExecution, actions: list[OrchestrationAction]) -> ExecutionView | None:
    """Project one execution with its actions, or None if its vocabulary is unknown.

    Dropped rather than raised for the same reason `_action_view` drops: one row
    written by a newer build must not make the whole flow unreadable to an operator
    diagnosing a different node. The drop is logged, and `total` still counts the
    row, so the view reports fewer executions than it counted rather than silently
    renumbering.
    """
    try:
        phase = ExecutionPhase(row.phase)
        status = ExecutionStatus(row.status)
    except ValueError:
        logger.warning(
            "execution read: dropping execution %s with unrecognised phase/status (%r/%r)",
            row.id,
            row.phase,
            row.status,
        )
        return None

    # Newest first: the recent actions are the ones that answer "what is pending".
    ordered = sorted(actions, key=lambda action: (action.created_at or datetime.min.replace(tzinfo=UTC)), reverse=True)
    capped = ordered[:MAX_ACTIONS_PER_EXECUTION]
    views = tuple(view for view in (_action_view(action) for action in capped) if view is not None)

    return ExecutionView(
        id=row.id,
        node_id=row.node_id,
        cycle=row.cycle,
        phase=phase,
        status=status,
        revision=row.revision,
        attempts=row.attempts,
        next_check_at=_as_aware(row.next_check_at),
        deadline_at=_as_aware(row.deadline_at),
        progressed_at=_as_aware(row.progressed_at),
        progress_note=row.progress_note,
        block=_block_view(row),
        pending_action_key=row.pending_action_key,
        notification_receipt_ref=_safe_ref(row.notification_receipt_ref),
        handoff_receipt_ref=_safe_ref(row.handoff_receipt_ref),
        created_at=_as_aware(row.created_at),
        updated_at=_as_aware(row.updated_at),
        actions=views,
        action_overflow=len(ordered) > MAX_ACTIONS_PER_EXECUTION,
    )


async def load_flow_execution_view(
    session: AsyncSession,
    *,
    org_id: str,
    flow_id: str,
    limit: int = MAX_EXECUTIONS_PER_PAGE,
    offset: int = 0,
) -> FlowExecutionView:
    """Read one flow's execution ledger for display, tenant-scoped and bounded.

    The caller MUST have resolved `flow_id` under `org_id` already (the route does,
    and answers 404 when it does not resolve). `org_id` is nonetheless in every
    predicate here rather than trusted from that resolution: it is what makes a
    cross-tenant id unresolvable rather than merely unlikely, and it matches the
    leading column of `ix_orchestration_executions_flow_id` so the filter is also
    the access path.

    Read-only. No lock is taken — `for_update` on live work would make an operator
    opening a screen contend with the runner that is trying to make progress — and
    nothing here writes.

    Three queries, never per-node: one count, one page of executions, one grouped
    fetch of actions for exactly the executions on that page. That is the bound the
    issue requires; a per-execution action query would be N+1 in node count and
    would fetch history nobody asked for.
    """
    now = datetime.now(UTC)
    bounded_limit = max(1, min(int(limit), MAX_EXECUTIONS_PER_PAGE))
    bounded_offset = max(0, int(offset))

    # Counted separately from the page so a client can tell how much it is not
    # showing. `func.count` over the same predicate uses the same index.
    total = (
        await session.execute(
            select(func.count())
            .select_from(OrchestrationExecution)
            .where(
                OrchestrationExecution.org_id == org_id,
                OrchestrationExecution.flow_id == flow_id,
            )
        )
    ).scalar_one()

    # Ordered by node then cycle so a node's repair cycles read in sequence and the
    # page boundary is stable across polls — an unordered page can return a row
    # twice and omit another as rows are written underneath it.
    rows = (
        (
            await session.execute(
                select(OrchestrationExecution)
                .where(
                    OrchestrationExecution.org_id == org_id,
                    OrchestrationExecution.flow_id == flow_id,
                )
                .order_by(
                    OrchestrationExecution.node_id,
                    OrchestrationExecution.cycle,
                )
                .limit(bounded_limit)
                .offset(bounded_offset)
            )
        )
        .scalars()
        .all()
    )

    actions_by_execution: dict[str, list[OrchestrationAction]] = {}
    if rows:
        # ONE query for the whole page's actions, scoped to this tenant AND to the
        # execution ids just read. Both predicates: `execution_id IN (...)` alone
        # would be correct only because the ids came from an org-scoped read, and a
        # filter that depends on an earlier query for its safety is one refactor away
        # from being wrong.
        #
        # ## Why a window function and not a `LIMIT`
        #
        # The bound has to be PER EXECUTION, and a plain `LIMIT` cannot express that.
        # `LIMIT (cap + 1) * len(ids)` looks like it implements the same cap and does
        # something quite different: one busy execution consumes the whole budget and a
        # quiet sibling comes back with ZERO rows. That does not degrade gracefully —
        # an empty action list is indistinguishable from "this execution has no
        # actions", and `action_overflow` would be False while saying it, so the view
        # would state positively that nothing happened on an execution that has
        # actions. Slow-but-truthful is a better failure than fast-and-wrong, so the
        # per-partition rank is the only acceptable shape here.
        #
        # `row_number()` over a partition of `execution_id` ranks each execution's own
        # actions independently, so `rn <= cap + 1` bounds every execution separately in
        # one query — the three-query invariant holds. Available on PostgreSQL and on
        # SQLite >= 3.25 (the test runner has far newer), so this is not a
        # dialect-specific path.
        #
        # `cap + 1` rather than `cap`: the extra row is exactly the evidence that older
        # rows exist. `_execution_view` slices back to `cap`, which makes
        # `len(ordered) > cap` a truthful overflow signal rather than a guess.
        ranked = (
            select(
                OrchestrationAction,
                func.row_number()
                .over(
                    partition_by=OrchestrationAction.execution_id,
                    # Newest first, matching `_execution_view`'s own ordering so the
                    # rows that survive the cap are the rows the view would have kept.
                    #
                    # The `id` tie-breaker is load-bearing, not decoration, and its
                    # direction matters: `created_at` defaults to `utcnow` and actions
                    # prepared together within one execution can share a timestamp to
                    # the microsecond. With a ties-ambiguous ordering, WHICH action
                    # lands at rank `cap + 1` is chosen by the planner, so
                    # `action_overflow` would flicker between runs on identical data —
                    # a test that passes locally and fails in CI for no visible reason.
                    # Kept DESC alongside `created_at DESC` so the rank order and the
                    # view's sort agree rather than merely both being stable.
                    order_by=(
                        OrchestrationAction.created_at.desc(),
                        OrchestrationAction.id.desc(),
                    ),
                )
                .label("rn"),
            )
            .where(
                OrchestrationAction.org_id == org_id,
                OrchestrationAction.execution_id.in_([row.id for row in rows]),
            )
            .subquery()
        )
        # Note for whoever next profiles this: the ordering is deliberately NOT
        # index-backed. `ix_orchestration_actions_execution_status` is
        # `(org_id, execution_id, status)`, which serves the `IN` but not the
        # `created_at` sort. At `cap + 1` rows per execution over a bounded page that
        # is a small sort, and adding an index is a migration — a larger change than
        # this read. Considered and declined, not overlooked.
        action_entity = aliased(OrchestrationAction, ranked)
        action_rows = (await session.execute(select(action_entity).where(ranked.c.rn <= MAX_ACTIONS_PER_EXECUTION + 1))).scalars().all()
        for action in action_rows:
            actions_by_execution.setdefault(action.execution_id, []).append(action)

    views = tuple(view for view in (_execution_view(row, actions_by_execution.get(row.id, [])) for row in rows) if view is not None)

    return FlowExecutionView(
        flow_id=flow_id,
        server_time=now,
        executions=views,
        total=total,
        limit=bounded_limit,
        offset=bounded_offset,
        # Keyed on the flow's TOTAL, not on this page being empty: page 3 of a
        # 2-page result is empty without the flow being legacy, and calling that
        # "no durable record" would be a false claim about a flow that has one.
        legacy=total == 0,
    )
