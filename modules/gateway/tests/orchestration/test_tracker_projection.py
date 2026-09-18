"""Execution-time projection of engine state onto the EPIC tracker region (#5284).

The defect is a display one, so most of these tests are about damage control rather
than about happy-path rendering. The region the engine rewrites sits inside an issue
body that is otherwise the user's own prose, and the pass runs on every tick against
every active flow — so a rendering bug is cosmetic, while a splicing bug silently
eats an EPIC's intent text and a staleness bug makes the display flap between two
truths.

The negative cases are chosen to be *discriminating*: each fails if its guard is
removed, and none of them passes merely because the code did nothing.

- `test_stale_snapshot_is_refused` fails if the version/watermark comparison is
  dropped; `test_older_snapshot_is_overwritten` fails if that comparison is inverted
  or made unconditional. Either alone would pass against code that never writes.
- `test_edit_made_before_the_read_is_preserved` fails if the write is built from
  anything other than the body just read.
- `test_overlapping_ticks_cannot_let_the_older_patch_land_last` forces the precise
  read/read/new-write/old-write order that a marker check alone cannot prevent.
- `test_unchanged_render_writes_nothing` is what *proves* idempotence (AC1) rather
  than asserting it, because it checks that no write was attempted at all.

The provider is stubbed here, and the stub records reads and writes separately: the
difference between "wrote the same bytes again" and "correctly skipped" is invisible
in the resulting body, and it is exactly what idempotence means. Whether GitHub
accepts the PATCH is not what these tests are for — `test_tracker_provider.py` covers
the adapter boundary itself.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.models import (
    DecisionKind,
    NodeKind,
    NodeState,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.tracker_projection import (
    REGION_END,
    REGION_START,
    PendingTrackerProjection,
    RegionRefusal,
    TrackerProjectionConfig,
    TrackerProjectionReport,
    _mixed_start,
    _projection_write_lock,
    _select_capped_flows,
    flush_tracker_projections,
    read_snapshot,
    render_region,
    run_tracker_projection_pass,
    splice_region,
)
from src.shared.models.base import Base

ORG_A = "org-alpha"
ORG_B = "org-beta"
REPO = "aws-e/adp"
EPIC = 4910
INSTALLATION = 4242
FLOW_SLUG = "aidlc-engine"

CONFIG = TrackerProjectionConfig(enabled=True, repo=REPO, max_flows_per_pass=20)

# A body in the shape the inception persona leaves behind: human intent above, the
# generated region in the middle, more human text below. The text outside the
# sentinels is what every preservation assertion checks byte-for-byte.
INTENT_ABOVE = "## What we want\n\nKeep the tracker current.\n\n"
INTENT_BELOW = "\n\n## Notes\n\nDo not lose this paragraph."


def _body(region: str) -> str:
    return f"{INTENT_ABOVE}{REGION_START}\n{region}\n{REGION_END}{INTENT_BELOW}"


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng.sync_engine, "connect")
    def _disable_pysqlite_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(eng.sync_engine, "begin")
    def _emit_explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s


@pytest.fixture(autouse=True)
async def _lock_session_factory(monkeypatch, engine):
    """Give direct flush tests the same SQLite-backed lock factory as their state."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: factory)


@pytest.fixture(autouse=True)
def _installation(monkeypatch):
    """One unambiguous installation per org, so target resolution is not the subject."""

    async def _resolve(_session, *, org_id):
        return INSTALLATION

    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", _resolve)


class StubProvider:
    """Stands in for `GitHubTrackerProvider`, recording every call it receives.

    Reads and writes are recorded separately so a test can assert not merely on the
    resulting body but on *whether a write was attempted at all* — a refusal and a
    successful no-op look identical in the body and are entirely different outcomes.
    """

    def __init__(self, body: str, *, fail_read: bool = False, fail_write: bool = False) -> None:
        self.body = body
        self.fail_read = fail_read
        self.fail_write = fail_write
        self.reads: list[tuple[str, int]] = []
        self.writes: list[str] = []

    async def read_issue_body(self, *, org_id, installation_id, repo, issue_number):
        if self.fail_read:
            raise RuntimeError("provider read unavailable")
        self.reads.append((org_id, issue_number))
        return self.body

    async def write_issue_body(self, *, org_id, installation_id, repo, issue_number, body):
        if self.fail_write:
            raise RuntimeError("provider write unavailable")
        self.writes.append(body)
        self.body = body


async def _flow(
    session: AsyncSession,
    *,
    org_id: str = ORG_A,
    epic: int = EPIC,
    states: list[str] | None = None,
    version: int = 1,
    slug: str = FLOW_SLUG,
    approved: bool = True,
    title: str = "Story",
    issue_ref: str | None = None,
) -> OrchestrationFlow:
    """A flow with one story node per requested state, plus an in-force accepted plan.

    The `PLAN_ACCEPTED` decision is written beside the plan row because that is what
    the engine does — `compile_proposal` appends the decision and then points the plan
    at it — and because projection now requires it (#5337 review finding). A fixture
    that seeded the plan alone would model a flow that cannot exist, and would make
    the authorization tests below pass for the wrong reason.
    """
    flow = OrchestrationFlow(org_id=org_id, slug=slug, title="AI-DLC engine", intent_ref="4191")
    session.add(flow)
    await session.flush()
    for index, state in enumerate(states or [NodeState.RUNNING.value], start=1):
        session.add(
            OrchestrationNode(
                org_id=org_id,
                flow_id=flow.id,
                epic_ref=f"epic-{epic}",
                wave_ref="wave-1",
                node_ref=f"U{index}",
                kind=NodeKind.STORY.value,
                state=state,
                title=f"{title} {index}",
                issue_ref=issue_ref if issue_ref is not None else str(5000 + index),
                attempts=1,
            )
        )
    if version:
        session.add(
            OrchestrationAcceptedPlan(
                org_id=org_id,
                flow_id=flow.id,
                version=version,
                plan_document={"nodes": []},
                plan_hash="f" * 64,
            )
        )
    if approved:
        session.add(
            OrchestrationDecision(
                org_id=org_id,
                flow_id=flow.id,
                kind=DecisionKind.PLAN_ACCEPTED.value,
                actor_id="approver",
                actor_role="admin",
                actor_kind="human",
            )
        )
    await session.flush()
    return flow


async def _nodes(session: AsyncSession) -> list[OrchestrationNode]:
    result = await session.execute(select(OrchestrationNode).order_by(OrchestrationNode.node_ref))
    return list(result.scalars().all())


async def _decision_ids(session: AsyncSession) -> set[str]:
    """Every decision row's id, so a test can assert the pass added none of its own."""
    return set((await session.execute(select(OrchestrationDecision.id))).scalars().all())


# --------------------------------------------------------------------------
# splice_region — the region is the ONLY thing that may change (AC2)
# --------------------------------------------------------------------------


def test_only_the_region_is_replaced():
    result = splice_region(_body("old kickoff snapshot"), "fresh execution snapshot")
    assert isinstance(result, str) and not isinstance(result, RegionRefusal)
    assert "fresh execution snapshot" in result
    assert "old kickoff snapshot" not in result
    # Both sentinels survive, so the next tick can find the region again. A splice
    # that consumed its own markers would work exactly once.
    assert result.count(REGION_START) == 1
    assert result.count(REGION_END) == 1


def test_text_outside_the_region_is_byte_identical():
    result = splice_region(_body("anything at all"), "replacement")
    head, _, rest = result.partition(REGION_START)
    _, _, tail = rest.partition(REGION_END)
    assert head == INTENT_ABOVE
    assert tail == INTENT_BELOW


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("No sentinels at all, just a normal issue.", "region_missing"),
        (f"{REGION_START}\nonly a start marker", "region_missing"),
        (f"only an end marker\n{REGION_END}", "region_missing"),
        (_body("one") + _body("two"), "region_duplicated"),
        (f"{REGION_END}\ninverted\n{REGION_START}", "region_malformed"),
    ],
)
def test_bad_sentinels_refuse_without_producing_a_body(body, reason):
    """Every malformed case yields a reason, never a body — so the caller cannot write.

    The refusal is its own type rather than `None` so a caller that forgets to check
    gets a `RegionRefusal` where it expected a body, instead of silently PATCHing the
    string "None" over somebody's issue.
    """
    result = splice_region(body, "replacement")
    assert isinstance(result, RegionRefusal)
    assert str(result) == reason


