"""Recovery against a REAL migrated PostgreSQL: the F13 stale-recovery fence.

Issue #5533 (w6-10), EPIC #4910. Added by the F13 repair.

F13 verbatim: "`registry.py:435 release_abandoned` deletes any `reserved` row for a
workspace_id without matching an attempt/generation, and `workspace.py:347
recover_interrupted_bootstrap` calls it based only on boolean flags in a state file that
carries no attempt token."

## Why this file exists separately from `test_recovery.py`

`test_recovery.py` drives the same function against `FakeRegistrationStore`, and it should:
the ordering, convergence and reporting decisions are `workspace.py`'s and a fake is the
right instrument for them. What a fake cannot establish is the thing this finding is
actually about.

The defect is a question about what TWO DATABASE SESSIONS see. A stale recovery deleting a
live successor's claim is a statement about rows committed by one connection and deleted
through another, and the fence that stops it is half in Python (`claim_fingerprint`) and
half in SQL (`_DELETE_CLAIMED_RESERVATION`'s `encode(sha256(convert_to(...)))`). A scripted
double cannot check that those two halves agree, and it cannot check that the
compare-and-delete is genuinely atomic against another session — it would answer whatever
it was scripted to answer, for both the fixed and the defective code.

Worse, the direction a digest mismatch fails in is the SAFE-LOOKING one: nothing matches,
so nothing is deleted, which is indistinguishable from a correctly-refused stale recovery.
That would pass every offline test in this package while leaving recovery permanently
unable to clear an abandoned claim — one stuck workspace per crash, which is the denial of
service the fence was explicitly designed not to cause. So it is checked here, on a real
server, through the public `recover_interrupted_bootstrap`.

## Everything here is offline and disposable

`pgserver` runs a bundled PostgreSQL binary over a unix socket in a temp directory: no
root, no Docker, no network, no shared instance. Each test creates its own database, applies
the whole alembic chain to it, and drops it. Nothing outside the fixture ever holds a handle
on it, so this is NOT a live migration and touches no deployed system.
"""

from __future__ import annotations

import pytest
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.registry import SqlRegistrationStore
from superplane_bootstrap.state import (
    BootstrapState,
    FileStateStore,
    claim_fingerprint,
)
from superplane_bootstrap.workspace import (
    BOOTSTRAP_TAINT_KEY,
    recover_interrupted_bootstrap,
)

