"""Migration chain integrity tests — Issue #5045 (U13), EPIC #4910.

WHAT THESE TESTS ESTABLISH, AND WHAT THEY DO NOT

Before this story the chain in `alembic/versions/` did not merely have two heads: it
could not be LOADED. Three files declared revision id `006`, three more declared `007`,
and three declared a parent of `"005"` that no file declares (the real id is
`005_add_research_proposals`). Alembic's own `ScriptDirectory` raised `KeyError: '005'`
building its revision map, which means NO alembic command worked against this chain —
not `upgrade`, not `heads`, not `current`.

That is the fault each test below pins, one fault per test, so a regression names itself
rather than surfacing as a generic "migrations broke".

These tests are OFFLINE by construction and that is a real limit on what they prove.
The required lane (`.github/workflows/superplane-domain-ci.yml`) is a deliberately
credential-free job with no database service, so there is no PostgreSQL to migrate here.
`test_full_chain_renders_as_postgresql_ddl` executes every `upgrade()` body in order
against the real PostgreSQL dialect via Alembic's offline (`--sql`) mode, which proves the
operations COMPILE for the target backend. It does NOT prove they APPLY to a live
database: offline mode never opens a connection, so nothing here observes a constraint
violation, a lock, or a failure that depends on existing rows.

The issue's live smoke check — `alembic upgrade head` against an ephemeral empty
PostgreSQL exiting 0 — remains unrun, as does R4 acceptance 3 (the data-preserving
upgrade against a real deployed database), which the issue records as deferred behind an
unresolved account/database/backup gate.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

API_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_DIR = API_ROOT / "alembic" / "versions"


@pytest.fixture(scope="module")
def script_directory() -> ScriptDirectory:
    """The chain as Alembic itself resolves it.

    Constructing this is the assertion that matters most: it is the call that raised
    `KeyError: '005'` before the repair, so every test taking this fixture depends on the
    chain being loadable at all.
    """
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


@pytest.fixture(scope="module")
def revisions(script_directory: ScriptDirectory) -> list:
    return list(script_directory.walk_revisions())


class TestChainIsSingleHeaded:
    """The story's headline requirement: a fresh install has one unambiguous target."""

    def test_chain_loads_at_all(self, script_directory: ScriptDirectory) -> None:
        """Regression for `KeyError: '005'` — the dangling parent.

        Kept as its own test even though every other test needs the chain to load,
        because "the chain cannot be parsed" and "the chain has two heads" are different
        defects with different fixes, and the first one masks the second.
        """
        assert script_directory.get_heads(), "the chain resolved no revisions at all"

    def test_exactly_one_head(self, script_directory: ScriptDirectory) -> None:
        """`alembic heads` returns exactly one head — the issue's smoke predicate."""
        heads = script_directory.get_heads()
        assert len(heads) == 1, (
            f"expected a single head so `alembic upgrade head` has one resolvable "
            f"target; found {len(heads)}: {sorted(heads)}"
        )

    def test_exactly_one_base(self, script_directory: ScriptDirectory) -> None:
        """One starting point, so the chain is a line and not two disjoint histories."""
        bases = script_directory.get_bases()
        assert len(bases) == 1, f"expected one base revision, found: {sorted(bases)}"

    def test_upgrade_head_resolves_to_a_full_ordered_path(
        self, script_directory: ScriptDirectory, revisions: list
    ) -> None:
        """`upgrade base->head` must reach EVERY revision.

        A chain can be single-headed and still orphan revisions on a side branch that
        `upgrade head` never walks; those migrations would silently never apply.
        """
        path = list(script_directory.iterate_revisions("heads", "base"))
        assert len(path) == len(revisions), (
            f"`upgrade head` walks {len(path)} revisions but the directory declares "
            f"{len(revisions)}; some revisions are not on the path to head"
        )


