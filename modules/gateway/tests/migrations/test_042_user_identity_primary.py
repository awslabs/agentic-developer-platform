"""Tests for Alembic migration 042 — user_identities.is_primary.

Issue #4843 (T4, EPIC #4839). Design note
`docs/design-notes/4828-platform-native-org-team-user.md` §4 defect (b).

This file is **mandatory**, and not only for coverage: `modules/gateway/alembic/**`
is absent from `gateway-ci.yml`'s trigger paths, so a migration-only change gets
**zero CI signal**. A test under `tests/` is what makes CI run at all for it.

What is under test, and why each assertion is load-bearing rather than a
restatement of the DDL:

  - **The backfill is a NO-OP IN EFFECT.** This is the assertion that matters most
    in the file and it is not obvious from the DDL. `person_budget_configs`
    .person_anchor stores anchor strings durably, so if the backfill flagged any row
    other than the one the resolvers were already picking, every affected person's
    live cap would be instantly orphaned — displaying a limit, matching nothing
    (#4511). The test therefore computes the pre-migration pick (`MIN(provider_user_id)`
    per `(user_id, provider)`) and asserts the flagged row IS that row, for a person
    holding two same-provider accounts.
  - **The column is NOT NULL with a server default.** Gateway pods running the
    pre-042 image INSERT without this column; a NOT NULL column with no default
    fails every one of those in-flight writes, turning a routine deploy into an
    identity-link outage (039's reasoning, which this follows).
  - **New rows default to False, not True.** A newly linked second account must not
    silently displace an existing primary — that is a cap re-key performed by an
    ordinary admin action.
  - **The uniqueness is PARTIAL.** A plain `UNIQUE (user_id, provider)` was
    investigated and rejected: three write paths accept a legitimate second
    same-provider account with no IntegrityError handler, so it would convert
    working admin actions into 500s. The test asserts two non-primary rows still
    insert, because "multi-account keeps working" is the property that made the
    partial index the right shape.
  - **Model/migration parity.** Both are hand-written, so drift is the live risk:
    the migration is what runs in dev, the model is what the app uses.
  - **The revision chains onto a single head** and its id fits
    `alembic_version.version_num` (#4123).

The partial UNIQUE INDEX itself is asserted statically (the SQL the migration
emits): it is `CREATE UNIQUE INDEX ... WHERE`, guarded to Postgres, and these tests
run on SQLite.
"""

import ast
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.vault import UserIdentity

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"
THIS_MIGRATION = "042_user_identity_primary.py"
TABLE = "user_identities"
COLUMN = "is_primary"
MAX_REVISION_ID_LEN = 32


def _load_migration(filename: str):
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_042 = _load_migration(THIS_MIGRATION)


