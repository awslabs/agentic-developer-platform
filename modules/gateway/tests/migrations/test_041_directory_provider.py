"""Tests for Alembic migration 041 — `directory` in the provider CHECK constraint.

Issue #4843 (T4, EPIC #4839). Design note
`docs/design-notes/4828-platform-native-org-team-user.md` §4 and the blast-radius
row "New provider insert fails at runtime".

This file is **mandatory**, and not only for coverage: `modules/gateway/alembic/**`
is absent from `gateway-ci.yml`'s trigger paths (`src/**`, `tests/**`, `cli/**`,
`pyproject.toml`, `Dockerfile`, frontend, `libs/`, `contracts/`), so a
migration-only change gets **zero CI signal**. A test under `tests/` is what makes
CI run at all for it. Precedent: `test_034_person_budget_configs.py`,
`test_039_flow_design_capture.py`.

What is under test, and why each assertion is load-bearing:

  - **Enum/migration parity, in the direction that catches the real bug.** The
    whole reason 041 exists is that adding a provider to the enum is NOT one line:
    the Postgres CHECK is baked at revision 009 from a hand-copied tuple, and
    SQLite (the test database) does not enforce it — so an enum-only addition
    passes every existing test and rejects every INSERT in production. The parity
    test here fails the moment somebody adds a provider without a follow-up
    migration, which is what turns the two-step rule from a docstring into a gate.
  - **The constraint is REPLACED, not added alongside.** Two overlapping CHECKs on
    one column must both pass, so an additive second constraint would still reject
    `directory` — a migration that appears to succeed and changes nothing.
  - **The docstring correction shipped.** §4 asks for the false "one line to add a
    provider" claim to be corrected in this story; asserted so a future edit cannot
    quietly restore the wrong instruction.
  - **The revision chains onto a single head** and its id fits
    `alembic_version.version_num` (#4123). A dangling or duplicated
    `down_revision` creates a SECOND HEAD, and `alembic upgrade head` then fails
    for **everyone**, blocking every subsequent gateway deploy.

Postgres-specific DDL is asserted **statically** (the constraint SQL the migration
would emit) rather than by executing it: these tests run on SQLite, where
`ALTER TABLE ... DROP CONSTRAINT` does not exist. The migration guards on
`dialect.name` for exactly that reason, and `test_upgrade_is_a_noop_on_sqlite`
pins that guard.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.identity.providers import SUPPORTED_PROVIDERS

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"
PROVIDERS_MODULE = Path(__file__).resolve().parents[2] / "src" / "shared" / "identity" / "providers.py"

THIS_MIGRATION = "041_directory_provider.py"
MAX_REVISION_ID_LEN = 32


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_041 = _load_migration(THIS_MIGRATION)


def _run_migration(sync_conn, fn):
    """Run a migration's upgrade()/downgrade() with alembic's `op` proxy bound.

    The version module calls the module-level `op` proxy, so it must point at a
    real Operations object for the duration. This runs the migration as written
    rather than a paraphrase of it.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        fn()


_PRE_041_USER_IDENTITIES = """
    CREATE TABLE user_identities (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        org_id VARCHAR(255) NOT NULL,
        team_id VARCHAR(255) NOT NULL,
        user_id VARCHAR(255) NOT NULL,
        provider VARCHAR(20) NOT NULL,
        provider_user_id VARCHAR(255) NOT NULL,
        provider_username VARCHAR(255),
        verification_method VARCHAR(20) NOT NULL,
        verified_at DATETIME,
        created_at DATETIME,
        updated_at DATETIME
    )
"""


class TestProviderParity:
    """The gate that makes "a provider addition is TWO changes" enforceable."""

    def test_migration_tuple_matches_the_live_provider_enum(self):
        """041's tuple == `SUPPORTED_PROVIDERS`.

        The load-bearing test in this file. It fails when a provider is added to the
        enum with no accompanying CHECK-constraint migration — the exact defect
        class 041 exists to close, and one that is otherwise invisible until the
        first Postgres INSERT. The fix when it fails is a NEW migration (042-style),
        never an edit to this tuple or to 009.
        """
        assert set(MIG_041.SUPPORTED_PROVIDERS) == set(SUPPORTED_PROVIDERS), (
            "The provider enum and the latest CHECK-constraint migration disagree. "
            "Adding a provider requires a NEW migration re-stating the constraint — "
            "the enum alone leaves Postgres rejecting every INSERT of the new value."
        )

    def test_directory_is_the_value_this_migration_adds(self):
        """`directory` is present now and absent from the pre-041 set.

        Pins the delta rather than only the end state, so the migration's purpose
        survives a future provider addition that rewrites the tuple.
        """
        assert "directory" in MIG_041.SUPPORTED_PROVIDERS
        assert "directory" not in MIG_041.PRE_041_PROVIDERS

    def test_pre_041_tuple_matches_009s_set(self):
        """The downgrade target is really 009's set, so a rollback restores that schema."""
        mig_009 = _load_migration("009_provider_check_constraint.py")
        assert set(MIG_041.PRE_041_PROVIDERS) == set(mig_009.SUPPORTED_PROVIDERS)

    def test_providers_docstring_no_longer_claims_one_line(self):
        """§4 asks for the false "one line" instruction to be corrected here.

        An agent or engineer following the old docstring ships an enum-only change
        that passes CI and breaks production writes. Asserted so the wrong
        instruction cannot be quietly restored.
        """
        docstring = PROVIDERS_MODULE.read_text().split('"""')[1]

        assert "Adding a new channel = one line in this set" not in docstring
        assert "CHECK" in docstring, "the corrected docstring must name the CHECK-constraint migration requirement"


