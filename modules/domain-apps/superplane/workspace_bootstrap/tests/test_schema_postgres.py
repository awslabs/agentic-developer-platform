"""Migration `017` applied to a disposable PostgreSQL — F11.

Issue #5533 (w6-10), EPIC #4910.

Review finding F11 in substance: the bootstrap reservation store was implemented and
tested entirely against a scripted double, so its table was named by SQL and created by no
migration. `017_add_workspace_bootstrap_reservations` is the schema; this file is what
establishes that the schema APPLIES and that its constraints hold.

## Offline, and on a database that did not exist a moment ago

The story's constraint is explicit: add migration source, do not apply migrations to live
systems. Both halves are honoured here and the distinction is worth stating, because
"applied to a real PostgreSQL" and "applied to a live system" sound similar and are not:

- The chain is rendered to DDL through Alembic's offline (`--sql`) mode, which never opens
  a connection. That much runs on any interpreter.
- It is then applied to a database CREATED by the fixture on a server the fixture started
  in a temporary directory, and DROPPED when the test ends. Nothing outside the test has
  ever had a handle on it, there is no DSN or host read from the environment, and the
  server is gone with the module.

What that buys over the existing offline check in
`src/superplane-api/tests/test_migrations.py` — which renders the chain and asserts it
compiles — is everything that depends on a real engine: a generated column actually
generating, a CHECK constraint actually refusing, a primary key actually colliding. That
file's own docstring names this limit ("It does NOT prove they APPLY to a live database").

## What each test pins

The constraints are not decoration. Each one closes a state that `registry.py` branches on
and could not otherwise rule out — a reservation held by nobody, a state the code has no
branch for, a claim attributable to no tenant. The tests are written one constraint per
test so a regression names the constraint rather than reporting "the schema broke".
"""

from __future__ import annotations

import pytest

from .postgres_support import (
    chain_revision_ids,
    render_migration_ddl,
    require_asyncpg,
    require_pgserver,
    revision_module,
    _Loop,
)

TABLE = "workspace_bootstrap_reservations"
REVISION = "017_add_workspace_bootstrap_reservations"

IDENTITY = '{"org_id": "11111111-1111-4111-8111-111111111111"}'


def _insert(
    workspace_id: str = "w1",
    state: str = "reserved",
    identity: str = IDENTITY,
    token: str = "an-attempt-token",
) -> tuple[object, ...]:
    """The statement and its bound values, flat, for `migrated.execute(*_insert(...))`.

    Flat rather than `(sql, [values])` so the splat reaches asyncpg's own `(sql, *args)`
    signature — a nested list arrives as one argument and every insert below fails on
    argument count rather than on the constraint it is testing.
    """
    return (
        f"INSERT INTO {TABLE} (workspace_id, state, identity_json, attempt_token) "
        "VALUES ($1, $2, $3, $4)",
        workspace_id,
        state,
        identity,
        token,
    )


# --- fixtures --------------------------------------------------------------------


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    pgserver = require_pgserver()
    instance = pgserver.get_server(
        tmp_path_factory.mktemp("workspace-bootstrap-schema")
    )
    try:
        yield instance.get_uri()
    finally:
        instance.cleanup()


@pytest.fixture
def loop():
    driver = _Loop()
    try:
        yield driver
    finally:
        driver.close()


@pytest.fixture
def migrated(server, loop, request):
    """A disposable database with the WHOLE chain applied, dropped afterwards.

    The whole chain, not revision 017 alone: a migration that applies only to an empty
    database has not been shown to apply to this one, and rendering the chain is also what
    establishes that 017 is reachable from its root rather than an orphan file no `upgrade`
    would ever walk.
    """
    asyncpg = require_asyncpg()
    upgrade, _ = render_migration_ddl()
    name = "wbs_" + str(abs(hash(request.node.name)))[:12]
    quoted = '"' + name.replace('"', '""') + '"'
    admin = loop.run(asyncpg.connect(server))
    loop.run(admin.execute(f"DROP DATABASE IF EXISTS {quoted}"))
    loop.run(admin.execute(f"CREATE DATABASE {quoted}"))
    connection = loop.run(asyncpg.connect(server, database=name))
    loop.run(connection.execute(upgrade))

    class _DB:
        def execute(self, sql: str, *arguments) -> None:
            loop.run(connection.execute(sql, *arguments))

        def fetch(self, sql: str, *arguments) -> list[dict]:
            return [dict(row) for row in loop.run(connection.fetch(sql, *arguments))]

        def fetchval(self, sql: str, *arguments):
            return loop.run(connection.fetchval(sql, *arguments))

    try:
        yield _DB()
    finally:
        loop.run(connection.close())
        loop.run(admin.execute(f"DROP DATABASE IF EXISTS {quoted}"))
        loop.run(admin.close())


