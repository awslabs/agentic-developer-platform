"""Tests for Alembic migration 051 — the story-to-PR binding table.

Issue #5301 (EPIC #4191). This file is **mandatory**, and not only for coverage:
`modules/gateway/alembic/**` is absent from `gateway-ci.yml`'s trigger paths
(`src/**`, `tests/**`, `cli/**`, `pyproject.toml`, `Dockerfile`, frontend, `libs/`,
`contracts/`), so a migration-only change gets **zero CI signal**. A test under
`tests/` is what makes CI run at all for this migration.

These tests exercise the REAL migration functions imported from the version module.
A test that re-implements the migration proves only that the author can write the
same bug twice.

What is under test:

  - `upgrade()` creates the table and its indexes on an **alembic-only** database —
    no `Base.metadata.create_all` anywhere. This catches a migration that "works"
    only because `create_all` already built the table: the failure mode where a
    table is declared in models, has no DDL, and is absent on every deployed
    database. For this story that failure is especially quiet, because the
    reconciliation path falls back to the legacy issue-closure query when no binding
    is found — so an absent table looks exactly like the bug being fixed.
  - `downgrade()` reverses it completely, and is deliberately unguarded because it IS
    the documented rollback path.
  - **No backfill.** The story explicitly refuses to adopt PRs by searching for
    issue mentions, so the migration must invent no rows. Asserted structurally as
    well as behaviourally.
  - The revision chains onto the real single head. A broken `down_revision` silently
    SKIPS the migration, and live code then queries an absent table.
  - **The unique index covers the pull request and nothing else.** Two concurrent
    registrations of one PR can both pass an application-level "is this already
    bound?" read; only one can win a unique index. Without it a duplicated event, a
    retried request or a restarted tick each insert a binding, and reconciliation
    then has several candidate PRs for one story with no basis to choose.
  - Postgres rendering, because tests run on SQLite but dev runs on Postgres.
    `provider_repository_id` and `installation_id` must render as `BIGINT`.
  - Migration/model schema parity: both are hand-written, so drift is the live risk.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.models import OrchestrationPullRequestBinding

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

TABLE = "orchestration_pr_bindings"
PR_INDEX = "uq_orchestration_pr_bindings_pr"


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_051 = _load_migration("051_orchestration_pr_bindings.py")


def _run_migration(sync_conn, fn):
    """Run a migration's upgrade()/downgrade() with alembic's `op` proxy bound.

    The version module calls the module-level `op` proxy, so it must point at a real
    Operations object for the duration. This runs the migration as written rather
    than a paraphrase of it.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        fn()


async def _bare_engine():
    """An engine with NO schema at all — no create_all, no models.

    This is the important fixture: `create_all` would build the table from ORM
    metadata and mask a migration that never creates it itself.
    """
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


async def _upgrade(engine):
    # The table carries FKs to orchestration_flows/orchestration_nodes. SQLite does
    # not enforce them unless asked, and this migration is not the place to prove
    # referential integrity — so the parent tables are deliberately absent. That is
    # also what keeps this an honestly "alembic-only, empty database" fixture.
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_051.upgrade)


async def _downgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_051.downgrade)


def _insert(
    binding_id="b1",
    org="org-1",
    repo_id=987_654_321,
    pr_node="PR_kwDOABCD1234",
    repo="acme/platform",
    pr_number=5293,
    node="node-a",
    attempt=1,
    head="6c7370387d5d57a6ff9ebb5a567f0744e7d99d0e",
    role="implementation",
    state="active",
):
    return sa.text(
        f"INSERT INTO {TABLE} "
        "(id, org_id, flow_id, node_id, attempt, run_id, provider_repository_id, provider_pr_node_id, "
        " repo, pr_number, installation_id, head_sha, role, state, registered_by, registered_by_kind, created_at) "
        "VALUES (:id, :org, 'flow-a', :node, :attempt, 'orch:run-1', :repo_id, :pr_node, "
        " :repo, :pr_number, 12345678901, :head, :role, :state, 'agent:developer', 'service', '2026-01-01 00:00:00')"
    ).bindparams(
        id=binding_id,
        org=org,
        node=node,
        attempt=attempt,
        repo_id=repo_id,
        pr_node=pr_node,
        repo=repo,
        pr_number=pr_number,
        head=head,
        role=role,
        state=state,
    )