def test_a_missing_region_is_never_appended():
    """There is deliberately no create path: only the persona initialises a region.

    An engine that appended a region when it found none would write a progress block
    onto whatever issue it was pointed at, including one that is not a tracker.
    """
    original = "A plain issue with no tracker region."
    assert isinstance(splice_region(original, "region"), RegionRefusal)


# --------------------------------------------------------------------------
# Rendering — pure, repeatable, and honest about what it knows
# --------------------------------------------------------------------------


async def test_render_is_pure_and_repeatable(session):
    """Byte-identical output for identical input. This is what makes AC1 idempotent."""
    flow = await _flow(session, states=[NodeState.PASSED.value, NodeState.RUNNING.value])
    nodes = await _nodes(session)
    at = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    first = render_region(flow=flow, nodes=nodes, bindings={}, version=1, watermark=5, observed_at=at)
    second = render_region(flow=flow, nodes=nodes, bindings={}, version=1, watermark=5, observed_at=at)
    assert first == second


async def test_render_states_source_flow_plan_version_and_snapshot_time(session):
    """AC1: a reader must be able to see where the snapshot came from and how fresh."""
    flow = await _flow(session, states=[NodeState.RUNNING.value])
    region = render_region(
        flow=flow,
        nodes=await _nodes(session),
        bindings={},
        version=3,
        watermark=9,
        observed_at=datetime(2026, 9, 17, 12, 30, 0, tzinfo=UTC),
    )
    assert FLOW_SLUG in region
    assert "v3" in region
    assert "2026-09-17 12:30:00" in region
    # AC2: retained kickoff inventory above the region is labelled historical, so a
    # reader does not read the planning snapshot as live status.
    assert "historical" in region


async def test_render_separates_review_from_running(session):
    """`awaiting_merge` is a different thing to tell a reader than `running` (AC1)."""
    flow = await _flow(session, states=[NodeState.RUNNING.value, NodeState.AWAITING_MERGE.value])
    region = render_region(
        flow=flow,
        nodes=await _nodes(session),
        bindings={},
        version=1,
        watermark=1,
        observed_at=datetime(2026, 9, 17, tzinfo=UTC),
    )
    assert "running" in region
    assert "in review" in region


async def test_render_does_not_claim_merged_without_evidence(session):
    """A bound-but-unaccepted story must not be rendered as merged.

    There is no `merged` column on a binding — merge is live provider evidence that
    this pass never reads. Printing "merged" from a binding's existence would assert
    a verification nobody performed, which is worse than being a tick behind.
    """
    flow = await _flow(session, states=[NodeState.AWAITING_MERGE.value])
    region = render_region(
        flow=flow,
        nodes=await _nodes(session),
        bindings={},
        version=1,
        watermark=1,
        observed_at=datetime(2026, 9, 17, tzinfo=UTC),
    )
    assert "merged," not in region
    assert "awaiting merge verification" in region


async def test_render_shows_progress_against_the_whole_plan(session):
    flow = await _flow(
        session,
        states=[NodeState.PASSED.value, NodeState.PASSED.value, NodeState.RUNNING.value, NodeState.PENDING.value],
    )
    region = render_region(
        flow=flow,
        nodes=await _nodes(session),
        bindings={},
        version=1,
        watermark=1,
        observed_at=datetime(2026, 9, 17, tzinfo=UTC),
    )
    assert "2/4 stories passed" in region


def test_snapshot_marker_round_trips():
    # Read from inside the generated region, which is the only place the engine
    # writes a marker and so the only place one is authoritative.
    assert read_snapshot(_body("<!-- aidlc-tracker-snapshot: v2 w14 -->")) == (2, 14)
    # A persona-written region carries no marker. Treated as older than anything, so
    # the first execution-time projection is free to land on it.
    assert read_snapshot(_body("no marker here")) is None
    # A marker in the human's own text outside the region is prose, not a snapshot.
    assert read_snapshot("<!-- aidlc-tracker-snapshot: v2 w14 -->") is None


# --------------------------------------------------------------------------
# Config — fail-closed, and never raising on the tick path
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "1", "yes", "TRUE-ish", "false", "0"])
def test_only_the_literal_true_enables_the_pass(monkeypatch, value):
    monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", REPO)
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", value)
    assert TrackerProjectionConfig.from_env().enabled is False


def test_the_flag_being_true_enables_the_pass(monkeypatch):
    monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", REPO)
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", " TRUE ")
    assert TrackerProjectionConfig.from_env().enabled is True


@pytest.mark.parametrize("cap", ["not-a-number", "0", "-3", ""])
def test_a_malformed_cap_degrades_instead_of_breaking_the_tick(monkeypatch, cap):
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", REPO)
    monkeypatch.setenv("ORCH_MAX_PROJECTIONS_PER_PASS", cap)
    assert TrackerProjectionConfig.from_env().max_flows_per_pass == 20


# --------------------------------------------------------------------------
# The pass — target resolution, tenant isolation, refusals (AC3)
# --------------------------------------------------------------------------


async def test_pass_is_a_silent_no_op_when_the_flag_is_off(session):
    await _flow(session)
    report = await run_tracker_projection_pass(session, TrackerProjectionConfig(enabled=False, repo=REPO))
    assert report.enabled is False
    assert report.pending == []
    assert report.flows_examined == 0


async def test_pass_is_a_no_op_without_a_configured_repository(session):
    """No repository means nothing to write to; reported as disabled, not as an error."""
    await _flow(session)
    report = await run_tracker_projection_pass(session, TrackerProjectionConfig(enabled=True, repo=""))
    assert report.enabled is False
    assert report.pending == []


async def test_pass_targets_the_epic_issue_from_the_graph_address(session):
    await _flow(session, states=[NodeState.PASSED.value, NodeState.RUNNING.value])
    report = await run_tracker_projection_pass(session, CONFIG)
    assert len(report.pending) == 1
    pending = report.pending[0]
    assert (pending.org_id, pending.repo, pending.issue_number) == (ORG_A, REPO, EPIC)
    assert pending.installation_id == INSTALLATION
    assert pending.version == 1


async def test_pass_records_no_decision_and_changes_no_state(session):
    """AC3, at the pass boundary: rendering is a read, and takes no decision.

    A projection that recorded a decision row would be an engine actor, and the
    append-only decisions table would carry entries nobody authorised.
    """
    await _flow(session, states=[NodeState.RUNNING.value, NodeState.PASSED.value])
    before = {n.id: (n.state, n.attempts) for n in await _nodes(session)}
    # Asserted as "no decision was *added*" rather than "the table is empty": the
    # fixture now seeds the flow's approval, because that is what the engine writes
    # and what projection requires. An empty-table assertion would have silently
    # become a test of the fixture instead of a test of the pass.
    decisions_before = await _decision_ids(session)

    await run_tracker_projection_pass(session, CONFIG)

    assert await _decision_ids(session) == decisions_before
    assert {n.id: (n.state, n.attempts) for n in await _nodes(session)} == before
    assert list(session.new) == []
    assert list(session.deleted) == []


async def test_two_epics_in_one_flow_are_refused_not_guessed(session):
    """Ambiguity is a refusal (AC3): guessing publishes one EPIC's progress on another."""
    flow = await _flow(session, states=[NodeState.RUNNING.value])
    session.add(
        OrchestrationNode(
            org_id=ORG_A,
            flow_id=flow.id,
            epic_ref="epic-9999",
            wave_ref="wave-1",
            node_ref="U2",
            kind=NodeKind.STORY.value,
            state=NodeState.RUNNING.value,
            title="Story in a different EPIC",
            attempts=1,
        )
    )
    await session.flush()

    report = await run_tracker_projection_pass(session, CONFIG)

    assert report.pending == []
    assert report.projections_refused == 1


async def test_an_unparseable_epic_reference_is_refused(session):
    await _flow(session, states=[NodeState.RUNNING.value])
    node = (await _nodes(session))[0]
    node.epic_ref = "not-an-epic"
    await session.flush()

    report = await run_tracker_projection_pass(session, CONFIG)

    assert report.pending == []
    assert report.projections_refused == 1


async def test_an_unresolved_installation_is_refused(session, monkeypatch):
    """Fail-closed on zero and on more than one installation, by the shared rule."""

    async def _none(_session, *, org_id):
        return None

    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", _none)
    await _flow(session)

    report = await run_tracker_projection_pass(session, CONFIG)

    assert report.pending == []
    assert report.projections_refused == 1


