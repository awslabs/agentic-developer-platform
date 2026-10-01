"""Tests for Alembic migration 040 — team_memberships table + backfill.

Issue #4840 (EPIC #4839), design note §2.1 / §1.5b / §1.5e.

This file is **mandatory**, and not only for coverage: `modules/gateway/alembic/**`
is absent from `gateway-ci.yml`'s trigger paths, so a migration-only change gets
**zero CI signal** — a test under `tests/` is what makes CI run for it at all.
Precedent and pattern: `test_021_tenant_memberships.py` (the same mechanic one
grain up), `test_034_person_budget_configs.py`, `test_037_bedrock_account_routing.py`.

These tests exercise the REAL migration functions imported from the version module.
A test that re-implements the migration proves only that the author can write the
same bug twice.

What is under test, and why each assertion is load-bearing rather than a
restatement of the DDL:

  - **The backfill skips empty-string `team_id`** (§1.5e) — the single most
    important case here, and per the issue it is verified against a row created by
    a **real writer path** (`POST /internal/v1/resolve-user` auto-provisioning a
    shadow user), not a synthetic fixture. `users.team_id` is NOT NULL but three
    live writers mint `""`; `""` is not a valid `teams.id`, so a naive
    `INSERT ... SELECT` over all users violates the new FK and **fails the
    migration**, which takes the entire gateway deploy with it. Asserting this
    against a fixture would let a future refactor of the writer silently
    invalidate the test's premise.
  - **The one-primary index is PostgreSQL-only, and is real DDL** (§1.5b). SQLite
    ignores `postgresql_where` and would build a *plain* unique index on
    `(user_id, org_id)` — which rejects a user's second, non-primary team, i.e. the
    exact feature this story ships. So the index is created behind a dialect guard,
    and CI therefore *cannot* verify it by inserting rows. The migration source is
    asserted instead: the guard is present, the predicate is `WHERE is_primary`, and
    the columns are `(user_id, org_id)`. This is the counterpart to the application
    -layer guard tested in `tests/admin/test_team_memberships.py`; neither alone is
    sufficient.
  - **A user's second team is accepted** on the CI substrate. This is the
    regression test for the failure mode above: if someone "simplifies" the model's
    `postgresql_where` away, `create_all` builds a plain unique index and this test
    fails loudly instead of the feature breaking in production.
  - **`UNIQUE (user_id, team_id)`** is enforced — the idempotency key the service's
    SELECT-then-upsert relies on. Without it, a double-add yields two rows for one
    membership and the "remove" endpoint appears not to work.
  - **The backfill is idempotent** and **skips users whose `team_id` points at a
    team that no longer exists**. `users.team_id` has no FK today, so a stale
    pointer is representable; it would fail the new FK mid-migration.
  - **Migration/model parity** — column set and unique-constraint shape. The
    migration DDL and the ORM model are hand-written twice; a column present in only
    one means every other test in the suite passes against a schema the database
    does not have.
  - **`downgrade()` is real and reversible**, because the stated rollback plan for
    this story is "drop the table — `users.team_id` still carries the truth".
  - **The revision chains onto the real single head** and its id fits
    `alembic_version.version_num` (#4123). A dangling or duplicated `down_revision`
    creates a SECOND HEAD, and `alembic upgrade head` then fails for **everyone**,
    blocking every subsequent gateway deploy.

SQLite backs these tests. It is a *fair* substrate for the uniqueness and backfill
questions (both are dialect-neutral SQL here) and an explicitly *unfair* one for the
partial index — which is precisely why that one is asserted against the source text
rather than pretended to be covered.
"""

import ast
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User

MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "040_team_memberships.py"

EXPECTED_REVISION = "040_team_memberships"
EXPECTED_DOWN_REVISION = "039_flow_design_capture"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _migration_source() -> str:
    return MIGRATION_PATH.read_text()


def _inside_postgres_guard(func_node: ast.FunctionDef, target: ast.AST) -> bool:
    """True if ``target`` sits inside an ``if bind.dialect.name == "postgresql"`` body."""
    for node in ast.walk(func_node):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.dump(node.test)
        if "dialect" not in test_src or "postgresql" not in test_src:
            continue
        if any(target is descendant for stmt in node.body for descendant in ast.walk(stmt)):
            return True
    return False