class TestAlembicOnlyDatabase:
    """The migration stands on its own, with no `create_all` involved."""

    async def test_upgrade_creates_the_table_without_create_all(self):
        """The table exists after upgrade() on a database that started empty."""
        engine = await _bare_engine()
        async with engine.connect() as conn:
            before = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        assert before == set(), "fixture must start with an empty database for this to prove anything"

        await _upgrade(engine)

        async with engine.connect() as conn:
            after = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()

        assert TABLE in after, f"upgrade() did not create {TABLE}"

    async def test_rows_are_insertable_after_alembic_only_upgrade(self):
        """The table is actually usable, not just present.

        Catches a table created with a shape the application cannot write to — e.g.
        a NOT NULL column the registration path never populates.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert())

        async with engine.connect() as conn:
            row = (await conn.execute(sa.text(f"SELECT repo, pr_number, provider_repository_id, role, state FROM {TABLE}"))).one()
        await engine.dispose()

        assert row == ("acme/platform", 5293, 987_654_321, "implementation", "active")

    async def test_installation_id_accepts_a_64_bit_value(self):
        """A provider integer past 2^31 round-trips.

        SQLite ignores declared integer width, so this proves the column is writable
        rather than the width; `TestPostgresRendering` covers the width where it is
        actually enforced. Both matter: this one would catch a `String` column.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await conn.execute(_insert())
        async with engine.connect() as conn:
            value = (await conn.execute(sa.text(f"SELECT installation_id FROM {TABLE}"))).scalar_one()
        await engine.dispose()
        assert value == 12345678901