class TestConstraintReplacement:
    """The constraint must be REPLACED; an additive second CHECK would change nothing."""

    def test_upgrade_drops_before_it_adds(self):
        """Both statements, in that order, against the same constraint name."""
        statements = _emitted_sql(MIG_041.upgrade)

        assert len(statements) == 2, f"expected DROP then ADD, got: {statements}"
        assert "DROP CONSTRAINT IF EXISTS ck_user_identities_provider" in statements[0]
        assert "ADD CONSTRAINT ck_user_identities_provider" in statements[1]

    def test_upgrade_constraint_lists_every_supported_provider(self):
        """Including `directory`, and each quoted as a SQL literal."""
        added = _emitted_sql(MIG_041.upgrade)[1]

        for provider in SUPPORTED_PROVIDERS:
            assert f"'{provider}'" in added, f"{provider} missing from the CHECK"

    def test_downgrade_restores_the_pre_041_provider_set(self):
        """`directory` is gone from the restored constraint; the other seven remain."""
        restored = _emitted_sql(MIG_041.downgrade)[1]

        assert "'directory'" not in restored
        for provider in MIG_041.PRE_041_PROVIDERS:
            assert f"'{provider}'" in restored

    def test_constraint_name_is_reused_not_suffixed(self):
        """One constraint per column, however many providers get added later.

        A new name each time would accumulate overlapping CHECKs that must ALL pass —
        so the oldest one would silently veto every newer provider.
        """
        for statement in _emitted_sql(MIG_041.upgrade):
            assert "ck_user_identities_provider" in statement
            assert "ck_user_identities_provider_" not in statement


def _emitted_sql(migration_fn) -> list[str]:
    """The SQL strings the migration would execute against Postgres.

    Captured through a mock bind rather than a real connection: the statements are
    Postgres-only DDL that SQLite cannot parse, and what is under test is *which
    statements* the migration emits.
    """
    from unittest.mock import MagicMock, patch

    bind = MagicMock()
    bind.dialect.name = "postgresql"
    captured: list[str] = []

    with (
        patch.object(MIG_041.op, "get_bind", return_value=bind),
        patch.object(MIG_041.op, "execute", side_effect=lambda clause: captured.append(str(clause))),
    ):
        migration_fn()
    return captured


class TestSqliteIsUntouched:
    """SQLite has no `ALTER TABLE ... DROP CONSTRAINT`; the guard must hold."""

    @pytest.mark.asyncio
    async def test_upgrade_is_a_noop_on_sqlite(self):
        """Runs to completion and leaves the table exactly as it was.

        Not a trivial assertion: without the `dialect.name` guard this migration
        raises on SQLite, which would fail every test that builds a schema through
        alembic. Provider validation in tests comes from the ORM's
        `@validates("provider")` hook instead.
        """
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        try:
            async with engine.begin() as conn:
                await conn.execute(sa.text(_PRE_041_USER_IDENTITIES))
                before = await conn.run_sync(lambda c: {col["name"] for col in sa_inspect(c).get_columns("user_identities")})
                await conn.run_sync(_run_migration, MIG_041.upgrade)
                after = await conn.run_sync(lambda c: {col["name"] for col in sa_inspect(c).get_columns("user_identities")})

            assert before == after

            # And a `directory` row inserts, since SQLite enforces no CHECK — which
            # is precisely why the parity test above, not this one, is the real guard.
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "INSERT INTO user_identities "
                        "(id, org_id, team_id, user_id, provider, provider_user_id, verification_method) "
                        "VALUES ('i1', 'o1', 't1', 'u1', 'directory', 'aad-1', 'admin_manual')"
                    )
                )
                assert (await conn.execute(sa.text("SELECT provider FROM user_identities WHERE id = 'i1'"))).scalar() == "directory"
        finally:
            await engine.dispose()


class TestRevisionChain:
    def test_chains_onto_a_single_head(self):
        """A dangling or duplicated `down_revision` blocks every later deploy."""
        revisions: set[str] = set()
        parents: set[str] = set()
        for path in MIGRATIONS_DIR.glob("*.py"):
            if path.name.startswith("__"):
                continue
            module = _load_migration(path.name)
            revisions.add(module.revision)
            if module.down_revision:
                parents.update((module.down_revision,) if isinstance(module.down_revision, str) else module.down_revision)

        heads = revisions - parents
        assert len(heads) == 1, f"expected exactly one head, found: {sorted(heads)}"
        assert MIG_041.revision in revisions
        assert MIG_041.revision in parents or heads == {MIG_041.revision}, "041 has been orphaned off the chain"

    def test_revises_040_team_memberships(self):
        assert MIG_041.down_revision == "040_team_memberships"

    def test_revision_id_fits_the_alembic_version_column(self):
        """SQLite does not enforce VARCHAR length; Postgres does (#4123).

        An over-long id runs `upgrade()` to completion and then overflows on the
        version write, rolling the whole migration back and leaving the schema one
        revision behind live code.
        """
        assert len(MIG_041.revision) <= MAX_REVISION_ID_LEN