class TestNoDuplicateOrDanglingRevisions:
    """The two concrete faults from the issue, each pinned separately."""

    def test_revision_ids_are_unique(self, revisions: list) -> None:
        """Regression for `006`/`007` each being declared three times.

        Duplicate ids are what made a stamped revision ambiguous: given `006` in
        `alembic_version`, three different files could have produced it.
        """
        seen: dict[str, int] = {}
        for rev in revisions:
            seen[rev.revision] = seen.get(rev.revision, 0) + 1
        duplicates = {rid: n for rid, n in seen.items() if n > 1}
        assert not duplicates, f"revision ids declared by more than one file: {duplicates}"

    def test_every_declared_parent_exists(self, revisions: list) -> None:
        """Regression for three files declaring parent `"005"`, which no file declares."""
        declared = {rev.revision for rev in revisions}
        dangling = {
            rev.revision: rev.down_revision
            for rev in revisions
            if rev.down_revision is not None
            and not set(
                rev.down_revision
                if isinstance(rev.down_revision, tuple)
                else (rev.down_revision,)
            ).issubset(declared)
        }
        assert not dangling, (
            f"revisions naming a parent that no file declares: {dangling}. The real id "
            f"of the 005 revision is '005_add_research_proposals'."
        )

    def test_no_revision_id_is_a_bare_prefix(self, revisions: list) -> None:
        """Ids must be descriptive, not the bare numeric prefixes that collided.

        A new `006`/`007`-style bare id is how the duplicate-id defect would return: the
        filename prefix and the declared `revision =` value are different things, and
        only the second one collides.
        """
        bare = sorted(r.revision for r in revisions if r.revision.isdigit())
        assert not bare, (
            f"bare numeric revision ids reintroduce the collision this story repaired: "
            f"{bare}. Use the descriptive form, e.g. '006_add_cognito_sub'."
        )


class TestSchemaMatchesModels:
    """The chain must actually build the schema the application expects.

    This is the class of gap that a head-count check cannot see: before this story the
    models declared `api_keys` and `budget_alerts` and NO migration created either, so a
    fresh database reached a single head still missing two tables the app queries.
    """

    def test_model_metadata_is_complete_on_its_own(self) -> None:
        """`import app.models` ALONE must register every table the model files declare.

        Added during review of #5045, and it must run in a subprocess. `Base.metadata` is
        process-global, and `tests/conftest.py` imports `app.main`, which reaches a model
        through a router as a side effect. So in this interpreter the metadata is already
        populated by more than `app.models`, and an omission in
        `app/models/__init__.py` is invisible — which is exactly how
        `research_proposal` came to be missing from it while the parity tests below still
        passed.

        A clean interpreter that imports only `app.models` is what `alembic/env.py`
        actually does to build `target_metadata`. That matters beyond tidiness:
        `alembic revision --autogenerate` diffs the live database against
        `target_metadata`, so a table missing from it is proposed for DELETION.

        The expected table names are parsed from the model files themselves, so adding a
        model without wiring it into the package fails here.
        """
        import ast
        import subprocess
        import sys

        models_dir = API_ROOT / "app" / "models"
        expected: set[str] = set()
        for path in sorted(models_dir.glob("*.py")):
            if path.stem == "__init__":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == "__tablename__"
                        for t in node.targets
                    )
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    expected.add(node.value.value)

        probe = (
            "import app.models\n"
            "from app.database import Base\n"
            "print(' '.join(sorted(Base.metadata.tables)))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(API_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"probe failed: {result.stderr}"
        registered = set(result.stdout.split())

        missing = expected - registered
        assert not missing, (
            f"model files declare {sorted(missing)} but `import app.models` does not "
            f"register them, so alembic/env.py's target_metadata is incomplete and "
            f"`--autogenerate` would propose DROPPING them. Add the import to "
            f"app/models/__init__.py."
        )

    def test_every_model_table_is_created_by_a_migration(self) -> None:
        import app.models  # noqa: F401  (registers every model on Base.metadata)
        from app.database import Base

        created = _tables_created_by_migrations()
        expected = set(Base.metadata.tables)
        missing = expected - created
        assert not missing, (
            f"tables declared by the models but created by no migration: "
            f"{sorted(missing)}"
        )

    def test_migrations_create_no_unknown_table(self) -> None:
        """Catches a migration creating a table no model knows about (a typo or a leak)."""
        import app.models  # noqa: F401
        from app.database import Base

        created = _tables_created_by_migrations()
        unknown = created - set(Base.metadata.tables)
        assert not unknown, (
            f"migrations create tables no model declares: {sorted(unknown)}"
        )


class TestColumnsFitTheValuesTheAppWrites:
    """A column must be wide enough for what the application actually stores.

    Added during review of #5045. This is the gap the rest of this module cannot see: a
    `CREATE TABLE` can be valid DDL, compile for PostgreSQL, and still reject the first
    row the application inserts. Offline `--sql` rendering never inserts a row, and the
    API test suite runs on SQLite, which does NOT enforce VARCHAR limits — so a width that
    is too small passes every other check here and fails only against a real database.

    Reviewed at `key_prefix` because this PR is what creates `api_keys` for the first
    time, so the width it ships is the width a fresh install gets.
    """

    def test_key_prefix_column_fits_the_prefix_the_router_stores(self) -> None:
        """`api_keys.key_prefix` must hold `raw_key[:12] + "..."` (15 characters).

        At VARCHAR(12) PostgreSQL raises 22001 `StringDataRightTruncation` rather than
        truncating, so `POST /auth/token` would return 500 the first time an API key is
        created. Derived from the router and the key generator rather than hardcoded, so
        changing either one fails here instead of in production.
        """
        from app.models.api_key import ApiKey
        from app.routers.auth import _generate_api_key

        stored_prefix = _generate_api_key()[:12] + "..."
        column_width = ApiKey.__table__.c.key_prefix.type.length
        assert column_width >= len(stored_prefix), (
            f"api_keys.key_prefix is VARCHAR({column_width}) but the router stores "
            f"{len(stored_prefix)} characters ({stored_prefix!r}). PostgreSQL rejects "
            f"the insert with 22001 StringDataRightTruncation; SQLite (used by the API "
            f"tests) does not, which is why this needs its own check."
        )

    def test_the_migration_and_the_model_agree_on_key_prefix_width(self) -> None:
        """The migration's width must match the model's, or autogenerate keeps diffing.

        The model is what `alembic/env.py` compares the database against, so a migration
        that disagrees with it leaves a permanent phantom difference.
        """
        import ast

        from app.models.api_key import ApiKey

        source = (VERSIONS_DIR / "008_add_api_keys_table.py").read_text(encoding="utf-8")
        widths = {
            node.args[0].value: node.args[1].args[0].value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "Column"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[1], ast.Call)
            and getattr(node.args[1].func, "attr", "") == "String"
            and node.args[1].args
            and isinstance(node.args[1].args[0], ast.Constant)
        }
        assert widths.get("key_prefix") == ApiKey.__table__.c.key_prefix.type.length, (
            f"008 creates key_prefix as VARCHAR({widths.get('key_prefix')}) but the "
            f"model declares VARCHAR({ApiKey.__table__.c.key_prefix.type.length})"
        )