class TestPullRequestUniqueness:
    """The invariant that makes duplicate registration converge instead of fork."""

    @pytest.fixture
    async def engine(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        yield engine
        await engine.dispose()

    async def test_pr_index_exists_and_is_unique(self, engine):
        """`(org_id, provider_repository_id, provider_pr_node_id)`, unique.

        Asserted on the migration's own DDL rather than the model's, because the
        migration is what runs against a deployed database. If this index is missing
        there, `register_binding`'s existence read is advisory: two concurrent
        registrations both read "not bound" and both insert.
        """
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes(TABLE))

        unique = {i["name"]: tuple(i["column_names"]) for i in indexes if i["unique"]}
        assert PR_INDEX in unique, f"pull-request uniqueness index missing; got {sorted(unique)}"
        assert unique[PR_INDEX] == ("org_id", "provider_repository_id", "provider_pr_node_id")

    async def test_duplicate_pull_request_is_rejected_by_the_database(self, engine):
        """Registering the same PR twice cannot produce two rows.

        This is the duplicated-event / retried-request / restarted-tick case from
        the acceptance criteria, enforced below the application layer.
        """
        async with engine.begin() as conn:
            await conn.execute(_insert(binding_id="b1"))

        with pytest.raises(sa.exc.IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(_insert(binding_id="b2"))

    async def test_the_unique_index_keys_on_the_immutable_pr_node_id(self, engine):
        """Not on `repo` + `pr_number`.

        Those are mutable display names: a repository rename or transfer re-points
        every name-keyed row, and two different repositories can both have a
        `#5293`. Keying on the provider's immutable node id is what makes a binding
        survive a rename. Same rationale as `OrchestrationWorkClaim`.
        """
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes(TABLE))
        pr_index = next(i for i in indexes if i["name"] == PR_INDEX)

        assert "provider_pr_node_id" in pr_index["column_names"]
        assert "repo" not in pr_index["column_names"]
        assert "pr_number" not in pr_index["column_names"]

    async def test_the_unique_index_does_not_include_state_or_attempt(self, engine):
        """A superseded binding must keep occupying its pull request.

        If `state` were part of the key, superseding a binding would free its PR for
        a *second* row — and the whole point of `SUPERSEDED` is that the old PR stays
        permanently fenced from completing new scope, with its provenance intact. If
        `attempt` were part of the key, the same PR could be re-bound on each retry,
        and a merged PR from attempt 1 could then complete attempt 3's scope.
        """
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes(TABLE))
        pr_index = next(i for i in indexes if i["name"] == PR_INDEX)
        assert "state" not in pr_index["column_names"]
        assert "attempt" not in pr_index["column_names"]

        async with engine.begin() as conn:
            await conn.execute(_insert(binding_id="b1"))
            await conn.execute(sa.text(f"UPDATE {TABLE} SET state = 'superseded' WHERE id = 'b1'"))

        with pytest.raises(sa.exc.IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(_insert(binding_id="b2", attempt=2))

    async def test_distinct_pull_requests_and_tenants_coexist(self, engine):
        """Uniqueness is scoped to the PR within a tenant, not global.

        The cross-tenant row matters: an identical provider PR id appearing under
        another `org_id` must not collide, or one tenant's registration could block
        another's.
        """
        async with engine.begin() as conn:
            await conn.execute(_insert(binding_id="b1", pr_node="PR_a"))
            await conn.execute(_insert(binding_id="b2", pr_node="PR_b", node="node-b"))
            await conn.execute(_insert(binding_id="b3", pr_node="PR_a", org="org-2"))
            await conn.execute(_insert(binding_id="b4", pr_node="PR_a", repo_id=111))

        async with engine.connect() as conn:
            count = (await conn.execute(sa.text(f"SELECT count(*) FROM {TABLE}"))).scalar_one()
        assert count == 4


class TestSchema:
    @pytest.fixture
    async def engine(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        yield engine
        await engine.dispose()

    @pytest.fixture
    async def columns(self, engine):
        async with engine.connect() as conn:
            return await conn.run_sync(lambda c: {x["name"]: x for x in sa_inspect(c).get_columns(TABLE)})

    async def test_table_carries_org_id(self, columns):
        """Tenant isolation is a column, not a convention."""
        assert "org_id" in columns
        assert columns["org_id"]["nullable"] is False

    async def test_identity_columns_are_required(self, columns):
        """A binding missing any of these identifies nothing verifiable.

        `head_sha` is in this set deliberately: it is what makes review and check
        evidence falsifiable. A NULL head would mean "this PR, at whatever commit it
        happens to be at", which is precisely the eligibility-inheritance bug the
        story forbids — an approval of one diff silently authorizing another.
        """
        for name in (
            "flow_id",
            "node_id",
            "attempt",
            "run_id",
            "provider_repository_id",
            "provider_pr_node_id",
            "repo",
            "pr_number",
            "installation_id",
            "head_sha",
            "role",
            "state",
        ):
            assert columns[name]["nullable"] is False, f"{name} must be NOT NULL"

    async def test_provenance_columns_are_required(self, columns):
        """Every binding records who established it.

        AC5 requires historical recovery to record who established the binding; that
        is only auditable if provenance can never be NULL, including on the ordinary
        self-registration path. An optional `registered_by` makes the recovery case
        indistinguishable from a row whose author was simply not recorded.
        """
        for name in ("registered_by", "registered_by_kind"):
            assert columns[name]["nullable"] is False, f"{name} must be NOT NULL"

    async def test_recovery_and_supersession_columns_are_nullable(self, columns):
        """Optional means nullable, not defaulted to a lie.

        `recovery_reason` is NULL on a self-registered binding and non-NULL only on a
        human-attributed recovery — that asymmetry is the audit signal, so a
        server_default would erase it. `superseded_*` are NULL while a binding is
        active; a defaulted `superseded_at` would mark every binding replaced.
        """
        for name in ("recovery_reason", "superseded_reason", "superseded_at", "updated_at"):
            assert columns[name]["nullable"] is True, f"{name} must be nullable"
            assert columns[name].get("default") is None, f"{name} must have no server_default"

    async def test_role_and_state_have_no_server_default(self, columns):
        """The application decides both; the database must not guess.

        A `server_default` of `'implementation'` would let a row that bypassed
        `register_binding` land looking like a legitimate implementation binding —
        and an implementation binding is the only kind that can complete a story.
        The model's Python-side default supplies the value on the real path.
        """
        assert columns["role"].get("default") is None
        assert columns["state"].get("default") is None

    async def test_node_and_flow_are_foreign_keys_with_cascade(self, engine):
        """A binding is meaningless without its story, so it dies with it.

        The opposite choice from `OrchestrationWorkClaim`, and deliberately: a work
        claim outlives the runs passing through it because its release reason
        explains why an issue is free. A binding asserts "this PR implements *this
        story*" — with the story deleted the row makes no claim about anything, and
        keeping it would leave a PR permanently occupying the unique index against a
        node that no longer exists.
        """
        async with engine.connect() as conn:
            fks = await conn.run_sync(lambda c: {tuple(f["constrained_columns"]): f for f in sa_inspect(c).get_foreign_keys(TABLE)})

        assert set(fks) == {("flow_id",), ("node_id",)}, f"unexpected foreign keys: {sorted(fks)}"
        assert fks[("flow_id",)]["referred_table"] == "orchestration_flows"
        assert fks[("node_id",)]["referred_table"] == "orchestration_nodes"
        for fk in fks.values():
            assert fk["options"].get("ondelete") == "CASCADE"

    async def test_read_indexes_exist(self, engine):
        """Reconciliation reads the active binding for one node, per tenant.

        Without `(org_id, node_id, state)` that read is a scan on every tick for
        every story in the sweep — a sequential scan of a table that grows with every
        PR the platform ever opens.
        """
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: {i["name"]: tuple(i["column_names"]) for i in sa_inspect(c).get_indexes(TABLE)})

        assert indexes.get("ix_orchestration_pr_bindings_node_id") == ("org_id", "node_id", "state")
        assert "ix_orchestration_pr_bindings_org_id" in indexes
        assert "ix_orchestration_pr_bindings_flow_id" in indexes


class TestDowngrade:
    async def test_downgrade_removes_the_table(self):
        """The documented rollback plan; prove it works."""
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)

        async with engine.connect() as conn:
            remaining = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()

        assert TABLE not in remaining, "downgrade() left the table behind"

    async def test_downgrade_drops_the_table_even_with_rows_present(self):
        """Unguarded means unguarded: existing bindings do not block rollback.

        Asserted explicitly because a nearby migration (049) raises on non-empty
        tables, and someone copying that pattern here would break the rollback. The
        cost of a downgrade is that bound stories revert to the issue-closure path
        and wait rather than completing — the safe direction, and why recovery is
        attributed and repeatable. If this behavior is ever meant to change, this
        test is the review signal.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await conn.execute(_insert())

        await _downgrade(engine)

        async with engine.connect() as conn:
            remaining = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()
        assert TABLE not in remaining

    async def test_upgrade_downgrade_upgrade_is_clean(self):
        """Rollback then re-deploy must work — that is the point of rollback.

        A downgrade that leaves an index behind fails the next upgrade on "index
        already exists", which is a broken deploy rather than a degraded one.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)
        await _upgrade(engine)

        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
            indexes = await conn.run_sync(lambda c: {i["name"] for i in sa_inspect(c).get_indexes(TABLE)})
        await engine.dispose()

        assert TABLE in tables
        assert PR_INDEX in indexes