def _run_migration(sync_conn, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        fn()


# The pre-042 table, written out rather than built from ORM metadata so it is the
# real starting state and not a restatement of today's model.
_PRE_042_USER_IDENTITIES = """
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

# A person holding TWO GitHub rows — the shape whose anchor depends on which row is
# picked, and therefore the only seed that proves the backfill is a no-op in effect.
# `11111111` sorts BEFORE `99999999`, so it is what `ORDER BY provider_user_id ASC`
# (the pre-042 convention in all three resolvers) already returned.
_SEED = """
    INSERT INTO user_identities
        (id, org_id, team_id, user_id, provider, provider_user_id, verification_method)
    VALUES
        ('i-first',  'o1', 't1', 'u-two-accounts', 'github',    '11111111', 'oauth'),
        ('i-second', 'o1', 't1', 'u-two-accounts', 'github',    '99999999', 'admin_manual'),
        ('i-single', 'o1', 't1', 'u-single',       'github',    '22222222', 'oauth'),
        ('i-dir',    'o1', 't1', 'u-two-accounts', 'directory', 'aad-1',    'admin_manual')
"""


async def _engine_at_pre_042(seed: bool = True):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(sa.text(_PRE_042_USER_IDENTITIES))
        if seed:
            await conn.execute(sa.text(_SEED))
    return engine


async def _upgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_042.upgrade)


async def _primary_ids(engine) -> set[str]:
    async with engine.begin() as conn:
        return set((await conn.execute(sa.text(f"SELECT id FROM {TABLE} WHERE {COLUMN}"))).scalars())


class TestBackfillIsANoOpInEffect:
    """The load-bearing property: no anchor changes across this migration."""

    @pytest.mark.asyncio
    async def test_flags_the_row_the_resolvers_already_picked(self):
        """For a two-GitHub-row person, the primary is the LOWEST provider_user_id.

        The pre-042 convention in all three resolvers was `ORDER BY provider_user_id
        ASC`. Computing that expectation from the data rather than hardcoding
        `i-first` means the test states the RULE ("the row the old convention
        picked") instead of an answer that happens to match.
        """
        engine = await _engine_at_pre_042()
        try:
            async with engine.begin() as conn:
                pre_migration_pick = (
                    await conn.execute(
                        sa.text(
                            f"SELECT id FROM {TABLE} WHERE user_id = 'u-two-accounts' AND provider = 'github' ORDER BY provider_user_id ASC LIMIT 1"
                        )
                    )
                ).scalar()

            await _upgrade(engine)

            async with engine.begin() as conn:
                flagged = (
                    (await conn.execute(sa.text(f"SELECT id FROM {TABLE} WHERE user_id = 'u-two-accounts' AND provider = 'github' AND {COLUMN}")))
                    .scalars()
                    .all()
                )

            assert flagged == [pre_migration_pick], (
                "the backfill flagged a different row than the pre-042 resolvers picked — every affected person's stored cap is now orphaned (#4511)"
            )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_exactly_one_primary_per_user_and_provider(self):
        """The invariant the partial index enforces, established by the backfill.

        Asserted over the DATA as well as via the index, because on SQLite the index
        is not created — so without this the backfill's correctness would be untested
        on the dialect the suite actually runs.
        """
        engine = await _engine_at_pre_042()
        try:
            await _upgrade(engine)
            async with engine.begin() as conn:
                over_flagged = (
                    await conn.execute(
                        sa.text(f"SELECT user_id, provider, COUNT(*) AS n FROM {TABLE} WHERE {COLUMN} GROUP BY user_id, provider HAVING COUNT(*) > 1")
                    )
                ).all()
            assert over_flagged == []
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_every_user_and_provider_pair_gets_a_primary(self):
        """No pair is left with zero primaries.

        An unflagged pair still resolves (the `provider_user_id ASC` tiebreaker
        survives in the ORDER BY), but it means the DB invariant does not cover that
        person — the convention-only state this migration exists to end.
        """
        engine = await _engine_at_pre_042()
        try:
            await _upgrade(engine)
            async with engine.begin() as conn:
                pairs = (await conn.execute(sa.text(f"SELECT DISTINCT user_id, provider FROM {TABLE}"))).all()
                flagged_pairs = (await conn.execute(sa.text(f"SELECT DISTINCT user_id, provider FROM {TABLE} WHERE {COLUMN}"))).all()
            assert set(pairs) == set(flagged_pairs)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_each_provider_is_flagged_independently(self):
        """A person's github primary and directory primary are separate rows.

        The invariant is per `(user_id, provider)`, not per user — a person anchored
        on GitHub still needs a determinate directory row for the day GitHub is
        unlinked.
        """
        engine = await _engine_at_pre_042()
        try:
            await _upgrade(engine)
            assert "i-dir" in await _primary_ids(engine)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_cross_org_tie_flags_exactly_one_row(self):
        """Two rows tying on MIN(provider_user_id) must not BOTH be flagged.

        Uniqueness on this table is `(provider, provider_user_id, org_id)`, so an
        identical (user_id, provider, provider_user_id) is a storable state across
        two orgs. Both rows tie on the backfill's `MIN(provider_user_id)`; without
        the `MIN(id)` tie-break both get flagged and the partial unique index —
        created immediately after, on Postgres only — aborts the whole migration
        mid-deploy on data it didn't cause. The tied rows carry the same anchor
        identifier, so which one wins is anchor-irrelevant; that exactly ONE wins,
        deterministically, is the property under test.
        """
        engine = await _engine_at_pre_042(seed=False)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        f"INSERT INTO {TABLE} (id, org_id, team_id, user_id, provider, provider_user_id, verification_method) VALUES "
                        f"('i-tie-a', 'o1', 't1', 'u-tied', 'github', '55555555', 'oauth'), "
                        f"('i-tie-b', 'o2', 't2', 'u-tied', 'github', '55555555', 'oauth')"
                    )
                )

            await _upgrade(engine)

            async with engine.begin() as conn:
                flagged = (
                    (await conn.execute(sa.text(f"SELECT id FROM {TABLE} WHERE user_id = 'u-tied' AND provider = 'github' AND {COLUMN}")))
                    .scalars()
                    .all()
                )
            assert flagged == ["i-tie-a"], (
                "the tie-break must flag exactly one deterministic row (lowest id); "
                f"flagging {flagged} would abort the CREATE UNIQUE INDEX on Postgres"
            )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_upgrade_survives_an_empty_table(self):
        """The backfill UPDATE must not fault on a fresh install."""
        engine = await _engine_at_pre_042(seed=False)
        try:
            await _upgrade(engine)
            assert await _primary_ids(engine) == set()
        finally:
            await engine.dispose()


class TestColumnShape:
    @pytest.mark.asyncio
    async def test_column_is_not_null_with_a_server_default(self):
        """Both properties together are what keeps the rollout writable.

        Pods on the pre-042 image INSERT without this column; NOT NULL with no
        default would fail every one of those writes mid-deploy.
        """
        engine = await _engine_at_pre_042()
        try:
            await _upgrade(engine)
            async with engine.begin() as conn:
                column = await conn.run_sync(lambda c: next(col for col in sa_inspect(c).get_columns(TABLE) if col["name"] == COLUMN))
            assert column["nullable"] is False
            assert column["default"] is not None, "no server default — pre-042 pods' INSERTs would fail"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_new_row_defaults_to_not_primary(self):
        """False, NOT True.

        A newly linked second account defaulting to primary would displace the
        existing one and re-key that person's anchor — a cap re-key performed by an
        ordinary admin action, which is exactly what must not be possible.
        """
        engine = await _engine_at_pre_042()
        try:
            await _upgrade(engine)
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        f"INSERT INTO {TABLE} (id, org_id, team_id, user_id, provider, provider_user_id, verification_method) "
                        f"VALUES ('i-new', 'o1', 't1', 'u-two-accounts', 'github', '33333333', 'admin_manual')"
                    )
                )
                value = (await conn.execute(sa.text(f"SELECT {COLUMN} FROM {TABLE} WHERE id = 'i-new'"))).scalar()
            assert not value

            # And the person's existing primary is untouched, so their anchor is too.
            assert "i-first" in await _primary_ids(engine)
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_multi_account_still_inserts(self):
        """The reason this is a PARTIAL index and not `UNIQUE (user_id, provider)`.

        Three write paths accept a legitimate second same-provider account and none
        has an IntegrityError handler; a plain unique constraint would turn those
        working admin actions into 500s. Two non-primary rows for one pair must
        remain insertable.
        """
        engine = await _engine_at_pre_042()
        try:
            await _upgrade(engine)
            async with engine.begin() as conn:
                for suffix in ("a", "b"):
                    await conn.execute(
                        sa.text(
                            f"INSERT INTO {TABLE} (id, org_id, team_id, user_id, provider, provider_user_id, verification_method) "
                            f"VALUES ('i-extra-{suffix}', 'o1', 't1', 'u-single', 'github', '4444444{suffix}', 'admin_manual')"
                        )
                    )
                count = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE} WHERE user_id = 'u-single' AND provider = 'github'"))).scalar()
            assert count == 3
        finally:
            await engine.dispose()


class TestPartialIndexSql:
    """The index is Postgres-only DDL, so its shape is asserted statically."""

    def test_upgrade_creates_a_partial_unique_index_on_user_id_and_provider(self):
        statements = _emitted_sql(MIG_042.upgrade)
        create = next(s for s in statements if "CREATE UNIQUE INDEX" in s)

        assert MIG_042.PARTIAL_UNIQUE_INDEX in create
        assert "(user_id, provider)" in create
        assert f"WHERE {COLUMN}" in create, "without the WHERE clause this is a PLAIN unique constraint — see the module docstring"

    def test_downgrade_drops_the_index(self):
        assert any("DROP INDEX IF EXISTS" in s and MIG_042.PARTIAL_UNIQUE_INDEX in s for s in _emitted_sql(MIG_042.downgrade))


class TestAppLayerPrimaryWriters:
    """Only guarded code paths may write `is_primary` on user_identities.

    The invariant "at most one primary per (user_id, provider)" is enforced ONLY by
    this migration's partial unique index, which is Postgres-only — the SQLite the
    test suite runs on never creates it, so **CI is structurally blind to a
    second-primary write**. Org placement (#4943) is now a guarded writer: it
    copies a source primary only when the destination has none. Its behavioral
    tests in test_org_member_add.py preserve the source anchor on new placement
    and exercise the partial unique index when the destination already has a
    primary. All other modules remain subject to this tripwire.

    This pin is the tripwire for the first promote/demote endpoint. A future
    `UPDATE ... SET is_primary = true` with no guard passes every SQLite test with
    two primary rows and then 500s on Postgres with a raw IntegrityError — the
    exact dialect gap called out in this migration's docstring. Whoever writes
    that path must consciously ship an app-layer guard alongside it (mirror
    `src/admin/team_memberships.py`'s handling for the twin
    `uq_team_memberships_one_primary` index: `add_membership` refuses to move an
    existing different primary implicitly, and `set_primary_team` demotes-then-
    promotes inside one transaction) — and then rescope this test to exempt the
    guarded module, the same way `TestSingleComposer` exempts `person_anchor.py`
    in `tests/shared/identity/test_person_anchor_namespaces.py`, whose AST-pin
    shape this follows.
    """

    SRC = Path(__file__).resolve().parents[2] / "src"
    GUARDED_WRITERS = {"admin/org_members.py"}

    @staticmethod
    def _is_primary_writes(path: Path, source: str | None = None) -> list[str]:
        """Locations in one module that WRITE `is_primary`, in any spelling.

        Only modules that mention `UserIdentity` or `user_identities` are
        examined at all: `is_primary` is also a legitimate, guarded column on
        team_memberships (`src/admin/team_memberships.py` must keep passing).
        Within a user-identity module, a write is any of:

          - a keyword argument `is_primary=` (constructor, `.values(...)`,
            `update(...)` — every ORM/Core spelling routes through a keyword),
          - an assignment to an `.is_primary` attribute (`row.is_primary = ...`),
          - a dict literal key `"is_primary"` (`.values({...})`, bulk mappings).

        Attribute READS (`UserIdentity.is_primary.desc()`) are deliberately not
        matched: ordering by the flag is the whole point of the column.
        """
        source = path.read_text() if source is None else source
        if "UserIdentity" not in source and "user_identities" not in source:
            return []

        offenders: list[str] = []
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if isinstance(node, ast.keyword) and node.arg == "is_primary":
                offenders.append(f"{path}:{node.value.lineno}")
            elif isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Attribute) and t.attr == "is_primary" for t in targets):
                    offenders.append(f"{path}:{node.lineno}")
            elif isinstance(node, ast.Dict):
                if any(isinstance(k, ast.Constant) and k.value == "is_primary" for k in node.keys):
                    offenders.append(f"{path}:{node.lineno}")
        return offenders

    def test_only_guarded_src_code_paths_write_is_primary_on_user_identities(self):
        offenders: list[str] = []
        for path in sorted(self.SRC.rglob("*.py")):
            if path.relative_to(self.SRC).as_posix() in self.GUARDED_WRITERS:
                continue
            offenders.extend(self._is_primary_writes(path))

        assert offenders == [], (
            "These locations write `is_primary` on user_identities. The one-primary "
            "invariant is a Postgres-only partial index that SQLite CI cannot see, so "
            "an unguarded write here passes every test and raises IntegrityError 500s "
            "in production. Add an app-layer guard first (see this class's docstring "
            f"and src/admin/team_memberships.py), then rescope this pin: {offenders}"
        )

    def test_the_detector_actually_detects(self, tmp_path):
        """Guard on the guard: a test that can never fail is worse than no test."""
        offending = tmp_path / "offender.py"
        offending.write_text(
            "from src.shared.models.vault import UserIdentity\n"
            "def promote(row):\n"
            "    row.is_primary = True\n"
            "def link(db):\n"
            "    db.add(UserIdentity(is_primary=True))\n"
        )

        assert len(self._is_primary_writes(offending)) == 2

    def test_the_detector_scopes_to_user_identity_modules(self, tmp_path):
        """team_memberships' own (guarded) is_primary writes must not trip the pin."""
        unrelated = tmp_path / "memberships.py"
        unrelated.write_text("def promote(membership):\n    membership.is_primary = True\n")

        assert self._is_primary_writes(unrelated) == []


def _emitted_sql(migration_fn) -> list[str]:
    """The raw SQL the migration would execute against Postgres."""
    from unittest.mock import MagicMock, patch

    bind = MagicMock()
    bind.dialect.name = "postgresql"
    captured: list[str] = []

    with (
        patch.object(MIG_042.op, "get_bind", return_value=bind),
        patch.object(MIG_042.op, "execute", side_effect=lambda clause: captured.append(str(clause))),
        patch.object(MIG_042.op, "add_column"),
        patch.object(MIG_042.op, "drop_column"),
    ):
        migration_fn()
    return captured


class TestDowngrade:
    @pytest.mark.asyncio
    async def test_downgrade_removes_the_column_and_leaves_rows_intact(self):
        """A rollback returns the anchor to the pre-042 convention, re-keying nothing.

        `provider_user_id ASC` alone reproduces the old pick exactly, which is why
        dropping the column is a complete and safe reversal rather than a partial one.
        """
        engine = await _engine_at_pre_042()
        try:
            await _upgrade(engine)
            async with engine.begin() as conn:
                await conn.run_sync(_run_migration, MIG_042.downgrade)
                columns = await conn.run_sync(lambda c: {col["name"] for col in sa_inspect(c).get_columns(TABLE)})
                rows = (await conn.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}"))).scalar()

            assert COLUMN not in columns
            assert rows == 4, "downgrade must not delete identity rows"
        finally:
            await engine.dispose()


class TestModelParity:
    """Migration and model are both hand-written, so drift is the live risk."""

    def test_model_declares_the_column(self):
        assert COLUMN in UserIdentity.__table__.columns

    def test_model_column_is_not_null_and_defaults_false(self):
        column = UserIdentity.__table__.columns[COLUMN]

        assert column.nullable is False
        assert column.server_default is not None
        assert column.default.arg is False, "the model must default False for the same reason the DDL does"


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
                parents.update((module.down_revision,) if isinstance(module.down_revision, str) else module.down_revision)

        heads = revisions - parents
        assert len(heads) == 1, f"expected exactly one head, found: {sorted(heads)}"
        assert MIG_042.revision in revisions
        assert MIG_042.revision in parents or heads == {MIG_042.revision}, "042 has been orphaned off the chain"

    def test_revises_041(self):
        assert MIG_042.down_revision == "041_directory_provider"

    def test_revision_id_fits_the_alembic_version_column(self):
        assert len(MIG_042.revision) <= MAX_REVISION_ID_LEN