class TestAppliedRevisionsAreNotRewritten:
    """Revisions that could already be applied somewhere must stay byte-identical.

    Rewriting an applied migration invalidates a deployed database's recorded history and
    makes that installation unupgradeable — which is why the issue puts it out of scope.
    `001`-`005` have unambiguous ids and are the only revisions that could plausibly have
    been applied, so they are pinned by content hash.

    The repaired `006`/`007` files are deliberately NOT pinned here: their ids were
    unreachable by construction (duplicate id or dangling parent meant Alembic could not
    load the chain, so `upgrade` could never have applied them), and rewriting an id that
    no database can have recorded invalidates no history.
    """

    # sha256 of each file as transferred by U22 (#5326), verified against the pinned
    # upstream reference. A failure here means an applied migration was edited.
    FROZEN = {
        "001_initial.py": "e100bb0ef030712d8675c89072ffd39f3d03c25d60266d4b5061eec76c40ee40",
        "002_add_research_workspace.py": "a2d56819052be36a98c38c61b632beeb326203f86a23432a08c8152383783f4d",
        "003_add_research_findings.py": "31ff7c276f89804086fed363553637e4dde351b97bc8eaf5e19ff77f10902c45",
        "004_jsonb_state_columns.py": "c7f989fd2a16d308d3e0a1babe2b85ef9e563d4a86c8e858d1b6c783db2d1eb2",
        "005_add_research_proposals.py": "d17b45243ec71d1f688abcf838588f30b3b9eefe820fa582b3a632da0d907470",
    }

    @pytest.mark.parametrize("filename", sorted(FROZEN))
    def test_revision_file_is_byte_identical(self, filename: str) -> None:
        path = VERSIONS_DIR / filename
        assert path.is_file(), f"{filename} is missing from the chain"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == self.FROZEN[filename], (
            f"{filename} changed. Revisions 001-005 may already be applied to a "
            f"deployed database; editing one invalidates that database's revision "
            f"history and makes the installation unupgradeable."
        )

    def test_the_early_chain_keeps_its_original_order(self, revisions: list) -> None:
        """The applied prefix must still be the same line, not merely the same files."""
        by_id = {r.revision: r for r in revisions}
        expected = [
            ("001_initial", None),
            ("002_add_research_workspace", "001_initial"),
            ("003_add_research_findings", "002_add_research_workspace"),
            ("004_jsonb_state_columns", "003_add_research_findings"),
            ("005_add_research_proposals", "004_jsonb_state_columns"),
        ]
        for rev_id, parent in expected:
            assert rev_id in by_id, f"{rev_id} is no longer in the chain"
            assert by_id[rev_id].down_revision == parent, (
                f"{rev_id} should follow {parent!r} as it did when applied, but now "
                f"follows {by_id[rev_id].down_revision!r}"
            )