class TestNoBackfill:
    """The migration creates one table and invents no associations."""

    async def test_preexisting_rows_are_untouched(self):
        """A populated pre-existing table is bit-identical after upgrade()."""
        engine = await _bare_engine()

        async with engine.begin() as conn:
            await conn.execute(sa.text("CREATE TABLE preexisting (id VARCHAR(36) PRIMARY KEY, payload VARCHAR(64))"))
            await conn.execute(sa.text("INSERT INTO preexisting (id, payload) VALUES ('a', 'original-a'), ('b', 'original-b')"))
            before = (await conn.execute(sa.text("SELECT id, payload FROM preexisting ORDER BY id"))).all()

        await _upgrade(engine)

        async with engine.connect() as conn:
            after = (await conn.execute(sa.text("SELECT id, payload FROM preexisting ORDER BY id"))).all()
            cols = await conn.run_sync(lambda c: {x["name"] for x in sa_inspect(c).get_columns("preexisting")})
        await engine.dispose()

        assert after == before == [("a", "original-a"), ("b", "original-b")]
        assert cols == {"id", "payload"}, "upgrade() must not add columns to existing tables"

    async def test_upgrade_creates_no_binding_rows(self):
        """No invented associations. This is a acceptance requirement, not tidiness.

        The obvious "helpful" migration would scan each waiting story's repository
        for a merged PR mentioning its issue and adopt it. That is exactly the
        authority #5301 refuses: a mention is a discovery hint, and adopting on that
        basis would bind reviewer-artifact PRs and unrelated follow-ups, then
        complete stories on them. A row invented here would also be
        indistinguishable from one a delivering run registered, destroying the
        provenance that makes the recovery path auditable.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.connect() as conn:
            count = (await conn.execute(sa.text(f"SELECT count(*) FROM {TABLE}"))).scalar_one()
        await engine.dispose()
        assert count == 0

    def test_migration_source_contains_no_alter_update_or_insert(self):
        """Structural guard: the migration creates a table and nothing else.

        Reading the source is the only way to assert the *absence* of a destructive
        or fabricating operation against tables this test does not know about. A
        future edit that backfills or alters has to change this test, which is the
        review signal.
        """
        source = (MIGRATIONS_DIR / "051_orchestration_pr_bindings.py").read_text()
        # Strip the docstring, which legitimately discusses backfill and ALTER.
        body = source.split('"""', 2)[-1]
        lowered = body.lower()

        for forbidden in (
            "alter table",
            "op.alter_column",
            "op.add_column",
            "op.drop_column",
            "update ",
            "insert into",
            "op.bulk_insert",
            "op.execute",
        ):
            assert forbidden not in lowered, f"migration must only create the new table; found {forbidden!r}"