async def test_each_flow_projects_under_its_own_tenant(session):
    """Two tenants, two targets — the write target comes from the flow, not a caller."""
    await _flow(session, org_id=ORG_A, epic=4910, slug="flow-a")
    await _flow(session, org_id=ORG_B, epic=5555, slug="flow-b")

    report = await run_tracker_projection_pass(session, CONFIG)

    assert {(p.org_id, p.issue_number) for p in report.pending} == {(ORG_A, 4910), (ORG_B, 5555)}


async def test_superseded_attempts_are_not_counted(session):
    """A superseded attempt would show one story twice and inflate the denominator."""
    flow = await _flow(session, states=[NodeState.PASSED.value])
    session.add(
        OrchestrationNode(
            org_id=ORG_A,
            flow_id=flow.id,
            epic_ref=f"epic-{EPIC}",
            wave_ref="wave-1",
            node_ref="U1-old",
            kind=NodeKind.STORY.value,
            state=NodeState.SUPERSEDED.value,
            title="A superseded attempt",
            attempts=1,
        )
    )
    await session.flush()

    report = await run_tracker_projection_pass(session, CONFIG)

    assert "A superseded attempt" not in report.pending[0].body_region
    assert "1/1 stories passed" in report.pending[0].body_region


async def test_the_in_force_plan_is_named_not_the_highest_version(session):
    """A superseded plan version must not be attributed as the authority (AC2)."""
    flow = await _flow(session, version=1)
    session.add(
        OrchestrationAcceptedPlan(
            org_id=ORG_A,
            flow_id=flow.id,
            version=2,
            plan_document={"nodes": []},
            plan_hash="a" * 64,
            superseded_at=datetime(2026, 9, 16, tzinfo=UTC),
        )
    )
    await session.flush()

    report = await run_tracker_projection_pass(session, CONFIG)

    assert report.pending[0].version == 1


async def _transition(session: AsyncSession, node: OrchestrationNode, to_state: str, *, kind: str) -> None:
    """Move a node the way the engine does: the new state **and** its decision row.

    Every writer of `OrchestrationNode.state` appends a decision in the same
    transaction as the transition (`tick.py`, `dispatch.py`, `controls.py`,
    `engine_commands.py`, `amend.py`, `adapters/github_comments.py`), so a test that
    sets `.state` alone is modelling something the engine never does. The watermark is
    counted from those rows, so the pairing is what makes these tests meaningful.
    """
    node.state = to_state
    session.add(
        OrchestrationDecision(
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=kind,
            actor_id="tester",
            actor_role="admin",
            actor_kind="human",
            to_state=to_state,
        )
    )
    await session.flush()


async def test_the_watermark_advances_as_the_flow_advances(session):
    """Monotonicity is what makes the staleness comparison meaningful."""
    await _flow(session, states=[NodeState.RUNNING.value, NodeState.PENDING.value])
    early = (await run_tracker_projection_pass(session, CONFIG)).pending[0].watermark

    for node in await _nodes(session):
        await _transition(session, node, NodeState.PASSED.value, kind="result_observed")
    later = (await run_tracker_projection_pass(session, CONFIG)).pending[0].watermark

    assert later > early


# --------------------------------------------------------------------------
# The watermark must never decrease (#5337 review finding)
#
# The watermark gates every write: `_write_one` refuses when the snapshot already on
# the issue is greater. So a watermark that falls while the flow really moves forward
# makes the engine permanently decline to publish real progress, and because a stale
# decline is a *correct* outcome that leaves the tick green, nothing reports it.
#
# Each of these three exercised a real regression in the original weighted-sum
# watermark. They are written through `run_tracker_projection_pass` rather than against
# the helper so they pin the value the flush actually compares, and each pairs the
# state change with the decision row the engine writes beside it.
# --------------------------------------------------------------------------


async def test_a_rejected_gate_does_not_lower_the_watermark(session):
    """`awaiting_gate -> rejected_at_gate` scored 2 -> 0 and never publishes.

    The worst of the three, because it is a *human* action: the operator rejects the
    gate and the tracker goes on telling every reader it is still waiting for them.
    `controls.py` is explicit that a gate answer does not increment `attempts`, so
    nothing compensated for the drop.
    """
    await _flow(session, states=[NodeState.AWAITING_GATE.value])
    before = (await run_tracker_projection_pass(session, CONFIG)).pending[0].watermark

    node = (await _nodes(session))[0]
    await _transition(session, node, NodeState.REJECTED_AT_GATE.value, kind="gate_rejected")
    after = (await run_tracker_projection_pass(session, CONFIG)).pending[0].watermark

    assert after >= before


async def test_a_failure_does_not_lower_the_watermark(session):
    """`running -> failed` scored 1 -> 0, so a failure could never be published."""
    await _flow(session, states=[NodeState.RUNNING.value])
    before = (await run_tracker_projection_pass(session, CONFIG)).pending[0].watermark

    node = (await _nodes(session))[0]
    await _transition(session, node, NodeState.FAILED.value, kind="result_observed")
    after = (await run_tracker_projection_pass(session, CONFIG)).pending[0].watermark

    assert after >= before


async def test_superseding_a_node_does_not_lower_the_watermark(session):
    """An amendment drops superseded rows out of the pass, and out of the old sum."""
    await _flow(session, states=[NodeState.PASSED.value, NodeState.PASSED.value])
    before = (await run_tracker_projection_pass(session, CONFIG)).pending[0].watermark

    node = (await _nodes(session))[0]
    await _transition(session, node, NodeState.SUPERSEDED.value, kind="plan_amended")
    after = (await run_tracker_projection_pass(session, CONFIG)).pending[0].watermark

    assert after >= before


async def test_a_rejected_gate_actually_reaches_the_issue(session):
    """The end-to-end consequence, not just the number.

    Asserts on the published body: the reader must stop being told a decision is
    needed once the human has made it. Fails against the original watermark with
    `projections_stale == 1` and the region still reading "needs your decision",
    which is the defect exactly as an operator would meet it.
    """
    await _flow(session, states=[NodeState.AWAITING_GATE.value])
    first = await run_tracker_projection_pass(session, CONFIG)
    provider = StubProvider(_body("kickoff snapshot"))
    await flush_tracker_projections(first, client_factory=provider)
    assert first.projections_written == 1

    node = (await _nodes(session))[0]
    await _transition(session, node, NodeState.REJECTED_AT_GATE.value, kind="gate_rejected")

    second = await run_tracker_projection_pass(session, CONFIG)
    await flush_tracker_projections(second, client_factory=provider)

    assert second.projections_stale == 0
    assert second.projections_written == 1
    assert "needs your decision" not in provider.body
    # The human's text is still there — the repair did not widen what gets written.
    assert provider.body.startswith(INTENT_ABOVE)
    assert provider.body.endswith(INTENT_BELOW)


async def test_mixed_timestamp_awareness_does_not_break_the_projection(session):
    """A naive `updated_at` beside an aware `created_at` must still render.

    Not hypothetical: `updated_at` is a Python-side `onupdate`, so after SQLAlchemy
    expires and reloads it the driver decides its awareness — Postgres `TIMESTAMPTZ`
    returns aware, SQLite naive. Comparing the two raises `TypeError`, and because the
    pass catches per-flow exceptions that surfaced as the flow being counted as an
    error and skipped: a tracker silently stuck at kickoff, which is the defect itself.

    **Every timestamp this test depends on is pinned** (review finding, PR #5337).
    `_observed_at` takes the max over the node's `updated_at`/`created_at` *and* the
    flow's `created_at`, and both `created_at` columns default to `utcnow` — so a
    version of this test that pinned only `updated_at` asserted on the rendered time
    only while the wall clock happened to sit behind the hardcoded literal, and turned
    permanently red the moment real time passed it. It did: `2026-09-17 23:25:00` went
    red at 23:25 UTC on 2026-09-17. A test that expires on a date is worse than no
    test, because it fails long after the change that would explain it and reads as a
    regression in whatever is being reviewed that day. The dates below are in the
    settled past and the naive stamp is the newest of the three by construction, so
    the assertion turns only on the awareness normalisation it was written for.
    """
    flow = await _flow(session, states=[NodeState.PASSED.value])
    flow.created_at = datetime(2026, 9, 1, 9, 0, 0, tzinfo=UTC)
    node = (await _nodes(session))[0]
    node.created_at = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)  # aware, as the default writes it
    node.updated_at = datetime(2026, 9, 1, 11, 30, 0)  # naive, as SQLite returns it
    await session.flush()

    report = await run_tracker_projection_pass(session, CONFIG)

    assert report.errors == 0
    assert len(report.pending) == 1
    # The naive value, read as UTC rather than local, and preferred over the two aware
    # ones because it is genuinely the latest — which is the comparison that used to
    # raise.
    assert "2026-09-01 11:30:00" in report.pending[0].body_region


