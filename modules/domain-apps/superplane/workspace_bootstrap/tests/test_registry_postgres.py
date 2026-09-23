"""`SqlRegistrationStore` against a real PostgreSQL and the real schema — F10, F11.

Issue #5533 (w6-10), EPIC #4910.

Two findings meet here, and neither could be closed by the offline suite.

## F11: the SQL had never met a table

Review finding F11 verbatim in substance: the store was implemented and tested entirely
against a scripted double, so nothing established that its statements were valid against a
schema that exists — and no migration created the table they name. `017_add_workspace_
bootstrap_reservations` is the schema half of that repair; this file is the half that runs
the production statements against it. `test_schema_postgres.py` covers the migration's own
constraints; here the migration is applied and then every assertion goes through
`SqlRegistrationStore` itself.

## F10: two processes, which is a thing one process cannot be

Review finding F10 verbatim: "After the transaction-scoped advisory lock is released, a
second process with the same identity encounters the existing `reserved` row and receives
`reserved: True`. Both processes can then mutate the namespace, controller, taint, state,
and registration concurrently. [...] A losing process may also release the shared
reservation while the winner is active, causing the winner's finalization to fail and
re-taint the workspace."

`tests/test_registry.py` pins the decisions that fix this with a scripted row. What it
cannot do is have two stores on two connections actually race, which is the situation the
finding describes — so that is what happens below: TWO `SqlRegistrationStore` instances,
each with its own connection to one database, reserving the same workspace.

The distinction the tests draw throughout is between SERIALIZING and EXCLUDING. The
advisory lock serializes: the second `reserve` waits. It does not exclude, because it is
transaction-scoped and therefore released when `reserve` commits, while the eight steps it
was protecting all run afterwards. Exclusion is the `attempt_token`, and the tests that
matter most here are the ones where the loser has a live connection, a valid identity, and
still cannot touch the winner's claim.

No credential appears in this file. The server is a disposable one created by the fixture;
see `postgres_support.py` for why it cannot reach a real database.
"""

from __future__ import annotations

import json

import pytest
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.registry import (
    REGISTERED,
    RESERVED,
    SqlRegistrationStore,
)
from superplane_bootstrap.state import CLAIM_DIGEST_PREFIX, claim_fingerprint

from .conftest import (
    ACCOUNT_ID,
    CLUSTER_ARN,
    CLUSTER_NAME,
    NAMESPACE,
    ORG_ID,
    REGION,
    WORKSPACE_ID,
)
from .postgres_support import (
    AsyncpgStore,
    _Loop,
    render_migration_ddl,
    require_asyncpg,
    require_pgserver,
)

TABLE = "workspace_bootstrap_reservations"

RESERVATION_IDENTITY: dict[str, str] = {
    "workspace_id": WORKSPACE_ID,
    "org_id": ORG_ID,
    "account_id": ACCOUNT_ID,
    "region": REGION,
    "cluster_name": CLUSTER_NAME,
    "cluster_arn": CLUSTER_ARN,
    "namespace": NAMESPACE,
}


class _Target:
    """A `WorkspaceTarget`-shaped record, read by attribute as `_target_mapping` does."""

    workspace_id = WORKSPACE_ID
    org_id = ORG_ID
    account_id = ACCOUNT_ID
    region = REGION
    cluster_name = CLUSTER_NAME
    cluster_arn = CLUSTER_ARN
    endpoint = "https://example.invalid"
    namespace = NAMESPACE
    namespace_uid = "namespace-uid-0001"
    cluster_ownership = "adp-created"
    credential_reference_id = "workspace-credential-0001"
    contract_version = "v1"