class TestPostgresRendering:
    """The tests run on SQLite, but dev runs on Postgres. Render for Postgres.

    Nothing else in this file would notice a Postgres-only DDL problem, because
    SQLite is more permissive — it does not even enforce integer width. Rendering the
    migration in alembic's offline (`--sql`) mode against the Postgres dialect
    exercises the compiler that actually matters, with no live database.
    """

    def _render_postgres_ddl(self) -> str:
        from sqlalchemy.dialects import postgresql

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        chunks: list[str] = []

        class _Buffer:
            def write(self, text):
                chunks.append(text)

            def flush(self):
                pass

        ctx = MigrationContext.configure(
            dialect=postgresql.dialect(),
            opts={"as_sql": True, "output_buffer": _Buffer()},
        )
        with Operations.context(ctx):
            MIG_051.upgrade()
        return "".join(chunks)

    def test_provider_integers_render_as_bigint(self):
        """`BIGINT`, not `INTEGER`, for both provider integers.

        GitHub repository and installation ids are 64-bit. An `INTEGER` column
        renders and applies without complaint, then rejects or truncates a value past
        2^31 — and SQLite would never reveal it, because SQLite ignores the declared
        integer width entirely. `pr_number` is deliberately NOT in this set: it is a
        per-repository counter, not a provider-wide id.
        """
        ddl = self._render_postgres_ddl()
        assert "provider_repository_id BIGINT" in ddl, f"must render as BIGINT on Postgres; got:\n{ddl[:2000]}"
        assert "installation_id BIGINT" in ddl
        assert "pr_number INTEGER" in ddl

    def test_timestamps_are_timezone_aware_on_postgres(self):
        """A naive timestamp makes supersession ordering ambiguous across zones.

        It also has a concrete failure mode here: reconciliation compares a
        binding's timestamps against an aware `utcnow()`, and a naive column turns
        that into a TypeError mid-sweep — which fails the whole result pass, not just
        one story.
        """
        ddl = self._render_postgres_ddl()
        assert "TIMESTAMP WITH TIME ZONE" in ddl
        assert ddl.count("TIMESTAMP WITHOUT TIME ZONE") == 0

    def test_head_sha_is_wide_enough_for_a_full_sha(self):
        """40 hex chars today, 64 for SHA-256 object formats.

        A `VARCHAR(40)` would silently truncate under Postgres' stricter length
        handling the day a repository uses SHA-256 — and a truncated head compares
        unequal to the provider's, which would hold every story on `head_moved`
        forever. That is the same invisible-stall class this story exists to remove.
        """
        ddl = self._render_postgres_ddl()
        assert "head_sha VARCHAR(64)" in ddl

    def test_one_table_and_one_unique_index_render(self):
        """Exactly one uniqueness rule; the rest are read paths.

        A second unique index would be a second way to refuse a registration, and
        refusals are what leave stories waiting.
        """
        ddl = self._render_postgres_ddl()
        assert ddl.count("CREATE TABLE") == 1
        assert ddl.count("CREATE UNIQUE INDEX") == 1
        assert PR_INDEX in ddl


