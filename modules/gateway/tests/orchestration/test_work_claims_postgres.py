"""Real-PostgreSQL concurrency tests for shared issue ownership (#5127).

The story requires "PostgreSQL concurrency integration tests, not SQLite-only
locking assertions", and that requirement is not pedantry. Everything that makes
`claim_work` safe under genuine concurrency is PostgreSQL behavior that SQLite
either lacks or fakes:

- `SELECT ... FOR UPDATE` is a **no-op** on SQLite. The row lock that forces the
  second caller to wait and then observe the winner's committed state simply does
  not exist there, so `tests/orchestration/test_work_claims.py` — which runs on
  SQLite — proves the *semantics* and deliberately proves nothing about locking.
- SQLite's default isolation and single-writer model hide the exact interleaving
  that produces a double admission: two transactions that both read "no owner"
  before either inserts.
- The unique index is the correctness backstop, and `IntegrityError` on a
  concurrent insert is what `claim_work` converts into a refusal. That path is
  unreachable without two genuinely concurrent writers.

So these tests use two real connections against a real server, with the second
overlapping the first, and assert the only acceptable outcome: **one admission
and one refusal**, never two admissions.

`asyncio.gather` on two separate sessions, not threads: the sessions are real and
concurrent at the database, while the failure mode stays reproducible rather than
depending on OS thread scheduling. Same reasoning the SQLite tests give for
modelling races as two passes that observed the same prior state.

Skips (never silently passes) when no PostgreSQL is available — `pgserver`
publishes wheels for Python <= 3.12 only, which CI's Test job uses. A skip here
means "not tested", and the PR reports it as such rather than as a pass.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.orchestration.models import ClaimState, OrchestrationExecution, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.work_claims import (
    ClaimBinding,
    ClaimOwner,
    Disposition,
    OwnerKind,
    ReleaseReason,
    WorkClaimError,
    bind_run,
    claim_work,
    release_work,
)

# Re-exported through tests/migrations/conftest.py, but this file lives in
# tests/orchestration/, so the fixtures are imported explicitly. `pg_server` is
# session-scoped, so a run that also touches the migration tests shares one server.
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

ORG_A = "org-alpha"
ORG_B = "org-beta"
REPO_ID = 987_654_321
ISSUE = 5127


async def test_lost_insert_race_preserves_callers_transaction(pg_session_factory, monkeypatch):
    from src.orchestration import work_claims

    async with pg_session_factory() as session:
        await claim_work(session, binding=_binding(), owner=ClaimOwner(OwnerKind.ENGINE_FLOW, "winner"), event_id="winner")
        await session.commit()

    async with pg_session_factory() as session:
        unrelated = await claim_work(
            session, binding=_binding(issue=ISSUE + 1), owner=ClaimOwner(OwnerKind.ENGINE_FLOW, "unrelated"), event_id="unrelated"
        )
        original = work_claims._locked_claim

        async def observed_before_winner(session, binding):
            if binding.issue_number == ISSUE:
                return None
            return await original(session, binding)

        monkeypatch.setattr(work_claims, "_locked_claim", observed_before_winner)
        with pytest.raises(WorkClaimError, match="concurrently"):
            await claim_work(session, binding=_binding(), owner=ClaimOwner(OwnerKind.DIRECT_DISPATCH, "loser"), event_id="loser")
        # No rollback from this caller. Both its pending work and session remain
        # usable after the real PostgreSQL unique violation inside admission.
        assert await session.get(OrchestrationWorkClaim, unrelated.claim_id) is not None
        await session.commit()
    async with pg_session_factory() as session:
        assert await session.get(OrchestrationWorkClaim, unrelated.claim_id) is not None


@pytest.fixture
async def pg_engine(pg_url):  # noqa: F811 - pg_url is a fixture, not a shadowed import
    """An async engine on a fresh PostgreSQL database with the claims table.

    The table is created straight from the ORM model rather than by running the
    Alembic chain: this file tests runtime concurrency, so building only the one
    table under test keeps it independent of unrelated migrations. Migration
    correctness — including that the DDL matches this model — is
    `tests/migrations/test_050_orchestration_work_claims.py` instead.
    """
    engine = create_async_engine(to_async_url(pg_url), echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(OrchestrationWorkClaim.__table__.create)
        # Release reads the ledger to preserve any committed continuation.
        await conn.run_sync(OrchestrationFlow.__table__.create)
        await conn.run_sync(OrchestrationNode.__table__.create)
        await conn.run_sync(OrchestrationExecution.__table__.create)
    yield engine
    await engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)


def _binding(org: str = ORG_A, repo_id: int = REPO_ID, issue: int = ISSUE) -> ClaimBinding:
    return ClaimBinding(org_id=org, provider_repository_id=repo_id, issue_number=issue)


async def _attempt(session_factory, owner: ClaimOwner, event_id: str, binding: ClaimBinding):
    """One full admission attempt in its own transaction, committed.

    Committing inside the attempt is what makes the race real: an uncommitted
    winner would be invisible to the loser no matter how the locking behaves.
    Returns the receipt, or the `WorkClaimError` if the attempt lost the insert
    race — both are legitimate outcomes for a *loser*, and the tests assert on the
    combination rather than on which arm fired.
    """
    async with session_factory() as session:
        try:
            receipt = await claim_work(session, binding=binding, owner=owner, event_id=event_id)
            await session.commit()
            return receipt
        except WorkClaimError as exc:
            await session.rollback()
            return exc


def _admitted(results) -> list:
    return [r for r in results if getattr(r, "disposition", None) is Disposition.ADMITTED]


def _refused(results) -> list:
    """Anything that is not an admission: a conflict receipt or a lost-race error."""
    return [r for r in results if getattr(r, "disposition", None) is not Disposition.ADMITTED]


@pytest.mark.integration
class TestConcurrentAdmission:
    """A0-1 under genuine concurrency: exactly one admission, always."""

    async def test_two_simultaneous_claims_admit_exactly_one(self, pg_session_factory):
        """The headline guarantee. Engine and direct dispatch, same instant.

        If this ever reports two admissions, the story's protection is absent
        regardless of what every other test says.
        """
        binding = _binding()
        results = await asyncio.gather(
            _attempt(pg_session_factory, ClaimOwner(kind=OwnerKind.ENGINE_FLOW, ref="flow-one"), "evt-engine", binding),
            _attempt(pg_session_factory, ClaimOwner(kind=OwnerKind.DIRECT_DISPATCH, ref="webhook"), "evt-webhook", binding),
        )

        assert len(_admitted(results)) == 1, f"expected exactly one admission, got {results}"
        assert len(_refused(results)) == 1, f"expected exactly one refusal, got {results}"

        # And the database holds exactly one owner row.
        async with pg_session_factory() as session:
            rows = (await session.execute(select(OrchestrationWorkClaim))).scalars().all()
            assert len(rows) == 1
            assert rows[0].state == ClaimState.HELD.value
            assert rows[0].generation == 1

    @pytest.mark.parametrize("fan_out", [5, 12])
    async def test_many_simultaneous_claims_admit_exactly_one(self, pg_session_factory, fan_out):
        """Scaled up, because a two-way race can pass by luck.

        A retry storm or a fanned-out tick produces many concurrent attempts on one
        issue; the invariant is the same at any width.
        """
        binding = _binding()
        results = await asyncio.gather(
            *[
                _attempt(
                    pg_session_factory,
                    ClaimOwner(kind=OwnerKind.DIRECT_DISPATCH, ref=f"lane-{index}"),
                    f"evt-{index}",
                    binding,
                )
                for index in range(fan_out)
            ]
        )

        assert len(_admitted(results)) == 1, f"expected exactly one admission out of {fan_out}, got {results}"
        assert len(_refused(results)) == fan_out - 1

        async with pg_session_factory() as session:
            count = (await session.execute(text("SELECT count(*) FROM orchestration_work_claims"))).scalar_one()
            assert count == 1

    async def test_concurrent_duplicate_event_never_admits_twice(self, pg_session_factory):
        """The same event delivered twice at once (at-least-once redelivery).

        One attempt admits; the other must NOT admit. It may report a duplicate or
        lose the insert race — both are correct, and neither starts a second run.
        """
        binding = _binding()
        results = await asyncio.gather(
            _attempt(pg_session_factory, ClaimOwner(kind=OwnerKind.ENGINE_FLOW, ref="flow-one"), "evt-same", binding),
            _attempt(pg_session_factory, ClaimOwner(kind=OwnerKind.ENGINE_FLOW, ref="flow-one"), "evt-same", binding),
        )

        assert len(_admitted(results)) == 1, f"a duplicate event produced {len(_admitted(results))} admissions: {results}"

    async def test_concurrent_claims_on_different_issues_all_admit(self, pg_session_factory):
        """The lock must not serialize unrelated work.

        Overbroad refusal is a listed failure mode: if the row lock were taken on
        something coarser than the binding, concurrent claims on *different* issues
        would block each other and the engine would lose its parallelism.
        """
        results = await asyncio.gather(
            *[
                _attempt(
                    pg_session_factory,
                    ClaimOwner(kind=OwnerKind.ENGINE_FLOW, ref=f"flow-{issue}"),
                    f"evt-{issue}",
                    _binding(issue=issue),
                )
                for issue in range(9_001, 9_007)
            ]
        )

        assert len(_admitted(results)) == 6, f"unrelated issues blocked each other: {results}"

    async def test_concurrent_claims_across_tenants_all_admit(self, pg_session_factory):
        """Same issue number, different tenants — independent by the unique key."""
        results = await asyncio.gather(
            _attempt(pg_session_factory, ClaimOwner(kind=OwnerKind.ENGINE_FLOW, ref="flow-a"), "evt-a", _binding(org=ORG_A)),
            _attempt(pg_session_factory, ClaimOwner(kind=OwnerKind.ENGINE_FLOW, ref="flow-b"), "evt-b", _binding(org=ORG_B)),
        )

        assert len(_admitted(results)) == 2, f"tenants blocked each other: {results}"


@pytest.mark.integration
class TestUniqueIndexIsTheBackstop:
    """The database, not the application logic, is what makes this correct."""

    async def test_binding_is_unique_at_the_database_level(self, pg_engine):
        """A direct duplicate INSERT is rejected by PostgreSQL.

        Asserted against raw SQL rather than through `claim_work`, because the
        point is that the guarantee does not depend on the admission code being
        called: a future launch path that writes this table directly still cannot
        create two owners.
        """
        insert = text(
            "INSERT INTO orchestration_work_claims "
            "(id, org_id, provider_repository_id, issue_number, owner_kind, owner_ref, state, generation, claimed_at, created_at) "
            "VALUES (:id, :org, :repo, :issue, 'engine_flow', :ref, 'held', 1, now(), now())"
        )
        params = {"org": ORG_A, "repo": REPO_ID, "issue": ISSUE}

        async with pg_engine.begin() as conn:
            await conn.execute(insert, {**params, "id": "claim-1", "ref": "flow-one"})

        # `IntegrityError` specifically, not a bare `Exception`: a broad catch here
        # would also swallow a typo in the SQL above and report a passing test for a
        # constraint that was never exercised.
        with pytest.raises(IntegrityError) as exc:
            async with pg_engine.begin() as conn:
                await conn.execute(insert, {**params, "id": "claim-2", "ref": "flow-two"})

        # Confirm it failed for the right reason, not incidentally.
        assert "uq_orchestration_work_claims_binding" in str(exc.value)

    async def test_released_row_still_blocks_a_second_row_for_the_same_issue(self, pg_engine, pg_session_factory):
        """The unique key covers the binding, not the state.

        A released claim keeps occupying its binding, which is what makes reuse
        *ordered* — the next admission advances the existing row's generation
        instead of inserting a competing row with a reset generation.
        """
        first = await _attempt(pg_session_factory, ClaimOwner(kind=OwnerKind.ENGINE_FLOW, ref="flow-one"), "evt-1", _binding())
        async with pg_session_factory() as session:
            await release_work(
                session,
                org_id=ORG_A,
                claim_id=first.claim_id,
                generation=first.generation,
                reason=ReleaseReason.COMPLETED,
                terminal_evidence="status=complete",
            )
            await session.commit()

        second = await _attempt(pg_session_factory, ClaimOwner(kind=OwnerKind.DIRECT_DISPATCH, ref="lane-two"), "evt-2", _binding())

        assert second.disposition is Disposition.ADMITTED
        assert second.claim_id == first.claim_id, "reuse must advance the existing row, not insert a new one"
        assert second.generation == 2

        async with pg_session_factory() as session:
            count = (await session.execute(text("SELECT count(*) FROM orchestration_work_claims"))).scalar_one()
            assert count == 1

    async def test_provider_repository_id_holds_a_value_beyond_32_bits(self, pg_session_factory):
        """`BigInteger`, not `Integer`. GitHub repository ids are 64-bit provider
        integers, and a value past 2^31 must round-trip rather than overflow."""
        big_repo_id = 9_223_372_036_854_775_100
        receipt = await _attempt(
            pg_session_factory,
            ClaimOwner(kind=OwnerKind.ENGINE_FLOW, ref="flow-one"),
            "evt-1",
            _binding(repo_id=big_repo_id),
        )

        assert receipt.disposition is Disposition.ADMITTED
        async with pg_session_factory() as session:
            row = (await session.execute(select(OrchestrationWorkClaim))).scalar_one()
            assert row.provider_repository_id == big_repo_id


@pytest.mark.integration
class TestConcurrentRunBinding:
    """A0-4 under concurrency: one mutating run, even with a valid generation."""

    async def test_two_workers_racing_to_bind_one_claim_admit_exactly_one(self, pg_session_factory):
        """Both workers hold a *current* generation — the stale-generation check
        cannot help here, so this is what `active_run_id` is for."""
        receipt = await _attempt(pg_session_factory, ClaimOwner(kind=OwnerKind.ENGINE_FLOW, ref="flow-one"), "evt-1", _binding())

        async def _bind(run_id: str):
            async with pg_session_factory() as session:
                result = await bind_run(session, org_id=ORG_A, claim_id=receipt.claim_id, generation=receipt.generation, run_id=run_id)
                await session.commit()
                return result

        results = await asyncio.gather(_bind("run-a"), _bind("run-b"))

        assert len(_admitted(results)) == 1, f"expected one bound run, got {results}"
        blocked = [r for r in results if r.disposition is Disposition.BLOCKED]
        assert len(blocked) == 1
        assert blocked[0].reason == "run_already_bound"