async def test_one_unrenderable_flow_does_not_stop_the_others(session, monkeypatch):
    """A render failure is counted and skipped; every other tenant still projects."""
    await _flow(session, org_id=ORG_A, epic=4910, slug="flow-a")
    await _flow(session, org_id=ORG_B, epic=5555, slug="flow-b")
    real = render_region

    def _explode(**kwargs):
        if kwargs["flow"].slug == "flow-a":
            raise RuntimeError("render failed for this tenant only")
        return real(**kwargs)

    monkeypatch.setattr("src.orchestration.tracker_projection.render_region", _explode)

    report = await run_tracker_projection_pass(session, CONFIG)

    assert report.errors == 1
    assert [p.issue_number for p in report.pending] == [5555]


async def test_the_cap_is_reported_rather_than_silently_truncating(session):
    """Running out of budget must never read as "everything is up to date"."""
    for index in range(3):
        await _flow(session, org_id=f"org-{index}", epic=5000 + index, slug=f"flow-{index}")

    report = await run_tracker_projection_pass(session, TrackerProjectionConfig(enabled=True, repo=REPO, max_flows_per_pass=2))

    assert report.capped is True
    assert len(report.pending) == 2


# --------------------------------------------------------------------------
# Which flows the cap keeps (#5337 review finding)
#
# The cap decides *which* flows are projected, so the ordering behind it is a
# correctness property, not a performance detail: a flow the cap never reaches has a
# frozen tracker while the tick stays green, which is the #5284 defect itself.
#
# These two tests are a discriminating pair, for the reason the module docstring gives
# about the staleness guard. `test_a_flow_that_advanced_is_projected_before_idle_ones`
# alone would pass against an implementation that ordered by anything correlated with
# recency, and `test_an_idle_flow_does_not_permanently_starve_an_active_one` alone
# would pass against one that simply raised the cap. Only together do they pin
# "ordered by last activity, descending".
# --------------------------------------------------------------------------


async def _flow_at(session, *, flow_id: str, epic: int, slug: str, moved_at: datetime | None, state: str = NodeState.RUNNING.value):
    """A flow whose id and last-activity time are both pinned.

    Both are set explicitly because the defect was an ordering one: the ids must not
    be random (the bug was invisible whenever the UUIDs happened to sort favourably —
    it reproduced on some seeds and not others) and the timestamps must not be "now"
    for every row, or every ordering looks alike.
    """
    flow = OrchestrationFlow(id=flow_id, org_id=ORG_A, slug=slug, title="AI-DLC engine", intent_ref="4191")
    session.add(flow)
    await session.flush()
    session.add(
        OrchestrationNode(
            org_id=ORG_A,
            flow_id=flow.id,
            epic_ref=f"epic-{epic}",
            wave_ref="wave-1",
            node_ref="U1",
            kind=NodeKind.STORY.value,
            state=state,
            title="Story 1",
            issue_ref="5001",
            attempts=1,
            updated_at=moved_at,
        )
    )
    session.add(OrchestrationAcceptedPlan(org_id=ORG_A, flow_id=flow.id, version=1, plan_document={"nodes": []}, plan_hash="f" * 64))
    session.add(
        OrchestrationDecision(
            org_id=ORG_A,
            flow_id=flow.id,
            kind=DecisionKind.PLAN_ACCEPTED.value,
            actor_id="approver",
            actor_role="admin",
            actor_kind="human",
        )
    )
    await session.flush()
    return flow


async def test_a_flow_that_advanced_is_projected_before_idle_ones(session, monkeypatch):
    """The flow that just moved is the one the cap must keep.

    Fails if the ordering is dropped or reversed: the recently-advanced flow is given
    the *highest* id, so any ordering by id alone puts it last and the assertion sees
    an idle flow instead.
    """
    for index in range(3):
        await _flow_at(
            session,
            flow_id=f"00000000-0000-0000-0000-00000000000{index}",
            epic=9100 + index,
            slug=f"idle-{index}",
            moved_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        )
    await _flow_at(
        session,
        flow_id="ffffffff-ffff-ffff-ffff-ffffffffffff",
        epic=9999,
        slug="just-advanced",
        moved_at=datetime(2026, 9, 17, 23, 25, tzinfo=UTC),
    )

    monkeypatch.setattr("src.orchestration.tracker_projection._rotation_slot", lambda: 0)
    report = await run_tracker_projection_pass(session, TrackerProjectionConfig(enabled=True, repo=REPO, max_flows_per_pass=2))

    assert report.capped is True
    assert report.pending[0].issue_number == 9999


async def test_an_idle_flow_does_not_permanently_starve_an_active_one(session, monkeypatch):
    """A starved flow must project as soon as it advances — on the next tick.

    The original defect was not that a flow waited, but that it waited *forever*:
    ordering by an immutable random id meant the same flows lost every tick. This runs
    the pass twice with the same cap and asserts the second tick follows the motion.
    """
    idle = await _flow_at(
        session,
        flow_id="00000000-0000-0000-0000-000000000001",
        epic=9100,
        slug="idle",
        moved_at=datetime(2026, 9, 17, 23, 0, tzinfo=UTC),
    )
    await _flow_at(
        session,
        flow_id="00000000-0000-0000-0000-000000000002",
        epic=9101,
        slug="newer-idle",
        moved_at=datetime(2026, 9, 17, 23, 1, tzinfo=UTC),
    )
    active = await _flow_at(
        session,
        flow_id="ffffffff-ffff-ffff-ffff-ffffffffffff",
        epic=9999,
        slug="active",
        moved_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )
    # A fixed slot chosen so the *first* pass rotates to `idle` and leaves `active`
    # starved, which is the precondition the assertions below need. Any slot whose
    # rotation start is 0 does that; this used to be slot 0 because the start was
    # `slot * rotation_count`, and it is 2 now that the start is mixed (F1, #5337).
    # The behaviour under test is the second pass following the motion, which does
    # not depend on which slot this is.
    monkeypatch.setattr("src.orchestration.tracker_projection._rotation_slot", lambda: 2)
    config = TrackerProjectionConfig(enabled=True, repo=REPO, max_flows_per_pass=2)

    first = await run_tracker_projection_pass(session, config)
    assert first.capped is True
    assert 9100 in [p.issue_number for p in first.pending]
    assert 9999 not in [p.issue_number for p in first.pending], "precondition: the active flow must start starved"

    # The starved flow now advances, exactly as a transition would leave it.
    node = (await session.execute(select(OrchestrationNode).where(OrchestrationNode.flow_id == active.id))).scalar_one()
    node.state = NodeState.PASSED.value
    node.updated_at = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    await session.flush()

    second = await run_tracker_projection_pass(session, config)

    assert second.capped is True
    assert second.pending[0].issue_number == 9999, "a flow that advanced was still starved by idle ones"
    assert idle.id != active.id


async def test_a_capped_terminal_flow_is_eventually_projected_without_advancing_again(session, monkeypatch):
    """The cap must delay a final snapshot, never strand it forever.

    More flows than the cap can finish in one tick.  A flow omitted from that
    tick has no later state change to move it back to the front, so repeatedly
    selecting the same most-recent rows leaves its tracker permanently stale.
    """
    for index in range(3):
        await _flow_at(
            session,
            flow_id=f"00000000-0000-0000-0000-00000000000{index}",
            epic=7000 + index,
            slug=f"finished-{index}",
            moved_at=datetime(2026, 9, 18, 8, index, tzinfo=UTC),
            state=NodeState.PASSED.value,
        )

    config = TrackerProjectionConfig(enabled=True, repo=REPO, max_flows_per_pass=2)
    examined: set[int] = set()
    slots = iter(range(3))
    monkeypatch.setattr("src.orchestration.tracker_projection._rotation_slot", lambda: next(slots))
    for _ in range(3):
        report = await run_tracker_projection_pass(session, config)
        examined.update(p.issue_number for p in report.pending)

    assert examined == {7000, 7001, 7002}