class TestRevisionChain:
    def test_revision_id_and_down_revision(self):
        """Chains onto the head that was real when this landed."""
        assert MIG_051.revision == "051_orch_pr_bindings"
        assert MIG_051.down_revision == "050_orchestration_work_claims"

    def test_revision_id_fits_the_alembic_version_column(self):
        """`alembic_version.version_num` is VARCHAR(32); a longer id fails at apply.

        This is why the revision is `051_orch_pr_bindings` rather than the filename
        stem `051_orchestration_pr_bindings` (35 chars), which would exceed it.
        """
        assert len(MIG_051.revision) <= 32

    def test_migration_leaves_exactly_one_head(self):
        """Two heads is a broken deploy, and it is invisible until a pod runs
        `alembic upgrade head`.

        Asserts the *count*, not the head's name: the head advances with every
        migration that lands, and a name-pinned assertion turns every future
        migration into a spurious failure here — which trains people to edit this
        test rather than read it. What must never change is that there is exactly one
        head, and that 051 is still on the chain.
        """
        import ast

        revisions: dict[str, str | tuple[str, ...] | None] = {}
        for path in MIGRATIONS_DIR.glob("*.py"):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            found: dict[str, str | tuple[str, ...] | None] = {}
            for node in tree.body:
                if not isinstance(node, ast.AnnAssign | ast.Assign):
                    continue
                targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
                names = {t.id for t in targets if isinstance(t, ast.Name)} & {"revision", "down_revision"}
                if not names or not isinstance(node.value, ast.Constant | ast.Tuple):
                    continue
                for name in names:
                    found[name] = ast.literal_eval(node.value)
            if "revision" in found:
                revisions[found["revision"]] = found.get("down_revision")

        parents = {parent for down in revisions.values() if down is not None for parent in ((down,) if isinstance(down, str) else down)}
        heads = sorted(rev for rev in revisions if rev not in parents)

        assert len(heads) == 1, f"expected exactly one head, got {heads}"
        assert "051_orch_pr_bindings" in revisions, "051 must still be on the chain"


class TestModelMigrationParity:
    """The migration and the model are hand-written separately, so they can drift.

    The migration is what runs against dev; the model is what every test uses. When
    they disagree, tests pass and production breaks — so compare them.
    """

    @pytest.fixture
    async def migrated_columns(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            cols = await conn.run_sync(lambda c: {x["name"]: x for x in sa_inspect(c).get_columns(TABLE)})
        await engine.dispose()
        return cols

    async def test_column_names_match(self, migrated_columns):
        model_cols = {c.name for c in OrchestrationPullRequestBinding.__table__.columns}
        assert model_cols == set(migrated_columns), (
            "model/migration column drift — "
            f"only in model: {model_cols - set(migrated_columns)}, "
            f"only in migration: {set(migrated_columns) - model_cols}"
        )

    async def test_nullability_matches(self, migrated_columns):
        """A column the model calls optional and the migration calls NOT NULL fails
        only on the deployed database, where nothing tests it."""
        model = {c.name: c.nullable for c in OrchestrationPullRequestBinding.__table__.columns}
        assert model == {name: col["nullable"] for name, col in migrated_columns.items()}

    async def test_column_types_match(self, migrated_columns):
        """Rendered types, not Python types: `Integer` vs `BigInteger` is the drift
        that matters here, and both are `int` in the model annotation."""
        from sqlalchemy.dialects import sqlite

        dialect = sqlite.dialect()
        model = {c.name: str(c.type.compile(dialect=dialect)) for c in OrchestrationPullRequestBinding.__table__.columns}
        migrated = {name: str(col["type"].compile(dialect=dialect)) for name, col in migrated_columns.items()}
        assert model == migrated

    async def test_index_names_match(self, migrated_columns):
        """Model-declared indexes must exist in the migration under the same names.

        Drift here is quiet: the ORM index only exists where `create_all` ran, so a
        query is fast in tests and a sequential scan in dev.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            migrated = await conn.run_sync(lambda c: {i["name"] for i in sa_inspect(c).get_indexes(TABLE)})
        await engine.dispose()

        declared = {ix.name for ix in OrchestrationPullRequestBinding.__table__.indexes}
        # `TenantMixin` declares org_id with index=True, which SQLAlchemy names
        # implicitly rather than adding to `__table__.indexes`.
        declared.add("ix_orchestration_pr_bindings_org_id")
        assert declared <= migrated, f"declared on the model but missing from the migration: {sorted(declared - migrated)}"