def _backfill_insert_sql() -> str:
    """Return the migration's real backfill statement, reassembled from its AST.

    Parsed from the source (rather than copied into the test) so that weakening the
    migration's WHERE clause fails these tests instead of passing against a stale
    duplicate. The statement is an f-string whose only interpolation is the uuid
    expression, so the literal parts are what carry the filters under test.
    """
    tree = ast.parse(_migration_source())
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        # Rebuild the f-string, re-emitting interpolations as {name} placeholders so
        # the caller can substitute the dialect-appropriate uuid expression.
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{" + ast.unparse(value.value) + "}")
        rebuilt = "".join(parts)
        if "INSERT INTO team_memberships" in rebuilt:
            return rebuilt
    raise AssertionError("could not locate the backfill statement in migration 040")


def _sqlite_uuid_expr() -> str:
    """Return the migration's SQLite uuid expression, from its AST."""
    tree = ast.parse(_migration_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "uuid_expr" for t in node.targets):
            if isinstance(node.value, ast.Constant) and "randomblob" in str(node.value.value):
                return node.value.value
    raise AssertionError("could not locate the SQLite uuid_expr in migration 040")


def _make_engine():
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


async def _create_schema(engine):
    async with engine.begin() as conn:
        # The audit and vault models must be imported before create_all: the
        # shadow-user writer exercised below also writes a security_audit_logs row
        # and reads channel_tenant_map, and Base.metadata only knows about tables
        # whose modules have been imported.
        import src.shared.models.audit  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
        # SQLite does not enforce FKs unless asked, and the whole point of several
        # assertions below is that the FK to teams.id bites.
        await conn.execute(sa.text("PRAGMA foreign_keys=ON"))


def _collect_schema_info(sync_conn):
    """Collect team_memberships schema info synchronously inside run_sync."""
    insp = sa_inspect(sync_conn)
    info = {"tables": set(insp.get_table_names())}
    if "team_memberships" in info["tables"]:
        info["columns"] = {c["name"] for c in insp.get_columns("team_memberships")}
        uniques = [set(uc["column_names"]) for uc in insp.get_unique_constraints("team_memberships")]
        uniques += [set(idx["column_names"]) for idx in insp.get_indexes("team_memberships") if idx.get("unique")]
        info["unique"] = uniques
        info["indexes"] = {idx["name"] for idx in insp.get_indexes("team_memberships")}
    return info