# --- the revision is part of the chain -------------------------------------------


def test_this_storys_migration_is_reached_by_the_single_headed_chain():
    """An orphan revision creates nothing, and every offline assertion about it passes.

    `render_migration_ddl` walks the chain Alembic resolves; a revision the walk does not
    reach — or a chain with two heads — means `alembic upgrade head` either skips this
    table or refuses to run at all. Checked without a database, so it runs in the offline
    lane too.

    Reachability, not "is the newest revision in the repository". The original form
    asserted this revision was the head, which made every later story's migration fail
    this test (#5671's 018 did). What this story needs is that the chain reaches its
    revision and still resolves to one head; what comes after it is not its business.
    """
    chain = chain_revision_ids()

    assert REVISION in chain, f"{REVISION} is not reached by the chain: {chain}"
    assert revision_module(REVISION).down_revision == "016_add_organization_grants"


def test_the_chain_renders_the_reservations_table_as_postgresql_ddl():
    """Compiles for the target backend — the claim the existing offline test makes."""
    upgrade, _ = render_migration_ddl()

    assert f"CREATE TABLE {TABLE}" in upgrade
    assert "GENERATED ALWAYS AS" in upgrade


# --- the table exists, with the columns registry.py names ------------------------


def test_the_migration_applies_to_an_empty_database(migrated):
    """The claim offline rendering cannot make: these operations run on a real engine."""
    assert migrated.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = $1)",
        TABLE,
    )


def test_every_column_the_store_selects_exists(migrated):
    """Keyed to `_SELECT_FOR_UPDATE`'s actual column list, imported rather than retyped.

    F11 was a schema and a query that had never met. Retyping the column names here would
    reintroduce exactly that gap in the test itself — two lists that agree today.
    """
    from superplane_bootstrap.registry import _SELECT_FOR_UPDATE

    selected = {
        name.strip()
        for name in _SELECT_FOR_UPDATE.split("SELECT")[1].split("FROM")[0].split(",")
    }
    present = {
        row["column_name"]
        for row in migrated.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
            TABLE,
        )
    }

    assert selected
    assert selected <= present, (
        f"the store selects columns the schema lacks: {selected - present}"
    )


def test_the_primary_key_is_the_workspace_id(migrated):
    """One workspace, at most one reservation — enforced by the database.

    The advisory lock is what `reserve` relies on; this is what holds if any future caller
    ever reaches an INSERT without it.
    """
    migrated.execute(*_insert())

    asyncpg = require_asyncpg()
    with pytest.raises(asyncpg.UniqueViolationError):
        migrated.execute(*_insert(token="a-different-token"))


# --- the constraints, one per test -----------------------------------------------


def test_a_state_the_code_has_no_branch_for_is_refused(migrated):
    """`registry.py` branches on exactly `reserved` and `registered`.

    A third value would put the replay-versus-live-claim decision — the F10 decision — into
    territory with no branch, where a row is neither a completed registration nor a live
    reservation and the store's behaviour is whatever the `if` chain falls through to.
    """
    asyncpg = require_asyncpg()

    with pytest.raises(asyncpg.CheckViolationError):
        migrated.execute(*_insert(state="half-done"))


@pytest.mark.parametrize("state", ["reserved", "registered"])
def test_both_states_the_code_uses_are_accepted(state, migrated):
    """The other half of the constraint: it refuses the unknown without refusing the known.

    A CHECK that rejected `registered` would make `finalize` fail at the last step of a
    successful bootstrap, which is the F5 interruption arriving by way of the schema.
    """
    migrated.execute(*_insert(workspace_id=f"w-{state}", state=state))

    assert (
        migrated.fetchval(
            f"SELECT state FROM {TABLE} WHERE workspace_id = $1", f"w-{state}"
        )
        == state
    )