def test_rotation_is_fair_at_every_tick_cadence():
    """Fairness must not depend on the tick interval, which is a supported knob.

    Review finding F1. `_rotation_slot` divides the wall clock by a hardcoded five
    minutes, but `orchestration_tick_schedule` -> `tick_schedule` is a documented
    Terraform setting whose description invites a slower cadence. At a cadence of
    `k` times five minutes the slot advances by `k` per pass, so this walks the
    slot in steps larger than one -- the case `test_a_capped_terminal_flow_...`
    cannot see, because it steps by consecutive integers, which is exactly why the
    defect survived the suite that was written for it.

    Fails against the previous `(slot * rotation_count) % pool_size`: the reachable
    starts are then the multiples of `gcd(rotation_count * k, pool_size)` and the
    flows outside that subgroup are never selected at all. The consequence is the
    permanent staleness this module exists to remove, with a green tick and no
    error, since a skipped projection is deliberately not a failure.

    Also fails against `slot % pool_size`, the tempting one-line repair, which only
    reduces starvation rather than removing it.
    """
    for step in (2, 3, 6, 60):
        for cap in (2, 3, 5, 20):
            for pool_size in (cap + 1, cap + 3, 2 * cap + 1, 79):
                flow_ids = [f"flow-{index:04d}" for index in range(pool_size)]
                seen: set[str] = set()
                for tick in range(1200):
                    seen.update(_select_capped_flows(flow_ids, cap=cap, slot=tick * step))
                missed = sorted(set(flow_ids) - seen)
                assert not missed, f"cadence step={step}, cap={cap}, pool={pool_size}: {len(missed)} flow(s) never projected in 1200 ticks"


def test_the_rotation_start_is_stable_for_a_given_slot():
    """Two processes ticking the same slot must agree, or they fight over the region.

    Concurrent tick Lambdas hold a per-issue advisory lock, so they cannot interleave
    one write; what they must not do is *disagree about which flows to project* for
    the same slot, which would make consecutive passes take turns publishing different
    subsets. That needs the mapping to be reproducible across processes.

    Pinned as literal expected values rather than by comparing the function to itself
    -- a self-comparison holds inside one process even for `hash()` on a `str`, which
    is per-process salted, so it would not notice a change to a non-reproducible
    mapping. These constants are what make the property testable; they are the
    splitmix64 finalizer's output and carry no other significance, so a deliberate
    change of mixing function is expected to update them.
    """
    assert _mixed_start(0, 2) == 1
    assert _mixed_start(0, 40) == 15
    assert _mixed_start(1, 40) == 25
    assert _mixed_start(12345, 40) == 24
    assert _mixed_start(12345, 79) == 9
    assert _mixed_start(999999999, 7) == 5

    flow_ids = [f"flow-{index:04d}" for index in range(40)]
    assert _select_capped_flows(flow_ids, cap=6, slot=12345) == _select_capped_flows(flow_ids, cap=6, slot=12345)


async def test_a_flow_whose_nodes_never_moved_is_still_a_candidate(session):
    """`updated_at` is NULL until the first update, which must not sort a flow away.

    Fails against an ordering that does not coalesce: a freshly-registered flow has
    NULL on every node, and on a NULLS-LAST driver it would lose to any flow that has
    ever moved — including flows that finished long ago.
    """
    await _flow_at(
        session,
        flow_id="00000000-0000-0000-0000-000000000001",
        epic=9100,
        slug="long-finished",
        moved_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        state=NodeState.PASSED.value,
    )
    await _flow_at(
        session,
        flow_id="00000000-0000-0000-0000-000000000002",
        epic=9101,
        slug="also-long-finished",
        moved_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        state=NodeState.PASSED.value,
    )
    await _flow_at(session, flow_id="ffffffff-ffff-ffff-ffff-ffffffffffff", epic=9999, slug="brand-new", moved_at=None)

    report = await run_tracker_projection_pass(session, TrackerProjectionConfig(enabled=True, repo=REPO, max_flows_per_pass=2))

    assert report.pending[0].issue_number == 9999


# --------------------------------------------------------------------------
# The flush — idempotence, staleness, concurrency, failure isolation
# --------------------------------------------------------------------------


async def _pending(session, **kwargs) -> TrackerProjectionReport:
    await _flow(session, **kwargs)
    return await run_tracker_projection_pass(session, CONFIG)


async def test_the_write_lands_in_the_region_and_preserves_the_rest(session):
    """The end-to-end shape of the fix: kickoff text out, live execution state in."""
    report = await _pending(session, states=[NodeState.PASSED.value, NodeState.RUNNING.value])
    provider = StubProvider(_body("Kickoff: U1 just dispatched, everything else waiting."))

    await flush_tracker_projections(report, client_factory=provider)

    assert report.projections_written == 1
    assert provider.body.startswith(INTENT_ABOVE)
    assert provider.body.endswith(INTENT_BELOW)
    assert "U1 just dispatched" not in provider.body
    assert "1/2 stories passed" in provider.body
    assert report.success is True


async def test_unchanged_render_writes_nothing(session):
    """Repeated identical work is idempotent (AC1) — proven, not asserted.

    The second flush must attempt no write at all, not merely write the same bytes.
    An issue edited on every tick floods its watchers and buries real changes in
    no-op revisions.
    """
    report = await _pending(session)
    provider = StubProvider(_body("kickoff"))
    await flush_tracker_projections(report, client_factory=provider)
    assert len(provider.writes) == 1

    again = await run_tracker_projection_pass(session, CONFIG)
    await flush_tracker_projections(again, client_factory=provider)

    assert len(provider.writes) == 1
    assert (again.projections_unchanged, again.projections_written) == (1, 0)


async def test_stale_snapshot_is_refused(session):
    """A newer snapshot already on the issue is never regressed (AC2/AC3).

    Fails if the version/watermark comparison is removed. Its sibling below fails if
    that comparison is inverted, so neither passes against code that never writes.
    """
    report = await _pending(session, states=[NodeState.RUNNING.value])
    pending = report.pending[0]
    newer = f"<!-- aidlc-tracker-snapshot: v{pending.version} w{pending.watermark + 10} -->\nA newer truth."
    provider = StubProvider(_body(newer))

    await flush_tracker_projections(report, client_factory=provider)

    assert provider.writes == []
    assert report.projections_stale == 1
    assert "A newer truth." in provider.body
    # Declining to go backwards is the correct outcome, so the pass is still healthy.
    assert report.success is True


async def test_a_snapshot_quoted_in_human_text_does_not_look_newer(session):
    """A marker outside the sentinels is prose, not a published snapshot.

    Review finding, PR #5337. Quoting last week's tracker text in one's own notes is
    an ordinary edit, but the staleness check used to search the whole body, so a
    quoted high watermark made every real update read as stale — a refusal, which is
    not a failure, so the tick stayed green while the region froze permanently.

    Discriminating in both directions: it fails against a whole-body search (nothing
    is written), and the write assertions fail against code that never writes.
    """
    report = await _pending(session, states=[NodeState.RUNNING.value])
    pending = report.pending[0]
    quoted = f"<!-- aidlc-tracker-snapshot: v{pending.version} w{pending.watermark + 500} -->"
    provider = StubProvider(f"Last week the tracker read:\n{quoted}\n" + _body("kickoff"))

    await flush_tracker_projections(report, client_factory=provider)

    assert report.projections_stale == 0
    assert report.projections_written == 1
    # The human's quoted line is theirs, so it is still there afterwards.
    assert quoted in provider.body


async def test_a_flow_marker_quoted_in_human_text_is_not_a_binding(session):
    """The region's owner is the flow marker the engine wrote, not one a human pasted.

    Same root cause as the test above, other reader: a flow marker copied from a
    sibling EPIC used to make the issue look bound to a different flow, refusing this
    flow's updates forever.
    """
    report = await _pending(session, states=[NodeState.RUNNING.value])
    provider = StubProvider("Copied from a sibling EPIC:\n<!-- aidlc-tracker-flow: some-other-flow -->\n" + _body("kickoff"))

    await flush_tracker_projections(report, client_factory=provider)

    assert report.projections_refused == 0
    assert report.projections_written == 1


