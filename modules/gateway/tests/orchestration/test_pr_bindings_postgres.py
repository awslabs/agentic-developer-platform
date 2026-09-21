"""Real-PostgreSQL concurrency tests for story-to-PR bindings (#5301).

The issue requires transactional and concurrency claims to be proven against real
PostgreSQL, and that is not pedantry here. The binding's central invariant is
**one row per pull request**, and the thing that enforces it is a unique index —
which only does real work under genuinely concurrent writers:

- Two concurrent registrations can both pass the application-level "is this PR
  already bound?" read before either inserts. The index is what turns the loser
  into a refusal instead of a second binding.
- `IntegrityError` on a concurrent insert is the path `register_binding` has to
  convert into convergence rather than an error, and that path is unreachable
  without two real writers.
- SQLite's single-writer model hides the exact interleaving that produces the
  double insert, so `test_pr_bindings.py` proves the *semantics* and deliberately
  proves nothing about concurrency.

Why this matters for the bug rather than in the abstract: a duplicated
registration is the *expected* case, not an edge one. The worker registers on both
PR paths, a tick restart re-registers, and SQS redelivery re-runs the whole
attempt. If any of those inserted a rival binding, the story would surface
`AMBIGUOUS_CANDIDATE` and hold forever — trading the original "waits on evidence
that never arrives" for a new permanent hold. Convergence under concurrency is what
keeps the fix from reintroducing the failure it removes.

Two separate sessions with `asyncio.gather`, not threads: the sessions are real and
concurrent at the database while the failure mode stays reproducible instead of
depending on OS thread scheduling. Same reasoning `test_work_claims_postgres.py`
gives.

Skips (never silently passes) when no PostgreSQL is available — `pgserver` ships
wheels for Python <= 3.12 only. A skip here means "not tested", and it is reported
as such rather than as a pass.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.models import (
    ActorKind,
    BindingState,
    DecisionKind,
    NodeKind,
    NodeState,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
)
from src.orchestration.pr_bindings import (
    BindingError,
    PullRequestIdentity,
    active_binding_for_node,
    register_binding,
    resolve_registration_target,
)

# Re-exported through tests/migrations/conftest.py, but this file lives in
# tests/orchestration/, so the fixtures are imported explicitly.
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

ORG_A = "org-alpha"
REPO = "aws-e/adp"
ISSUE = 5049
REPO_ID = 987_654_321
PR_NUMBER = 5293
PR_NODE = "PR_kwDOABCD12345"
HEAD = "6c7370387d5d57a6ff9ebb5a567f0744e7d99d0e"
NEW_HEAD = "aaaa111122223333444455556666777788889999"
SERVICE = "scaledjob-worker"


@pytest.fixture
async def pg_engine(pg_url):  # noqa: F811 - pg_url is a fixture, not a shadowed import
    """An async engine on a fresh database with only the tables under test.

    Built from the ORM models rather than by running the Alembic chain: this file
    tests runtime concurrency, so it stays independent of unrelated migrations.
    Migration correctness for this table is a migrations test's job.
    """
    engine = create_async_engine(to_async_url(pg_url), echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(OrchestrationFlow.__table__.create)
        await conn.run_sync(OrchestrationAcceptedPlan.__table__.create)
        await conn.run_sync(OrchestrationNode.__table__.create)
        await conn.run_sync(OrchestrationDecision.__table__.create)
        await conn.run_sync(OrchestrationPullRequestBinding.__table__.create)
        from src.orchestration.run_reports import OrchestrationRunReport

        await conn.run_sync(OrchestrationRunReport.__table__.create)
    yield engine
    await engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _installation(monkeypatch):
    async def _resolve(_session, *, org_id):
        return 4242

    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_installation_id", _resolve)


def _pr(*, number: int = PR_NUMBER, node_id: str = PR_NODE, head: str = HEAD) -> PullRequestIdentity:
    return PullRequestIdentity(
        provider_repository_id=REPO_ID,
        provider_pr_node_id=node_id,
        repo=REPO,
        pr_number=number,
        head_sha=head,
    )


async def _dispatched_story(session_factory) -> tuple[str, str]:
    """Commit a dispatched story and return `(node_id, run_id)`."""
    async with session_factory() as session:
        flow = OrchestrationFlow(org_id=ORG_A, slug="flow-5049", title="Deliver the epic", state="running")
        session.add(flow)
        await session.flush()
        node = OrchestrationNode(
            org_id=ORG_A,
            flow_id=flow.id,
            epic_ref="epic-1",
            wave_ref="wave-1",
            node_ref=f"story-{ISSUE}",
            kind=NodeKind.STORY.value,
            state=NodeState.AWAITING_MERGE.value,
            title="Implement the thing",
            issue_ref=str(ISSUE),
            attempts=1,
        )
        session.add(node)
        await session.flush()
        run_id = attempt_run_id(node.id, 1)
        session.add(
            OrchestrationDecision(
                org_id=ORG_A,
                flow_id=flow.id,
                node_id=node.id,
                kind=DecisionKind.NODE_DISPATCHED.value,
                actor_id="engine",
                actor_role="service",
                actor_kind=ActorKind.SERVICE.value,
                reason=json.dumps({"run_id": run_id, "attempt": 1, "repo": REPO, "issue": ISSUE, "pr_binding_required": True}),
            )
        )
        await session.commit()
        return node.id, run_id


async def _attempt(session_factory, run_id: str, pr: PullRequestIdentity):
    """One full registration in its own transaction, committed.

    Committing inside the attempt is what makes the race real: an uncommitted
    winner is invisible to the loser however the index behaves. Returns
    `(binding_id, created)` or the `BindingError` — both are legitimate outcomes for
    a loser, so the tests assert on the combination rather than on which arm fired.
    """
    async with session_factory() as session:
        try:
            binding, created = await register_binding(
                session,
                target=await resolve_registration_target(session, run_id=run_id),
                pr=pr,
                actor_id=SERVICE,
                actor_kind=ActorKind.SERVICE,
            )
            await session.commit()
            return binding.id, created
        except BindingError as exc:
            await session.rollback()
            return exc


async def test_concurrent_registrations_converge_on_one_binding(pg_session_factory):
    """The idempotency invariant under two genuinely concurrent writers.

    This is the shape SQS redelivery and a tick restart produce, so it is the
    expected case rather than an edge one. Two bindings here would surface
    `AMBIGUOUS_CANDIDATE` and hold the story permanently.
    """
    _, run_id = await _dispatched_story(pg_session_factory)

    results = await asyncio.gather(
        _attempt(pg_session_factory, run_id, _pr()),
        _attempt(pg_session_factory, run_id, _pr()),
    )

    async with pg_session_factory() as session:
        rows = (await session.execute(select(OrchestrationPullRequestBinding))).scalars().all()
    assert len(rows) == 1, "a concurrent duplicate registration inserted a rival binding"

    # Both callers must end up pointing at that one row: a caller that saw an error
    # would report a binding failure for work that is in fact correctly bound.
    ids = {r[0] for r in results if isinstance(r, tuple)}
    assert ids == {rows[0].id}
    assert sum(1 for r in results if isinstance(r, tuple) and r[1]) <= 1, "two callers both claimed to have created the binding"


async def test_concurrent_head_repairs_leave_one_active_binding(pg_session_factory):
    """Two pushes landing at once repair the same row rather than rivalling it."""
    node_id, run_id = await _dispatched_story(pg_session_factory)
    await _attempt(pg_session_factory, run_id, _pr())

    await asyncio.gather(
        _attempt(pg_session_factory, run_id, _pr(head=NEW_HEAD)),
        _attempt(pg_session_factory, run_id, _pr(head=NEW_HEAD)),
    )

    async with pg_session_factory() as session:
        rows = (await session.execute(select(OrchestrationPullRequestBinding))).scalars().all()
        assert len(rows) == 1
        assert rows[0].head_sha == NEW_HEAD
        active = await active_binding_for_node(session, org_id=ORG_A, node_id=node_id, attempt=1)
        assert active is not None and active.id == rows[0].id


async def test_registration_commits_durably(pg_session_factory):
    """A binding survives into a separate connection, in ACTIVE state.

    The whole point of the row is durability across the tick that registers it and
    the later tick that reconciles from it, and those are different connections.
    """
    node_id, run_id = await _dispatched_story(pg_session_factory)
    await _attempt(pg_session_factory, run_id, _pr())

    async with pg_session_factory() as session:
        binding = await active_binding_for_node(session, org_id=ORG_A, node_id=node_id, attempt=1)
        assert binding is not None
        assert binding.state == BindingState.ACTIVE.value
        assert binding.pr_number == PR_NUMBER
        assert binding.head_sha == HEAD
        assert binding.provider_pr_node_id == PR_NODE


async def test_second_pull_request_for_same_story_is_refused_not_duplicated(pg_session_factory):
    """A different PR arriving concurrently must not quietly take over the story."""
    _, run_id = await _dispatched_story(pg_session_factory)

    results = await asyncio.gather(
        _attempt(pg_session_factory, run_id, _pr()),
        _attempt(pg_session_factory, run_id, _pr(number=PR_NUMBER + 1, node_id="PR_kwDOZZZZ99999")),
    )

    async with pg_session_factory() as session:
        active = (
            (await session.execute(select(OrchestrationPullRequestBinding).where(OrchestrationPullRequestBinding.state == BindingState.ACTIVE.value)))
            .scalars()
            .all()
        )
    # Exactly one story-completing candidate. The other attempt either lost the race
    # or was refused as ambiguous; both are correct, and neither may produce a second
    # active binding that makes the story's implementing PR unknowable.
    assert len(active) == 1
    assert any(isinstance(r, BindingError) or isinstance(r, tuple) for r in results)


@pytest.mark.parametrize("replacement", [False, True], ids=["head-repair", "authorized-replacement"])
async def test_provider_read_cannot_complete_a_concurrently_changed_binding(pg_session_factory, monkeypatch, replacement):
    """Pause after the snapshot, commit a mutation, then return old green evidence."""
    from src.orchestration.pr_bindings import recover_binding
    from src.orchestration.results import observe_results
    from tests.orchestration.test_pr_binding_lifecycle import RunStore
    from tests.orchestration.test_story_reconciliation import _bind, _green, _story

    async def installation(*args, **kwargs):
        return 4242

    monkeypatch.setattr("src.orchestration.results.resolve_installation_id", installation)
    async with pg_session_factory() as session:
        node, dispatch = await _story(session, binding_marker=True)
        await _bind(session, node, dispatch)
        node_id, run_store = node.id, RunStore(node)
        await session.commit()
    fetched, mutated = asyncio.Event(), asyncio.Event()

    class Source:
        async def bound_pull_request(self, **kwargs):
            fetched.set()
            await asyncio.wait_for(mutated.wait(), timeout=10)
            return _green()

    async def observe():
        async with pg_session_factory() as session:
            report = await observe_results(session, run_store=run_store, evidence=Source())
            await session.commit()
            return report

    async def mutate():
        await asyncio.wait_for(fetched.wait(), timeout=10)
        async with pg_session_factory() as session:
            if replacement:
                await recover_binding(
                    session,
                    org_id=ORG_A,
                    node_id=node_id,
                    pr=_pr(number=PR_NUMBER + 1, node_id="PR_replacement", head=NEW_HEAD),
                    installation_id=4242,
                    actor_id="operator",
                    reason="verified changed implementation",
                    replaces_reason="original implementation abandoned",
                )
            else:
                await register_binding(
                    session,
                    target=await resolve_registration_target(session, run_id=dispatch["run_id"]),
                    pr=_pr(head=NEW_HEAD),
                    actor_id=SERVICE,
                    actor_kind=ActorKind.SERVICE,
                )
            await session.commit()
        mutated.set()

    report, _ = await asyncio.wait_for(asyncio.gather(observe(), mutate()), timeout=15)
    assert report.advanced == 0 and report.errors == 0
    async with pg_session_factory() as session:
        node = (await session.execute(select(OrchestrationNode).where(OrchestrationNode.id == node_id))).scalar_one()
        assert node.state == "awaiting_merge"
        decisions = (
            (
                await session.execute(
                    select(OrchestrationDecision).where(
                        OrchestrationDecision.node_id == node_id,
                        OrchestrationDecision.kind == DecisionKind.RESULT_OBSERVED.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(decisions) == 1
        assert "merge_receipt" not in json.loads(decisions[0].reason)
        assert "binding changed" in json.loads(decisions[0].reason)["evidence"]


async def test_carry_forward_transaction_fences_late_old_worker(pg_session_factory):
    from src.orchestration.pr_bindings import BindingRefusal, carry_forward_binding

    node_id, run_id = await _dispatched_story(pg_session_factory)
    await _attempt(pg_session_factory, run_id, _pr())
    async with pg_session_factory() as reader:
        old_target = await resolve_registration_target(reader, run_id=run_id)
    dispatch_locked, old_started = asyncio.Event(), asyncio.Event()

    async def repair():
        async with pg_session_factory() as session:
            node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.id == node_id).with_for_update())
            node.attempts = 2
            session.add(
                OrchestrationDecision(
                    org_id=ORG_A,
                    flow_id=node.flow_id,
                    node_id=node.id,
                    kind=DecisionKind.NODE_DISPATCHED.value,
                    actor_id="engine",
                    actor_role="service",
                    actor_kind="service",
                    reason=json.dumps({"run_id": attempt_run_id(node_id, 2), "attempt": 2, "repo": REPO, "issue": ISSUE}),
                )
            )
            await session.flush()
            dispatch_locked.set()
            await old_started.wait()
            current = await carry_forward_binding(
                session,
                node=node,
                previous_attempt=1,
                target=await resolve_registration_target(session, run_id=attempt_run_id(node_id, 2)),
                pr=_pr(head=NEW_HEAD),
                expected_revision=1,
            )
            await session.commit()
            return current.id

    async def late_worker():
        await dispatch_locked.wait()
        async with pg_session_factory() as session:
            old_started.set()
            with pytest.raises(BindingError) as refused:
                await register_binding(session, target=old_target, pr=_pr(), actor_id="late", actor_kind=ActorKind.SERVICE)
            assert refused.value.code == BindingRefusal.STALE_RUN
            await session.rollback()

    binding_id, _ = await asyncio.gather(repair(), late_worker())
    async with pg_session_factory() as session:
        current = await active_binding_for_node(session, org_id=ORG_A, node_id=node_id, attempt=2)
        assert current.id == binding_id and current.revision == 2 and current.head_sha == NEW_HEAD
        assert await active_binding_for_node(session, org_id=ORG_A, node_id=node_id, attempt=1) is None


async def test_attempt_zero_adoption_races_dispatch_without_inventing_worker(pg_engine, pg_session_factory, monkeypatch):
    from sqlalchemy import update

    from src.orchestration.delivery_adoption import adopt_delivery
    from src.orchestration.models import OrchestrationWorkClaim
    from src.orchestration.pr_bindings import _accepted_scope
    from tests.orchestration.test_story_reconciliation import _green

    monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", REPO)

    async def installation(*args, **kwargs):
        return 4242

    monkeypatch.setattr("src.orchestration.delivery_adoption.resolve_installation_id", installation)
    async with pg_engine.begin() as conn:
        await conn.run_sync(OrchestrationWorkClaim.__table__.create)
    async with pg_session_factory() as session:
        flow = OrchestrationFlow(org_id=ORG_A, slug="historical", title="Historical", state="running")
        session.add(flow)
        await session.flush()
        node = OrchestrationNode(
            org_id=ORG_A,
            flow_id=flow.id,
            epic_ref="epic",
            wave_ref="wave",
            node_ref="story",
            kind="story",
            title="Already delivered",
            issue_ref=str(ISSUE),
            state="ready",
            attempts=0,
        )
        session.add(node)
        await session.flush()
        node_id, scope = node.id, await _accepted_scope(session, node)
        await session.commit()

    async def adopt():
        async with pg_session_factory() as session:
            try:
                binding = await adopt_delivery(
                    session,
                    org_id=ORG_A,
                    node_id=node_id,
                    pr=_pr(),
                    installation_id=4242,
                    actor_id="operator",
                    reason="Verified historical scope",
                    evidence=_green(),
                    expected_scope=scope,
                )
                await session.commit()
                return binding.id
            except BindingError:
                await session.rollback()
                return None

    async def dispatch():
        async with pg_session_factory() as session:
            rows = (
                await session.execute(
                    update(OrchestrationNode)
                    .where(
                        OrchestrationNode.id == node_id,
                        OrchestrationNode.state == "ready",
                        OrchestrationNode.attempts == 0,
                    )
                    .values(state="running", attempts=1)
                )
            ).rowcount
            await session.commit()
            return rows

    adoption, dispatched = await asyncio.gather(adopt(), dispatch())
    assert bool(adoption) != bool(dispatched)
    async with pg_session_factory() as session:
        node = await session.get(OrchestrationNode, node_id)
        bindings = (await session.scalars(select(OrchestrationPullRequestBinding))).all()
        if adoption:
            assert node.state == "awaiting_merge" and node.attempts == 0
            assert len(bindings) == 1 and bindings[0].run_id is None
        else:
            assert node.state == "running" and node.attempts == 1 and not bindings