@pytest.mark.parametrize("token", ["", "   ", "\t"])
def test_a_reservation_held_by_nobody_is_refused(token, migrated):
    """The F10 fence, made unrepresentable rather than merely unused.

    The token is what distinguishes a live claim from an abandoned one and authorizes
    `finalize` and `release`. A row with a blank token is a claim held by nobody that
    nonetheless occupies the workspace's primary key: no attempt could ever finalize it and
    only `release_abandoned` could clear it. `registry.py` refuses to write one; the
    database refuses to hold one.
    """
    asyncpg = require_asyncpg()

    with pytest.raises(asyncpg.CheckViolationError):
        migrated.execute(*_insert(token=token))


@pytest.mark.parametrize("workspace_id", ["", "   "])
def test_a_reservation_keyed_on_blank_is_refused(workspace_id, migrated):
    """Blank-but-present is this package's partial-record failure mode.

    `WorkspaceTarget` refuses every blank field in code for the same reason: a record that
    exists with blank identity is read downstream as present and cannot be told apart from
    a complete one.
    """
    asyncpg = require_asyncpg()

    with pytest.raises(asyncpg.CheckViolationError):
        migrated.execute(*_insert(workspace_id=workspace_id))


# --- the tenant identity constraint ---------------------------------------------


def test_the_tenant_column_is_generated_from_the_stored_identity(migrated):
    """Generated, so it cannot disagree with the identity the claim was taken for.

    A separately-written tenant column is a column that can be wrong — and a reservation
    attributed to the wrong org is a cross-tenant record in the table that decides which
    org owns a workspace. This one is derived from the same bytes `registry.py` compares
    identities against, so there is no write path that could set them inconsistently.
    """
    migrated.execute(
        *_insert(identity='{"org_id": "org-alpha", "region": "us-east-1"}')
    )

    assert (
        migrated.fetchval(f"SELECT org_id FROM {TABLE} WHERE workspace_id = 'w1'")
        == "org-alpha"
    )


def test_the_tenant_column_follows_an_updated_identity(migrated):
    """STORED and generated, so `finalize`'s UPDATE of `identity_json` carries it along.

    `_MARK_REGISTERED` rewrites `identity_json` with the full twelve-field record. A
    trigger-free generated column is what keeps the tenant attribution correct across that
    write without `finalize` having to know the column exists.
    """
    migrated.execute(*_insert(identity='{"org_id": "org-before"}'))

    migrated.execute(
        f"UPDATE {TABLE} SET identity_json = $1 WHERE workspace_id = 'w1'",
        '{"org_id": "org-after"}',
    )

    assert (
        migrated.fetchval(f"SELECT org_id FROM {TABLE} WHERE workspace_id = 'w1'")
        == "org-after"
    )


def test_a_reservation_attributable_to_no_tenant_is_refused(migrated):
    """The generated column's NOT NULL, which is load-bearing and not documentation.

    An identity carrying no `org_id` yields NULL and the row is refused, so an
    unattributable reservation cannot exist. `registration.py` already requires a non-blank
    `org_id` before reserving; this is the same rule where it cannot be bypassed.
    """
    asyncpg = require_asyncpg()

    with pytest.raises(asyncpg.NotNullViolationError):
        migrated.execute(*_insert(identity='{"region": "us-east-1"}'))


def test_an_identity_that_is_not_json_is_refused(migrated):
    """The cast in the generated column doubles as validation at write time.

    `_decode_identity` refuses unreadable JSON on the read path — correctly, since an
    empty identity compares equal to nothing and would turn a binding into a conflict. This
    means such a row cannot be written in the first place.
    """
    asyncpg = require_asyncpg()

    with pytest.raises(asyncpg.DataError):
        migrated.execute(*_insert(identity="not json at all"))


# --- the downgrade ---------------------------------------------------------------


def test_the_downgrade_removes_the_table_and_its_indexes(migrated):
    """A downgrade that fails is a migration that cannot be backed out.

    Applied to the real schema rather than rendered, because the failure mode is an index
    dropped by a name that does not match the one created — which renders perfectly and
    fails only against a database that has the index.
    """
    # Names the revision explicitly: this used to take the chain's last revision, which
    # stopped being this story's the moment another was appended (#5671's 018).
    _, downgrade = render_migration_ddl(upgrade_only=False, downgrade_revision=REVISION)
    migrated.execute(*_insert())

    migrated.execute(downgrade)

    assert not migrated.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = $1)",
        TABLE,
    )
    assert (
        migrated.fetch("SELECT indexname FROM pg_indexes WHERE tablename = $1", TABLE)
        == []
    )