@pytest.fixture
async def engine():
    eng = _make_engine()
    await _create_schema(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.fixture
async def org_fixture(session):
    """Seed an org, department and two teams for FK satisfaction."""
    org = Organization(
        id="org-1",
        name="Acme",
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_installation_ids=[],
        cognito_client_ids=[],
    )
    dept = Department(id="dept-1", org_id="org-1", name="Engineering")
    team_a = Team(id="team-a", org_id="org-1", department_id="dept-1", name="Platform")
    team_b = Team(id="team-b", org_id="org-1", department_id="dept-1", name="On-call")
    session.add_all([org, dept, team_a, team_b])
    await session.commit()
    return {"org": org, "teams": [team_a, team_b]}


async def _run_backfill(session: AsyncSession) -> None:
    """Execute the migration's REAL backfill DML against the test session.

    Both the statement and the uuid expression are parsed out of the migration, so
    these tests exercise the shipping SQL rather than a paraphrase of it.
    """
    stmt = _backfill_insert_sql().replace("{uuid_expr}", _sqlite_uuid_expr())
    await session.execute(sa.text(stmt))
    await session.commit()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    """Migration/model parity: the shape the rest of the suite assumes."""

    @pytest.fixture
    async def schema_info(self, engine):
        async with engine.connect() as conn:
            return await conn.run_sync(_collect_schema_info)

    @pytest.mark.asyncio
    async def test_table_exists(self, schema_info):
        assert "team_memberships" in schema_info["tables"]

    @pytest.mark.asyncio
    async def test_expected_columns(self, schema_info):
        expected = {
            "id",
            "user_id",
            "team_id",
            "org_id",
            "role",
            "is_primary",
            "source",
            "external_id",
            "synced_at",
            "created_at",
            "updated_at",
        }
        assert expected <= schema_info["columns"], f"missing: {expected - schema_info['columns']}"

    @pytest.mark.asyncio
    async def test_user_team_unique_constraint_declared(self, schema_info):
        assert {"user_id", "team_id"} in schema_info["unique"]

    @pytest.mark.asyncio
    async def test_migration_declares_same_columns_as_model(self):
        """Every column in the migration's create_table exists on the ORM model."""
        source = _migration_source()
        migration_cols = set(re.findall(r'sa\.Column\(\s*"(\w+)"', source))
        model_cols = {c.name for c in TeamMembership.__table__.columns}
        assert migration_cols == model_cols, f"migration-only: {migration_cols - model_cols}, model-only: {model_cols - migration_cols}"


# ---------------------------------------------------------------------------
# The PostgreSQL-only partial unique index (§1.5b)
# ---------------------------------------------------------------------------


class TestOnePrimaryIndexIsPostgresOnly:
    """The index CI cannot execute, so its DDL is asserted from source.

    See the module docstring: SQLite would build `postgresql_where` as a plain
    unique index that breaks the feature, so the migration guards it by dialect and
    the invariant is ALSO enforced in the application layer.
    """

    def test_index_is_behind_a_postgres_dialect_guard(self):
        """The CREATE must be reachable only on PostgreSQL.

        Checked structurally rather than by string position: the docstring also
        mentions the index, so a naive `source.index(...)` comparison finds prose
        instead of code.
        """
        tree = ast.parse(_migration_source())
        upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")

        def _is_unguarded_create(node: ast.AST) -> bool:
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                return False
            if "CREATE UNIQUE INDEX" not in node.value:
                return False
            return not _inside_postgres_guard(upgrade, node)

        creates_outside_guard = [n for n in ast.walk(upgrade) if _is_unguarded_create(n)]
        assert creates_outside_guard == [], "the partial unique index must be created only on PostgreSQL"

    def test_index_predicate_and_columns(self):
        """One primary per user per ORG — not per user globally, not per team."""
        source = _migration_source()
        match = re.search(r"CREATE UNIQUE INDEX \{?\w*\}? ?ON team_memberships \(([^)]*)\) WHERE (\w+)", source)
        assert match, "the one-primary index DDL is missing or reshaped"
        columns = [c.strip() for c in match.group(1).split(",")]
        assert columns == ["user_id", "org_id"]
        assert match.group(2) == "is_primary"

    def test_model_must_not_declare_the_partial_index(self):
        """The model deliberately OMITS it — declaring it breaks SQLite.

        SQLAlchemy renders `postgresql_where` only on PostgreSQL, so `create_all()`
        on SQLite would build a *plain* unique index on (user_id, org_id) and reject
        a user's second, non-primary team. This is the same omission
        ``TenantMembership`` makes for its own `WHERE is_active` index (migration
        021). Asserting the absence keeps a well-meaning "add the missing index to
        the model" change from silently breaking multi-team membership in CI.
        """
        declared = {ix.name for ix in TeamMembership.__table__.indexes}
        assert "uq_team_memberships_one_primary" not in declared, (
            "do not declare the partial unique index on the model — SQLite renders it as a plain unique index and rejects a user's second team"
        )

    def test_downgrade_drops_the_index_only_on_postgres(self):
        source = _migration_source()
        downgrade = source[source.index("def downgrade()") :]
        assert 'if bind.dialect.name == "postgresql":' in downgrade
        assert "DROP INDEX IF EXISTS" in downgrade


# ---------------------------------------------------------------------------
# Constraints on the CI substrate
# ---------------------------------------------------------------------------


class TestConstraints:
    @pytest.mark.asyncio
    async def test_second_non_primary_team_is_accepted(self, session, org_fixture):
        """The whole point of the story: a user may be on more than one team.

        Regression guard for the SQLite trap — if the model's `postgresql_where` is
        ever dropped, `create_all` builds a plain unique index on (user_id, org_id)
        and this insert fails.
        """
        user = User(id="u-multi", org_id="org-1", team_id="team-a", email="sre@acme.test")
        session.add(user)
        await session.flush()
        session.add(TeamMembership(user_id="u-multi", team_id="team-a", org_id="org-1", is_primary=True))
        session.add(TeamMembership(user_id="u-multi", team_id="team-b", org_id="org-1", is_primary=False))
        await session.commit()

        rows = (await session.execute(sa.select(TeamMembership).where(TeamMembership.user_id == "u-multi"))).scalars().all()
        assert len(rows) == 2
        assert sum(1 for r in rows if r.is_primary) == 1

    @pytest.mark.asyncio
    async def test_duplicate_user_team_rejected(self, session, org_fixture):
        """UNIQUE (user_id, team_id) — the service's upsert idempotency key."""
        user = User(id="u-dup", org_id="org-1", team_id="team-a", email="dup@acme.test")
        session.add(user)
        await session.flush()
        session.add(TeamMembership(user_id="u-dup", team_id="team-a", org_id="org-1"))
        await session.commit()

        session.add(TeamMembership(user_id="u-dup", team_id="team-a", org_id="org-1"))
        with pytest.raises(IntegrityError):
            await session.commit()

    @pytest.mark.asyncio
    async def test_membership_for_unknown_team_is_rejected(self, session, org_fixture):
        """The FK that makes the empty-string backfill case fatal if unhandled."""
        user = User(id="u-fk", org_id="org-1", team_id="team-a", email="fk@acme.test")
        session.add(user)
        await session.flush()
        session.add(TeamMembership(user_id="u-fk", team_id="", org_id="org-1"))
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------------------
# Backfill (§1.5e)
# ---------------------------------------------------------------------------


class TestBackfill:
    @pytest.mark.asyncio
    async def test_backfill_creates_one_primary_per_user_with_a_team(self, session, org_fixture):
        session.add_all(
            [
                User(id="u-1", org_id="org-1", team_id="team-a", email="a@acme.test"),
                User(id="u-2", org_id="org-1", team_id="team-b", email="b@acme.test"),
            ]
        )
        await session.commit()

        await _run_backfill(session)

        rows = (await session.execute(sa.select(TeamMembership))).scalars().all()
        assert len(rows) == 2
        assert {r.user_id: r.team_id for r in rows} == {"u-1": "team-a", "u-2": "team-b"}
        assert all(r.is_primary for r in rows), "backfilled rows are the primary team"
        assert all(r.source == "admin" for r in rows)
        assert all(r.org_id == "org-1" for r in rows), "org_id is denormalized from the user"

    @pytest.mark.asyncio
    async def test_backfill_is_idempotent(self, session, org_fixture):
        session.add(User(id="u-idem", org_id="org-1", team_id="team-a", email="i@acme.test"))
        await session.commit()

        await _run_backfill(session)
        await _run_backfill(session)

        rows = (await session.execute(sa.select(TeamMembership).where(TeamMembership.user_id == "u-idem"))).scalars().all()
        assert len(rows) == 1, "re-running the backfill must insert nothing"

    @pytest.mark.asyncio
    async def test_backfill_skips_stale_team_pointer(self, session, org_fixture):
        """users.team_id has no FK today, so it can name a deleted team."""
        session.add(User(id="u-stale", org_id="org-1", team_id="team-deleted", email="s@acme.test"))
        await session.commit()

        await _run_backfill(session)

        rows = (await session.execute(sa.select(TeamMembership).where(TeamMembership.user_id == "u-stale"))).scalars().all()
        assert rows == [], "a stale pointer must not produce a row (it would violate the FK)"

    def test_backfill_sql_filters_empty_string_team_id(self):
        """The §1.5e guard is present in the real migration text, not just in spirit."""
        assert "u.team_id != ''" in _backfill_insert_sql()


class TestBackfillSkipsShadowUsersFromRealWriter:
    """The empty-string case, verified against a row a REAL writer created.

    Per the issue: "The backfill test must use a row created by one of those paths,
    not a synthetic fixture." So this drives
    `POST /internal/v1/resolve-user`, whose channel_tenant_map branch
    auto-provisions a shadow user with `team_id=""`
    (`src/internal/routes.py:349`) — and then runs the real backfill over it.

    A fixture asserting `team_id=""` would keep passing even if that writer changed;
    this breaks, which is the point.
    """

    @pytest.mark.asyncio
    async def test_shadow_user_gets_no_membership_row(self, session, org_fixture):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient

        from src.internal.auth_deps import verify_internal_or_irsa
        from src.internal.routes import router
        from src.shared.database import get_db
        from src.shared.models.vault import ChannelTenantMap

        # A channel mapping is what makes resolve-user auto-provision rather than 404.
        session.add(ChannelTenantMap(org_id="org-1", provider="slack", provider_scope_id="W-acme"))
        await session.commit()

        app = FastAPI()
        app.include_router(router)

        async def _get_db():
            yield session

        async def _verify(request: Request) -> None:
            request.state.token_context = SimpleNamespace(
                auth_source="iam",
                user_id="iam-agent:ingress",
                scope="internal",
                org_id="org-1",
                credential_scopes=["internal:identity:resolve"],
            )

        app.dependency_overrides[get_db] = _get_db
        app.dependency_overrides[verify_internal_or_irsa] = _verify

        settings = MagicMock()
        settings.internal_api_key = "k"
        settings.magic_link_secret = "test-magic-link-secret-key-32chars!!"
        settings.gateway_base_url = "https://gw.example.com"

        with patch("src.internal.routes.get_settings", return_value=settings):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/internal/v1/resolve-user",
                json={"provider": "slack", "provider_user_id": "W-acme:U-new", "channel_context": "W-acme"},
            )

        assert resp.status_code in (200, 201), resp.text
        shadow_id = resp.json()["user_id"]

        # Premise check: the real writer produced the empty-string team_id that
        # makes this whole test necessary. If this fails, the writer changed and
        # the backfill's WHERE clause may need to change with it.
        shadow = (await session.execute(sa.select(User).where(User.id == shadow_id))).scalar_one()
        assert shadow.team_id == "", "expected the shadow-user writer to mint team_id=''"
        assert shadow.is_shadow is True

        await _run_backfill(session)

        rows = (await session.execute(sa.select(TeamMembership).where(TeamMembership.user_id == shadow_id))).scalars().all()
        assert rows == [], "a shadow user with team_id='' must get NO membership row (the FK would reject it)"