class TestChainCompilesForPostgres:
    """Every migration body must be valid DDL for the real target backend.

    Scope limit, restated because it is easy to over-read a green result: this renders
    SQL, it does not apply it. See the module docstring.
    """

    def test_alembic_upgrade_head_renders_the_whole_chain_offline(
        self, tmp_path: Path
    ) -> None:
        """Drive `alembic upgrade head --sql` through Alembic's own command API.

        This is deliberately NOT the hand-rolled loop below. It goes through the same
        entry point the deploy path uses, so it exercises Alembic's real head resolution
        and revision ordering rather than a reimplementation of it that could agree with a
        broken chain. It is the closest offline analogue of the issue's live smoke check.

        Still offline: `--sql` mode never opens a connection. It proves the chain resolves
        a single target and every migration compiles for PostgreSQL, NOT that applying it
        to a live database succeeds.
        """
        import contextlib

        from alembic import command

        config = Config(str(API_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(API_ROOT / "alembic"))
        # A URL that is never connected to — offline mode only needs it to pick a dialect.
        config.set_main_option(
            "sqlalchemy.url", "postgresql+psycopg2://user:pw@localhost/db"
        )

        # `--sql` mode writes the migration script to stdout, so capture it there.
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            # No revision argument beyond "head": if the chain had more than one head this
            # raises rather than silently picking one.
            command.upgrade(config, "head", sql=True)

        sql = buffer.getvalue()
        assert "CREATE TABLE organizations" in sql, "the chain rendered no base schema"

        # Every revision must stamp itself into alembic_version, so counting the stamps is
        # a direct check that `upgrade head` walked the entire chain rather than a prefix.
        stamps = sql.count("alembic_version SET version_num") + sql.count(
            "INSERT INTO alembic_version"
        )
        revision_count = len(list(VERSIONS_DIR.glob("*.py")))
        assert stamps == revision_count, (
            f"`upgrade head` stamped {stamps} revisions but the chain declares "
            f"{revision_count}; some revisions were not applied by the walk"
        )

    def test_full_chain_renders_as_postgresql_ddl(
        self, script_directory: ScriptDirectory
    ) -> None:
        import io

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        buffer = io.StringIO()
        engine = sa.create_mock_engine("postgresql://", lambda sql, *a, **kw: None)
        context = MigrationContext.configure(
            dialect=engine.dialect,
            opts={
                "as_sql": True,
                "output_buffer": buffer,
                "transactional_ddl": False,
            },
        )
        # Alembic's `op` proxy is module-global; bind it for the duration so each
        # migration's `upgrade()` emits into this context.
        with Operations.context(context):
            for revision in reversed(list(script_directory.walk_revisions())):
                try:
                    revision.module.upgrade()
                except Exception as exc:  # pragma: no cover - failure path
                    pytest.fail(
                        f"revision {revision.revision} failed to render as PostgreSQL "
                        f"DDL: {type(exc).__name__}: {exc}"
                    )

        rendered = buffer.getvalue().lower()
        # Sanity-check that rendering actually happened rather than silently no-opping,
        # using the two tables this story adds.
        assert "create table" in rendered, "no DDL was rendered for the chain"
        for table in ("api_keys", "budget_alerts"):
            assert table in rendered, (
                f"{table} was never rendered, so its migration did not run in this pass"
            )


def _tables_created_by_migrations() -> set[str]:
    """Table names passed to `op.create_table(...)` across the chain, read statically.

    Parsed with `ast` rather than by executing the migrations, so this stays independent
    of the DDL-rendering test above: if executing the chain regressed, this check should
    still be able to report which tables are missing.

    Only `upgrade()` is inspected — a `create_table` inside `downgrade()` is a rollback
    step, not part of the schema a fresh install builds.
    """
    import ast

    created: set[str] = set()
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        upgrades = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
        ]
        for upgrade_fn in upgrades:
            for node in ast.walk(upgrade_fn):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else ""
                if name != "create_table":
                    continue
                try:
                    table = ast.literal_eval(node.args[0])
                except (ValueError, SyntaxError):
                    continue
                if isinstance(table, str):
                    created.add(table)
    return created