from .conftest import (
    ACCOUNT_ID,
    CLUSTER_ARN,
    CLUSTER_NAME,
    NAMESPACE,
    ORG_ID,
    REGION,
    WORKSPACE_ID,
    FakeClusterAccess,
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


# --- fixtures --------------------------------------------------------------------
#
# Deliberately the same shape as `test_registry_postgres.py`'s. Sharing them through a
# helper module was the alternative and is worse here: a module-scoped server fixture
# imported into two files yields ONE server for whichever module runs first, which couples
# the two files' isolation to collection order.


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A disposable PostgreSQL for this module, destroyed after it."""
    pgserver = require_pgserver()
    instance = pgserver.get_server(
        tmp_path_factory.mktemp("workspace-bootstrap-recover")
    )
    try:
        yield instance.get_uri()
    finally:
        instance.cleanup()


@pytest.fixture(scope="module")
def schema_ddl():
    """The whole alembic chain as PostgreSQL DDL, rendered once."""
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
    """A fresh migrated database per test, dropped afterwards."""
    asyncpg = require_asyncpg()
    name = "wbr_" + str(abs(hash(request.node.name)))[:12]
    quoted = '"' + name.replace('"', '""') + '"'
    admin = loop.run(asyncpg.connect(server))
    loop.run(admin.execute(f"DROP DATABASE IF EXISTS {quoted}"))
    loop.run(admin.execute(f"CREATE DATABASE {quoted}"))
    migrator = loop.run(asyncpg.connect(server, database=name))
    loop.run(migrator.execute(schema_ddl))
    from .conftest import ORG_ID
    from uuid import UUID

    loop.run(
        migrator.execute(
            "INSERT INTO organizations (id, name, adp_org_id, billing_plan) VALUES ($1, 'test-org', $2, 'free')",
            UUID(ORG_ID),
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
def inspector(database):
    """A connection used only to READ, never through the store under test.

    Every assertion about which row survived goes through this, so a store that reported
    success without writing anything — or a recovery that reported a release it did not
    perform — is caught by the database rather than by its own return value.
    """
    return database()


def _state_file(tmp_path, *, claim: str, taint_cleared: bool = True) -> FileStateStore:
    """A real on-disk state file in the exact interrupted shape, naming `claim`.

    `FileStateStore`, not the in-memory fake: the finding is about a record that OUTLIVED
    its process, and a state store that dies with the test cannot represent one. The file
    also forces the record through `to_mapping`/`state_from_mapping`, so a fingerprint that
    did not survive serialization would fail here rather than silently reverting recovery
    to the unfenced behaviour.
    """
    store = FileStateStore(tmp_path / "bootstrap-state.json")
    store.save(
        BootstrapState(
            workspace_id=WORKSPACE_ID,
            cluster_arn=CLUSTER_ARN,
            prerequisites_recorded=True,
            registration_reserved=True,
            registration_claim=claim,
            taint_cleared=taint_cleared,
            registration_finalized=False,
        )
    )
    return store


def _recover(store, state_store, access):
    return recover_interrupted_bootstrap(
        access=access,
        store=store,
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )


def _tokens(inspector) -> list[str]:
    return [
        str(row["attempt_token"])
        for row in inspector.fetch(f"SELECT attempt_token FROM {TABLE}")
    ]


# --- F13: the reported defect ----------------------------------------------------


def test_stale_recovery_does_not_admit_a_third_writer(database, inspector, tmp_path):
    """**The reported F13 scenario, end to end through the public recovery function.**

    The sequence, which is the supervisor's and is kept verbatim in behaviour:

    1. An attempt claims the workspace and records the claim durably.
    2. Its claim is released properly — but the process dies BEFORE writing that down, so
       its state file still says a reservation is held. This is the crash window the
       finding names, and it is what makes the record stale rather than merely old.
    3. A successor legitimately claims the workspace and is now mid-bootstrap.
    4. Recovery runs from the FIRST attempt's stale file.

    Before the fix, step 4 deleted the successor's live claim and reported success, so a
    third attempt could reserve the same workspace while the second was still mutating the
    namespace, controller, taint and registration. All three could then write concurrently
    — F10's defect reached around F10's fence.

    The third reservation attempt is the assertion that matters. Checking only that the row
    survived would pass against a recovery that deleted it and let the successor's own
    `finalize` recreate it; what must be true is that no OTHER writer can get in.
    """
    predecessor = SqlRegistrationStore(store=database())
    recovery_store = SqlRegistrationStore(store=database())
    third = SqlRegistrationStore(store=database())

    first = predecessor.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    state_store = _state_file(
        tmp_path, claim=claim_fingerprint(str(first["attempt_token"]))
    )
    predecessor.release(WORKSPACE_ID, str(first["attempt_token"]))

    successor = predecessor.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    live_token = str(successor["attempt_token"])

    outcome = _recover(
        recovery_store, state_store, FakeClusterAccess(crds=[], taints=[])
    )

    assert outcome.reservation_released is False, (
        "stale recovery reported releasing a claim it does not own"
    )
    assert _tokens(inspector) == [live_token], (
        "stale recovery deleted the live successor's claim"
    )
    assert third.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)["reserved"] is False, (
        "a third writer was admitted while the successor was still bootstrapping; all "
        "three can now mutate this workspace concurrently"
    )


def test_stale_recovery_does_not_re_taint_a_cluster_a_successor_is_using(
    database, inspector, tmp_path
):
    """The other half of "no mutation over a live successor": the interlock.

    `restore_bootstrap_taint` writes `NoSchedule` to every node. Applied while a successor
    is mid-bootstrap, it makes that successor's workloads unschedulable — the successor
    then fails readiness on a cluster it correctly prepared, for reasons nothing in its own
    logs explains.

    This is why the release moved AHEAD of the restoration in `recover_interrupted_bootstrap`
    rather than merely being fenced. Authority has to be established before the cluster is
    touched, because a taint write cannot be taken back by discovering afterwards that the
    record was stale.
    """
    predecessor = SqlRegistrationStore(store=database())
    recovery_store = SqlRegistrationStore(store=database())

    first = predecessor.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    state_store = _state_file(
        tmp_path, claim=claim_fingerprint(str(first["attempt_token"]))
    )
    predecessor.release(WORKSPACE_ID, str(first["attempt_token"]))
    predecessor.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    access = FakeClusterAccess(crds=[], taints=[])
    outcome = _recover(recovery_store, state_store, access)

    assert access.restored_taints == [], (
        "stale recovery re-applied the bootstrap interlock while a successor held the "
        "workspace, which makes that successor's nodes unschedulable mid-run"
    )
    assert outcome.taint_restored is False
    assert len(_tokens(inspector)) == 1


def test_a_record_with_no_claim_identity_refuses_rather_than_deleting(
    database, inspector, tmp_path
):
    """A record written before claim fingerprinting cannot prove what it owns.

    This is the pre-change record, and it is the case where "unblock the workspace" and
    "never delete a live claim" genuinely conflict: the boolean says a claim is held and
    nothing says which. The old code resolved that conflict by deleting whatever it found,
    which is exactly F13.

    So it refuses, and says why, with an action an operator can take. The alternative —
    guessing — is not available, because from here a dead attempt's claim and a live
    successor's are the same row, which is the whole finding.
    """
    holder = SqlRegistrationStore(store=database())
    recovery_store = SqlRegistrationStore(store=database())

    live = holder.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    state_store = _state_file(tmp_path, claim="")

    access = FakeClusterAccess(crds=[], taints=[])
    outcome = _recover(recovery_store, state_store, access)

    assert outcome.reservation_released is False
    assert _tokens(inspector) == [str(live["attempt_token"])], (
        "a record that cannot identify its claim deleted one anyway"
    )
    assert access.restored_taints == []
    assert isinstance(outcome.refusal, BootstrapRefused)
    assert "cannot identify WHICH claim" in str(outcome.refusal)


# --- what the fence must NOT break ----------------------------------------------


def test_a_genuinely_abandoned_claim_is_still_recovered(database, inspector, tmp_path):
    """The case recovery EXISTS for, against the real schema.

    An attempt claimed the workspace, recorded the claim, and died holding the only copy of
    its token. Nothing else can clear that row: `reserve` sees a live-looking claim and
    refuses, and `release` needs the token that died with the process. If recovery could
    not clear it either, the workspace would be permanently unbootstrappable — the fence
    would have turned F13's fix into a denial of service, one workspace per crash.

    Asserted against the database rather than the return value, and followed by a real
    reservation: "the row is gone" and "the workspace is usable again" are different
    claims, and the second is the one an operator cares about.
    """
    dead = SqlRegistrationStore(store=database())
    recovery_store = SqlRegistrationStore(store=database())
    retry = SqlRegistrationStore(store=database())

    claim = dead.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    state_store = _state_file(
        tmp_path, claim=claim_fingerprint(str(claim["attempt_token"]))
    )

    access = FakeClusterAccess(crds=[], taints=[])
    outcome = _recover(recovery_store, state_store, access)

    assert outcome.reservation_released is True
    assert _tokens(inspector) == []
    assert access.restored_taints == [BOOTSTRAP_TAINT_KEY], (
        "the interlock was cleared by the dead attempt and was not put back"
    )
    assert retry.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)["reserved"] is True, (
        "the claim was dropped but the workspace is still not bootstrappable"
    )


def test_recovery_converges_and_does_not_replay_the_release(
    database, inspector, tmp_path
):
    """A second recovery does nothing — including nothing to a workspace reclaimed since.

    Convergence was F12's requirement and this is it against a real database, but the
    stronger property here is the second half. After a successful recovery the record's
    fingerprint is cleared, so even if a NEW attempt claims the workspace between the two
    calls, the second recovery has no claim to name and cannot touch it.

    Without clearing the fingerprint this would still pass — the old claim's row is gone,
    so the compare-and-delete matches nothing — which is why the state file is asserted
    directly. A record still naming a released claim is a false statement waiting for a
    collision, and this package's discipline is that the record says only what is true.
    """
    dead = SqlRegistrationStore(store=database())
    recovery_store = SqlRegistrationStore(store=database())
    newcomer = SqlRegistrationStore(store=database())

    claim = dead.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    state_store = _state_file(
        tmp_path, claim=claim_fingerprint(str(claim["attempt_token"]))
    )
    access = FakeClusterAccess(crds=[], taints=[])

    assert _recover(recovery_store, state_store, access).reservation_released is True

    persisted = state_store.load()
    assert persisted is not None
    assert persisted.registration_reserved is False
    assert persisted.registration_claim == "", (
        "the record still names a claim it released, so it asserts something untrue"
    )

    fresh = newcomer.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    second = _recover(recovery_store, state_store, access)

    assert second.refusal is None
    assert second.reservation_released is False
    assert _tokens(inspector) == [str(fresh["attempt_token"])], (
        "a second recovery deleted a claim taken after the first one finished"
    )
    assert access.restored_taints == [BOOTSTRAP_TAINT_KEY], (
        "the second recovery re-applied a taint that was already restored"
    )


def test_a_registered_workspace_is_not_unpublished_by_its_own_recovery(
    database, inspector, tmp_path
):
    """The fingerprint matches, and the workspace is REGISTERED. Nothing may be deleted.

    The state clause carries this alone — the record names exactly the claim that was
    finalized, so the fingerprint agrees and would permit the delete. Deleting a
    `registered` row unpublishes a fully bootstrapped workspace that tenants are using,
    which is a worse outcome than the stranded claim recovery exists to clear.
    """
    holder = SqlRegistrationStore(store=database())
    recovery_store = SqlRegistrationStore(store=database())

    claim = holder.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    token = str(claim["attempt_token"])
    state_store = _state_file(tmp_path, claim=claim_fingerprint(token))

    # Registered through the same connection that holds the claim, as production does.
    class _Target:
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

    holder.finalize(_Target(), token)

    outcome = _recover(
        recovery_store, state_store, FakeClusterAccess(crds=[], taints=[])
    )

    assert outcome.reservation_released is False
    assert inspector.fetch(f"SELECT state FROM {TABLE}") == [{"state": "registered"}], (
        "recovery unpublished a completed registration"
    )


def test_the_state_file_never_contains_the_attempt_token(database, tmp_path):
    """What makes the durable record safe to keep at all.

    Recovery needs to identify a claim across a process boundary, and the direct way to do
    that — writing the token down — would make the state file a durable copy of the
    permission to publish or unpublish this workspace. `state.py` promises the opposite in
    as many words, and this reads the bytes on disk rather than the dataclass, because the
    guarantee is about the file.
    """
    holder = SqlRegistrationStore(store=database())
    claim = holder.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    token = str(claim["attempt_token"])

    state_store = _state_file(tmp_path, claim=claim_fingerprint(token))
    raw = state_store.path.read_text()

    assert token not in raw, "the attempt token was written to the durable state file"
    assert claim_fingerprint(token) in raw
    # The halves of a hex token, in case a future writer splits or reformats it.
    assert token[:32] not in raw
    assert token[32:] not in raw


def test_an_unreachable_database_keeps_the_fingerprint_for_the_retry(
    database, inspector, loop, tmp_path
):
    """An UNREACHABLE store must stay identifiable, or the retry becomes the unfenced case.

    If the fingerprint were cleared whenever a release did not succeed, the next recovery
    would hold a record saying "a claim is held" with no way to name it — which lands in the
    refusal path and strands the workspace permanently. So the fingerprint is cleared only
    when the claim is SETTLED, and this pins the one case where it is not: the database could
    not be asked, so nothing at all is known about the claim.

    The distinction this test exists to hold is the one my own first fix got wrong. "The
    database said no claim matches" and "the database could not be reached" both surface as
    a release that returned false, and they need opposite handling — the former settles the
    claim (see `test_stale_recovery_does_not_re_taint_a_cluster_a_successor_is_using`), the
    latter must retain it. Collapsing them either strands a workspace forever or drops a
    claim that may still be live.

    Unreachability is produced by closing the connection under the store rather than by a
    mock that raises, so the failure is a real driver error from the real server — the shape
    a dead database actually presents, including its exception type.
    """
    holder = SqlRegistrationStore(store=database())
    live = holder.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)

    recovery_connection = database()
    recovery_store = SqlRegistrationStore(store=recovery_connection)
    # The claim is the LIVE one, so a released fingerprint would be indistinguishable from
    # success if the connection still worked: only the unreachability can produce false here.
    claim = claim_fingerprint(str(live["attempt_token"]))
    state_store = _state_file(tmp_path, claim=claim)
    loop.run(recovery_connection._connection.close())

    outcome = _recover(
        recovery_store, state_store, FakeClusterAccess(crds=[], taints=[])
    )

    assert outcome.reservation_released is False
    persisted = state_store.load()
    assert persisted is not None
    assert persisted.registration_reserved is True
    assert persisted.registration_claim == claim, (
        "the fingerprint was dropped although the database was never reached, so a later "
        "recovery could no longer identify the claim and would have to refuse forever"
    )
    assert _tokens(inspector) == [str(live["attempt_token"])], (
        "the live claim was deleted by a recovery that could not even reach the database"
    )


def test_recovery_holds_reservation_lock_through_cluster_restoration(
    database, tmp_path
):
    """A real second connection cannot acquire the successor's lock mid-restore."""
    from superplane_bootstrap.registry import _LOCK_PREFIX

    owner = SqlRegistrationStore(store=database())
    recovery = SqlRegistrationStore(store=database())
    contender = database()
    newcomer = SqlRegistrationStore(store=contender)
    claim = owner.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    state = _state_file(tmp_path, claim=claim_fingerprint(claim["attempt_token"]))
    calls = []

    class CheckingAccess(FakeClusterAccess):
        def restore_bootstrap_taint(self, key):
            with contender.transaction():
                rows = contender.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0)) AS owned",
                    {"key": _LOCK_PREFIX + WORKSPACE_ID},
                )
                assert rows[0]["owned"] is False
            calls.append("restore-under-lock")
            return super().restore_bootstrap_taint(key)

    access = CheckingAccess(crds=[], taints=[])
    result = _recover(recovery, state, access)
    assert result.taint_restored and result.reservation_released
    assert calls == ["restore-under-lock"]
    assert newcomer.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)["reserved"]
    assert _recover(recovery, state, access).refusal is None
    assert calls == ["restore-under-lock"]


def test_failed_restoration_retains_real_claim_for_retry(database, tmp_path):
    owner = SqlRegistrationStore(store=database())
    recovery = SqlRegistrationStore(store=database())
    newcomer = SqlRegistrationStore(store=database())
    claim = owner.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    state = _state_file(tmp_path, claim=claim_fingerprint(claim["attempt_token"]))
    access = FakeClusterAccess(crds=[], taints=[], restore_fails_for=1)
    first = _recover(recovery, state, access)
    assert first.nodes_left_schedulable and not first.reservation_released
    assert not newcomer.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)["reserved"]
    second = _recover(recovery, state, access)
    assert second.taint_restored and second.reservation_released
    assert newcomer.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)["reserved"]
