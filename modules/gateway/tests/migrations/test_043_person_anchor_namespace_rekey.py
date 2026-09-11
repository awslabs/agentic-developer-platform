"""Tests for Alembic migration 043 — person-cap anchor re-key guard.

Issue #4843 (T4, EPIC #4839). Design note
`docs/design-notes/4828-platform-native-org-team-user.md` §4, §1.6.

043 is an unusual migration: it contains **no UPDATE**, and that absence is its
contract. `person_budget_configs.person_anchor` is the one place an anchor string
is stored durably, so introducing a namespace registry with a precedence order is a
data-compatibility question about exactly that column. Because `github:` keeps
priority 1 the re-key set is expected to be empty — and the migration **asserts
that emptiness rather than assuming it**.

So the thing under test is not "did the data change" (nothing changes) but **"does
the guard actually fire"**. A guard that cannot fail is indistinguishable from a
comment, and this one is the only executable protection against a future precedence
reorder silently orphaning every dual-identity person's cap (#4511). Hence:

  - the **happy path**: a realistic seeded database — including a person holding
    BOTH a github and a directory identity, which is the only shape precedence can
    move — passes and mutates nothing;
  - **check 1 fires** on a cap stored in an unregistered namespace;
  - **check 2 fires** on the precedence regression it exists for, provoked by
    reversing the precedence tuple rather than by corrupting data, because reversing
    the tuple is the mistake a future engineer will actually make;
  - the **degenerate databases** (no cap table, no identity table, no rows) are
    no-ops, since a migration that fails on a fresh install blocks every new
    deployment;
  - **parity** between 043's snapshot tuple and the live registry, which is what
    makes check 2 meaningful at all;
  - **downgrade is a no-op** and the revision chains onto a single head.

`alembic/**` is outside `gateway-ci.yml`'s trigger paths, so this file is what gives
043 any CI signal.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.identity.person_anchor import (
    PERSON_ANCHOR_INTERNAL_NAMESPACE,
    PERSON_ANCHOR_NAMESPACES,
    PERSON_ANCHOR_PROVIDER_PRECEDENCE,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"
THIS_MIGRATION = "043_person_anchor_namespace_rekey.py"
MAX_REVISION_ID_LEN = 32


def _load_migration(filename: str):
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_043 = _load_migration(THIS_MIGRATION)


def _run_migration(sync_conn, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        fn()


_IDENTITY_TABLE = """
    CREATE TABLE user_identities (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        org_id VARCHAR(255) NOT NULL,
        team_id VARCHAR(255) NOT NULL,
        user_id VARCHAR(255) NOT NULL,
        provider VARCHAR(20) NOT NULL,
        provider_user_id VARCHAR(255) NOT NULL,
        provider_username VARCHAR(255),
        verification_method VARCHAR(20) NOT NULL,
        is_primary BOOLEAN NOT NULL DEFAULT 0,
        verified_at DATETIME,
        created_at DATETIME,
        updated_at DATETIME
    )
"""

# Mirrors 034's shape (the columns 043 reads, plus the NOT NULLs so a seed row is
# realistic rather than a two-column stub).
_CAP_TABLE = """
    CREATE TABLE person_budget_configs (
        id VARCHAR(255) NOT NULL PRIMARY KEY,
        person_anchor VARCHAR(255) NOT NULL,
        period_type VARCHAR(10) NOT NULL,
        budget_amount_usd NUMERIC(10, 2) NOT NULL,
        enforcement_mode VARCHAR(10) NOT NULL DEFAULT 'soft',
        authored_by_user_id VARCHAR(255) NOT NULL,
        CONSTRAINT uq_person_budget_config UNIQUE (person_anchor, period_type)
    )