# --- fixtures --------------------------------------------------------------------


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A disposable PostgreSQL, created for this module and destroyed after it.

    Module-scoped because starting a server costs seconds and the per-test isolation that
    matters is per-DATABASE, not per-server — `database` below creates a fresh one for
    every test, so no test can see another's rows.
    """
    pgserver = require_pgserver()
    instance = pgserver.get_server(tmp_path_factory.mktemp("workspace-bootstrap-pg"))
    try:
        yield instance.get_uri()
    finally:
        instance.cleanup()


@pytest.fixture(scope="module")
def schema_ddl():
    """The whole alembic chain as PostgreSQL DDL, rendered once.

    The WHOLE chain rather than just revision 017, deliberately. A migration that only
    applies to an empty database is not a migration that applies to this database, and
    rendering the chain is also what proves 017 is reachable from the chain's root rather
    than an orphan file.
    """
    upgrade, _ = render_migration_ddl()
    return upgrade


@pytest.fixture
def loop():
    driver = _Loop()
    try:
        yield driver
    finally:
        driver.close()


@pytest.fixture
def database(server, schema_ddl, loop, request):
    """A fresh migrated database per test, dropped afterwards.

    The name carries the test's own name so a leaked database (a test that crashed the
    server, say) says which test leaked it.
    """
    asyncpg = require_asyncpg()
    name = "wb_" + str(abs(hash(request.node.name)))[:12]
    quoted = '"' + name.replace('"', '""') + '"'
    admin = loop.run(asyncpg.connect(server))
    loop.run(admin.execute(f"DROP DATABASE IF EXISTS {quoted}"))
    loop.run(admin.execute(f"CREATE DATABASE {quoted}"))
    migrator = loop.run(asyncpg.connect(server, database=name))
    # Applied to a database created a moment ago and dropped a moment from now. This is
    # the "offline disposable database" the story requires and is NOT a live migration:
    # nothing outside this fixture has ever had a handle on it.
    loop.run(migrator.execute(schema_ddl))
    loop.run(
        migrator.execute(
            "INSERT INTO organizations (id, name, adp_org_id, billing_plan) VALUES ($1, 'test-org', $2, 'free')",
            __import__("uuid").UUID(ORG_ID),
            ORG_ID,
        )
    )
    loop.run(migrator.close())
    connections: list[object] = []

    def connect() -> AsyncpgStore:
        connection = loop.run(asyncpg.connect(server, database=name))
        connections.append(connection)
        return AsyncpgStore(loop, connection)

    try:
        yield connect
    finally:
        for connection in connections:
            loop.run(connection.close())
        loop.run(admin.execute(f"DROP DATABASE IF EXISTS {quoted}"))
        loop.run(admin.close())


@pytest.fixture
def two_stores(database):
    """Two independent stores on two connections to one database.

    This pair IS the F10 scenario. Returned as a tuple rather than built inside each test
    because every concurrency test needs exactly this and a test that accidentally shared
    one connection would prove nothing while passing.
    """
    first, second = database(), database()
    return (
        SqlRegistrationStore(store=first),
        SqlRegistrationStore(store=second),
        first,
        second,
    )


# --- F11: the statements run against the real schema -----------------------------


def test_a_reservation_round_trips_through_the_migrated_schema(database):
    """The claim F11 says was never established: this SQL works on this table.

    Reserve, finalize, read — the whole contract, against the schema `017` creates. Every
    offline test in this directory passes with a table that does not exist.
    """
    store = database()
    registry = SqlRegistrationStore(store=store)

    outcome = registry.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    assert outcome["reserved"] is True
    assert outcome["replayed"] is False

    registry.finalize(_Target(), str(outcome["attempt_token"]))
    recorded = registry.read(WORKSPACE_ID)

    # Read by ATTRIBUTE, because that is how `registration._refuse_conflict` reads it —
    # the store is free to return its own row type and this one is `_RecordedRegistration`.
    assert recorded is not None
    assert recorded.workspace_id == WORKSPACE_ID
    assert recorded.cluster_arn == CLUSTER_ARN
    assert recorded.namespace_uid == "namespace-uid-0001"


def test_the_reserved_row_is_visible_to_a_second_connection(database):
    """Durability across connections, which is the property the row exists for.

    The F5 interruption case is a process that DIES between clearing the taint and writing
    the registration. `_Recorder` keeps its rows in a Python dict, so the offline suite
    asserts this against storage that would vanish with the process. Here the row is read
    by a connection the writer never touched.
    """
    writer, reader = database(), database()
    SqlRegistrationStore(store=writer).reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    rows = reader.fetch(
        f"SELECT workspace_id, state, org_id FROM {TABLE} WHERE workspace_id = $1",
        WORKSPACE_ID,
    )

    assert rows == [{"workspace_id": WORKSPACE_ID, "state": RESERVED, "org_id": ORG_ID}]


def test_the_stored_identity_is_the_canonical_serialization(database):
    """`registry.py` compares identities as `sort_keys=True` JSON text.

    Asserted against a real column because this is why `identity_json` is `text` and not
    `jsonb`: `jsonb` normalizes, so the bytes read back would not be the bytes written and
    the identity comparison would run against a value nothing produced.
    """
    store = database()
    SqlRegistrationStore(store=store).reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    stored = store.fetch(f"SELECT identity_json FROM {TABLE}")[0]["identity_json"]

    assert stored == json.dumps(RESERVATION_IDENTITY, sort_keys=True)


def test_a_workspace_id_containing_sql_is_stored_as_data(database):
    """Parameter binding, verified by a database rather than by reading the constant.

    `tests/test_registry.py` asserts no interpolation by inspecting the statement text.
    That is the right offline assertion and it is not the same claim: this one would fail
    if the binding were wrong in a way the text inspection cannot see.
    """
    hostile = "'; DROP TABLE workspace_bootstrap_reservations; --"
    store = database()
    registry = SqlRegistrationStore(store=store)

    outcome = registry.reserve(
        hostile, {**RESERVATION_IDENTITY, "workspace_id": hostile}
    )

    assert outcome["reserved"] is True
    assert store.fetch(f"SELECT workspace_id FROM {TABLE}") == [
        {"workspace_id": hostile}
    ]


# --- F10: two stores, one workspace ---------------------------------------------


def test_the_second_store_is_refused_not_handed_the_same_claim(two_stores):
    """The finding's first half, with two real connections.

    "A second process with the same identity encounters the existing `reserved` row and
    receives `reserved: True`. Both processes can then mutate the namespace, controller,
    taint, state, and registration concurrently."

    Same identity, same workspace, separate connection: refused, and — the part that makes
    the refusal safe rather than merely discouraging — handed no token, so there is nothing
    it could authorize a mutation with.
    """
    winner, loser, _, _ = two_stores

    first = winner.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    second = loser.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    assert first["reserved"] is True
    assert second["reserved"] is False
    assert "attempt_token" not in second
    assert "already holds an unfinalized reservation" in str(second["conflict"])


def test_only_one_of_two_racing_stores_gets_a_claim(two_stores):
    """Exactly one reservation exists afterwards, and exactly one token was issued."""
    winner, loser, store, _ = two_stores

    outcomes = [
        winner.reserve(WORKSPACE_ID, RESERVATION_IDENTITY),
        loser.reserve(WORKSPACE_ID, RESERVATION_IDENTITY),
    ]

    assert [o["reserved"] for o in outcomes] == [True, False]
    assert [o for o in outcomes if "attempt_token" in o] != []
    assert len([o for o in outcomes if "attempt_token" in o]) == 1
    assert store.fetch(f"SELECT count(*) AS n FROM {TABLE}") == [{"n": 1}]


def test_the_loser_cannot_finalize_the_winners_claim(two_stores):
    """A live connection, a valid identity, no token — and no authority.

    The offline test asserts the refusal against a scripted row. This one asserts it
    against a row the WINNER actually holds, from a second connection that could otherwise
    write to it freely.
    """
    winner, loser, store, _ = two_stores
    winner.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    with pytest.raises(BootstrapRefused, match="held by a different bootstrap attempt"):
        loser.finalize(_Target(), "an-attempt-token-the-loser-made-up")

    assert store.fetch(f"SELECT state FROM {TABLE}") == [{"state": RESERVED}]


def test_the_loser_cannot_release_the_winners_claim(two_stores):
    """The finding's second half, which is the one that re-taints a live workspace.

    "A losing process may also release the shared reservation while the winner is active,
    causing the winner's finalization to fail and re-taint the workspace."

    The loser's DELETE is narrowed by `attempt_token`, so it matches no row. It is told it
    released nothing — which is true — and the winner's claim is still there.
    """
    winner, loser, store, _ = two_stores
    winner.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    released = loser.release(WORKSPACE_ID, "an-attempt-token-the-loser-made-up")

    assert released is False
    assert store.fetch(f"SELECT count(*) AS n FROM {TABLE}") == [{"n": 1}]


def test_the_winner_still_finalizes_after_the_loser_tried_everything(two_stores):
    """The consequence that made F10 a blocker, asserted as an outcome.

    A loser that could release the claim would make the winner's `finalize` fail — and the
    winner, believing its own registration failed, re-taints a workspace that is in fact
    fully bootstrapped. So the assertion is not just "the loser was refused" but "the
    winner's bootstrap completed anyway", after the loser attempted both mutations.
    """
    winner, loser, store, _ = two_stores
    claim = winner.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    with pytest.raises(BootstrapRefused):
        loser.finalize(_Target(), "an-attempt-token-the-loser-made-up")
    assert loser.release(WORKSPACE_ID, "an-attempt-token-the-loser-made-up") is False

    winner.finalize(_Target(), str(claim["attempt_token"]))

    assert store.fetch(f"SELECT state FROM {TABLE}") == [{"state": REGISTERED}]
    assert winner.read(WORKSPACE_ID) is not None


def test_the_winner_can_release_its_own_claim_leaving_nothing_behind(two_stores):
    """The refusal path's cleanup, which is what makes a refused bootstrap retryable.

    A store that refused after reserving must leave no row, or every later attempt for that
    workspace is refused as a live claim forever.
    """
    winner, _, store, _ = two_stores
    claim = winner.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    assert winner.release(WORKSPACE_ID, str(claim["attempt_token"])) is True
    assert store.fetch(f"SELECT count(*) AS n FROM {TABLE}") == [{"n": 0}]


def test_after_a_release_the_other_store_can_reserve(two_stores):
    """Convergence: the loser is refused while the claim is live, and only while.

    Without this the F10 fence would be a denial of service — correct exclusion that never
    lets go. The loser reserves successfully the moment the winner's claim is gone, and it
    gets its OWN token rather than inheriting the released one.
    """
    winner, loser, _, _ = two_stores
    claim = winner.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    winner.release(WORKSPACE_ID, str(claim["attempt_token"]))

    second = loser.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    assert second["reserved"] is True
    assert second["replayed"] is False
    assert second["attempt_token"] != claim["attempt_token"]


def test_a_completed_registration_is_a_replay_for_either_store(two_stores):
    """Completed-registration idempotence, preserved across the F10 fence.

    Re-running a finished bootstrap is the most ordinary operator action there is, and it
    must still work from a process that holds no token — the token that published the
    registration is long gone. Safe precisely because a `registered` row is not a live
    attempt: the replay is handed no token, so it cannot finalize or release anything.
    """
    winner, other, _, _ = two_stores
    claim = winner.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    winner.finalize(_Target(), str(claim["attempt_token"]))

    replay = other.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    assert replay["reserved"] is True
    assert replay["replayed"] is True
    assert "attempt_token" not in replay


def test_a_second_store_rebinding_to_another_cluster_is_refused(two_stores):
    """A different identity is a rebinding, and is refused with the divergence named.

    Distinct from the same-identity refusal above: both are `reserved: False`, but an
    operator acts on them differently, so the conflict text must distinguish them.
    """
    winner, other, _, _ = two_stores
    winner.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    elsewhere = {
        **RESERVATION_IDENTITY,
        "cluster_arn": CLUSTER_ARN.replace("cluster/", "cluster/other-"),
    }

    outcome = other.reserve(WORKSPACE_ID, elsewhere)

    assert outcome["reserved"] is False
    assert "cluster_arn" in str(outcome["conflict"])


def test_a_finalize_with_no_prior_reservation_is_refused(database):
    """The claim is gone, so something else may own this workspace now.

    Against a real table, so "no row" means the database found none rather than a double
    having been scripted to say so.
    """
    registry = SqlRegistrationStore(store=database())

    with pytest.raises(BootstrapRefused, match="no reservation exists"):
        registry.finalize(_Target(), "a-token-for-a-claim-that-does-not-exist")


# --- F13: the recovery release, fenced on a claim fingerprint -------------------
#
# Against a real server because the fence lives in SQL: the statement digests the
# `attempt_token` column and compares it to a digest Python computed. Those two
# computations agreeing is the whole mechanism, and no double can check it — a scripted
# store would happily report a match for a digest PostgreSQL would never produce. Note the
# direction that failure takes: a mismatch deletes NOTHING, which looks exactly like the
# safe stale case. It would pass every offline test in the suite while making recovery
# permanently unable to clear an abandoned claim.


def test_the_recovery_release_drops_a_claim_whose_holder_is_gone(two_stores):
    """Resumability after a death, which the fence would otherwise have removed.

    The winner stands in for a process that died holding its token. From inside `reserve`
    its row is indistinguishable from a live one, so taking over needs authority the
    database does not have: the durable record, which names the claim by fingerprint.

    This is the case recovery EXISTS for, and F13's fix had to preserve it — a fence that
    also blocked legitimate recovery would have converted a concurrent-mutation defect into
    one permanently-stuck workspace per crash.
    """
    winner, recovery, store, _ = two_stores
    claim = winner.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    recorded = claim_fingerprint(str(claim["attempt_token"]))

    assert recovery.release_claim(WORKSPACE_ID, recorded) is True
    assert store.fetch(f"SELECT count(*) AS n FROM {TABLE}") == [{"n": 0}]


def test_the_sql_digest_and_the_python_fingerprint_agree(two_stores):
    """The two halves of the fence, pinned against each other on a real server.

    `claim_fingerprint` hashes in Python; `_DELETE_CLAIMED_RESERVATION` hashes in SQL. If
    they disagree — a different prefix, a different encoding, `digest()` vs `sha256()` —
    then nothing matches, nothing is ever released, and every offline test still passes
    because "released nothing" is also the correct answer for a stale record.

    Asserted directly rather than only through the delete, so a failure says WHICH half
    drifted instead of presenting as an unexplained non-deletion.
    """
    winner, _, store, _ = two_stores
    claim = winner.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    in_sql = store.fetch(
        "SELECT encode(sha256(convert_to($1 || attempt_token, 'UTF8')), 'hex') AS d "
        f"FROM {TABLE} WHERE workspace_id = $2",
        CLAIM_DIGEST_PREFIX.decode(),
        WORKSPACE_ID,
    )

    assert in_sql == [{"d": claim_fingerprint(str(claim["attempt_token"]))}]


def test_a_stale_fingerprint_does_not_delete_a_live_successors_claim(two_stores):
    """**The F13 defect at the store level.**

    The predecessor's claim is released legitimately. A successor then takes the workspace
    — a different attempt, a different token, a live claim. Recovery run from the
    PREDECESSOR's record must not touch it.

    `release_abandoned` deleted it, because its statement was keyed on the workspace and
    the `reserved` state and nothing else. That deletion admitted a third writer while the
    successor was still mutating the namespace, controller, taint and registration: the
    concurrent-mutation defect the token fence exists to prevent, reached around the fence.

    Two connections, because one connection could not show this — what is being asserted is
    what a SECOND session's committed row looks like to the recovering one.
    """
    predecessor, recovery, store, _ = two_stores
    first = predecessor.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    stale = claim_fingerprint(str(first["attempt_token"]))
    predecessor.release(WORKSPACE_ID, str(first["attempt_token"]))

    successor = predecessor.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    live_token = str(successor["attempt_token"])
    assert live_token != str(first["attempt_token"])

    assert recovery.release_claim(WORKSPACE_ID, stale) is False, (
        "a stale record was reported as having released a claim it does not own"
    )
    assert store.fetch(f"SELECT attempt_token FROM {TABLE}") == [
        {"attempt_token": live_token}
    ], "stale recovery deleted the live successor's claim, admitting a third writer"


def test_a_stale_fingerprint_does_not_delete_a_registered_successors_row(two_stores):
    """The same stale record against a COMPLETED registration.

    Two guards have to hold at once here, and each alone would let this through: the state
    clause (never drop a `registered` row) and the fingerprint (never act on a claim this
    record does not own). A successor that finished bootstrapping is a live, published
    workspace, and deleting its row would unpublish it.
    """
    predecessor, recovery, store, _ = two_stores
    first = predecessor.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    stale = claim_fingerprint(str(first["attempt_token"]))
    predecessor.release(WORKSPACE_ID, str(first["attempt_token"]))

    successor = predecessor.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    predecessor.finalize(_Target(), str(successor["attempt_token"]))

    assert recovery.release_claim(WORKSPACE_ID, stale) is False
    assert store.fetch(f"SELECT state FROM {TABLE}") == [{"state": REGISTERED}]


def test_the_recovery_release_will_not_drop_a_completed_registration(two_stores):
    """Still narrowed by `state = 'reserved'`, so it is not a hole in the fence.

    Here the fingerprint MATCHES — this is the attempt's own claim, finalized rather than
    abandoned — so the state clause is the only thing standing between recovery and
    unpublishing a fully bootstrapped workspace. That makes this the test that pins the
    clause rather than incidentally passing because of the fingerprint.
    """
    winner, recovery, store, _ = two_stores
    claim = winner.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    token = str(claim["attempt_token"])
    winner.finalize(_Target(), token)

    assert recovery.release_claim(WORKSPACE_ID, claim_fingerprint(token)) is False
    assert store.fetch(f"SELECT state FROM {TABLE}") == [{"state": REGISTERED}]


def test_the_recovery_release_reports_false_when_there_was_nothing_to_release(database):
    """`RETURNING` is what makes this answer earned rather than assumed.

    `workspace.py` records it in `BootstrapOutcome.reservation_released`, so an
    unconditional True would be a reported fact nothing established.
    """
    registry = SqlRegistrationStore(store=database())

    assert (
        registry.release_claim(WORKSPACE_ID, claim_fingerprint("a-token-never-issued"))
        is False
    )


# --- the lock: serialization, which is necessary and not sufficient ------------


def test_reserve_holds_the_advisory_lock_for_the_whole_transaction(database):
    """The lock is taken, and it is transaction-scoped — the fact F10 turns on.

    Asserted from the database's own lock table: after `reserve` commits, no advisory lock
    remains held by that connection. That is correct and deliberate (a crashed process
    leaks nothing), and it is exactly why the eight steps that run after `reserve` are
    unprotected by it — which is what the `attempt_token` is for.
    """
    store = database()
    registry = SqlRegistrationStore(store=store)

    registry.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    held = store.fetch(
        "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
    )
    assert held == [{"n": 0}], (
        "the advisory lock outlived reserve's transaction; a session-scoped lock leaked "
        "by a crashed process blocks every later attempt for this workspace"
    )
    assert any("pg_advisory_xact_lock" in statement for statement in store.statements)


def test_the_primary_key_refuses_a_second_reservation_even_without_the_lock(database):
    """Defence in depth, and the reason `workspace_id` is the PRIMARY KEY.

    The advisory lock is the mechanism `reserve` relies on; the key is what holds if a
    future caller ever reaches the INSERT without it. Driven by issuing the store's own
    INSERT statement twice outside any lock — the situation the lock exists to prevent —
    and the database still refuses the duplicate.
    """
    asyncpg = require_asyncpg()
    store = database()
    registry = SqlRegistrationStore(store=store)
    claim = registry.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    assert claim["reserved"] is True

    from superplane_bootstrap.registry import _INSERT_RESERVATION

    with pytest.raises(asyncpg.UniqueViolationError):
        store.execute(
            _INSERT_RESERVATION,
            {
                "workspace_id": WORKSPACE_ID,
                "state": RESERVED,
                "identity_json": json.dumps(RESERVATION_IDENTITY, sort_keys=True),
                "attempt_token": "a-second-token-for-the-same-workspace",
            },
        )


def test_finalization_is_discoverable_through_controller_management(
    database, monkeypatch
):
    import sys
    from pathlib import Path
    from uuid import UUID
    from sqlalchemy.dialects import postgresql

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/superplane-api"))
    # API models initialize an engine at import time. Give it the same explicit,
    # credential-free target as the API test suite; query execution below uses
    # only the disposable PostgreSQL fixture, never this lazy engine.
    from app.config import settings

    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql+asyncpg://localhost/superplane_offline_test",
    )
    # This unused import-time engine has an explicit local fixture target.
    # Production connections still require their configured CA bundle.
    monkeypatch.setenv("SUPERPLANE_DATABASE_ALLOW_UNVERIFIED_LOCAL_TLS", "true")
    from app.routers.controller_management import registered_targets_query

    store = database()
    registry = SqlRegistrationStore(store=store)
    claim = registry.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    query = str(
        registered_targets_query(UUID(ORG_ID)).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert store.fetch(query) == []
    registry.finalize(_Target(), str(claim["attempt_token"]))
    rows = store.fetch(query)
    assert len(rows) == 1
    assert str(rows[0]["workspace_id"]) == WORKSPACE_ID
    assert rows[0]["namespace"] == NAMESPACE
    assert rows[0]["cluster_arn"] == CLUSTER_ARN
    assert rows[0]["endpoint"] == _Target.endpoint
    assert rows[0]["workspace_status"] == "active"
    metadata = store.fetch("SELECT actual_state_json FROM clusters")[0][
        "actual_state_json"
    ]
    assert (
        json.loads(metadata)["workspace_bootstrap"]["credential_reference_id"]
        == _Target.credential_reference_id
    )
    assert (
        store.fetch(
            str(
                registered_targets_query(
                    UUID("99999999-9999-4999-8999-999999999999")
                ).compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
        )
        == []
    )


def test_canonical_publication_conflict_rolls_back_and_keeps_claim(database):
    from uuid import UUID

    store = database()
    registry = SqlRegistrationStore(store=store)
    claim = registry.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    store.fetch(
        "INSERT INTO organizations (id, name, adp_org_id, billing_plan) VALUES ($1, 'other', 'other', 'free')",
        UUID("99999999-9999-4999-8999-999999999999"),
    )
    store.fetch(
        "INSERT INTO workspaces (id, org_id, name, isolation_mode, status, is_default) VALUES ($1, $2, 'other', 'namespace', 'pending', false)",
        UUID(WORKSPACE_ID),
        UUID("99999999-9999-4999-8999-999999999999"),
    )
    with pytest.raises(BootstrapRefused, match="another tenant"):
        registry.finalize(_Target(), str(claim["attempt_token"]))
    assert store.fetch("SELECT state FROM workspace_bootstrap_reservations") == [
        {"state": RESERVED}
    ]
    assert store.fetch("SELECT id FROM clusters") == []


def test_publication_reuses_selected_cluster_identity(database):
    from uuid import UUID
    from .conftest import CLUSTER_ID

    store = database()
    store.fetch(
        "INSERT INTO clusters (id, org_id, name, status, eks_cluster_arn) VALUES ($1, $2, 'selected', 'Pending', $3)",
        UUID(CLUSTER_ID),
        UUID(ORG_ID),
        CLUSTER_ARN,
    )
    store.fetch(
        "INSERT INTO workspaces (id, org_id, cluster_id, name, isolation_mode, status, is_default) VALUES ($1, $2, $3, 'selected', 'namespace', 'pending', false)",
        UUID(WORKSPACE_ID),
        UUID(ORG_ID),
        UUID(CLUSTER_ID),
    )
    registry = SqlRegistrationStore(store=store)
    claim = registry.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    registry.finalize(_Target(), str(claim["attempt_token"]))
    assert store.fetch("SELECT id FROM clusters") == [{"id": UUID(CLUSTER_ID)}]
    assert store.fetch("SELECT cluster_id FROM workspaces") == [
        {"cluster_id": UUID(CLUSTER_ID)}
    ]