async def test_overlapping_ticks_cannot_let_the_older_patch_land_last():
    """Serialize the complete read/compare/write, not only the marker check.

    The provider deliberately creates the harmful order: the older writer reads and
    pauses; the newer writer starts; then the older writer resumes. Without the
    target lock both read the kickoff body, the newer PATCH lands, and the older
    PATCH regresses it. With the lock the newer writer cannot read until the older
    one releases, so its newer snapshot necessarily lands last.
    """

    base = PendingTrackerProjection(
        org_id=ORG_A,
        flow_id="flow",
        repo=REPO,
        issue_number=EPIC,
        installation_id=INSTALLATION,
        body_region="<!-- aidlc-tracker-snapshot: v1 w1 -->\nOlder snapshot",
        version=1,
        watermark=1,
    )
    newer = replace(
        base,
        body_region="<!-- aidlc-tracker-snapshot: v1 w2 -->\nNewer snapshot",
        watermark=2,
    )

    class RemoteIssue:
        def __init__(self) -> None:
            self.body = _body("Kickoff")
            self.older_read = asyncio.Event()
            self.release_older = asyncio.Event()

    class RacingProvider:
        def __init__(self, remote: RemoteIssue, *, older: bool) -> None:
            self.remote = remote
            self.older = older

        async def read_issue_body(self, **_kwargs):
            captured = self.remote.body
            if self.older:
                self.remote.older_read.set()
                await self.remote.release_older.wait()
            return captured

        async def write_issue_body(self, *, body, **_kwargs):
            self.remote.body = body

    remote = RemoteIssue()
    older_report = TrackerProjectionReport(pending=[base])
    newer_report = TrackerProjectionReport(pending=[newer])
    older_task = asyncio.create_task(flush_tracker_projections(older_report, client_factory=RacingProvider(remote, older=True)))
    await remote.older_read.wait()
    newer_task = asyncio.create_task(flush_tracker_projections(newer_report, client_factory=RacingProvider(remote, older=False)))

    # Give an unlocked newer writer a full turn to read and PATCH before allowing
    # the older one to continue. Under the target lock it is still waiting to read.
    await asyncio.sleep(0)
    remote.release_older.set()
    await asyncio.gather(older_task, newer_task)

    assert read_snapshot(remote.body) == (1, 2)
    assert "Newer snapshot" in remote.body
    assert (older_report.projections_written, newer_report.projections_written) == (1, 1)


