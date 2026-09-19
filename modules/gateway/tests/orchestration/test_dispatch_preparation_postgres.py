"""PMM-07: preparation on a real PostgreSQL transaction, not on SQLite.

The other two preparation test files run on SQLite, which is right for behaviour
but silently unable to test the one database property this seam depends on.
SQLite ignores a failed statement's effect on the surrounding transaction;
PostgreSQL aborts the whole transaction on the first error (``25P02``) and
refuses every later statement until a rollback. That difference is the entire
reason `build_root_snapshot` wraps its report-only reads in savepoints, and on
SQLite a version with the savepoints removed still passes.

It matters here specifically because of what preparation runs next to. The tick
reserves a work claim -- the row that makes "exactly one mutating run per issue"
hold -- on the same `AsyncSession` the snapshot build later reads from. Two
distinct hazards follow from an unguarded failed read, and they are worth keeping
apart because only one of them is what these tests observe:

- A claim that is still *uncommitted* when the read fails is lost outright: the
  abort takes the whole transaction, claim included, and `ROLLBACK` is the only
  legal next statement. That is the admission path, where
  `ensure_snapshot_for_admission` reads inside the tick transaction before the
  commit.
- A claim that is already *committed* -- the case here, since preparation runs
  after the tick commits -- is durable and cannot be undone by anything that
  happens afterwards. The damage is to the session, not the row: the aborted
  transaction refuses every subsequent statement, so the rest of the tick's
  post-commit work on that session fails, and the claim becomes unreadable
  through it even though it is safely on disk.

These tests pin the second hazard, which is the one this seam can actually
produce. They prove the injected failure is contained to the read that caused it:
the session stays usable afterwards, and the row is confirmed durable from a
fresh connection rather than only from the session that wrote it.

Two cases, deliberately: the session and claim survive a snapshot build that
fails partway, and they survive one that succeeds. Bounded on purpose -- this
file exists to prove the transaction property on real SQL, not to re-run the
behavioural matrix that `test_dispatch_preparation.py` already covers on a
faster engine.

Marked `integration` and skipped (not failed) where `pgserver` is unavailable,
following `test_execution_runner_postgres.py`. DynamoDB is still moto and SQS is
still a double: the database is the only thing being made real here.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.orchestration.dispatch_pass import prepare_pending, publish_pending, run_dispatch_pass
from src.orchestration.models import ClaimState, OrchestrationWorkClaim
from src.shared.models.base import Base

# Imported for their side effect on the shared metadata: `create_all` below must
# emit these tables or the snapshot build's reads fail for the wrong reason.
from src.shared.models.persona_models import (  # noqa: F401
    PersonaModelPolicySetting,
    PersonaModelPreference,
)
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.orchestration.test_dispatch_pass import ORG_A, FakeSQS, _config, _ready_story
from tests.orchestration.test_dispatch_pass import protected_engine as protected_engine_fixture
from tests.orchestration.test_dispatch_pass import run_store as run_store_fixture

protected_engine = protected_engine_fixture
run_store = run_store_fixture

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_session_factory(pg_url):  # noqa: F811
    """A real PostgreSQL 16 database with the full ORM schema.

    `create_all` over the whole metadata rather than a hand-picked table list: the
    snapshot build reads users, persona preferences and policy settings as well as
    the orchestration tables, and a missing table would surface as an
    `unavailable` receipt that looked exactly like the failure under test.
    """
    engine = create_async_engine(to_async_url(pg_url), echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def work_claims_on(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(return_value=5478))


async def test_a_failing_snapshot_read_leaves_the_committed_work_claim_intact(pg_session_factory, protected_engine, work_claims_on):
    """The failure mode SQLite cannot show: a poisoned session after the commit.

    A real broken read is injected -- `SELECT` against a table that does not exist,
    which is what a mid-deploy schema skew actually produces -- at the point
    `build_root_snapshot` reads persona preferences. On PostgreSQL that aborts the
    transaction. The claim row itself is already committed and so cannot be lost;
    what breaks without the savepoint is the *session*, which then refuses every
    later statement, leaving the claim unreadable through it and the remaining
    post-commit work unable to run.

    What must hold: the receipt says the evidence is unavailable, the session is
    still usable, the claim row still reads back as `held` by this run -- both
    through this session and from a fresh connection -- and the dispatch still
    publishes. Report-only means a snapshot defect costs the run nothing.
    """
    from sqlalchemy import text

    store, writer = protected_engine
    async with pg_session_factory() as session:
        flow, _, _ = await _ready_story(session)
        report = await run_dispatch_pass(session, _config())
        await session.commit()
        assert report.dispatched == 1
        invocation_id = report.pending[0].invocation_id()

        # The claim exists and is durable *before* preparation runs. Asserted, not
        # assumed: the whole point is what happens to an already-committed row.
        claim_before = (await session.scalars(select(OrchestrationWorkClaim))).one()
        assert claim_before.state == ClaimState.HELD.value
        assert claim_before.active_run_id == invocation_id

        real_scalars = AsyncSession.scalars
        broke = {"done": False}

        async def failing_once(self, statement, *args, **kwargs):
            # Break exactly the preference read, once. Narrow on purpose: breaking
            # every read would test the fallback path instead of the savepoint.
            if not broke["done"] and "persona_model_preferences" in str(statement):
                broke["done"] = True
                return await real_scalars(self, text("SELECT 1 FROM table_removed_by_a_partial_deploy"), *args, **kwargs)
            return await real_scalars(self, statement, *args, **kwargs)

        # A private `MonkeyPatch` rather than the `monkeypatch` fixture, because the
        # injection has to be undone *mid-test* while the fixtures' own patches stay
        # up. pytest hands one `MonkeyPatch` instance to a test and every fixture it
        # uses, so `monkeypatch.undo()` here would also revert `EngineRunStore`, the
        # authority writer and the feature env vars -- and the publish below would
        # then reach a table moto never created, failing for a reason that has
        # nothing to do with savepoints.
        with pytest.MonkeyPatch.context() as injected:
            injected.setattr(AsyncSession, "scalars", failing_once)
            await prepare_pending(session, report, writer=writer)

        assert broke["done"], "the injected failure never fired, so nothing was proved"

        receipt = report.model_policy_receipts[invocation_id]
        assert receipt["status"] == "unavailable"
        # The reason is itself evidence that the savepoint unwound *before* the
        # fallback ran: a failed preference read routes into the last-known-good
        # cache lookup, and reaching that lookup at all means the transaction was
        # usable again. On an aborted transaction it would never be attempted.
        assert receipt["reason"] == "snapshot_cache_missing"

        # The session is still usable: proof the transaction was not left aborted.
        claim_after = (await session.scalars(select(OrchestrationWorkClaim))).one()
        assert claim_after.state == ClaimState.HELD.value
        assert claim_after.active_run_id == invocation_id
        assert claim_after.owner_ref == flow.id
        assert claim_after.generation == claim_before.generation

        # And the dispatch is unaffected: it publishes, and the protected record
        # that authorizes it is intact.
        sqs = FakeSQS()
        publish_pending(report, _config(), client=sqs)
        assert len(sqs.calls) == 1
        assert store._read(f"TENANT#{ORG_A}", f"EXEC#{invocation_id}")["status"] == {"S": "pending"}
        assert report.publish_failed == 0

    # Committed state, read on a *fresh* connection -- the only way to show the row
    # really survived rather than merely still being visible in a dirty session.
    async with pg_session_factory() as verify:
        await verify.commit()
        persisted = (await verify.scalars(select(OrchestrationWorkClaim))).one()
        assert persisted.active_run_id == invocation_id
        assert persisted.state == ClaimState.HELD.value


async def test_a_successful_snapshot_build_commits_alongside_its_work_claim(pg_session_factory, protected_engine, work_claims_on):
    """The happy path on real SQL, so the guard above is not the only evidence.

    The savepoints `build_root_snapshot` opens have to be harmless when nothing
    fails -- a `begin_nested` left un-exited, or one that swallowed a successful
    read's results, would show up here and nowhere in the SQLite tests. The claim
    and the snapshot must both be durable afterwards.
    """
    store, writer = protected_engine
    async with pg_session_factory() as session:
        await _ready_story(session)
        report = await run_dispatch_pass(session, _config())
        await session.commit()
        invocation_id = report.pending[0].invocation_id()

        await prepare_pending(session, report, writer=writer)

        assert report.model_policy_receipts[invocation_id]["status"] == "available"
        execution = store._read(f"TENANT#{ORG_A}", f"EXEC#{invocation_id}")
        assert execution["model_policy_snapshot_digest"]["S"] == report.model_policy_receipts[invocation_id]["snapshot_digest"]
        assert execution["status"] == {"S": "pending"}

        sqs = FakeSQS()
        publish_pending(report, _config(), client=sqs)
        assert len(sqs.calls) == 1

    async with pg_session_factory() as verify:
        claim = (await verify.scalars(select(OrchestrationWorkClaim))).one()
        assert claim.active_run_id == invocation_id
        assert claim.state == ClaimState.HELD.value
