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

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import utcnow

from .cost import escape_like
from .display_state import DisplayState, FlowStatus, derive_flow_status
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationFlow,
    OrchestrationNode,
)
from .progress_projection import node_progress_rows

# The sorts `GET /orchestration/flows` accepts. `cost` is deliberately absent:
# cost lives in `usage_logs`, joinable only by address prefix, so it cannot
# participate in the sorted+paginated statement — sorting by it would rank the
# fetched page while appearing to rank every row.
FLOW_SORTS: tuple[str, ...] = ("created", "updated", "stalled")

KIND_COUNTS = {"story_count": "story", "gate_count": "gate", "eval_count": "eval"}
FLOW_COUNT_FIELDS = (*KIND_COUNTS, "changes_requested_count", "completed_story_count", "eval_story_count", "completed_eval_story_count")


def _kind_count_columns(nodes):
    return [func.count().filter(nodes.c.kind == kind, nodes.c.state != "superseded").label(name) for name, kind in KIND_COUNTS.items()]


@dataclass(frozen=True)
class FlowDisplayCounts:
    """Node counts per display state for one flow or one wave.

    Each node occupies one current bucket. Blocked executions need attention;
    capacity waits are queued. Historical stall decisions do not add counts.
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
    story_count: int = 0
    gate_count: int = 0
    eval_count: int = 0

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
    # Compatibility field: always equals display_counts.stalled.
    stalled_count: int
    status: FlowStatus
    story_count: int = 0
    gate_count: int = 0
    eval_count: int = 0
    changes_requested_count: int = 0
    completed_story_count: int = 0
    # Additive presentation counts; preserve the existing engine-kind counts.
    eval_story_count: int = 0
    completed_eval_story_count: int = 0
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
        return self.display_counts.gate > 0 or self.display_counts.stalled > 0 or self.stalled_count > 0

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

    async def create_flow(
        self,
        *,
        org_id: str,
        slug: str,
        title: str,
        intent_ref: str | None = None,
        description: str | None = None,
        design_history: dict | None = None,
    ) -> OrchestrationFlow:
        """Insert a flow, or return the concurrent winner for the same slug.

        `description` / `design_history` default to NULL (#4885). Both default to
        `None` rather than to a placeholder, so a caller that does not know the
        design story writes "we do not know" — the honest value — and a
        hand-created flow needs no argument at all to get it right.

        **Concurrency (#4898).** `(org_id, slug)` is unique from migration 053,
        because a flow's slug is the first segment of every node's graph address
        and two same-slug flows in one tenant would merge their model spend into
        one total that looks authoritative. Callers reach here after looking for
        the slug and not finding it (`compile._resolve_flow`), which is a
        read-then-write race: two concurrent registrations can both read "absent"
        and both insert. The database decides that race; this method absorbs the
        loss.

        The insert therefore runs in its own SAVEPOINT. On a uniqueness violation
        only that savepoint rolls back, leaving the caller's surrounding
        transaction usable — which matters because the caller is mid-compile
        inside its own `begin_nested()`, and a poisoned transaction would fail the
        whole plan submission rather than converge. The winning row is then re-read
        and returned, so both concurrent registrations resolve to ONE flow
        identity and the loser proceeds normally instead of erroring.

        Recovery is deliberately narrow: it returns an existing flow only when one
        is actually found. A violation with no readable winner is re-raised rather
        than papered over, because that shape is a real integrity fault (some other
        constraint), not this race. Nothing here mutates the winner — an existing
        flow keeps its own title, intent and design history, exactly as
        `_resolve_flow` documents: this resolves a flow, it does not reconcile one.
        """
        flow = OrchestrationFlow(
            org_id=org_id,
            slug=slug,
            title=title,
            intent_ref=intent_ref,
            description=description,
            design_history=design_history,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(flow)
                await self._session.flush()
        except IntegrityError:
            existing = await self.get_flow_by_slug(org_id=org_id, slug=slug)
            if existing is None:
                # Not the slug race — do not swallow a different integrity fault.
                raise
            return existing
        return flow

    async def get_flow_by_slug(self, *, org_id: str, slug: str) -> OrchestrationFlow | None:
        """The tenant's flow with this slug, or None.

        Tenant-scoped because a slug identifies a flow only WITHIN a tenant: two
        customers may each run a `delivery-loop`, they are different flows, and
        migration 053's unique index is `(org_id, slug)` for that reason.

        **Why this tolerates duplicates instead of asserting uniqueness.** Once
        migration 053 is applied at most one row can match, so `LIMIT 1` and a
        "exactly one or none" assertion are equivalent — on that schema. They are
        NOT equivalent on a database that still holds duplicate `(org_id, slug)`
        groups, and that state is reachable *by design*: migration 053 deliberately
        REFUSES on pre-existing duplicates so an operator resolves them
        explicitly, while `gateway-deploy.yml` runs `run-migrations` only AFTER
        `deploy-backend`. So this code is live against the un-migrated schema, and
        on a tenant with duplicates a `scalar_one_or_none()` here would raise
        `MultipleResultsFound` — turning what the previous full scan handled (it
        took the newest match) into a failed plan submission for that tenant, and
        failing exactly the deployments the refusing migration exists to protect.

        Ordering matches the scan this replaced: `list_flows` returns
        `created_at DESC` and `compile._resolve_flow` took its first match, so the
        newest flow wins. Identical results once uniqueness holds, and the same
        results as before it does.
        """
        stmt = (
            select(OrchestrationFlow)
            .where(
                OrchestrationFlow.org_id == org_id,
                OrchestrationFlow.slug == slug,
            )
            .order_by(OrchestrationFlow.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

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

    async def _node_agg(self, *, org_id: str):
        """Aggregate current display buckets in SQL, without per-flow reads."""
        from .plan_lineage import preserved_execution_pairs

        preserved = await preserved_execution_pairs(self._session, org_id=org_id)
        nodes = node_progress_rows(org_id=org_id, preserved_executions=preserved)
        evaluation_story = and_(nodes.c.kind == "eval", func.trim(nodes.c.issue_ref) != "", nodes.c.state != "superseded")
        columns = [func.count().filter(nodes.c.display_state == display.value).label(display.value) for display in DisplayState]
        return (
            select(
                nodes.c.flow_id,
                *columns,
                *_kind_count_columns(nodes),
                func.count().filter(nodes.c.state == "rejected_at_gate").label("changes_requested_count"),
                func.count().filter(nodes.c.kind == "story", nodes.c.state == "passed").label("completed_story_count"),
                func.count().filter(evaluation_story).label("eval_story_count"),
                func.count().filter(evaluation_story, nodes.c.state == "passed").label("completed_eval_story_count"),
            )
            .group_by(nodes.c.flow_id)
            .subquery()
        )

    async def node_display_states(self, *, org_id: str, flow_id: str) -> dict[str, str | None]:
        """The graph uses exactly the same projection as list and wave counts."""
        from .plan_lineage import preserved_execution_pairs

        preserved = await preserved_execution_pairs(self._session, org_id=org_id, flow_ids=[flow_id])
        nodes = node_progress_rows(org_id=org_id, flow_ids=[flow_id], preserved_executions=preserved)
        return dict((await self._session.execute(select(nodes.c.node_id, nodes.c.display_state))).all())

    async def _joined_flows(self, *, org_id: str) -> tuple[Select, dict[str, Any]]:
        """`orchestration_flows` LEFT JOINed to current node aggregates, org-filtered.

        LEFT, not inner: a flow with zero nodes must still appear (as
        `status=empty`), and an inner join would drop it — the row would simply be
        missing from the list with nothing indicating why. Nulls are coalesced to
        0 here so every consumer sees integers.

        Returns the base select and the coalesced derived columns, so the page
        query and the (unfiltered) chip query share one definition of them rather
        than each having its own copy to drift.
        """
        node_agg = await self._node_agg(org_id=org_id)

        derived: dict[str, Any] = {display.value: func.coalesce(getattr(node_agg.c, display.value), 0) for display in DisplayState}
        derived["stalled_count"] = derived["stalled"]
        for name in FLOW_COUNT_FIELDS:
            derived[name] = func.coalesce(getattr(node_agg.c, name), 0)

        base = select(OrchestrationFlow).outerjoin(node_agg, node_agg.c.flow_id == OrchestrationFlow.id).where(OrchestrationFlow.org_id == org_id)
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

        base, derived = await self._joined_flows(org_id=org_id)

        stmt = base.add_columns(
            *(derived[display.value].label(display.value) for display in DisplayState),
            derived["stalled_count"].label("stalled_count"),
            *(derived[name].label(name) for name in FLOW_COUNT_FIELDS),
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
            stmt = stmt.where(or_(derived["gate"] > 0, derived["stalled"] > 0, derived["stalled_count"] > 0))

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
                    story_count=agg.story_count,
                    gate_count=agg.gate_count,
                    eval_count=agg.eval_count,
                    changes_requested_count=agg.changes_requested_count,
                    completed_story_count=agg.completed_story_count,
                    eval_story_count=agg.eval_story_count,
                    completed_eval_story_count=agg.completed_eval_story_count,
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
            **{name: int(getattr(row, name) or 0) for name in FLOW_COUNT_FIELDS},
            status=derive_flow_status(
                queued=counts.queued,
                in_progress=counts.in_progress,
                gate=counts.gate,
                stalled=counts.stalled,
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
        stalled = derived["stalled"]
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
        base, derived = await self._joined_flows(org_id=org_id)
        stmt = base.add_columns(
            *(derived[display.value].label(display.value) for display in DisplayState),
            derived["stalled_count"].label("stalled_count"),
            *(derived[name].label(name) for name in FLOW_COUNT_FIELDS),
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

        from .plan_lineage import preserved_execution_pairs

        preserved = await preserved_execution_pairs(self._session, org_id=org_id, flow_ids=flow_ids)
        nodes = node_progress_rows(org_id=org_id, flow_ids=flow_ids, preserved_executions=preserved)
        columns = [func.count().filter(nodes.c.display_state == display.value).label(display.value) for display in DisplayState]
        stmt = (
            select(nodes.c.flow_id, nodes.c.epic_ref, nodes.c.wave_ref, *columns, *_kind_count_columns(nodes))
            .group_by(nodes.c.flow_id, nodes.c.epic_ref, nodes.c.wave_ref)
            .order_by(func.min(nodes.c.created_at), nodes.c.epic_ref, nodes.c.wave_ref)
        )

        waves: dict[str, list[WaveAggregate]] = {flow_id: [] for flow_id in flow_ids}
        for row in (await self._session.execute(stmt)).all():
            waves[row.flow_id].append(
                WaveAggregate(
                    flow_id=row.flow_id,
                    epic_ref=row.epic_ref,
                    wave_ref=row.wave_ref,
                    display_counts=FlowDisplayCounts(**{display.value: int(getattr(row, display.value) or 0) for display in DisplayState}),
                    **{name: int(getattr(row, name) or 0) for name in KIND_COUNTS},
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

    async def display_metadata_for_flows(self, *, org_id: str, flow_ids: list[str]) -> dict[str, dict[str, list[dict]]]:
        """One tenant-scoped read of current display text for the whole page."""
        if not flow_ids:
            return {}
        rows = await self._session.execute(
            select(
                OrchestrationAcceptedPlan.flow_id,
                OrchestrationAcceptedPlan.plan_document["wave_metadata"],
                OrchestrationAcceptedPlan.plan_document["epic_metadata"],
            ).where(
                OrchestrationAcceptedPlan.org_id == org_id,
                OrchestrationAcceptedPlan.flow_id.in_(flow_ids),
                OrchestrationAcceptedPlan.superseded_at.is_(None),
            )
        )
        return {flow_id: {"wave_metadata": waves or [], "epic_metadata": epics or []} for flow_id, waves, epics in rows}

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
        # Serialize plan changes with execution-ledger authority checks.  The flow
        # exists even before version 1, so this also closes the first-plan race.
        await self._session.execute(
            select(OrchestrationFlow.id).where(OrchestrationFlow.org_id == org_id, OrchestrationFlow.id == flow_id).with_for_update()
        )

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
        decision_id: str | None = None,
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
        if decision_id is not None:
            decision.id = decision_id
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
