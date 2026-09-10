"""Tenant-scoped data access for the orchestration graph store.

Issue #4196. Every method takes `org_id` as a keyword-only argument and filters
on it; there is no method that reads or writes across tenants, and no method that
updates an `orchestration_decisions` row.

The absent decisions-update method is the point, not an omission: combined with
the `before_update` hook in `models.py`, an attempt to rewrite gate attribution
raises rather than succeeding.

Shape follows `shared/services/credential_resolver.py` — constructor-injected
`AsyncSession`, keyword-only arguments, SQLAlchemy 2.0 `select()` +
`session.execute()`. Reads and writes flush but do not commit: the caller owns
the transaction, so a decision and the plan version it accepted can land
atomically.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import utcnow

from .cost import escape_like
from .display_state import DISPLAY_TO_ENGINE, DisplayState, FlowStatus, derive_flow_status
from .models import (
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationFlow,
    OrchestrationNode,
)

# The sorts `GET /orchestration/flows` accepts. `cost` is deliberately absent:
# cost lives in `usage_logs`, joinable only by address prefix, so it cannot
# participate in the sorted+paginated statement — sorting by it would rank the
# fetched page while appearing to rank every row.
FLOW_SORTS: tuple[str, ...] = ("created", "updated", "stalled")


@dataclass(frozen=True)
class FlowDisplayCounts:
    """Node counts per display state for one flow or one wave.

    `stalled` is **not** simply the count of `failed`/`halted`/`rejected_at_gate`
    nodes at flow level — see `FlowAggregate.stalled_count`. At wave level it is,
    because the decision-derived count is aggregated per flow, not per wave.
    """

    queued: int = 0
    in_progress: int = 0
    gate: int = 0
    stalled: int = 0
    complete: int = 0

    @property
    def total(self) -> int:
        """Total live nodes. The sum of the five buckets — `superseded` is in none
        of them, so there is no subtraction step to forget."""
        return self.queued + self.in_progress + self.gate + self.stalled + self.complete


@dataclass(frozen=True)
class WaveAggregate:
    """One `(epic_ref, wave_ref)` group, in first-appearance order."""

    flow_id: str
    epic_ref: str
    wave_ref: str
    display_counts: FlowDisplayCounts

    @property
    def total(self) -> int:
        return self.display_counts.total

    @property
    def done(self) -> int:
        return self.display_counts.complete

    @property
    def finished(self) -> bool:
        """Whether this wave has no unfinished work left.

        A wave of zero live nodes counts as finished: it has nothing left to do.
        `current_wave_ref` is the first wave for which this is False.
        """
        return self.total == self.done


@dataclass(frozen=True)
class FlowAggregate:
    """One row of the flows list: the flow plus everything derived about it."""

    flow: OrchestrationFlow
    display_counts: FlowDisplayCounts
    # Nodes whose most recent stall-or-halt decision was a *stall*. Distinct from
    # `display_counts.stalled`, which is state-derived and also covers plain
    # failures, halts and gate rejections.
    stalled_count: int
    status: FlowStatus
    waves: tuple[WaveAggregate, ...] = field(default_factory=tuple)

    @property
    def awaiting_gate_count(self) -> int:
        return self.display_counts.gate

    @property
    def needs_me(self) -> bool:
        """Whether this flow is waiting on a human.

        Deliberately not `status in (attention_needed, awaiting_you)`: `status` is
        first-match-wins, so a flow that is both stalled and gated reports only
        `attention_needed` while satisfying this predicate on either ground.
        """
        return self.display_counts.gate > 0 or self.stalled_count > 0

    @property
    def epic_count(self) -> int:
        return len({wave.epic_ref for wave in self.waves})

    @property
    def current_wave_ref(self) -> str | None:
        """The first wave, in first-appearance order, with unfinished work.

        None when every wave is finished (or there are no waves) — a flow with
        nothing in flight has no current wave, and naming its last one would read
        as "this is where the work is".
        """
        for wave in self.waves:
            if not wave.finished:
                return wave.wave_ref
        return None


@dataclass(frozen=True)
class FlowPage:
    """One page of flows plus the honest count of everything the filters matched."""

    flows: tuple[FlowAggregate, ...]
    # Rows matching the filters across ALL pages, from `COUNT(*) OVER ()` in the
    # same statement — not the length of `flows`.
    total: int


class OrchestrationRepository:
    """Tenant-scoped reads and appends over the orchestration graph tables."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- flows --------------------------------------------------------------

    async def create_flow(self, *, org_id: str, slug: str, title: str, intent_ref: str | None = None) -> OrchestrationFlow:
        flow = OrchestrationFlow(org_id=org_id, slug=slug, title=title, intent_ref=intent_ref)
        self._session.add(flow)
        await self._session.flush()
        return flow

    async def get_flow(self, *, org_id: str, flow_id: str) -> OrchestrationFlow | None:
        stmt = select(OrchestrationFlow).where(
            OrchestrationFlow.org_id == org_id,
            OrchestrationFlow.id == flow_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_flows(self, *, org_id: str) -> list[OrchestrationFlow]:
        """EVERY flow in the tenant, unbounded. Do not add a limit here.

        ⚠️ This method must keep taking `org_id` and nothing else — not even a
        defaulted `limit`. Both production callers depend on seeing the whole set:

          - `compile._resolve_flow` scans it for a matching slug and creates a new
            flow when it finds none. Bound the list and a compile whose flow is
            older than one page silently creates a **duplicate flow under the same
            slug** — and since `orchestration_flows` has no `uq(org_id, slug)`,
            nothing rejects it. Both flows' costs then share an address prefix and
            become indistinguishable.
          - `registration` resolves the flow the same way before raising
            `DraftFlowConflictError`; a miss disables the in-force-plan conflict
            guard, so a second plan lands over one already accepted.

        Paginated reads belong to `list_flows_page_with_aggregates` below, which is
        a separate method for exactly this reason.
        `tests/orchestration/test_list_flows.py` pins all three behaviours with 30
        flows — more than one page — so a limit added here fails CI rather than
        corrupting a tenant's graph.
        """
        stmt = select(OrchestrationFlow).where(OrchestrationFlow.org_id == org_id).order_by(OrchestrationFlow.created_at.desc())
        return list((await self._session.execute(stmt)).scalars().all())

    # -- flows list page (derived aggregates) ---------------------------------

    def _node_agg(self, *, org_id: str):
        """Per-flow node counts, one row per flow, five `COUNT(*) FILTER` buckets.

        The bucket state lists come from `DISPLAY_TO_ENGINE`, which is the
        inversion of the single 9→5 mapping — they are never written out here. Two
        consequences worth being explicit about:

        - Adding a tenth engine state cannot leave it silently uncounted.
        - `superseded` maps to no display state, so it appears in no bucket's list
          and matches no `FILTER`. It is excluded **structurally**; there is no
          `state != 'superseded'` predicate to accidentally drop, and `total_nodes`
          is just the sum of the five buckets.

        `COUNT(*) FILTER (WHERE …)` is portable to both SQLite 3.40.1 and
        PostgreSQL; `.filter()` in SQLAlchemy Core emits exactly that.
        """
        columns = [func.count().filter(OrchestrationNode.state.in_(DISPLAY_TO_ENGINE[display])).label(display.value) for display in DisplayState]
        return (
            select(OrchestrationNode.flow_id.label("flow_id"), *columns)
            .where(OrchestrationNode.org_id == org_id)
            .group_by(OrchestrationNode.flow_id)
            .subquery()
        )

    def _stall_agg(self, *, org_id: str):
        """Per-flow count of nodes whose LATEST stall-or-halt decision was a stall.

        Stall detection moves the node to `failed` and records a `node_stalled`
        decision beside it (`stall.py`), so "stalled" is not readable from `state`
        — a stall and an ordinary failure are the same state.

        Latest-wins via `ROW_NUMBER()`, not any-match: a node that stalled, was
        resumed, then halted must not still read as stalled. `id` breaks ties on
        `created_at`, because two decisions written in one transaction can share a
        timestamp and `rn = 1` would then be arbitrary.

        `ROW_NUMBER()` rather than `DISTINCT ON` — the latter is PostgreSQL-only
        and the suite runs on SQLite. Decisions are append-only (`models.py`'s
        `before_update` guard), which is what makes latest-wins sound: no row is
        rewritten behind this query.
        """
        ranked = (
            select(
                OrchestrationDecision.flow_id.label("flow_id"),
                OrchestrationDecision.kind.label("kind"),
                func.row_number()
                .over(
                    partition_by=[OrchestrationDecision.flow_id, OrchestrationDecision.node_id],
                    order_by=[OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc()],
                )
                .label("rn"),
            )
            .where(
                OrchestrationDecision.org_id == org_id,
                OrchestrationDecision.node_id.is_not(None),
                OrchestrationDecision.kind.in_((DecisionKind.NODE_STALLED.value, DecisionKind.NODE_HALTED.value)),
            )
            .subquery()
        )
        return (
            select(ranked.c.flow_id.label("flow_id"), func.count().label("stalled_count"))
            .where(ranked.c.rn == 1, ranked.c.kind == DecisionKind.NODE_STALLED.value)
            .group_by(ranked.c.flow_id)
            .subquery()
        )

    def _joined_flows(self, *, org_id: str) -> tuple[Select, dict[str, Any]]:
        """`orchestration_flows` LEFT JOINed to both aggregates, org-filtered.

        LEFT, not inner: a flow with zero nodes must still appear (as
        `status=empty`), and an inner join would drop it — the row would simply be
        missing from the list with nothing indicating why. Nulls are coalesced to
        0 here so every consumer sees integers.

        Returns the base select and the coalesced derived columns, so the page
        query and the (unfiltered) chip query share one definition of them rather
        than each having its own copy to drift.
        """
        node_agg = self._node_agg(org_id=org_id)
        stall_agg = self._stall_agg(org_id=org_id)

        derived: dict[str, Any] = {display.value: func.coalesce(getattr(node_agg.c, display.value), 0) for display in DisplayState}
        derived["stalled_count"] = func.coalesce(stall_agg.c.stalled_count, 0)

        base = (
            select(OrchestrationFlow)
            .outerjoin(node_agg, node_agg.c.flow_id == OrchestrationFlow.id)
            .outerjoin(stall_agg, stall_agg.c.flow_id == OrchestrationFlow.id)
            .where(OrchestrationFlow.org_id == org_id)
        )
        return base, derived

    async def list_flows_page_with_aggregates(
        self,
        *,
        org_id: str,
        limit: int = 25,
        offset: int = 0,
        q: str | None = None,
        status: FlowStatus | None = None,
        needs_me: bool = False,
        sort: str = "created",
    ) -> FlowPage:
        """One page of flows with their derived counts, statuses and waves.

        Everything this endpoint filters and sorts on — `status`, `needs_me`,
        `stalled_count` — is **derived** from `orchestration_nodes` and
        `orchestration_decisions`. None of it is a column on
        `orchestration_flows`, so `LIMIT/OFFSET` cannot be applied before those
        values exist, and a filtered `total` is not computable at all if
        pagination happens first. Hence: grouped subqueries LEFT JOINed, then
        filtered, sorted and paginated in ONE statement, with `COUNT(*) OVER ()`
        carrying the filtered total out of the same pass.

        `COUNT(*) OVER ()` is evaluated after `WHERE` and before `LIMIT`, so it is
        the count of everything the filters matched across all pages. That is why
        there is no `count_flows` helper: a second statement could disagree with
        the first, and this one cannot.

        Two statements total (three counting the wave aggregate, which is a
        separate method): the page, and the waves for the page's flow ids. Neither
        grows with page size. **No correlated scalar subqueries in the SELECT
        list** — they re-execute per row, reintroducing the N+1 while still
        looking like one statement to a statement counter.
        """
        if sort not in FLOW_SORTS:
            raise ValueError(f"unknown sort {sort!r}; expected one of {FLOW_SORTS}")

        base, derived = self._joined_flows(org_id=org_id)

        stmt = base.add_columns(
            *(derived[display.value].label(display.value) for display in DisplayState),
            derived["stalled_count"].label("stalled_count"),
            # The honest filtered total, from the same pass as the rows.
            func.count().over().label("total_matching"),
        )

        if q:
            # Substring, case-insensitive, over the three fields an operator would
            # search by. Mid-string matching is required, not a nicety: "4645"
            # must find both title "Delivery loop for #4645" and slug
            # "aidlc-delivery-loop-4645". Metacharacters are escaped with the same
            # helper as the cost prefix, so a `%` in the box cannot widen the
            # match to everything.
            pattern = f"%{escape_like(q)}%"
            stmt = stmt.where(
                or_(
                    OrchestrationFlow.title.ilike(pattern, escape="\\"),
                    OrchestrationFlow.slug.ilike(pattern, escape="\\"),
                    OrchestrationFlow.intent_ref.ilike(pattern, escape="\\"),
                )
            )

        if needs_me:
            # Not a `status` alias — see `FlowAggregate.needs_me`.
            stmt = stmt.where(or_(derived["gate"] > 0, derived["stalled_count"] > 0))

        if status is not None:
            stmt = stmt.where(self._status_predicate(status, derived))

        stmt = stmt.order_by(*self._order_by(sort, derived)).limit(limit).offset(offset)

        rows = (await self._session.execute(stmt)).all()

        total = int(rows[0].total_matching) if rows else 0
        aggregates = [self._to_aggregate(row) for row in rows]

        # One grouped query for every flow on the page — not one per flow.
        waves_by_flow = await self.list_wave_aggregates(org_id=org_id, flow_ids=[agg.flow.id for agg in aggregates])

        return FlowPage(
            flows=tuple(
                FlowAggregate(
                    flow=agg.flow,
                    display_counts=agg.display_counts,
                    stalled_count=agg.stalled_count,
                    status=agg.status,
                    waves=tuple(waves_by_flow.get(agg.flow.id, ())),
                )
                for agg in aggregates
            ),
            total=total,
        )

    @staticmethod
    def _to_aggregate(row: Any) -> FlowAggregate:
        """Project one result row onto a `FlowAggregate`.

        `status` comes from `derive_flow_status`, the same function the chip counts
        use — so a chip can never claim a status no row reports.
        """
        counts = FlowDisplayCounts(**{display.value: int(getattr(row, display.value) or 0) for display in DisplayState})
        stalled_count = int(row.stalled_count or 0)
        return FlowAggregate(
            flow=row[0],
            display_counts=counts,
            stalled_count=stalled_count,
            status=derive_flow_status(
                queued=counts.queued,
                in_progress=counts.in_progress,
                gate=counts.gate,
                # The decision-derived count, not the state-derived bucket: a
                # plain failure is not a stall, and only a stall means "a human
                # needs to go find out why this is wedged".
                stalled=stalled_count,
                complete=counts.complete,
            ),
        )

    @staticmethod
    def _status_predicate(status: FlowStatus, derived: dict[str, Any]):
        """The SQL form of `derive_flow_status`, as a first-match-wins predicate.

        Each branch restates the precedence by negating the branches above it,
        which is what keeps this in agreement with the Python function: asking for
        `running` must exclude a flow that is also stalled, because that flow
        reports `attention_needed` and would otherwise appear under a filter for a
        status it does not have.
        """
        stalled = derived["stalled_count"]
        gate = derived["gate"]
        in_progress = derived["in_progress"]
        queued = derived["queued"]
        complete = derived["complete"]

        if status is FlowStatus.ATTENTION_NEEDED:
            return stalled > 0
        if status is FlowStatus.AWAITING_YOU:
            return (stalled == 0) & (gate > 0)
        if status is FlowStatus.RUNNING:
            return (stalled == 0) & (gate == 0) & (in_progress > 0)
        if status is FlowStatus.QUEUED:
            return (stalled == 0) & (gate == 0) & (in_progress == 0) & (queued > 0)
        if status is FlowStatus.COMPLETE:
            return (stalled == 0) & (gate == 0) & (in_progress == 0) & (queued == 0) & (complete > 0)
        # EMPTY: nothing in any bucket. Either no nodes, or every node superseded.
        return (stalled == 0) & (gate == 0) & (in_progress == 0) & (queued == 0) & (complete == 0)

    @staticmethod
    def _order_by(sort: str, derived: dict[str, Any]) -> list[Any]:
        """The ORDER BY for one sort. Every one ends with the `id` tiebreaker.

        The tiebreaker is not cosmetic: rows sharing a sort key have no
        deterministic order without it, so under `LIMIT/OFFSET` a row can appear
        on two pages or on none.

        `updated` coalesces `updated_at` to `created_at` rather than sorting the
        raw column. `updated_at` is nullable with only `onupdate`, so a
        never-updated flow is NULL — and raw NULLs sort **first** under
        PostgreSQL `DESC` but **last** under SQLite. Sorting the raw column would
        lead the production list with untouched flows while passing its test
        locally.
        """
        tiebreak = OrchestrationFlow.id.desc()
        if sort == "updated":
            return [func.coalesce(OrchestrationFlow.updated_at, OrchestrationFlow.created_at).desc(), tiebreak]
        if sort == "stalled":
            return [derived["stalled_count"].desc(), OrchestrationFlow.created_at.desc(), tiebreak]
        return [OrchestrationFlow.created_at.desc(), tiebreak]

    async def count_flows_by_status(self, *, org_id: str) -> dict[FlowStatus, int]:
        """How many flows are in each status, across the WHOLE tenant.

        Deliberately unfiltered, and therefore necessarily a separate statement:
        the chips describe the population an operator is choosing *among*, so with
        `needs_me` on the summary reads "Showing 3 of 5" while the chips still
        total 5. Reusing the filtered aggregate would make every unselected chip
        read `0`, which tells the operator nothing.

        Every status is present in the result, including zeroes — a chip reading
        `0` is information ("nothing is stalled"), and omitting it would make the
        chip row's shape jump around as work moves.

        This aggregate touches all of the tenant's flows and is the real ceiling
        on this endpoint. If it ever hurts: **cache it, do not filter it.**
        Filtering it would change what it means.
        """
        base, derived = self._joined_flows(org_id=org_id)
        stmt = base.add_columns(
            *(derived[display.value].label(display.value) for display in DisplayState),
            derived["stalled_count"].label("stalled_count"),
        )

        counts: dict[FlowStatus, int] = dict.fromkeys(FlowStatus, 0)
        for row in (await self._session.execute(stmt)).all():
            # Same derivation as the rows, via `_to_aggregate` — one function, two
            # callers, so the chips cannot disagree with what the list shows.
            counts[self._to_aggregate(row).status] += 1
        return counts

    async def list_wave_aggregates(self, *, org_id: str, flow_ids: list[str]) -> dict[str, list[WaveAggregate]]:
        """Wave rollups for the given flows, in **first-appearance order**.

        One `GROUP BY flow_id, epic_ref, wave_ref` for the whole page, not one
        query per flow.

        **Ordered by `MIN(node.created_at)` per group, and that is a correctness
        requirement.** Two wrong ways to get an order here, both real:

        - Sorting `wave_ref` lexicographically puts `wave-10` before `wave-2`.
          `flowLayout.ts` warns about this verbatim for the same data.
        - Parsing an integer out of `wave_ref` assumes a `wave-<n>` shape nothing
          enforces — `_SEGMENT` permits `wave-as`, which exists in the tests.

        `MIN(created_at)` reproduces the first-appearance order the detail view
        already uses. A `GROUP BY` guarantees no order at all, so leaving it
        implicit is not "usually fine": the failure is invisible below 10 waves
        and appears exactly at the 7–14 wave scale the rail is built for.
        """
        if not flow_ids:
            # No page, no query. Returning early keeps the statement count honest
            # rather than emitting a `WHERE flow_id IN ()`.
            return {}

        columns = [func.count().filter(OrchestrationNode.state.in_(DISPLAY_TO_ENGINE[display])).label(display.value) for display in DisplayState]
        stmt = (
            select(
                OrchestrationNode.flow_id.label("flow_id"),
                OrchestrationNode.epic_ref.label("epic_ref"),
                OrchestrationNode.wave_ref.label("wave_ref"),
                *columns,
            )
            .where(
                OrchestrationNode.org_id == org_id,
                OrchestrationNode.flow_id.in_(flow_ids),
            )
            .group_by(OrchestrationNode.flow_id, OrchestrationNode.epic_ref, OrchestrationNode.wave_ref)
            .order_by(func.min(OrchestrationNode.created_at), OrchestrationNode.epic_ref, OrchestrationNode.wave_ref)
        )

        waves: dict[str, list[WaveAggregate]] = {flow_id: [] for flow_id in flow_ids}
        for row in (await self._session.execute(stmt)).all():
            waves[row.flow_id].append(
                WaveAggregate(
                    flow_id=row.flow_id,
                    epic_ref=row.epic_ref,
                    wave_ref=row.wave_ref,
                    display_counts=FlowDisplayCounts(**{display.value: int(getattr(row, display.value) or 0) for display in DisplayState}),
                )
            )
        return waves

    # -- nodes --------------------------------------------------------------

    async def add_node(
        self,
        *,
        org_id: str,
        flow_id: str,
        epic_ref: str,
        wave_ref: str,
        node_ref: str,
        kind: str,
        title: str,
        issue_ref: str | None = None,
        state: str | None = None,
    ) -> OrchestrationNode:
        # `state=None` means "use the column default" (pending), which is what
        # every compile has always done. It is an argument at all only for issue
        # #4528's acceptance gate, which must be *born* in `awaiting_gate`: a node
        # created pending and then moved would be a second writer of node state
        # outside `transition()`, and `transition()` has no edge into
        # `awaiting_gate` from `pending` to offer it.
        node = OrchestrationNode(
            org_id=org_id,
            flow_id=flow_id,
            epic_ref=epic_ref,
            wave_ref=wave_ref,
            node_ref=node_ref,
            kind=kind,
            title=title,
            issue_ref=issue_ref,
            **({"state": state} if state is not None else {}),
        )
        self._session.add(node)
        await self._session.flush()
        return node

    async def get_node(self, *, org_id: str, node_id: str) -> OrchestrationNode | None:
        stmt = select(OrchestrationNode).where(
            OrchestrationNode.org_id == org_id,
            OrchestrationNode.id == node_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_nodes(self, *, org_id: str, flow_id: str) -> list[OrchestrationNode]:
        stmt = (
            select(OrchestrationNode)
            .where(
                OrchestrationNode.org_id == org_id,
                OrchestrationNode.flow_id == flow_id,
            )
            .order_by(OrchestrationNode.wave_ref, OrchestrationNode.node_ref)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # -- edges --------------------------------------------------------------

    async def add_edge(self, *, org_id: str, flow_id: str, from_node_id: str, to_node_id: str) -> OrchestrationEdge:
        edge = OrchestrationEdge(org_id=org_id, flow_id=flow_id, from_node_id=from_node_id, to_node_id=to_node_id)
        self._session.add(edge)
        await self._session.flush()
        return edge

    async def list_edges(self, *, org_id: str, flow_id: str) -> list[OrchestrationEdge]:
        stmt = select(OrchestrationEdge).where(
            OrchestrationEdge.org_id == org_id,
            OrchestrationEdge.flow_id == flow_id,
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # -- accepted plans -----------------------------------------------------

    async def record_accepted_plan(
        self,
        *,
        org_id: str,
        flow_id: str,
        plan_document: dict,
        plan_hash: str,
        accepted_by_decision_id: str | None = None,
    ) -> OrchestrationAcceptedPlan:
        """Append the next accepted-plan version, superseding the one in force.

        Amendment supersedes rather than mutates, so the version in force at any
        past gate stays readable. Version numbers are allocated from the current
        maximum for the flow; the unique index on `(flow_id, version)` is what
        makes a concurrent double-accept fail loudly instead of silently
        producing two "version 2" plans.
        """
        current = await self.get_accepted_plan(org_id=org_id, flow_id=flow_id)
        if current is not None:
            current.superseded_at = utcnow()

        stmt = select(OrchestrationAcceptedPlan.version).where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
        )
        versions = list((await self._session.execute(stmt)).scalars().all())
        next_version = (max(versions) + 1) if versions else 1

        plan = OrchestrationAcceptedPlan(
            org_id=org_id,
            flow_id=flow_id,
            version=next_version,
            plan_document=plan_document,
            plan_hash=plan_hash,
            accepted_by_decision_id=accepted_by_decision_id,
        )
        self._session.add(plan)
        await self._session.flush()
        return plan

    async def get_accepted_plan(self, *, org_id: str, flow_id: str) -> OrchestrationAcceptedPlan | None:
        """The accepted plan currently in force (the one not yet superseded)."""
        stmt = select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_plan_versions(self, *, org_id: str, flow_id: str) -> list[OrchestrationAcceptedPlan]:
        stmt = (
            select(OrchestrationAcceptedPlan)
            .where(
                OrchestrationAcceptedPlan.org_id == org_id,
                OrchestrationAcceptedPlan.flow_id == flow_id,
            )
            .order_by(OrchestrationAcceptedPlan.version)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # -- decisions (append-only: no update method, by design) ----------------

    async def append_decision(
        self,
        *,
        org_id: str,
        flow_id: str,
        kind: str,
        actor_id: str,
        actor_role: str,
        actor_kind: str,
        node_id: str | None = None,
        reason: str | None = None,
        rejection_reason: str | None = None,
        from_state: str | None = None,
        to_state: str | None = None,
    ) -> OrchestrationDecision:
        """Append one decision record. There is deliberately no counterpart
        `update_decision` / `delete_decision` — see the class docstring."""
        decision = OrchestrationDecision(
            org_id=org_id,
            flow_id=flow_id,
            node_id=node_id,
            kind=kind,
            actor_id=actor_id,
            actor_role=actor_role,
            actor_kind=actor_kind,
            reason=reason,
            rejection_reason=rejection_reason,
            from_state=from_state,
            to_state=to_state,
        )
        self._session.add(decision)
        await self._session.flush()
        return decision

    async def list_decisions(self, *, org_id: str, flow_id: str) -> list[OrchestrationDecision]:
        stmt = (
            select(OrchestrationDecision)
            .where(
                OrchestrationDecision.org_id == org_id,
                OrchestrationDecision.flow_id == flow_id,
            )
            .order_by(OrchestrationDecision.created_at)
        )
        return list((await self._session.execute(stmt)).scalars().all())