async def test_postgres_lock_is_acquired_and_released_on_failure():
    """The production lock path uses PostgreSQL and cannot leak after an error."""

    calls: list[str] = []

    class Dialect:
        name = "postgresql"

    class Bind:
        dialect = Dialect()

    class LockSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            calls.append("close")

        async def execute(self, statement):
            calls.append(str(statement))

        async def rollback(self):
            calls.append("rollback")

    class Factory:
        kw = {"bind": Bind()}

        def __call__(self):
            return LockSession()

    pending = PendingTrackerProjection(
        org_id=ORG_A,
        flow_id="flow",
        repo=REPO,
        issue_number=EPIC,
        installation_id=INSTALLATION,
        body_region="region",
        version=1,
        watermark=1,
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        async with _projection_write_lock(pending, session_factory=Factory()):  # type: ignore[arg-type]
            calls.append("inside")
            raise RuntimeError("provider failed")

    assert "pg_advisory_xact_lock" in calls[0]
    assert calls[1:] == ["inside", "rollback", "close"]


async def test_a_supplied_provider_still_gets_the_database_lock(monkeypatch, engine):
    """A caller's provider choice must not decide how writes are serialized.

    Reviewer regression, PR #5337. The lock factory was previously resolved only
    when `client_factory` was *also* absent, so any caller that supplied a provider
    silently got the process-local fallback instead of the database lock. That is
    invisible in this suite because the tests run on SQLite, where both branches
    produce a process-local lock — so lock *behaviour* cannot discriminate the two.
    What discriminates them is whether the flush resolves a factory at all, which is
    what this asserts. Without it, restoring the `and client_factory is None`
    conjunction passes every other test in this file.
    """
    resolved = 0
    real = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    def _factory():
        nonlocal resolved
        resolved += 1
        return real

    monkeypatch.setattr("src.shared.database.get_session_factory", _factory)

    report = TrackerProjectionReport(
        pending=[
            PendingTrackerProjection(
                org_id=ORG_A,
                flow_id="flow",
                repo=REPO,
                issue_number=EPIC,
                installation_id=INSTALLATION,
                body_region="<!-- aidlc-tracker-snapshot: v1 w1 -->\nAnything",
                version=1,
                watermark=1,
            )
        ]
    )

    # Counting the resolution is the observation: it proves the flush consulted the
    # database module for a lock factory rather than falling through to the
    # process-local one. The write itself is expected to succeed normally.
    provider = StubProvider(_body("Old"))
    await flush_tracker_projections(report, client_factory=provider)

    assert resolved == 1
    assert report.projections_written == 1
    assert report.success is True


async def test_older_snapshot_is_overwritten(session):
    """The discriminating other half: an older marker on the issue IS replaced."""
    report = await _pending(session, states=[NodeState.PASSED.value])
    pending = report.pending[0]
    provider = StubProvider(_body(f"<!-- aidlc-tracker-snapshot: v{pending.version} w0 -->\nAn older truth."))

    await flush_tracker_projections(report, client_factory=provider)

    assert len(provider.writes) == 1
    assert report.projections_written == 1
    assert "An older truth." not in provider.body


async def test_a_persona_written_region_with_no_marker_is_replaced(session):
    """The very first execution-time projection must be able to land — the actual bug.

    The inception persona emits no snapshot marker, so an absent marker has to read
    as "older than anything". Treating it as unknown-and-therefore-unsafe would
    reproduce the reported defect exactly.
    """
    report = await _pending(session, states=[NodeState.PASSED.value])
    provider = StubProvider(_body("U1 just dispatched — kickoff inventory, no marker."))

    await flush_tracker_projections(report, client_factory=provider)

    assert report.projections_written == 1
    assert "no marker" not in provider.body


async def test_a_newer_accepted_version_outranks_a_higher_watermark(session):
    """Version leads the comparison, so an amended plan is never stale-blocked (AC2).

    After an amendment the node set changes, so the old plan's watermark is not
    comparable — a bigger number under v1 must not veto a v4 snapshot.
    """
    report = await _pending(session, states=[NodeState.PASSED.value], version=4)
    pending = report.pending[0]
    assert pending.version == 4
    provider = StubProvider(_body(f"<!-- aidlc-tracker-snapshot: v1 w{pending.watermark + 999} -->\nUnder the old plan."))

    await flush_tracker_projections(report, client_factory=provider)

    assert report.projections_written == 1
    assert "Under the old plan." not in provider.body


async def test_two_flows_cannot_claim_the_same_tracker_region(session):
    """Counters from two approved flows targeting one EPIC are not comparable."""
    await _flow(session, org_id=ORG_A, epic=EPIC, slug="flow-a")
    await _flow(session, org_id=ORG_A, epic=EPIC, slug="flow-b")
    report = await run_tracker_projection_pass(session, CONFIG)
    provider = StubProvider(_body("kickoff"))

    await flush_tracker_projections(report, client_factory=provider)

    assert len(report.pending) == 2
    assert len(provider.writes) == 1
    assert (report.projections_written, report.projections_refused) == (1, 1)
    assert any(f"aidlc-tracker-flow: {pending.flow_id}" in provider.body for pending in report.pending)


async def test_edit_made_before_the_read_is_preserved(session):
    """A human edit observed by the provider read survives the projection.

    This is the read-modify-write property that matters: the body written is derived
    from the body just read, so anything a human added is carried through. An
    implementation that wrote a cached or reconstructed body would drop it.
    """
    report = await _pending(session)
    provider = StubProvider(_body("kickoff"))
    await flush_tracker_projections(report, client_factory=provider)

    # A human edits the issue between ticks, outside the sentinels.
    provider.body = provider.body.replace(INTENT_BELOW, INTENT_BELOW + "\n\nBlocked on the security review — Dana")

    for node in await _nodes(session):
        node.state = NodeState.PASSED.value
    await session.flush()
    later = await run_tracker_projection_pass(session, CONFIG)
    await flush_tracker_projections(later, client_factory=provider)

    assert later.projections_written == 1
    assert "Blocked on the security review — Dana" in provider.body
    assert "1/1 stories passed" in provider.body


async def test_missing_sentinels_on_the_live_issue_refuse_the_write(session):
    """An issue with no region is left completely alone (AC2)."""
    report = await _pending(session)
    original = "An EPIC issue that was never initialised with a tracker region."
    provider = StubProvider(original)

    await flush_tracker_projections(report, client_factory=provider)

    assert provider.writes == []
    assert provider.body == original
    assert report.projections_refused == 1
    assert report.success is True


async def test_a_duplicated_region_refuses_rather_than_picking_one(session):
    """Two regions means two writers disagree; a human needs to look."""
    report = await _pending(session)
    provider = StubProvider(_body("one") + _body("two"))

    await flush_tracker_projections(report, client_factory=provider)

    assert provider.writes == []
    assert report.projections_refused == 1


async def test_a_provider_read_failure_is_counted_and_never_raises(session):
    """AC3: a provider failure cannot propagate out of the flush and fail the tick."""
    report = await _pending(session)
    provider = StubProvider(_body("kickoff"), fail_read=True)

    await flush_tracker_projections(report, client_factory=provider)

    assert report.projections_failed == 1
    assert report.success is False
    assert provider.writes == []


async def test_a_provider_write_failure_is_counted_and_never_raises(session):
    report = await _pending(session)
    provider = StubProvider(_body("kickoff"), fail_write=True)

    await flush_tracker_projections(report, client_factory=provider)

    assert report.projections_failed == 1
    assert report.success is False


async def test_a_transient_failure_converges_on_the_next_tick(session):
    """AC3: a later success reaches current state; the update is delayed, not lost."""
    report = await _pending(session, states=[NodeState.RUNNING.value])
    provider = StubProvider(_body("kickoff"), fail_write=True)
    await flush_tracker_projections(report, client_factory=provider)
    assert report.projections_failed == 1

    # The engine moved on while GitHub was unavailable.
    for node in await _nodes(session):
        node.state = NodeState.PASSED.value
    await session.flush()
    provider.fail_write = False

    recovered = await run_tracker_projection_pass(session, CONFIG)
    await flush_tracker_projections(recovered, client_factory=provider)

    assert recovered.projections_written == 1
    # Current state, not a replay of the snapshot that failed to send.
    assert "1/1 stories passed" in provider.body


async def test_one_failing_issue_does_not_stop_the_others(session):
    """Per-projection containment: the second tenant still gets its update."""
    await _flow(session, org_id=ORG_A, epic=4910, slug="flow-a")
    await _flow(session, org_id=ORG_B, epic=5555, slug="flow-b")
    report = await run_tracker_projection_pass(session, CONFIG)
    assert len(report.pending) == 2
    written: list[int] = []

    class Selective(StubProvider):
        async def read_issue_body(self, *, org_id, installation_id, repo, issue_number):
            if issue_number == 4910:
                raise RuntimeError("this tenant's issue is unreachable")
            return _body("kickoff")

        async def write_issue_body(self, *, org_id, installation_id, repo, issue_number, body):
            written.append(issue_number)

    await flush_tracker_projections(report, client_factory=Selective(""))

    assert written == [5555]
    assert (report.projections_failed, report.projections_written) == (1, 1)


async def test_a_failed_projection_leaves_every_node_state_untouched(session):
    """AC3, stated as a property rather than argued.

    The flush runs after the transition commit and its separate advisory-lock session
    touches no engine rows, so this is structural — but it is the whole safety claim
    of the story, so it is asserted.
    """
    await _flow(session, states=[NodeState.RUNNING.value, NodeState.PASSED.value])
    before = {n.id: (n.state, n.attempts) for n in await _nodes(session)}
    decisions_before = await _decision_ids(session)

    report = await run_tracker_projection_pass(session, CONFIG)
    await flush_tracker_projections(report, client_factory=StubProvider(_body("kickoff"), fail_write=True))

    session.expire_all()
    assert {n.id: (n.state, n.attempts) for n in await _nodes(session)} == before
    assert report.projections_failed == 1
    assert await _decision_ids(session) == decisions_before


async def test_a_flush_with_nothing_pending_does_no_io(session):
    """A quiet tick must not touch the provider at all."""
    provider = StubProvider(_body("kickoff"))
    await flush_tracker_projections(TrackerProjectionReport(), client_factory=provider)
    assert (provider.reads, provider.writes) == ([], [])


async def test_counters_are_recorded_per_tenant(session):
    """An operator needs to know *whose* projection is failing, not just that one is."""
    await _flow(session, org_id=ORG_A, epic=4910, slug="flow-a")
    await _flow(session, org_id=ORG_B, epic=5555, slug="flow-b")
    report = await run_tracker_projection_pass(session, CONFIG)

    class PerIssueProvider:
        """A real provider returns a separate body for each issue target."""

        async def read_issue_body(self, **_kwargs):
            return _body("kickoff")

        async def write_issue_body(self, **_kwargs):
            return None

    await flush_tracker_projections(report, client_factory=PerIssueProvider())

    assert report.per_org[ORG_A]["projections_written"] == 1
    assert report.per_org[ORG_B]["projections_written"] == 1
    assert report.per_org[ORG_A]["flows_examined"] == 1


# --------------------------------------------------------------------------
# Author-supplied text must not be able to forge the region's own structure
# (#5337 review finding)
#
# `title`, `issue_ref` and `flow.slug` are stored verbatim by plan registration, and
# every one of them is interpolated into the region. A sentinel that survives into the
# rendered text lands once and then makes `splice_region` answer `region_duplicated`
# forever — a refusal that is deliberately NOT a failure, so the tick stays green and
# the tracker is frozen with nobody told. The poison lives on the node row, so it
# re-renders even after a human repairs the issue body by hand.
#
# These go through `run_tracker_projection_pass` + the flush rather than calling
# `render_region` directly, because the property is about what reaches the issue over
# two ticks, and a test of the renderer alone cannot see the second one.
# --------------------------------------------------------------------------


async def test_a_forged_end_sentinel_in_a_title_cannot_duplicate_the_region(session):
    """The whole failure: land once, then refuse forever.

    Fails against the unneutralized render, which produces a body carrying two end
    sentinels and a second pass that writes nothing while reporting success.
    """
    await _flow(session, states=[NodeState.RUNNING.value], title=f"Fix tracker {REGION_END} now")

    provider = StubProvider(_body("kickoff"))
    first = await run_tracker_projection_pass(session, CONFIG)
    await flush_tracker_projections(first, client_factory=provider)

    assert first.projections_written == 1
    # The sentinels still bound exactly one region, which is what keeps the next tick
    # able to find it.
    assert provider.body.count(REGION_START) == 1
    assert provider.body.count(REGION_END) == 1
    assert REGION_END not in provider.body[provider.body.index(REGION_START) + len(REGION_START) : provider.body.index(REGION_END)]

    # And the second tick can still write, which is the part the counters hide.
    await _transition(session, (await _nodes(session))[0], NodeState.PASSED.value, kind="result_checked")
    second = await run_tracker_projection_pass(session, CONFIG)
    await flush_tracker_projections(second, client_factory=provider)
    assert (second.projections_written, second.projections_refused) == (1, 0)


async def test_a_forged_start_sentinel_in_an_issue_ref_is_neutralized(session):
    """`issue_ref` is 64 chars and was not escaped at all — a start sentinel fits."""
    await _flow(session, states=[NodeState.RUNNING.value], issue_ref=f"1 {REGION_START}")

    provider = StubProvider(_body("kickoff"))
    report = await run_tracker_projection_pass(session, CONFIG)
    await flush_tracker_projections(report, client_factory=provider)

    assert report.projections_written == 1
    assert provider.body.count(REGION_START) == 1
    assert provider.body.count(REGION_END) == 1


async def test_the_engines_own_snapshot_marker_is_the_one_read_back(session):
    """Pins the *ordering* that makes marker forgery unexploitable — it is not proof of
    the neutralizer, and says so.

    Marker forgery via a node title is blocked twice over, and verified by mutation to
    be blocked by *either* guard alone: the neutralizer stops the marker rendering at
    all, and `read_snapshot` takes the first match while the engine emits its own
    marker as the region's first line, so a marker in a table row below it is never the
    one compared. Removing either alone leaves this passing; removing both fails it.

    It is kept precisely because it is the only thing that checks the second guard —
    an ordering property that is load-bearing and entirely implicit. Move the marker
    below the table for any presentational reason and the neutralizer becomes the sole
    defense, with nothing else in the suite noticing the drop from two to one.
    """
    await _flow(session, states=[NodeState.RUNNING.value], title="Fix <!-- aidlc-tracker-snapshot: v9 w9999 --> now")

    provider = StubProvider(_body("kickoff"))
    first = await run_tracker_projection_pass(session, CONFIG)
    await flush_tracker_projections(first, client_factory=provider)
    assert first.projections_written == 1

    # The marker the body now carries is the engine's own, not the forged one.
    assert read_snapshot(provider.body) == (first.pending[0].version, first.pending[0].watermark)

    await _transition(session, (await _nodes(session))[0], NodeState.PASSED.value, kind="result_checked")
    second = await run_tracker_projection_pass(session, CONFIG)
    await flush_tracker_projections(second, client_factory=provider)
    assert (second.projections_written, second.projections_stale) == (1, 0)


async def test_a_newline_in_a_title_cannot_forge_a_status_row(session):
    """Row-breaking is misrepresentation, not a cosmetic defect.

    An unescaped newline lets an author add a row to a table captioned "written by
    the engine from its own records", claiming a story passed and naming a PR — read
    by the person about to answer a gate.
    """
    await _flow(
        session,
        states=[NodeState.RUNNING.value],
        title="innocent\n| #4192 shipped | ✅ passed | #998 |",
    )

    provider = StubProvider(_body("kickoff"))
    report = await run_tracker_projection_pass(session, CONFIG)
    await flush_tracker_projections(report, client_factory=provider)

    assert report.projections_written == 1
    region = provider.body[provider.body.index(REGION_START) : provider.body.index(REGION_END)]
    rows = [line for line in region.splitlines() if line.startswith("|")]
    # One story row plus the two table-shape rows: the forged row did not become one.
    assert len(rows) == 3
    # The forged text survives as *content of one cell* — pipes escaped, newline
    # folded — which is the correct outcome. What must not happen is a second data
    # row asserting a status, so the claim is checked against the row structure
    # rather than against the substring: the story's real status is still `running`,
    # and no row says otherwise.
    # Escaped pipes are not cell boundaries, so only the unescaped ones are counted:
    # four delimiters means exactly the three intended columns.
    assert rows[2].count("|") - rows[2].count("\\|") == 4
    assert "🔄 running" in rows[2]
    assert not any(row.strip().endswith("| ✅ passed | #998 |") for row in rows)


def test_neutralizing_leaves_ordinary_titles_byte_identical():
    """The fix must not cost legibility, or it gets reverted for a real reason."""
    at = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    flow = OrchestrationFlow(org_id=ORG_A, slug=FLOW_SLUG, title="AI-DLC engine")
    node = OrchestrationNode(
        org_id=ORG_A,
        flow_id="f",
        epic_ref=f"epic-{EPIC}",
        wave_ref="wave-1",
        node_ref="U1",
        kind=NodeKind.STORY.value,
        state=NodeState.RUNNING.value,
        title="Add rate limits to /v1/chat (see [PR #123]) — 50% faster",
        issue_ref="5001",
    )
    region = render_region(flow=flow, nodes=[node], bindings={}, version=1, watermark=1, observed_at=at)
    assert "Add rate limits to /v1/chat (see [PR #123]) — 50% faster" in region
    assert FLOW_SLUG in region


# --------------------------------------------------------------------------
# The write target must be authorized, not merely named (#5337 review finding)
#
# The EPIC number is parsed out of `epic_ref`, which plan registration stores verbatim
# from the author's node address, and the repository is one process-wide variable. So
# "which issue does the engine write?" was answered entirely by author-supplied data.
# `PLAN_DRAFT` reaches every ordinary member (admin/config.py), and draft registration
# records `PLAN_DRAFTED` — deliberately absent from `APPROVAL_DECISION_KINDS` — while
# still compiling nodes and an accepted-plan row. Projection now requires the same
# human approval that arms dispatch.
# --------------------------------------------------------------------------


async def test_an_unapproved_draft_is_never_projected(session):
    """A draft naming someone else's EPIC must not reach that issue.

    Fails if the approval check is removed: the draft is otherwise a perfectly
    projectable flow, so the guard is the only thing standing between it and a write.
    """
    await _flow(session, states=[NodeState.RUNNING.value], approved=False)

    provider = StubProvider(_body("kickoff"))
    report = await run_tracker_projection_pass(session, CONFIG)
    await flush_tracker_projections(report, client_factory=provider)

    assert report.pending == []
    assert (report.projections_refused, report.projections_written) == (1, 0)
    # Asserted as "no write attempted", not as a counter: a refusal and a no-op are
    # indistinguishable in the resulting body.
    assert provider.writes == []
    assert provider.body == _body("kickoff")


async def test_a_draft_decision_alone_does_not_authorize_a_projection(session):
    """`PLAN_DRAFTED` is a decision row, but it is not an approval.

    Fails if the check is loosened to "any decision exists" — which is the natural
    way to get this wrong, since a draft does append a row.
    """
    flow = await _flow(session, states=[NodeState.RUNNING.value], approved=False)
    session.add(
        OrchestrationDecision(
            org_id=ORG_A,
            flow_id=flow.id,
            kind=DecisionKind.PLAN_DRAFTED.value,
            actor_id="member",
            actor_role="member",
            actor_kind="human",
        )
    )
    await session.flush()

    report = await run_tracker_projection_pass(session, CONFIG)
    assert (report.projections_refused, report.projections_written) == (1, 0)


@pytest.mark.parametrize(
    ("actor_kind", "actor_id"),
    [
        ("service", "system:orchestration-dispatch"),
        ("human", ""),
    ],
)
async def test_an_approval_kind_without_an_attributed_human_does_not_authorize_projection(
    session,
    actor_kind,
    actor_id,
):
    """Projection and dispatch must agree on what constitutes human approval.

    `resolve_engine_genesis` refuses both service-authored and unattributed rows.
    The tracker must not publish either row as accepted work merely because its
    `kind` happens to be one of the approval kinds.
    """
    flow = await _flow(session, states=[NodeState.RUNNING.value], approved=False)
    session.add(
        OrchestrationDecision(
            org_id=ORG_A,
            flow_id=flow.id,
            kind=DecisionKind.PLAN_ACCEPTED.value,
            actor_id=actor_id,
            actor_role="engine" if actor_kind == "service" else "admin",
            actor_kind=actor_kind,
        )
    )
    await session.flush()

    report = await run_tracker_projection_pass(session, CONFIG)
    assert (report.projections_refused, report.projections_written) == (1, 0)


async def test_latest_service_approval_kind_cannot_fall_back_to_an_older_human(session):
    """Match genesis: validate the latest approval row instead of skipping it."""
    flow = await _flow(session, states=[NodeState.RUNNING.value], approved=True)
    session.add(
        OrchestrationDecision(
            org_id=ORG_A,
            flow_id=flow.id,
            kind=DecisionKind.PLAN_ACCEPTED.value,
            actor_id="system:engine",
            actor_role="engine",
            actor_kind="service",
            created_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
    )
    await session.flush()

    report = await run_tracker_projection_pass(session, CONFIG)
    assert report.pending == []
    assert (report.projections_refused, report.projections_written) == (1, 0)


async def test_an_approval_in_another_tenant_does_not_authorize_this_flow(session):
    """The approval query filters `org_id` in SQL, by the shared rule.

    Fails if the `org_id` predicate is dropped — a same-flow-id row in another tenant
    would otherwise satisfy it.
    """
    flow = await _flow(session, org_id=ORG_A, states=[NodeState.RUNNING.value], approved=False)
    session.add(
        OrchestrationDecision(
            org_id=ORG_B,
            flow_id=flow.id,
            kind=DecisionKind.PLAN_ACCEPTED.value,
            actor_id="other-tenant-approver",
            actor_role="admin",
            actor_kind="human",
        )
    )
    await session.flush()

    report = await run_tracker_projection_pass(session, CONFIG)
    assert (report.projections_refused, report.projections_written) == (1, 0)


async def test_a_gate_approval_authorizes_a_projection(session):
    """The discriminating half: the guard must not refuse everything.

    `GATE_APPROVED` is in `APPROVAL_DECISION_KINDS`, so a hand-run flow approved at a
    gate still projects. Without this, a guard that always refused would pass every
    test above.
    """
    flow = await _flow(session, states=[NodeState.AWAITING_GATE.value], approved=False)
    session.add(
        OrchestrationDecision(
            org_id=ORG_A,
            flow_id=flow.id,
            kind=DecisionKind.GATE_APPROVED.value,
            actor_id="approver",
            actor_role="admin",
            actor_kind="human",
        )
    )
    await session.flush()

    report = await run_tracker_projection_pass(session, CONFIG)
    await flush_tracker_projections(report, client_factory=StubProvider(_body("kickoff")))
    assert (report.projections_written, report.projections_refused) == (1, 0)
