"""Real-PostgreSQL proof that PMM-06 report-only reads cannot lose a work claim.

Why this file exists next to the mocked tests in ``tests/agentauth/test_model_policy.py``:
the defect it guards against is not "the snapshot code raised an exception". It is
PostgreSQL's aborted-transaction rule. Once any statement in a transaction fails,
PostgreSQL refuses every later statement on that connection with ``25P02
in_failed_sql_transaction`` and turns the eventual COMMIT into a silent ROLLBACK
that reports success.

Work admission (``src/orchestration/work_admission.admit_pending``) reserves the
work claim and then runs the optional model-policy snapshot reads on the *same*
``AsyncSession``, deliberately before committing — ``claim_work`` documents that it
"does not commit; the caller owns the transaction boundary". So catching the Python
exception without rolling back to a savepoint discards the reservation while the
producer still receives a successful admission receipt: work everything downstream
believes is owned, with no row to show for it.

A mocked session raising ``SQLAlchemyError`` cannot reproduce that, and neither can
SQLite: in both, the surrounding transaction stays perfectly usable, so the
assertion passes whether or not the fix is present. That is exactly why the mocked
suite missed this. Each test here therefore

  * fails a **real** SQL statement — a genuinely absent table, which is what a
    gateway pod deployed ahead of its migration actually hits — and
  * asserts the claim row is visible **from a separate psycopg2 connection after
    COMMIT**, the only check that distinguishes a durable write from a silent
    rollback.

Three scenarios, because they fail differently:

``test_unavailable_snapshot_preserves_work_claim``
    The report-only *failure* path. Reproduces the reviewed finding directly.

``test_lkg_fallback_success_preserves_work_claim``
    The report-only *success* path, and the subtler one. ``build_root_snapshot``
    catches the preference-read failure **internally** and returns a last-known-good
    snapshot, so no exception ever reaches the outer report-only handler. A savepoint
    wrapped only around the outer call would RELEASE over an already-poisoned
    transaction and still lose the claim — while reporting success. This is the case
    that separates a correct rollback boundary from a plausible-looking one.

``test_admission_owned_session_commits_claim_after_failed_snapshot_read``
    The branch that owns and commits its own session, rather than borrowing the
    producer's. Same guarantee, different transaction owner.

Requires a real server: either ``pgserver`` (Python <= 3.12) or an already-running
PostgreSQL addressed by ``BG_TEST_POSTGRES_URI``. See tests/migrations/README-postgres.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import psycopg2
import pytest
from moto import mock_aws
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.agentauth.grants import AuthorityReference, DelegatedGrant
from src.agentauth.model_policy import ModelPolicyError, build_root_snapshot
from src.orchestration.work_claims import ClaimBinding, ClaimOwner, OwnerKind, claim_work
from tests.migrations.conftest_postgres import to_async_url, upgrade

TENANT = "tenant-pg-a"
NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
REPOSITORY_ID = 4242
ISSUE = 77

# A real user row must exist: the snapshot resolves the grant's human subject to a
# canonical users.id before it ever reads preferences, so without one the run fails
# in identity resolution and never reaches the statement under test.
SEED_USER = """
INSERT INTO users (id, org_id, team_id, email, cognito_sub, user_kind, is_shadow, created_at)
VALUES ('user-pg', :tenant, 'team-pg', 'pg@example.com', 'pg-sub', 'human', false, now())
"""


@pytest.fixture(autouse=True)
def _token_secret(monkeypatch):
    """The allowlist read builds a TokenContext, which requires a signing key.

    A test-only value; it signs nothing that leaves the process.
    """
    monkeypatch.setenv("BG_TOKEN_SECRET_KEY", "postgres-regression-test-key-not-a-secret")


class _Store:
    """The authority store, backed by moto rather than stubbed.

    A hand-written stub is what made an earlier version of this test vacuous: raising
    from the store aborted the run *before* any SQL executed, so the transaction was
    never poisoned and the test passed against the unfixed code. Driving the real
    DynamoDB read path keeps execution going all the way to the preference query.
    """

    def __init__(self, client):
        self.client = client
        self.table = "authority"

    def _read(self, pk, sk):
        return self.client.get_item(
            TableName=self.table,
            Key={"pk": {"S": pk}, "sk": {"S": sk}},
            ConsistentRead=True,
        ).get("Item")


@pytest.fixture
def policy_store():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="authority",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        client.put_item(
            TableName="authority",
            Item={
                "pk": {"S": f"TENANT#{TENANT}"},
                "sk": {"S": "AUTHORITY#authority-pg"},
                "authority_kind": {"S": "github_event"},
                "human_id": {"S": "pg-sub"},
            },
        )
        yield _Store(client)


def _grant() -> DelegatedGrant:
    return DelegatedGrant(
        grant_id="grant-pg",
        tenant_id=TENANT,
        principal="root-pg#1",
        authority=AuthorityReference("github_event", "authority-pg", "pg-sub", TENANT),
        allowed_actions=frozenset(),
        expires_at=NOW + timedelta(hours=4),
    )


@pytest.fixture
def schema(pg_url):
    """A fully migrated database with a seeded user, at Alembic head."""
    upgrade(pg_url, "head")
    connection = psycopg2.connect(pg_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(SEED_USER.replace(":tenant", "%s"), (TENANT,))
    finally:
        connection.close()
    return pg_url


def _break_preference_read(pg_url: str) -> None:
    """Make the preference read fail for real, the way an unmigrated pod does.

    Dropping the table produces a genuine ``42P01`` from the server, so the
    transaction is actually aborted. Nothing else in these paths touches it.
    """
    connection = psycopg2.connect(pg_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE persona_model_preferences")
    finally:
        connection.close()


def _persisted_claims(pg_url: str) -> int:
    """Count claim rows over a FRESH connection: only a real COMMIT is visible."""
    connection = psycopg2.connect(pg_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM orchestration_work_claims WHERE org_id = %s AND issue_number = %s",
                (TENANT, ISSUE),
            )
            return cursor.fetchone()[0]
    finally:
        connection.close()


async def _reserve_claim(session: AsyncSession, invocation_id: str) -> None:
    """Reserve the lane exactly as ``work_admission.admit`` does, without committing."""
    receipt = await claim_work(
        session,
        binding=ClaimBinding(TENANT, REPOSITORY_ID, ISSUE),
        owner=ClaimOwner(OwnerKind.DIRECT_DISPATCH, "flow-pg"),
        event_id=invocation_id,
    )
    assert receipt.admitted, f"precondition failed: the claim was not admitted ({receipt.reason})"


async def _snapshot(session: AsyncSession, store, invocation_id: str, now: datetime = NOW):
    return await build_root_snapshot(
        session,
        store=store,
        invocation_id=invocation_id,
        tenant_id=TENANT,
        execution={"flow_id": {"S": "chain-pg"}},
        grant=_grant(),
        now=now,
    )


async def test_unavailable_snapshot_preserves_work_claim(schema, policy_store):
    """A failed report-only read must not take the producer's reservation with it.

    On the unfixed code the receipt still reports ``snapshot_cache_missing`` and the
    COMMIT still *succeeds* — it is simply a rollback in disguise, and the claim row
    count is 0.
    """
    _break_preference_read(schema)
    engine = create_async_engine(to_async_url(schema))
    try:
        async with AsyncSession(engine) as session:
            await _reserve_claim(session, "inv-unavailable")

            # No last-known-good row is cached, so this surfaces as unavailable —
            # the reason string must not change just because the read is isolated.
            with pytest.raises(ModelPolicyError) as failure:
                await _snapshot(session, policy_store, "inv-unavailable")
            assert failure.value.reason == "snapshot_cache_missing"

            # Decisive: on an aborted transaction this raises 25P02 instead.
            await session.execute(text("SELECT 1"))
            await session.commit()
    finally:
        await engine.dispose()

    assert _persisted_claims(schema) == 1, "the reserved work claim was silently discarded by a report-only snapshot read"


async def test_lkg_fallback_success_preserves_work_claim(schema, policy_store):
    """The internally-swallowed *success* path must also leave a usable transaction.

    The last-known-good row is warmed by a genuine successful snapshot first, so the
    fallback is reached the way production reaches it rather than by patching the
    loader. ``build_root_snapshot`` then returns a successful snapshot despite the
    failed statement — which is why the savepoint has to wrap the read itself.
    """
    engine = create_async_engine(to_async_url(schema))
    try:
        # 1. A healthy read, which caches the last-known-good snapshot.
        async with AsyncSession(engine) as session:
            warm = await _snapshot(session, policy_store, "inv-warm")
            assert warm.source == "live", "precondition failed: the warming read was not a live snapshot"
            await session.commit()

        _break_preference_read(schema)

        # 2. The same read against the broken schema, holding an uncommitted claim.
        async with AsyncSession(engine) as session:
            await _reserve_claim(session, "inv-lkg")
            snapshot = await _snapshot(session, policy_store, "inv-lkg", now=NOW + timedelta(minutes=1))
            assert snapshot.source == "last_known_good_cache", "expected the swallowed-error fallback, not a live read"

            await session.execute(text("SELECT 1"))
            await session.commit()
    finally:
        await engine.dispose()

    assert _persisted_claims(schema) == 1, "the successful last-known-good fallback left the transaction aborted and lost the claim"


async def test_admission_owned_session_commits_claim_after_failed_snapshot_read(schema, policy_store):
    """The admission-owned branch must persist its claim too.

    ``admit_pending`` opens and commits its own session when the caller supplies
    none. The savepoint is what makes that final ``commit()`` a real one; the
    ``async with`` block below mirrors that ownership.
    """
    _break_preference_read(schema)
    engine = create_async_engine(to_async_url(schema))
    try:
        async with AsyncSession(engine) as owned_session:
            await _reserve_claim(owned_session, "inv-owned")
            with pytest.raises(ModelPolicyError):
                await _snapshot(owned_session, policy_store, "inv-owned")
            await owned_session.commit()
    finally:
        await engine.dispose()

    assert _persisted_claims(schema) == 1, "the admission-owned session committed nothing after a failed report-only read"