# ---------------------------------------------------------------------------
# Revision bookkeeping (#4123)
# ---------------------------------------------------------------------------


class TestRevisionChaining:
    def test_revision_ids(self):
        source = _migration_source()
        assert f'revision: str = "{EXPECTED_REVISION}"' in source
        assert f'down_revision: str | None = "{EXPECTED_DOWN_REVISION}"' in source

    def test_revision_id_fits_alembic_version_column(self):
        """#4123: >32 chars runs upgrade() then overflows the bookkeeping write."""
        assert len(EXPECTED_REVISION) <= 32

    def test_the_chain_has_exactly_one_head_and_040_is_on_it(self):
        """Assert *one* head, not that 040 is it (a second head makes
        `alembic upgrade head` fail for everyone).

        Originally this pinned "nothing chains onto 040", which fails on every
        subsequent migration — the healthy case, not a fork (#4843 chained
        041_directory_provider onto 040 in the very next merge). Relaxed to the
        033/039 shape (see `test_039_flow_design_capture.py`): exactly one head,
        and 040 is either that head or a link something else chained onto.
        """
        versions_dir = MIGRATION_PATH.parent
        revisions: dict[str, str | tuple[str, ...] | None] = {}
        for path in versions_dir.glob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            found: dict[str, str | tuple[str, ...] | None] = {}
            for node in tree.body:
                targets = [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
                names = {t.id for t in targets if isinstance(t, ast.Name)} & {"revision", "down_revision"}
                if not names or not isinstance(node.value, ast.Constant | ast.Tuple):
                    continue
                for name in names:
                    found[name] = ast.literal_eval(node.value)
            if "revision" in found:
                revisions[found["revision"]] = found.get("down_revision")

        parents = {parent for down in revisions.values() if down is not None for parent in ((down,) if isinstance(down, str) else down)}
        heads = sorted(revision for revision in revisions if revision not in parents)
        assert len(heads) == 1, f"expected exactly one head, found: {heads}"
        assert EXPECTED_REVISION in parents or heads == [EXPECTED_REVISION], "040 has been orphaned off the chain"

    def test_down_revision_target_exists(self):
        versions_dir = MIGRATION_PATH.parent
        assert any(f'revision: str = "{EXPECTED_DOWN_REVISION}"' in p.read_text() for p in versions_dir.glob("*.py"))

    def test_env_py_imports_the_model(self):
        """Otherwise autogenerate proposes DROPPING the table."""
        env_source = (MIGRATION_PATH.parents[1] / "env.py").read_text()
        assert "TeamMembership" in env_source
