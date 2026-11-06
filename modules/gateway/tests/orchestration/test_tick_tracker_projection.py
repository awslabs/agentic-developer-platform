"""The tick's wiring of tracker projection (#5284).

`test_tracker_projection.py` calls the pass and the flush directly. This file goes
through the real `tick_handler._run()`, because the properties that matter most are
properties of the *wiring*, not of the pass:

- the render happens inside the tick's transaction and the GitHub write strictly
  after its commit, which is what makes AC3's isolation structural;
- a projection failure — of any kind, including an unforeseen one — cannot roll back
  the tick's durable work, change a node's state, or dispatch an agent;
- the counters reach the summary the operator actually reads.

`test_projection_failure_does_not_discard_engine_work` is the load-bearing one. Remove
the `try` around the pass call in `_run()` and it fails, because the exception reaches
the outer handler that rolls back and re-raises — discarding correct transitions
because a display feature broke.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration import repository_binding
from src.orchestration import tick_handler as tick_handler_module
from src.orchestration.models import (
    DecisionKind,
    NodeKind,
    NodeState,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.shared.models.base import Base

ORG = "org-alpha"
REPO = "aws-e/adp"
EPIC = 4910
INSTALLATION = 4242

REGION_START = "<!-- aidlc-tracker:start -->"
REGION_END = "<!-- aidlc-tracker:end -->"
INTENT = "## Intent\n\nShip the engine.\n\n"
BODY = f"{INTENT}{REGION_START}\nU1 just dispatched.\n{REGION_END}\n\nTrailing human text."


@pytest.fixture
async def session_factory(monkeypatch):
    """A session factory the tick handler will use, over one shared in-memory database."""
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

    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("src.orchestration.tick_handler.get_session_factory", lambda: factory)
    monkeypatch.setattr("src.orchestration.tick_handler.reset_engine", lambda: None)
    yield factory
    await eng.dispose()


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", REPO)
    monkeypatch.delenv("WEBHOOK_EVENTS_TABLE", raising=False)

    async def verified(_session, **kwargs):
        return repository_binding.VerifiedRepository(
            "github",
            kwargs["repository_id"],
            kwargs["name"],
            kwargs["installation_id"],
            kwargs["name"],
            kwargs["issue"],
            kwargs["operation"],
        )

    monkeypatch.setattr(repository_binding, "resolve_story_repository", verified)

    async def _resolve(_session, *, org_id):
        return INSTALLATION

    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", _resolve)


@pytest.fixture
def provider(monkeypatch):
    """A recording stub installed where the flush constructs its provider."""

    class Recorder:
        def __init__(self) -> None:
            self.body = BODY
            self.writes: list[str] = []
            self.fail_write = False

        async def read_issue_body(self, **kwargs):
            return self.body

        async def write_issue_body(self, *, body, **kwargs):
            if self.fail_write:
                raise RuntimeError("provider unavailable")
            self.writes.append(body)
            self.body = body

    recorder = Recorder()
    monkeypatch.setattr("src.orchestration.tracker_provider.GitHubTrackerProvider", lambda: recorder)
    return recorder


async def _seed(factory, *, states: list[str], approved: bool = True) -> list[str]:
    """An approved flow with one story node per state, as the engine would hold it.

    The `PLAN_ACCEPTED` row is part of the seed because projection now requires the
    same human approval that arms dispatch (#5337 review finding), and because the
    engine never has a compiled graph without one on the approval path. `approved=False`
    is what the authorization test below uses to model a draft.
    """
    async with factory() as session:
        reference = dict(provider="github", repository_id=3003, repository=REPO, installation_id=INSTALLATION, issue_number=EPIC)
        tracker_refs = [dict(scope="epic", address=f"engine/epic-{EPIC}", issue=reference)]
        flow = OrchestrationFlow(org_id=ORG, slug="engine", title="Engine", intent_ref="4191", tracker_refs=tracker_refs)
        session.add(flow)
        await session.flush()
        if approved:
            session.add(
                OrchestrationDecision(
                    org_id=ORG,
                    flow_id=flow.id,
                    kind=DecisionKind.PLAN_ACCEPTED.value,
                    actor_id="approver",
                    actor_role="admin",
                    actor_kind="human",
                )
            )
        ids = []
        for index, state in enumerate(states, start=1):
            node = OrchestrationNode(
                org_id=ORG,
                flow_id=flow.id,
                epic_ref=f"epic-{EPIC}",
                wave_ref="wave-1",
                node_ref=f"U{index}",
                kind=NodeKind.STORY.value,
                state=state,
                title=f"Story {index}",
                issue_ref=str(5000 + index),
                attempts=1,
            )
            session.add(node)
            await session.flush()
            ids.append(node.id)
        session.add(
            OrchestrationAcceptedPlan(
                org_id=ORG, flow_id=flow.id, version=1, plan_document={"nodes": [], "tracker_refs": tracker_refs}, plan_hash="f" * 64
            )
        )
        await session.commit()
        return ids


async def _states(factory) -> dict[str, str]:
    async with factory() as session:
        rows = (await session.execute(select(OrchestrationNode))).scalars().all()
        return {row.id: row.state for row in rows}


async def test_the_tick_projects_current_state_onto_the_region(session_factory, provider):
    """The fix, through the real tick: kickoff text replaced, human text preserved."""
    await _seed(session_factory, states=[NodeState.PASSED.value, NodeState.RUNNING.value])

    report = await tick_handler_module._run()

    assert provider.writes, "the tick performed no projection write"
    assert "U1 just dispatched." not in provider.body
    assert provider.body.startswith(INTENT)
    assert provider.body.endswith("Trailing human text.")
    assert "1/2 stories passed" in provider.body
    assert getattr(report, "tracker_projection_report").projections_written == 1


async def test_the_write_happens_after_the_commit(session_factory, provider, monkeypatch):
    """AC3's isolation is an ordering property, so the ordering is asserted directly.

    A write issued before the commit would mean a GitHub round trip holding row locks,
    and a rolled-back tick having already published progress that never happened.
    """
    await _seed(session_factory, states=[NodeState.RUNNING.value])
    order: list[str] = []

    original_commit = AsyncSession.commit

    async def recording_commit(self):
        order.append("commit")
        return await original_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", recording_commit)

    async def recording_write(self, *, body, **kwargs):
        order.append("write")

    monkeypatch.setattr(type(provider), "write_issue_body", recording_write)

    await tick_handler_module._run()

    assert "write" in order, "no write was attempted"
    assert order.index("commit") < order.index("write")


async def test_projection_failure_does_not_discard_engine_work(session_factory, provider, monkeypatch):
    """An unforeseen raise in the pass must not roll back the tick's transitions.

    The pass call sits last inside `_run()`'s try, whose `except` rolls back and
    re-raises. Without the guard around it, a bug in a *display* feature throws away
    correct forward motion — which is exactly what AC3 forbids. Remove the try in
    `_run()` and this test fails.
    """
    ids = await _seed(session_factory, states=[NodeState.READY.value])

    def _explode(*args, **kwargs):
        raise RuntimeError("unforeseen projection bug")

    monkeypatch.setattr("src.orchestration.tick_handler.run_tracker_projection_pass", _explode)

    report = await tick_handler_module._run()

    # The tick completed rather than raising, and its own work is durable.
    assert report is not None
    assert set(ids) == set((await _states(session_factory)).keys())
    # The failure is visible, not swallowed silently. Note it does NOT appear on
    # `TickReport.success`, which by design reflects only the tick's own node errors —
    # the projection surfaces through the handler's summary status, the same way the
    # stall, dispatch and engine-command reports do.
    assert getattr(report, "tracker_projection_report").errors == 1
    assert provider.writes == []


async def test_failed_outbox_insert_does_not_rollback_dispatch_or_flush_tracker(session_factory, provider, monkeypatch):
    from src.orchestration.dispatch_pass import DispatchPassReport
    from src.orchestration.models import OrchestrationTrackerPublication
    from src.orchestration.tracker_outbox import stage_tracker_publications

    await _seed(session_factory, states=[NodeState.RUNNING.value])

    async def dispatch(session):
        await session.execute(update(OrchestrationFlow).values(title="Committed dispatch state"))
        return DispatchPassReport()

    async def sql_failure(session, report):
        await stage_tracker_publications(session, report)
        await session.execute(text("INSERT INTO orchestration_tracker_publications (id) VALUES ('invalid')"))

    monkeypatch.setattr(tick_handler_module, "run_dispatch_pass", dispatch)
    monkeypatch.setattr(tick_handler_module, "stage_tracker_publications", sql_failure)
    report = await tick_handler_module._run()

    assert report.tracker_projection_report.errors == 1
    assert provider.writes == []
    async with session_factory() as session:
        assert (await session.scalars(select(OrchestrationFlow))).one().title == "Committed dispatch state"
        assert (await session.scalars(select(OrchestrationTrackerPublication))).all() == []


async def test_a_provider_outage_leaves_state_and_dispatch_untouched(session_factory, provider):
    """AC3: a GitHub outage changes no node state, bypasses no gate, dispatches nothing."""
    await _seed(session_factory, states=[NodeState.RUNNING.value, NodeState.AWAITING_GATE.value])
    before = await _states(session_factory)
    provider.fail_write = True

    report = await tick_handler_module._run()

    assert await _states(session_factory) == before
    assert getattr(report, "tracker_projection_report").projections_failed == 1
    # The gate is still a gate: nothing about a failed display write advanced it.
    assert NodeState.AWAITING_GATE.value in (await _states(session_factory)).values()


async def test_a_second_tick_over_unchanged_state_writes_nothing(session_factory, provider):
    """Idempotence through the real tick, not just through the pass (AC1)."""
    await _seed(session_factory, states=[NodeState.PASSED.value])

    await tick_handler_module._run()
    assert len(provider.writes) == 1

    second = await tick_handler_module._run()

    assert len(provider.writes) == 1
    assert getattr(second, "tracker_projection_report").pending == []


async def test_failed_tracker_write_restarts_with_the_same_target_and_content(session_factory, provider):
    from src.orchestration.models import OrchestrationTrackerPublication

    await _seed(session_factory, states=[NodeState.RUNNING.value])
    provider.fail_write = True
    first = await tick_handler_module._run()
    assert first.tracker_projection_report.projections_failed == 1

    async with session_factory() as session:
        rows = (await session.scalars(select(OrchestrationTrackerPublication))).all()
        assert len(rows) == 1
        snapshot = (rows[0].id, rows[0].repository_id, rows[0].repo, rows[0].installation_id, rows[0].issue_number, rows[0].body_region)
        assert rows[0].completed_at is None

    provider.fail_write = False
    second = await tick_handler_module._run()
    assert second.tracker_projection_report.projections_written >= 1
    assert snapshot[5] in provider.writes[0]
    async with session_factory() as session:
        recovered = await session.get(OrchestrationTrackerPublication, snapshot[0])
        assert (
            recovered.id,
            recovered.repository_id,
            recovered.repo,
            recovered.installation_id,
            recovered.issue_number,
            recovered.body_region,
        ) == snapshot
        assert recovered.completed_at is not None


@pytest.mark.parametrize("revoked_after_staging", [False, True])
async def test_tick_refuses_revoked_planning_connection_before_provider_write(
    session_factory,
    provider,
    monkeypatch,
    revoked_after_staging,
):
    from src.orchestration.models import OrchestrationTrackerPublication

    node_ids = await _seed(session_factory, states=[NodeState.PASSED.value])
    revoked = not revoked_after_staging
    provider_read = AsyncMock(return_value=BODY)
    monkeypatch.setattr(provider, "read_issue_body", provider_read)

    async def verified(_session, **kwargs):
        if revoked:
            raise repository_binding.RepositoryBindingError("connection_revoked")
        return repository_binding.VerifiedRepository(
            "github",
            kwargs["repository_id"],
            kwargs["name"],
            kwargs["installation_id"],
            kwargs["name"],
            kwargs["issue"],
            kwargs["operation"],
        )

    monkeypatch.setattr(repository_binding, "resolve_story_repository", verified)
    original_stage = tick_handler_module.stage_tracker_publications

    async def stage(session, report):
        nonlocal revoked
        await original_stage(session, report)
        if revoked_after_staging:
            assert len(report.pending) == 1
            revoked = True

    monkeypatch.setattr(tick_handler_module, "stage_tracker_publications", stage)
    result = await tick_handler_module._run()
    assert await _states(session_factory) == {node_ids[0]: NodeState.PASSED.value}
    assert provider.writes == []
    provider_read.assert_not_awaited()
    async with session_factory() as session:
        owed = (await session.scalars(select(OrchestrationTrackerPublication))).all()
        assert len(owed) == (1 if revoked_after_staging else 0)
        if owed:
            assert owed[0].completed_at is None
            assert (owed[0].repository_id, owed[0].repo, owed[0].installation_id) == (3003, REPO, INSTALLATION)
    assert result.tracker_projection_report.projections_refused == 1


async def test_lost_tracker_ack_reconciles_without_a_second_write(session_factory, provider, monkeypatch):
    from src.orchestration import tracker_outbox
    from src.orchestration.models import OrchestrationTrackerPublication

    await _seed(session_factory, states=[NodeState.PASSED.value])
    original = tracker_outbox.complete_publication

    async def lost_ack(*args, **kwargs):
        raise RuntimeError("completion acknowledgment lost")

    monkeypatch.setattr(tracker_outbox, "complete_publication", lost_ack)
    first = await tick_handler_module._run()
    assert first.tracker_projection_report.projections_failed == 1
    assert len(provider.writes) == 1
    async with session_factory() as session:
        pending = (await session.scalars(select(OrchestrationTrackerPublication))).one()
        operation_id = pending.id
        assert pending.completed_at is None

    monkeypatch.setattr(tracker_outbox, "complete_publication", original)
    second = await tick_handler_module._run()
    assert second.tracker_projection_report.projections_unchanged == 1
    assert len(provider.writes) == 1
    async with session_factory() as session:
        completed = (await session.scalars(select(OrchestrationTrackerPublication))).one()
        assert completed.id == operation_id
        assert completed.completed_at is not None


async def test_planning_repository_id_above_signed_integer_range_is_preserved(session_factory, provider):
    from src.orchestration.models import OrchestrationTrackerPublication

    await _seed(session_factory, states=[NodeState.PASSED.value])
    large_id = 2**31 + 7
    async with session_factory() as session:
        flow = (await session.scalars(select(OrchestrationFlow))).one()
        plan = (await session.scalars(select(OrchestrationAcceptedPlan))).one()
        refs = [
            dict(
                scope="epic",
                address=f"engine/epic-{EPIC}",
                issue=dict(
                    provider="github",
                    repository_id=large_id,
                    repository=REPO,
                    installation_id=INSTALLATION,
                    issue_number=EPIC,
                ),
            )
        ]
        flow.tracker_refs = refs
        plan.plan_document = {**plan.plan_document, "tracker_refs": refs}
        await session.commit()

    await tick_handler_module._run()
    assert len(provider.writes) == 1
    async with session_factory() as session:
        row = (await session.scalars(select(OrchestrationTrackerPublication))).one()
        assert row.repository_id == large_id


async def test_owed_tracker_backlog_preserves_new_capacity_and_limits_growth(session_factory, provider):
    from src.orchestration.models import OrchestrationTrackerPublication
    from src.orchestration.tracker_outbox import stage_tracker_publications
    from src.orchestration.tracker_projection import PendingTrackerProjection, TrackerProjectionReport

    await _seed(session_factory, states=[NodeState.PASSED.value])
    provider.fail_write = True
    for _ in range(3):
        await tick_handler_module._run()
    async with session_factory() as session:
        owed = (await session.scalars(select(OrchestrationTrackerPublication))).all()
        assert len(owed) == 1
        flow = (await session.scalars(select(OrchestrationFlow))).one()
        session.add(
            OrchestrationTrackerPublication(
                id="2" * 64,
                org_id=ORG,
                flow_id=flow.id,
                repository_id=3003,
                epic_ref=f"epic-{EPIC}",
                repo=REPO,
                installation_id=INSTALLATION,
                issue_number=EPIC + 1,
                body_region="old",
                version=1,
                watermark=1,
            )
        )
        await session.flush()
        report = TrackerProjectionReport(
            pending=[
                PendingTrackerProjection(
                    org_id=ORG,
                    flow_id=flow.id,
                    repo="example/new",
                    repository_id=2**31 + 7,
                    epic_ref=f"epic-{EPIC}",
                    installation_id=INSTALLATION,
                    issue_number=EPIC,
                    body_region="new",
                    version=2,
                    watermark=2,
                )
            ]
        )
        await stage_tracker_publications(session, report, limit=2)
        assert len(report.pending) == 2
        assert report.pending[0].operation_id in {owed[0].id, "2" * 64}
        assert report.pending[1].repo == "example/new"
        await session.rollback()


async def test_completed_tracker_operation_ages_out_in_bounded_cleanup(session_factory, provider):
    from src.orchestration.models import OrchestrationTrackerPublication
    from src.orchestration.tracker_outbox import stage_tracker_publications
    from src.orchestration.tracker_projection import TrackerProjectionReport

    await _seed(session_factory, states=[NodeState.PASSED.value])
    await tick_handler_module._run()
    async with session_factory() as session:
        row = (await session.scalars(select(OrchestrationTrackerPublication))).one()
        row.completed_at = datetime.now(UTC) - timedelta(days=31)
        await session.commit()
    async with session_factory() as session:
        report = TrackerProjectionReport()
        await stage_tracker_publications(session, report)
        assert report.pending == []
        assert (await session.scalars(select(OrchestrationTrackerPublication))).all() == []
        await session.commit()


async def test_legacy_unbound_plan_remains_readable_without_queuing_a_write(session_factory, provider):
    from src.orchestration.models import OrchestrationTrackerPublication

    await _seed(session_factory, states=[NodeState.RUNNING.value])
    async with session_factory() as session:
        flow = (await session.scalars(select(OrchestrationFlow))).one()
        plan = (await session.scalars(select(OrchestrationAcceptedPlan))).one()
        flow.tracker_refs = None
        plan.plan_document = {"nodes": []}
        plan.plan_hash = "old-hash"
        await session.commit()

    report = await tick_handler_module._run()
    assert report.tracker_projection_report.projections_refused == 1
    assert provider.writes == []
    async with session_factory() as session:
        assert (await session.scalars(select(OrchestrationTrackerPublication))).all() == []
        assert (await session.scalars(select(OrchestrationAcceptedPlan))).one().plan_hash == "old-hash"


async def test_changed_planning_binding_does_not_move_queued_write(session_factory, provider, monkeypatch):
    from src.orchestration.models import OrchestrationTrackerPublication

    await _seed(session_factory, states=[NodeState.PASSED.value])
    provider.fail_write = True
    await tick_handler_module._run()
    async with session_factory() as session:
        old = (await session.scalars(select(OrchestrationTrackerPublication))).one()
        original_id = old.id
        flow = (await session.scalars(select(OrchestrationFlow))).one()
        plan = (await session.scalars(select(OrchestrationAcceptedPlan))).one()
        changed = dict(provider="github", repository_id=5005, repository="example/planning", installation_id=505, issue_number=EPIC)
        flow.tracker_refs = [dict(scope="epic", address=f"engine/epic-{EPIC}", issue=changed)]
        plan.plan_document = {**plan.plan_document, "tracker_refs": flow.tracker_refs}
        await session.commit()

    attempted = []
    original_write = provider.write_issue_body

    async def capture_write(*, body, **kwargs):
        attempted.append(kwargs)
        await original_write(body=body, **kwargs)

    monkeypatch.setattr(provider, "write_issue_body", capture_write)
    provider.fail_write = False
    second = await tick_handler_module._run()
    assert second.tracker_projection_report.projections_refused == 1
    assert second.tracker_projection_report.projections_written == 1
    assert [(item["repo"], item["installation_id"]) for item in attempted] == [("example/planning", 505)]
    async with session_factory() as session:
        old = await session.get(OrchestrationTrackerPublication, original_id)
        assert old.completed_at is None
        assert (old.repository_id, old.repo, old.installation_id) == (3003, REPO, INSTALLATION)


async def test_the_pass_is_off_unless_the_flag_is_literally_true(session_factory, provider, monkeypatch):
    """A disabled projection is visibly disabled and touches nothing."""
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "1")
    await _seed(session_factory, states=[NodeState.RUNNING.value])

    report = await tick_handler_module._run()

    assert provider.writes == []
    projection = getattr(report, "tracker_projection_report")
    assert projection.enabled is False
    # And a disabled pass is not an unhealthy one.
    assert projection.success is True


def test_projection_counters_reach_the_handler_summary(session_factory, provider):
    """The counters are the only way an operator sees a refusal or a failure.

    By construction this pass cannot report a problem by updating the region it failed
    to update, so if the summary omits these the failure mode is invisible — which is
    the defect this story exists to remove. Goes through `handler()` because the
    summary dict is what the smoke check reads.
    """
    import asyncio

    asyncio.run(_seed(session_factory, states=[NodeState.PASSED.value]))
    summary = tick_handler_module.handler({}, None)

    assert summary["status"] == "ok"
    assert summary["projections_written"] == 1
    for key in (
        "projection_flows_examined",
        "projections_unchanged",
        "projections_refused",
        "projections_stale",
        "projections_failed",
        "projection_errors",
        "projections_capped",
        "projections_enabled",
    ):
        assert key in summary, f"the summary omits {key}"


def test_an_undelivered_projection_makes_the_handler_status_an_error(session_factory, provider):
    """A tracker silently drifting from engine state must not report as a green tick.

    This is the counterpart to the refusal cases: `refused` and `stale` are correct
    outcomes and stay `ok`, but an update that *never arrived* is the invisible
    staleness this story removes, so it has to break the one line the smoke check reads.
    """
    import asyncio

    asyncio.run(_seed(session_factory, states=[NodeState.PASSED.value]))
    provider.fail_write = True

    summary = tick_handler_module.handler({}, None)

    assert summary["status"] == "error"
    assert summary["projections_failed"] == 1


def test_a_refused_projection_leaves_the_handler_status_ok(session_factory, provider):
    """An EPIC never initialised with sentinels is normal, not a fault.

    The discriminating pair to the test above: if refusals also turned the status red,
    every hand-run flow would page someone, and the signal would be trained away.
    """
    import asyncio

    asyncio.run(_seed(session_factory, states=[NodeState.PASSED.value]))
    provider.body = "An issue with no tracker region at all."

    summary = tick_handler_module.handler({}, None)

    assert summary["status"] == "ok"
    assert summary["projections_refused"] == 1
    assert summary["projections_written"] == 0


class TestProjectionTakesNoEngineAction:
    """Source-level: the projection modules never transition, dispatch or publish.

    An AST assertion rather than a string search, mirroring `TestNoBypass` in
    `test_tick.py`: it survives reformatting, cannot be defeated by a comment, and
    holds against a *future* refactor, which is the case the behavioural tests above
    cannot reach. One-way projection is a security property — if the region ever
    became an engine input, a human editing generated prose would steer execution.
    """

    # Bare function calls that would mean the display pass had become an actor.
    FORBIDDEN_FUNCTIONS = {
        "transition",  # would change node state
        "run_dispatch_pass",
        "publish_pending",  # would dispatch an agent
        "record_decision",  # would make the projection a decision of record
    }

    # Methods forbidden *on a session*. Deliberately not matched by bare name:
    # `set.add` and `dict.update` are ordinary local work, and a check that cannot
    # tell them from `session.add` is a check that gets deleted the first time it
    # cries wolf.
    #
    # `execute` is not here, because the pass must read: it is handled separately by
    # inspecting the statement, since `session.execute(select(...))` is the whole job
    # and `session.execute(update(...))` is the thing being forbidden.
    FORBIDDEN_SESSION_METHODS = {"add", "add_all", "delete", "flush", "commit", "merge"}

    # Statement constructors that write. Anything not in this set — including raw
    # `text()`, whose contents this check cannot see — is treated as a violation when
    # passed to `session.execute`, so the guard fails closed on the unrecognised.
    READ_ONLY_STATEMENTS = {"select", "exists"}

    @staticmethod
    def _receiver(node: ast.expr) -> str:
        """The left-hand side of an attribute call, as source-ish text."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{TestProjectionTakesNoEngineAction._receiver(node.value)}.{node.attr}"
        if isinstance(node, ast.Call):
            return TestProjectionTakesNoEngineAction._receiver(node.func)
        return ""

    @classmethod
    def _violations(cls, source: str) -> list[str]:
        tree = ast.parse(source)
        found: list[str] = []
        for sub in ast.walk(tree):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if isinstance(func, ast.Name):
                if func.id in cls.FORBIDDEN_FUNCTIONS:
                    found.append(f"{func.id}()")
            elif isinstance(func, ast.Attribute):
                if func.attr in cls.FORBIDDEN_FUNCTIONS:
                    found.append(f"...{func.attr}()")
                receiver = cls._receiver(func.value)
                on_session = "session" in receiver.lower()
                if on_session and func.attr in cls.FORBIDDEN_SESSION_METHODS:
                    found.append(f"{receiver}.{func.attr}()")
                if on_session and func.attr == "execute" and not cls._is_read_only(sub):
                    found.append(f"{receiver}.execute(<not a select>)")
        return sorted(set(found))

    @classmethod
    def _is_read_only(cls, call: ast.Call) -> bool:
        """Whether `session.execute(...)`'s first argument is a read.

        Walks to the root constructor so the builder chain
        `select(X).where(...).distinct()` still reads as a `select`.
        """
        if not call.args:
            return False
        root = call.args[0]
        while isinstance(root, ast.Call) and isinstance(root.func, ast.Attribute):
            root = root.func.value
        if isinstance(root, ast.Call) and isinstance(root.func, ast.Name):
            return root.func.id in cls.READ_ONLY_STATEMENTS
        return False

    @pytest.mark.parametrize("module_name", ["tracker_projection", "tracker_provider"])
    def test_no_engine_mutation_is_reachable(self, module_name):
        import importlib

        module = importlib.import_module(f"src.orchestration.{module_name}")
        source = Path(inspect.getfile(module)).read_text()
        assert self._violations(source) == [], f"{module_name} reaches engine mutation"

    def test_the_check_would_notice(self):
        """The guard above passes trivially if the walk is broken, so prove it bites.

        Without this, replacing `_violations` with `return []` would leave a green suite
        advertising a property nobody is checking.
        """
        found = self._violations("async def go(session):\n    session.add(thing)\n    await transition(x)\n")

        assert "session.add()" in found
        assert "transition()" in found

    def test_it_sees_through_an_indirect_session_reference(self):
        """A session reached via an attribute is still a session.

        `self._session.add(...)` is the shape this would most plausibly regress into,
        and a check that only matched a bare local name would wave it through.
        """
        found = self._violations("def go(self):\n    self._session.delete(node)\n")

        assert found == ["self._session.delete()"]

    @pytest.mark.parametrize(
        "statement",
        [
            "update(OrchestrationNode).values(state='passed')",
            "delete(OrchestrationNode)",
            "insert(OrchestrationNode)",
            "text('UPDATE orchestration_nodes SET state = :s')",
        ],
    )
    def test_a_writing_statement_is_a_violation(self, statement):
        """`session.execute` is allowed only for reads, so the statement is inspected.

        Dropping this would let the one call the pass legitimately makes become the
        route by which it starts transitioning nodes — the single most likely
        regression, because it needs no new import and no new call site. `text()` counts
        as a violation precisely because its contents are opaque here.
        """
        found = self._violations(f"async def go(session):\n    await session.execute({statement})\n")

        assert found == ["session.execute(<not a select>)"]

    @pytest.mark.parametrize(
        "statement",
        [
            "select(OrchestrationNode)",
            "select(OrchestrationNode).where(x == 1).order_by(y).limit(5)",
            "select(OrchestrationNode.flow_id).distinct().scalars()",
        ],
    )
    def test_reads_the_pass_must_perform_are_allowed(self, statement):
        """The discriminating half: a guard that flagged every `execute` would be wrong.

        The pass reads the graph, the run and the accepted plan through exactly this
        call. Without this test the rule above could be satisfied by a check that bans
        reading, which would make the projection impossible rather than safe.
        """
        assert self._violations(f"async def go(session):\n    await session.execute({statement})\n") == []

    def test_ordinary_local_calls_are_not_violations(self):
        """And prove it does not bite the innocent — the false positive that started this.

        `_epic_issue_number` calls `.add()` on a local `set`. A name-only check flags it,
        which is how a real guard gets weakened into a `# noqa` or deleted outright.
        """
        source = "def f(refs):\n    numbers = set()\n    numbers.add(1)\n    counts = {}\n    counts.update(a=1)\n    return numbers\n"

        assert self._violations(source) == []


async def test_the_tick_never_projects_an_unapproved_flow(session_factory, provider):
    """The authorization guard through the real tick (#5337 review finding).

    The EPIC issue number is parsed out of `epic_ref`, which plan registration stores
    verbatim from the author's node address, and the repository is one process-wide
    variable. So without an approval requirement, a draft — which any holder of
    `PLAN_DRAFT` can register, and which still compiles nodes and an accepted-plan row
    — would make the engine PATCH the named issue under its own bot identity.

    Asserted as *no write attempted*, and through the tick rather than the pass,
    because the pass returning empty `pending` and the flush declining are different
    outcomes that look identical in the resulting body.
    """
    await _seed(session_factory, states=[NodeState.RUNNING.value], approved=False)

    report = await tick_handler_module._run()

    assert provider.writes == []
    projection = getattr(report, "tracker_projection_report")
    assert (projection.projections_written, projection.projections_refused) == (0, 1)
    # A refusal is not a failure: an unapproved flow is an ordinary state, and
    # reddening it would page someone for every draft in the system.
    assert projection.success is True