"""

GITHUB_ID = "20402445"
DIRECTORY_ID = "aad-0000-1111-2222"
BOTH_USER_ID = "user-with-both"
DIR_ONLY_USER_ID = "user-directory-only"

# The dual-identity person is the point of the seed: they are the ONLY shape whose
# anchor namespace can change when precedence is reordered, so a fixture without
# them would let check 2 pass vacuously.
_IDENTITIES = [
    ("i-gh", BOTH_USER_ID, "github", GITHUB_ID),
    ("i-dir", BOTH_USER_ID, "directory", DIRECTORY_ID),
    ("i-dir-only", DIR_ONLY_USER_ID, "directory", "aad-9999"),
]

_CAPS = [
    ("c-gh", f"github:{GITHUB_ID}"),
    ("c-dir", "directory:aad-9999"),
    ("c-internal", f"{PERSON_ANCHOR_INTERNAL_NAMESPACE}:orphaned-canonical-id"),
]


async def _seeded_engine(*, identities=_IDENTITIES, caps=_CAPS, cap_table: bool = True, identity_table: bool = True):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        if identity_table:
            await conn.execute(sa.text(_IDENTITY_TABLE))
            for row_id, user_id, provider, provider_user_id in identities:
                await conn.execute(
                    sa.text(
                        "INSERT INTO user_identities "
                        "(id, org_id, team_id, user_id, provider, provider_user_id, verification_method, is_primary) "
                        "VALUES (:id, 'o1', 't1', :user_id, :provider, :pid, 'oauth', 1)"
                    ),
                    {"id": row_id, "user_id": user_id, "provider": provider, "pid": provider_user_id},
                )
        if cap_table:
            await conn.execute(sa.text(_CAP_TABLE))
            for cap_id, anchor in caps:
                await conn.execute(
                    sa.text(
                        "INSERT INTO person_budget_configs "
                        "(id, person_anchor, period_type, budget_amount_usd, authored_by_user_id) "
                        "VALUES (:id, :anchor, 'monthly', 100.00, 'admin-1')"
                    ),
                    {"id": cap_id, "anchor": anchor},
                )
    return engine


async def _upgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_043.upgrade)


class TestTheExpectedEmptyResult:
    """`github:` keeps priority 1, so a consistent database passes untouched."""

    @pytest.mark.asyncio
    async def test_a_realistic_database_passes(self):
        """No raise on a seed that includes a dual-identity person.

        This is the assertion the issue asks for ("should be zero rows while GitHub
        keeps priority 1"), made executable against the shape that could break it.
        """
        engine = await _seeded_engine()
        try:
            await _upgrade(engine)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_no_anchor_is_rewritten(self):
        """The migration writes nothing — the absence of an UPDATE is the contract.

        Compared before/after rather than merely trusting that the source has no
        UPDATE, so a future edit that adds one has to justify itself against a
        failing test.
        """
        engine = await _seeded_engine()
        try:
            async with engine.begin() as conn:
                before = (await conn.execute(sa.text("SELECT id, person_anchor FROM person_budget_configs ORDER BY id"))).all()

            await _upgrade(engine)

            async with engine.begin() as conn:
                after = (await conn.execute(sa.text("SELECT id, person_anchor FROM person_budget_configs ORDER BY id"))).all()

            assert before == after
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_users_namespace_anchor_is_tolerated(self):
        """An internal `users:` anchor names no identity row, so it is not adjudicated.

        It is registered (the read path emits it) but not authorable, so its presence
        is a pre-existing condition — raising on it would fail deploys over a state
        this story did not create and cannot fix.
        """
        engine = await _seeded_engine(caps=[("c-internal", f"{PERSON_ANCHOR_INTERNAL_NAMESPACE}:some-canonical-id")])
        try:
            await _upgrade(engine)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_an_orphaned_github_anchor_is_tolerated(self):
        """A cap whose identity row was deleted is pre-existing, not a re-key.

        Deleting somebody's spending limit is not a decision a schema migration gets
        to make, so this must not raise — the guard is about precedence, not about
        cleaning up unrelated orphans.
        """
        engine = await _seeded_engine(caps=[("c-ghost", "github:999999999")])
        try:
            await _upgrade(engine)
        finally:
            await engine.dispose()


class TestCheck1UnregisteredNamespace:
    @pytest.mark.asyncio
    async def test_raises_on_an_anchor_in_an_unregistered_namespace(self):
        """A cap the enforcement layer can never resolve must not pass silently.

        Such a row displays a limit and governs nothing (#4511); surfacing it at
        deploy time, when somebody is watching, is the whole value of the check.
        """
        engine = await _seeded_engine(caps=[("c-bad", "gitlab:12345")])
        try:
            with pytest.raises(RuntimeError) as exc_info:
                await _upgrade(engine)

            message = str(exc_info.value)
            assert "gitlab" in message
            assert "unregistered namespace" in message
            # The message must tell the operator what to DO, not only what is wrong.
            assert "re-key or delete" in message
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_raises_on_an_anchor_with_no_namespace_at_all(self):
        """A bare identifier — the pre-registry hand-rolled-string failure mode."""
        engine = await _seeded_engine(caps=[("c-bare", "20402445")])
        try:
            with pytest.raises(RuntimeError, match="unregistered namespace"):
                await _upgrade(engine)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_every_registered_namespace_is_accepted(self):
        """The complement of the check: nothing in the live registry trips it.

        Parametrized over `PERSON_ANCHOR_NAMESPACES` itself, so adding a namespace to
        the registry without adding it to 043's snapshot fails HERE as well as in the
        parity test.
        """
        engine = await _seeded_engine(
            identities=[],
            caps=[(f"c-{i}", f"{namespace}:some-id") for i, namespace in enumerate(PERSON_ANCHOR_NAMESPACES)],
        )
        try:
            await _upgrade(engine)
        finally:
            await engine.dispose()


class TestCheck2PrecedenceRegression:
    """The guard's reason for existing: it must actually fire on a reorder."""

    @pytest.mark.asyncio
    async def test_reordering_precedence_is_caught(self, monkeypatch):
        """Reverse the precedence tuple; the dual-identity person's cap is flagged.

        Provoked by reversing the tuple rather than by corrupting data because that
        reorder is the mistake a future engineer will actually make — demoting
        `github` in favour of a "cleaner" directory-first order, with no idea that
        every dual-identity person's live cap silently stops enforcing.
        """
        monkeypatch.setattr(MIG_043, "PROVIDER_PRECEDENCE", ("directory", "github"))

        engine = await _seeded_engine()
        try:
            with pytest.raises(RuntimeError) as exc_info:
                await _upgrade(engine)

            message = str(exc_info.value)
            assert f"github:{GITHUB_ID}" in message, "the message must name the affected anchor"
            assert "priority 1" in message
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_single_identity_person_is_unaffected_by_a_reorder(self, monkeypatch):
        """Precedence can only move somebody who holds more than one identity.

        Pins the guard's precision: if it flagged single-identity people too it would
        be raising on databases where nothing is wrong, and would be turned off.
        """
        monkeypatch.setattr(MIG_043, "PROVIDER_PRECEDENCE", ("directory", "github"))

        engine = await _seeded_engine(
            identities=[("i-gh-only", "user-github-only", "github", "555555")],
            caps=[("c-gh-only", "github:555555")],
        )
        try:
            await _upgrade(engine)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_check_follows_shared_identities_to_every_owner(self, monkeypatch):
        """A second `users.id` sharing the github id also counts as an owner.

        Mirrors the read path's fusion. Missing this would let the guard pass while a
        cap that DOES re-key for one of its owners slips through.
        """
        monkeypatch.setattr(MIG_043, "PROVIDER_PRECEDENCE", ("directory", "github"))

        engine = await _seeded_engine(
            identities=[
                # First owner: github only — would not move on its own.
                ("i-a", "user-a", "github", GITHUB_ID),
                # Second owner of the SAME github id, who also holds a directory row.
                ("i-b", "user-b", "github", GITHUB_ID),
                ("i-b-dir", "user-b", "directory", "aad-b"),
            ],
            caps=[("c-shared", f"github:{GITHUB_ID}")],
        )
        try:
            with pytest.raises(RuntimeError, match="would re-key"):
                await _upgrade(engine)
        finally:
            await engine.dispose()


class TestDegenerateDatabases:
    """A migration that fails on a fresh install blocks every new deployment."""

    @pytest.mark.asyncio
    async def test_no_cap_table_is_a_noop(self):
        """A database that never ran 034 has no caps to check."""
        engine = await _seeded_engine(cap_table=False)
        try:
            await _upgrade(engine)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_empty_cap_table_is_a_noop(self):
        engine = await _seeded_engine(caps=[])
        try:
            await _upgrade(engine)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_no_identity_table_skips_only_the_precedence_check(self):
        """Check 1 still runs; check 2 cannot, since nobody is resolvable.

        Both halves asserted: the namespace check must not be skipped just because
        the identity table is missing, or a malformed anchor would slip through on
        exactly the databases least likely to be inspected.
        """
        engine = await _seeded_engine(identity_table=False, caps=[("c-gh", f"github:{GITHUB_ID}")])
        try:
            await _upgrade(engine)
        finally:
            await engine.dispose()

        engine = await _seeded_engine(identity_table=False, caps=[("c-bad", "gitlab:1")])
        try:
            with pytest.raises(RuntimeError, match="unregistered namespace"):
                await _upgrade(engine)
        finally:
            await engine.dispose()


class TestRegistryParity:
    """043's snapshot must match the live registry, or check 2 guards nothing."""

    def test_precedence_snapshot_matches_the_live_registry(self):
        """Same providers, SAME ORDER — order is the entire property under guard.

        The snapshot is deliberately a literal (an already-applied migration's
        meaning must not shift when the registry does), which makes drift possible;
        this is the test that catches it. When it fails, the fix is a NEW migration
        that re-keys the affected rows — not an edit to 043's tuple.
        """
        assert MIG_043.PROVIDER_PRECEDENCE == tuple(p.value for p in PERSON_ANCHOR_PROVIDER_PRECEDENCE), (
            "043's precedence snapshot has drifted from PERSON_ANCHOR_PROVIDER_PRECEDENCE. "
            "A reorder re-keys stored cap anchors and needs its own data migration."
        )

    def test_internal_namespace_snapshot_matches(self):
        assert MIG_043.INTERNAL_NAMESPACE == PERSON_ANCHOR_INTERNAL_NAMESPACE

    def test_registered_namespaces_match_the_live_registry(self):
        assert set(MIG_043.REGISTERED_NAMESPACES) == set(PERSON_ANCHOR_NAMESPACES)

    def test_github_is_first(self):
        """Ruling R7, restated where the migration can enforce it."""
        assert MIG_043.PROVIDER_PRECEDENCE[0] == "github"


def _sql_statements() -> list[str]:
    """Every SQL string the migration hands to `sa.text(...)`.

    Collected via AST rather than by grepping the source, because a text scan for
    "UPDATE"/"DELETE" also matches the guards' error-message prose ("re-key or delete
    the rows") — it fails on a migration that is perfectly read-only, which is the
    kind of false positive that gets a test deleted instead of fixed. Walking to the
    actual `sa.text()` arguments asks the real question: is any statement a write?
    """
    import ast

    source = (MIGRATIONS_DIR / THIS_MIGRATION).read_text()
    statements: list[str] = []

    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "text"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                statements.append(arg.value)
            elif isinstance(arg, ast.JoinedStr):
                # An f-string: keep the literal chunks. The interpolations are table
                # names, never verbs, so the leading keyword is always literal.
                statements.append("".join(v.value for v in arg.values if isinstance(v, ast.Constant) and isinstance(v.value, str)))
    return statements


class TestNoDataMutation:
    def test_every_statement_in_the_migration_is_a_select(self):
        """043 has no write path at all — for any database, not just the seeded one.

        The structural companion to `test_no_anchor_is_rewritten`: that test proves
        the data is unchanged for one seed, this proves there is nothing that *could*
        change it. If a future edit adds a re-key UPDATE here, it belongs in its own
        revision with a documented inverse (module docstring), so this failing is the
        correct outcome rather than a signal to relax the test.
        """
        statements = _sql_statements()
        assert statements, "found no sa.text() statements — the AST walk has stopped matching"

        for statement in statements:
            verb = statement.strip().split()[0].upper()
            assert verb == "SELECT", f"043 must be read-only; found a {verb} statement: {statement.strip()!r}"

    @pytest.mark.asyncio
    async def test_downgrade_is_a_noop(self):
        """Nothing was written, so there is nothing to reverse.

        Runs it to confirm the no-op is real rather than a stub that raises.
        """
        engine = await _seeded_engine()
        try:
            async with engine.begin() as conn:
                before = (await conn.execute(sa.text("SELECT id, person_anchor FROM person_budget_configs ORDER BY id"))).all()
                await conn.run_sync(_run_migration, MIG_043.downgrade)
                after = (await conn.execute(sa.text("SELECT id, person_anchor FROM person_budget_configs ORDER BY id"))).all()

            assert before == after
        finally:
            await engine.dispose()


class TestRevisionChain:
    def test_chains_onto_a_single_head(self):
        revisions: set[str] = set()
        parents: set[str] = set()
        for path in MIGRATIONS_DIR.glob("*.py"):
            if path.name.startswith("__"):
                continue
            module = _load_migration(path.name)
            revisions.add(module.revision)
            if module.down_revision:
                parents.add(module.down_revision)

        heads = revisions - parents
        assert len(heads) == 1, f"expected exactly one head, found: {sorted(heads)}"
        assert MIG_043.revision in revisions
        assert MIG_043.revision in parents or heads == {MIG_043.revision}, "043 has been orphaned off the chain"

    def test_revises_042(self):
        assert MIG_043.down_revision == "042_user_identity_primary"

    def test_revision_id_fits_the_alembic_version_column(self):
        """Shortened from the filename on purpose — `043_person_anchor_rekey` (#4123).

        An over-long id runs `upgrade()` to completion and then overflows on the
        version write, rolling back and leaving the schema a revision behind live code.
        """
        assert len(MIG_043.revision) <= MAX_REVISION_ID_LEN
