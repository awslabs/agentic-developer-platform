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

import functools
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
        assert not duplicates, (
            f"revision ids declared by more than one file: {duplicates}"
        )

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

        source = (VERSIONS_DIR / "008_add_api_keys_table.py").read_text(
            encoding="utf-8"
        )
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

        # A real PostgreSQL run previously failed when a descriptive revision ID
        # exceeded Alembic's default VARCHAR(32), although every stamp compiled.
        import re

        version_table = re.search(
            r"CREATE TABLE alembic_version \(.*?version_num VARCHAR\((\d+)\)", sql, re.S
        )
        assert version_table is not None
        longest_revision = max(
            len(revision.revision)
            for revision in ScriptDirectory.from_config(config).walk_revisions()
        )
        assert int(version_table.group(1)) >= longest_revision
        assert (
            "ALTER TABLE IF EXISTS alembic_version ALTER COLUMN version_num TYPE VARCHAR"
            in sql
        )

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
    """Table names in create_table calls or referenced literal SQL, read statically.

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
        constants = {
            target.id: node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        upgrades = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
        ]
        for upgrade_fn in upgrades:
            # Some journals execute a literal DDL batch one statement at a time
            # for asyncpg compatibility. Inspect only constants referenced by
            # upgrade(), so an unused string or downgrade-only table is excluded.
            import re

            referenced = {node.id for node in ast.walk(upgrade_fn) if isinstance(node, ast.Name)}
            for constant in referenced & constants.keys():
                value = constants[constant]
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    created.update(re.findall(r"\bCREATE\s+TABLE\s+([a-z_][a-z0-9_]*)\s*\(", value.value, flags=re.IGNORECASE))
            for node in ast.walk(upgrade_fn):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else ""
                if name not in {"create_table", "execute"}:
                    continue
                try:
                    argument = node.args[0]
                    if isinstance(argument, ast.Name):
                        argument = constants.get(argument.id, argument)
                    table = ast.literal_eval(argument)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(table, str):
                    if name == "create_table":
                        created.add(table)
                    else:
                        # Revisions 040/041 execute each prepared DDL statement
                        # directly. Count only literal SQL passed by upgrade(),
                        # not unrelated strings or rollback-only definitions.
                        created.update(
                            re.findall(
                                r"\bCREATE\s+TABLE\s+([a-z_][a-z0-9_]*)\s*\(",
                                table,
                                flags=re.IGNORECASE,
                            )
                        )
    return created


class TestCredentialReferenceMigrationRefusesToGuess:
    """Revision 011 (issue #5046, U13b) — the state machine around the cutover.

    WHY THIS MIGRATION IS ALLOWED TO REFUSE, since a migration that raises normally
    indicates a bug. Revision 011 replaces a copied secret ARN with an ADP credential ID:
    an opaque handle only the ADP vault can resolve. Those two are not translations of each
    other. An ARN is an address in Secrets Manager; an ADP credential ID is a vault-owned
    reference. Nothing inside a schema migration can turn the first into the second, and
    Superplane is deliberately not given broad read access to ADP secrets to try.

    The requirement is that any mapping must *establish, not assume*, that each mapped
    credential is ADP-owned and ADP-readable under the relevant account and KMS
    permissions, and that rotation and revocation work through the new reference. A
    migration cannot check any of that. So it classifies instead:

      supported state -> no credential rows and no account secret references; apply.
      unknown state   -> any credential row or account secret ARN remains; REFUSE.

    Fabricating a reference in the unknown state is the "migration assumes ADP ownership"
    row of the issue's risk table: records would point at credentials nobody confirmed ADP
    can read, and the failure would be silent. Refusing leaves the database untouched and
    still upgradeable once the audited vault-owned migration (R7 acc. 6-7, deferred to U7)
    has produced verified references.

    These tests run against SQLite, which is what the credential-free required lane can
    offer. That is a real limit: SQLite does not enforce VARCHAR widths and its ALTER
    TABLE support differs from PostgreSQL's. `TestChainCompilesForPostgres` above covers
    PostgreSQL *rendering* of the same revision, and the live `alembic upgrade head`
    against a real database remains the deferred criterion.
    """

    REVISION_FILE = "012_adp_credential_reference.py"

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def _revision_module():
        """Load the revision through Alembic, so the test uses the real chain entry.

        Cached deliberately. Each `ScriptDirectory.from_config` re-imports the revision
        file as a fresh module object, so a second load defines a *different*
        `UnverifiedCredentialReferencesError` class. `pytest.raises` compares by identity,
        so an uncached helper would raise the refusal correctly and still fail the test —
        the class the test holds would not be the class the migration raised.
        """
        config = Config(str(API_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(API_ROOT / "alembic"))
        script = ScriptDirectory.from_config(config)
        return script.get_revision("012_adp_credential_reference").module

    @staticmethod
    def _apply(connection, direction: str = "upgrade") -> None:
        """Run 012's upgrade() or downgrade() against a live SQLite connection.

        Binds Alembic's module-global `op` proxy to a real (online) MigrationContext, so
        `op.get_context().as_sql` is False and the row-state check actually runs — the
        thing the offline rendering test above cannot exercise.
        """
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        module = TestCredentialReferenceMigrationRefusesToGuess._revision_module()
        context = MigrationContext.configure(connection=connection)
        with Operations.context(context):
            getattr(module, direction)()

    @staticmethod
    def _minimal_schema(connection) -> None:
        """The two tables 011 alters, in their pre-011 shape.

        Hand-built rather than produced by running the whole chain: 001-010 include
        PostgreSQL-specific types (UUID, JSONB) that do not apply cleanly to SQLite, and
        this test is about 011's row-state decision, not the earlier chain's portability
        (covered by the tests above).
        """
        connection.exec_driver_sql(
            "CREATE TABLE credential_registry ("
            " id TEXT PRIMARY KEY,"
            " provider TEXT,"
            " friendly_name TEXT,"
            " secret_arn VARCHAR(512) NOT NULL,"
            " kms_key_id VARCHAR(512),"
            " status TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE cloud_accounts ("
            " id TEXT PRIMARY KEY,"
            " account_identifier TEXT,"
            " cross_account_role_arn VARCHAR(512),"
            " irsa_role_arns_json TEXT,"
            " secret_arns_json TEXT)"
        )

    @staticmethod
    def _columns(connection, table: str) -> set:
        import sqlalchemy as sa

        return {c["name"] for c in sa.inspect(connection).get_columns(table)}

    @pytest.fixture()
    def connection(self):
        import sqlalchemy as sa

        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            self._minimal_schema(conn)
            yield conn

    # --- supported state ---

    def test_supported_state_applies_the_cutover(self, connection) -> None:
        """No rows reference secret material, so the schema change goes through."""
        self._apply(connection)

        credential_columns = self._columns(connection, "credential_registry")
        assert "adp_credential_id" in credential_columns
        assert "secret_arn" not in credential_columns, (
            "the copied secret address must be gone after a supported-state upgrade"
        )
        assert "kms_key_id" not in credential_columns, (
            "the decryption key for a secret this record no longer resolves must be gone"
        )

        account_columns = self._columns(connection, "cloud_accounts")
        assert "adp_credential_ids_json" in account_columns
        assert "secret_arns_json" not in account_columns

    def test_supported_state_keeps_iam_role_arn_columns(self, connection) -> None:
        """Role ARNs are identities, not secret material, and must survive.

        Guards against an over-broad implementation that strips every column with "arn" in
        its name: that would break cross-account role assumption while protecting nothing,
        because a role ARN names who may act and carries no secret value.
        """
        self._apply(connection)

        account_columns = self._columns(connection, "cloud_accounts")
        assert "cross_account_role_arn" in account_columns
        assert "irsa_role_arns_json" in account_columns

    def test_an_empty_arn_list_is_not_treated_as_a_reference(self, connection) -> None:
        """`'[]'` is an empty list — it addresses nothing, so it must not block."""
        connection.exec_driver_sql(
            "INSERT INTO cloud_accounts (id, account_identifier, secret_arns_json) "
            "VALUES ('a1', '123456789012', '[]')"
        )
        self._apply(connection)
        assert "adp_credential_ids_json" in self._columns(connection, "cloud_accounts")

    def test_a_credential_row_with_empty_arn_is_refused_unchanged(
        self, connection
    ) -> None:
        """An empty legacy ARN is not evidence of a verified ADP credential ID.

        Before 012 there is no column that could hold that ID. Refusing before DDL keeps
        the row available for the audited vault-owned migration instead of silently
        replacing its missing value with another unusable empty value.
        """
        module = self._revision_module()
        connection.exec_driver_sql(
            "INSERT INTO credential_registry (id, provider, secret_arn, status) "
            "VALUES ('c1', 'nebius', '', 'Active')"
        )
        before = self._columns(connection, "credential_registry")

        with pytest.raises(module.UnverifiedCredentialReferencesError) as excinfo:
            self._apply(connection)

        assert "credential_registry: 1 row(s)" in str(excinfo.value)
        assert self._columns(connection, "credential_registry") == before
        preserved = connection.exec_driver_sql(
            "SELECT secret_arn FROM credential_registry WHERE id = 'c1'"
        ).scalar()
        assert preserved == ""

    # --- unknown state: refuse ---

    def test_a_credential_row_holding_a_secret_arn_is_refused(self, connection) -> None:
        module = self._revision_module()
        connection.exec_driver_sql(
            "INSERT INTO credential_registry (id, provider, secret_arn, status) VALUES "
            "('c1', 'nebius', "
            "'arn:aws:secretsmanager:us-east-1:123456789012:secret:k-AbCdEf', 'Active')"
        )

        with pytest.raises(module.UnverifiedCredentialReferencesError) as excinfo:
            self._apply(connection)

        message = str(excinfo.value)
        assert "credential_registry: 1 row(s)" in message
        assert "audited" in message, (
            "the refusal must name the audited vault-owned migration that has to run first"
        )

    def test_an_account_row_holding_secret_arns_is_refused(self, connection) -> None:
        module = self._revision_module()
        connection.exec_driver_sql(
            "INSERT INTO cloud_accounts (id, account_identifier, secret_arns_json) VALUES "
            "('a1', '123456789012', "
            "'[\"arn:aws:secretsmanager:us-east-1:123456789012:secret:k-AbCdEf\"]')"
        )

        with pytest.raises(module.UnverifiedCredentialReferencesError) as excinfo:
            self._apply(connection)
        assert "cloud_accounts.secret_arns_json: 1 row(s)" in str(excinfo.value)

    def test_a_refusal_changes_nothing(self, connection) -> None:
        """The decisive property: a refused database is left exactly as it was.

        A migration that raised halfway would leave a half-migrated schema that is neither
        the old shape nor the new one, and the operator's rollback would have nothing
        coherent to return to. The state check therefore runs BEFORE any DDL.
        """
        module = self._revision_module()
        connection.exec_driver_sql(
            "INSERT INTO credential_registry (id, provider, secret_arn, kms_key_id, status)"
            " VALUES ('c1', 'nebius', "
            "'arn:aws:secretsmanager:us-east-1:123456789012:secret:k-AbCdEf', "
            "'arn:aws:kms:us-east-1:123456789012:key/abcd', 'Active')"
        )
        before = self._columns(connection, "credential_registry")

        with pytest.raises(module.UnverifiedCredentialReferencesError):
            self._apply(connection)

        assert self._columns(connection, "credential_registry") == before, (
            "a refused migration must not have altered the schema"
        )
        preserved = connection.exec_driver_sql(
            "SELECT secret_arn FROM credential_registry WHERE id = 'c1'"
        ).scalar()
        assert preserved.startswith("arn:aws:secretsmanager:"), (
            "the row must be left untouched so the audited migration can still read it"
        )

    def test_the_refusal_counts_every_offending_row(self, connection) -> None:
        """The operator needs the real scope of the work, not just "something blocked"."""
        module = self._revision_module()
        for i in range(3):
            connection.exec_driver_sql(
                "INSERT INTO credential_registry (id, provider, secret_arn, status) VALUES"
                f" ('c{i}', 'nebius', "
                f"'arn:aws:secretsmanager:us-east-1:123456789012:secret:k{i}', 'Active')"
            )

        with pytest.raises(module.UnverifiedCredentialReferencesError) as excinfo:
            self._apply(connection)
        assert "3 row(s)" in str(excinfo.value)

    # --- rollback ---

    def test_downgrade_restores_the_columns_empty(self, connection) -> None:
        """Rollback must restore the shape WITHOUT re-copying a secret ARN.

        This is the issue's explicit rollback constraint. The prior column held an address
        this revision did not create, so repopulating it on downgrade would silently
        recreate the unmanaged second reference to secret material that the upgrade exists
        to remove. Prior values are recoverable only from the pre-migration backup.
        """
        self._apply(connection, "upgrade")
        connection.exec_driver_sql(
            "INSERT INTO credential_registry "
            "(id, provider, adp_credential_id, status) "
            "VALUES ('c1', 'nebius', 'adp-cred-01HQ8V3XK2WERTY', 'Active')"
        )
        self._apply(connection, "downgrade")

        columns = self._columns(connection, "credential_registry")
        assert "secret_arn" in columns, "downgrade must restore the prior shape"
        assert "adp_credential_id" not in columns

        restored = connection.exec_driver_sql(
            "SELECT secret_arn FROM credential_registry WHERE id = 'c1'"
        ).scalar()
        assert not restored, (
            f"downgrade must leave secret_arn empty, got {restored!r}. Re-copying a secret "
            f"ARN would recreate the second, unmanaged reference to secret material."
        )

    def test_downgraded_secret_arn_is_nullable(self, connection) -> None:
        """The restored column must be nullable, since there is no value to put in it.

        Restoring it NOT NULL (as it originally was) would either fail outright or force a
        fabricated ARN into every row to satisfy the constraint.
        """
        import sqlalchemy as sa

        self._apply(connection, "upgrade")
        self._apply(connection, "downgrade")

        column = next(
            c
            for c in sa.inspect(connection).get_columns("credential_registry")
            if c["name"] == "secret_arn"
        )
        assert column["nullable"] is True

    # --- static guarantees ---

    def test_the_revision_extends_the_repaired_single_head(self) -> None:
        """012 must extend U13's repaired chain, which is the issue's hard dependency.

        Asserted as "on the single-headed chain's path to head, with 010 among its
        ancestors" rather than as a literal `down_revision == "010_add_workspace_grants"`.
        The literal form was what this test originally checked, and it encoded the wrong
        requirement: the dependency is on U13's repaired chain being *underneath* this
        revision, not on this revision being the immediate child of one particular id.
        When U15 (#5387) landed its own revision on 010, satisfying the literal assertion
        would have meant leaving the chain two-headed -- passing the test by breaking the
        invariant it exists to protect.

        The same correction applies one level up, and is why this no longer asserts
        `get_heads() == ["012_adp_credential_reference"]`. That form pinned 012 as the
        *terminal* revision, which is a different and stronger claim than the invariant:
        012 being terminal is not what makes `alembic upgrade head` unambiguous -- the
        chain being single-headed is. Requiring 012 to stay terminal would mean no later
        revision could ever extend it, so issue #5054's 013 (provider-operation records)
        would have had to land parallel to 012 to keep this test passing, i.e. satisfy
        the assertion by creating the second head whose absence it is checking for.

        What is checked instead: exactly one head, and 012 lies on the path `upgrade head`
        walks. That still fails if 012 is detached from the chain, if it ends up on a side
        branch that head never reaches, or if any second head appears -- including one
        created by a descendant of 012.
        """
        config = Config(str(API_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(API_ROOT / "alembic"))
        script = ScriptDirectory.from_config(config)

        heads = list(script.get_heads())
        assert len(heads) == 1, (
            f"the chain must have a single head; a second head makes "
            f"`alembic upgrade head` ambiguous and applies no migration at all. "
            f"Found {len(heads)}: {sorted(heads)}"
        )

        # "Reached by `upgrade head`", not "is head". `iterate_revisions` walks from the
        # head down to base, which is exactly the set of revisions an upgrade applies, so
        # a 012 that sits on an orphaned side branch fails here even though the chain is
        # single-headed.
        on_path_to_head = {
            rev.revision for rev in script.iterate_revisions("heads", "base")
        }
        assert "012_adp_credential_reference" in on_path_to_head, (
            "this revision must lie on the path `alembic upgrade head` walks; a revision "
            "off that path is never applied no matter how many heads the chain has"
        )

        ancestors = {
            rev.revision
            for rev in script.iterate_revisions("012_adp_credential_reference", "base")
        }
        assert "010_add_workspace_grants" in ancestors, (
            "U13's repaired chain (through 010) must remain an ancestor of this revision"
        )

    def test_the_upgrade_never_writes_a_secret_arn_into_the_new_column(self) -> None:
        """Statically: no UPDATE in 012 copies an ARN column into the reference column.

        Belt-and-braces against the exact defect the risk table names first. Even with the
        row-state check in place, an `UPDATE ... SET adp_credential_id = secret_arn` would
        satisfy every behavioral test above on an empty database and silently copy secret
        addresses on a populated one.
        """
        source = (VERSIONS_DIR / self.REVISION_FILE).read_text(encoding="utf-8")
        upgrade_body = source.split("def upgrade()")[1].split("def downgrade()")[0]
        lowered = upgrade_body.lower()

        assert "set adp_credential_id = secret_arn" not in lowered
        assert "adp_credential_ids_json = secret_arns_json" not in lowered


class TestProviderConnectionRevisionAppliesAndReverses:
    """014 creates the two R7 tables, and its downgrade removes exactly those.

    Issue #5053 (U7b). Run ONLINE against a live SQLite connection, not only through
    the offline `--sql` rendering above: offline mode never opens a connection, so it
    cannot observe that `downgrade()` leaves the surrounding schema alone. The
    downgrade half is the part worth exercising, because a revision that creates
    tables holding a credential reference must be reversible without reintroducing
    anything — and "drops only what it created" is a claim about other tables, which
    a rendering test cannot check.
    """

    TABLES = ("provider_connections", "provider_connection_bindings")

    @staticmethod
    def _revision_module():
        import importlib.util

        path = API_ROOT / "alembic" / "versions" / "014_add_provider_connections.py"
        spec = importlib.util.spec_from_file_location("_revision_014", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @pytest.fixture()
    def connection(self):
        import sqlalchemy as sa

        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            # Only the two tables 014's foreign keys point at, in a minimal shape —
            # for the same reason the 012 tests hand-build theirs: the earlier chain
            # uses PostgreSQL types that do not apply cleanly to SQLite, and that
            # portability question is covered by the rendering tests above.
            conn.exec_driver_sql("CREATE TABLE organizations (id TEXT PRIMARY KEY)")
            conn.exec_driver_sql("CREATE TABLE workspaces (id TEXT PRIMARY KEY)")
            yield conn

    def _run(self, connection, direction: str) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        module = self._revision_module()
        context = MigrationContext.configure(connection=connection)
        with Operations.context(context):
            getattr(module, direction)()

    def _tables(self, connection) -> set:
        import sqlalchemy as sa

        return set(sa.inspect(connection).get_table_names())

    def test_the_upgrade_creates_both_tables(self, connection) -> None:
        self._run(connection, "upgrade")
        assert set(self.TABLES) <= self._tables(connection)

    def test_the_downgrade_removes_exactly_what_the_upgrade_added(
        self, connection
    ) -> None:
        """A full round trip returns the schema to byte-level table parity."""
        before = self._tables(connection)
        self._run(connection, "upgrade")
        self._run(connection, "downgrade")
        assert self._tables(connection) == before

    def test_the_binding_uniqueness_constraint_is_in_the_applied_schema(
        self, connection
    ) -> None:
        """The constraint reaches a real database, not just the model metadata.

        `tests/test_models.py` proves the constraint holds against the metadata the
        test suite creates with `create_all`. That is a different artifact from what
        this migration applies, and production gets the migration's version. A
        constraint present in one and absent from the other is a gap no model test
        can see.
        """
        import sqlalchemy as sa

        self._run(connection, "upgrade")
        constraints = sa.inspect(connection).get_unique_constraints(
            "provider_connection_bindings"
        )
        assert any(c["column_names"] == ["connection_id"] for c in constraints), (
            f"one-binding-per-connection is missing from the applied schema: {constraints}"
        )

    def test_the_applied_schema_enforces_one_binding_per_connection(
        self, connection
    ) -> None:
        """Insert two bindings for one connection through SQL and be refused.

        The strongest form available offline: it bypasses the model, the validator
        and the service layer entirely, so what it exercises is the database rule
        that survives all three.
        """
        import sqlalchemy as sa

        self._run(connection, "upgrade")
        connection.exec_driver_sql("INSERT INTO organizations (id) VALUES ('org-1')")
        connection.exec_driver_sql("INSERT INTO workspaces (id) VALUES ('ws-1')")
        connection.exec_driver_sql("INSERT INTO workspaces (id) VALUES ('ws-2')")
        connection.exec_driver_sql(
            "INSERT INTO provider_connections"
            " (id, org_id, provider, adp_credential_id, credential_service,"
            "  credential_label, owner_principal, status)"
            " VALUES ('c-1', 'org-1', 'nebius', 'adp-cred-1', 'nebius',"
            "         'prod', 'user-owner', 'pending')"
        )
        connection.exec_driver_sql(
            "INSERT INTO provider_connection_bindings"
            " (id, connection_id, adp_credential_id, workspace_id, bound_by)"
            " VALUES ('b-1', 'c-1', 'adp-cred-1', 'ws-1', 'user-owner')"
        )
        with pytest.raises(sa.exc.IntegrityError):
            connection.exec_driver_sql(
                "INSERT INTO provider_connection_bindings"
                " (id, connection_id, adp_credential_id, workspace_id, bound_by)"
                " VALUES ('b-2', 'c-1', 'adp-cred-1', 'ws-2', 'user-owner')"
            )

    def test_capacity_and_the_validation_readings_are_nullable_in_the_applied_schema(
        self, connection
    ) -> None:
        """ "Not measured" must be representable after the migration, not just in the model.

        A NOT NULL `observed_capacity` defaulting to 0 would make "we did not look"
        indistinguishable from "there is nothing free" for every row in a real
        database, which is R7 acceptance 3 undone at the storage layer.
        """
        import sqlalchemy as sa

        self._run(connection, "upgrade")
        columns = {
            c["name"]: c
            for c in sa.inspect(connection).get_columns("provider_connections")
        }
        for name in (
            "credential_valid",
            "permissions_sufficient",
            "quota_available",
            "observed_capacity",
            "validated_at",
        ):
            assert columns[name]["nullable"] is True, (
                f"{name} must be nullable: unmeasured is not a reading"
            )

    def test_the_revision_writes_no_rows(self, connection) -> None:
        """Statically: no INSERT or UPDATE anywhere in the revision.

        The revision deliberately creates empty tables. A backfill would have to
        invent a vault ownership record — asserting an ownership fact nobody
        established and then feeding it to `authorize_delegation` as evidence, which
        is the "delegates a credential they were never authorized to share" failure
        manufactured by a migration. R7's acceptances 6-7 hold that work open behind
        an unresolved vault-access gate.
        """
        path = API_ROOT / "alembic" / "versions" / "014_add_provider_connections.py"
        source = path.read_text(encoding="utf-8")
        lowered = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        ).lower()
        for forbidden in ("insert into", "op.bulk_insert", "update "):
            assert forbidden not in lowered, (
                f"014 appears to write rows ({forbidden!r}); it must create empty "
                "tables and leave population to a live vault response"
            )
