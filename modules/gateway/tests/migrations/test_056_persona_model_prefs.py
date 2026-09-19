"""Tests for Alembic migration 056 — persona-model preferences and service-principal identity.

Issue #5419 (PMM-02). This file is **mandatory**, and its absence was not a
coverage gap but the direct cause of three shipped defects. Two independent
reasons it has to exist:

1. `modules/gateway/alembic/**` is absent from `gateway-ci.yml`'s trigger paths,
   so a migration-only change gets **zero CI signal**. A test under `tests/` is
   what makes CI run at all for this migration.
2. The acceptance suite for this story builds its schema with
   `Base.metadata.create_all` on SQLite. That never executes the migration, so
   the migration could be — and was — invalid DDL while 28 tests passed green.

Three defects in this migration were PostgreSQL-only, and none was caught by
those 28 green tests:

  - `is_active BOOLEAN DEFAULT 1` — PostgreSQL will not implicitly cast integer 1
    to boolean in a column default and fails the upgrade with
    `42804 datatype mismatch`. SQLite accepts `1` silently.
  - `COALESCE(revoked_at, '')` in the active-alias unique index — `revoked_at` is
    `TIMESTAMP WITH TIME ZONE` and PostgreSQL cannot resolve `''` as a timestamp,
    so this was a *second*, independent upgrade failure hiding behind the first.
  - `COALESCE(CAST(revoked_at AS VARCHAR), '')` — the repair for the second, and
    itself invalid: timestamptz -> text is STABLE, not IMMUTABLE (it depends on
    the `TimeZone` and `DateStyle` GUCs), so PostgreSQL refuses it in an index
    expression: `functions in index expression must be marked IMMUTABLE`.

The third is the instructive one and it shaped this file. It compiled *cleanly*
for the PostgreSQL dialect and executed *fine* on SQLite — both of the cheap
checks passed. Only `alembic upgrade head` against a real server rejected it.
Compiled DDL is a useful cheap signal; it is not proof. The migration now uses a
**partial** unique index (`WHERE revoked_at IS NULL`), which needs no expression
over `revoked_at` at all and so is immune to all three failure modes.

These tests exercise the REAL migration functions imported from the version
module. A test that re-implements the migration proves only that the author can
write the same bug twice.

Three layers, cheapest first, each catching what the previous cannot:

1. `TestAlembicOnly*` / `TestRoundTrip` / parity — execute the migration on a
   bare SQLite database with no `create_all`. Fast, always run.
2. `TestPostgresRendering` — compile the migration for the PostgreSQL dialect in
   alembic's offline (`--sql`) mode and assert on the DDL text. Needs no server,
   so it always runs; catches dialect-specific *spelling* like `DEFAULT 1`.
3. `TestRealPostgres` — run the real Alembic CLI end to end against a real
   PostgreSQL 16 server and assert on behaviour. This is the only layer that
   could have caught the IMMUTABLE defect, and the only one that satisfies AC-01
   as written ("clean upgrade, downgrade to predecessor, re-upgrade").

What is under test:

  - `upgrade()` creates all four tables on an **alembic-only** database — no
    `Base.metadata.create_all` anywhere. This is the assertion the acceptance
    suite structurally cannot make.
  - `upgrade / downgrade / upgrade` round-trips, because AC-01 requires the
    migration to run forward then backward and a rollback path that has never
    been run is not a rollback path. (Disposable database only.)
  - **Active-alias uniqueness behaviourally**, by inserting a second active alias
    and requiring an `IntegrityError`. An index can exist, be named exactly
    right, and cover the wrong columns or carry the wrong predicate; only the
    insert proves the invariant.
  - **Revoke-and-re-register semantics**, which is what the partial index buys
    over a plain unique constraint, and which the approved design (§4.1) requires.
  - The revision chains onto the real single head, and the chain still has
    exactly one head. A broken `down_revision` silently SKIPS the migration and
    live code then queries an absent table.
  - Migration/model schema parity across all four tables: both files are
    hand-written, so drift is the live risk. This is what pins the model's
    `server_default=true()` to the migration's `sa.true()`, and the model's
    partial `Index(...)` to the migration's.
  - The approved alias-source vocabulary, because an under-specified CHECK makes
    a legitimate caller class permanently unrecordable rather than merely
    mislabelled.
  - **No backfill**: the migration creates tables and seeds one settings row.

**Coverage boundary.** `TestRealPostgres` needs the `pgserver` package, which
publishes wheels for Python <= 3.12 only; on the repo's 3.13 venv those tests
**skip** rather than fail. CI's Test job runs Python 3.12, so they execute there.
Layers 1 and 2 run everywhere. See `README-postgres.md`; `conftest.py` provides
the `pg_url` fixture and `conftest_postgres.py` the `upgrade`/`downgrade` helpers
that shell out to the real Alembic CLI.
"""

import ast
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.persona_models import (
    ALIAS_SOURCES,
    PRINCIPAL_SOURCES,
    PersonaModelPolicySetting,
    PersonaModelPreference,
    ServicePrincipal,
    ServicePrincipalAlias,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"
MIGRATION_FILE = "056_persona_model_prefs.py"

SP = "service_principals"
SPA = "service_principal_aliases"
PREF = "persona_model_preferences"
SETTINGS = "persona_model_policy_settings"
ALL_TABLES = (SP, SPA, PREF, SETTINGS)

ACTIVE_ALIAS_INDEX = "uq_spa_active_alias"

ORG_A = "org-alpha"
ORG_B = "org-beta"
TS = "2026-09-18 00:00:00"


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_056 = _load_migration(MIGRATION_FILE)


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


async def _bare_engine():
    """An engine with NOTHING pre-created.

    All four tables are self-contained — the only foreign key is
    `service_principal_aliases` -> `service_principals`, both created by this
    migration — so nothing needs to exist first, and deliberately nothing does.
    `create_all` on the full metadata would build the tables from the ORM and
    mask a migration that never creates them itself. That is not a hypothetical
    failure mode here: it is precisely how `BOOLEAN DEFAULT 1` reached main.
    """
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


async def _upgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_056.upgrade)


async def _downgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_056.downgrade)


async def _table_names(engine):
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))


async def _insert_principal(conn, canonical_id: str, org_id: str) -> None:
    await conn.execute(
        text(
            f"INSERT INTO {SP} (canonical_service_principal_id, org_id, display_name, status, created_at, approved_by) "
            "VALUES (:cid, :org, 'Worker', 'active', :ts, 'admin-1')"
        ),
        {"cid": canonical_id, "org": org_id, "ts": TS},
    )


async def _insert_alias(
    conn,
    *,
    alias_row_id: str,
    canonical_id: str,
    org_id: str,
    alias_source: str = "agent_registry",
    alias_id: str = "worker-1",
    is_active: bool = True,
    revoked_at: str | None = None,
) -> None:
    await conn.execute(
        text(
            f"INSERT INTO {SPA} (id, canonical_service_principal_id, org_id, alias_source, alias_id, "
            "is_active, registered_at, registered_by, revoked_at) "
            "VALUES (:id, :cid, :org, :src, :alias, :active, :ts, 'admin-1', :revoked)"
        ),
        {
            "id": alias_row_id,
            "cid": canonical_id,
            "org": org_id,
            "src": alias_source,
            "alias": alias_id,
            "active": is_active,
            "ts": TS,
            "revoked": revoked_at,
        },
    )


class TestAlembicOnlyDatabase:
    """The migration builds its own schema, with no help from `create_all`."""

    async def test_upgrade_creates_all_four_tables(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        tables = await _table_names(engine)
        await engine.dispose()
        missing = [t for t in ALL_TABLES if t not in tables]
        assert not missing, f"migration did not create: {missing}"

    async def test_upgrade_creates_the_named_lookup_indexes(self):
        """The non-unique lookup indexes, by name.

        Separate from the uniqueness tests below, which are behavioural: these
        indexes carry no invariant, so an insert cannot detect their absence. A
        missing one is a silent full-scan on every tenant-scoped read rather than
        a wrong answer, which is exactly the kind of regression only a name check
        catches.

        `uq_spa_active_alias` is deliberately NOT asserted here: it is partial, and
        the predicate — the entire point of it — does not survive reflection. It is
        covered behaviourally in `TestActiveAliasUniqueness` and by `indexdef` in
        `TestRealPostgres`. `uq_persona_model_pref_scope` is a UniqueConstraint,
        which SQLite reports as a constraint rather than an index.
        """
        expected = {
            SP: {"ix_service_principals_org_id"},
            SPA: {"ix_spa_principal", "ix_spa_org_id"},
            PREF: {"ix_persona_model_pref_org_id", "ix_persona_model_pref_principal"},
        }
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            for table, names in expected.items():
                found = {index["name"] for index in await conn.run_sync(lambda c, t=table: sa_inspect(c).get_indexes(t))}
                assert not names - found, f"missing indexes on {table}: {sorted(names - found)}"
        await engine.dispose()

    async def test_every_table_accepts_a_minimal_row(self):
        """A table can be created and still be unusable.

        A NOT NULL column with no default that the application never supplies, or
        a CHECK that rejects the ordinary case, both pass a
        "does the table exist" assertion and fail on first write. This inserts the
        minimal legal row into each table — relying on server defaults for
        everything optional — so the defaults themselves are exercised.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"INSERT INTO {SP} (canonical_service_principal_id, org_id, display_name, approved_by) "
                    "VALUES ('sp-1', 'org-a', 'Worker', 'admin-1')"
                )
            )
            await conn.execute(
                text(
                    f"INSERT INTO {SPA} (id, canonical_service_principal_id, org_id, alias_source, alias_id, registered_by) "
                    "VALUES ('alias-1', 'sp-1', 'org-a', 'agent_registry', 'worker-1', 'admin-1')"
                )
            )
            await conn.execute(
                text(
                    f"INSERT INTO {PREF} (id, org_id, principal_kind, principal_source, principal_id, persona_key, "
                    "canonical_model_id, updated_by, updated_by_source) "
                    "VALUES ('pref-1', 'org-a', 'human', 'self', 'user-1', 'developer', 'model-a', 'user-1', 'self')"
                )
            )

        async with engine.connect() as conn:
            # Server defaults must have filled in: status, is_active, revision.
            status = await conn.scalar(text(f"SELECT status FROM {SP}"))
            is_active = await conn.scalar(text(f"SELECT is_active FROM {SPA}"))
            revision = await conn.scalar(text(f"SELECT revision FROM {PREF}"))
        await engine.dispose()

        assert status == "active"
        assert bool(is_active) is True, "is_active server default did not apply"
        assert revision == 1, "revision server default must start at 1 for compare-and-set to mean anything"

    async def test_upgrade_seeds_exactly_one_settings_row(self):
        """Resolution needs a canonical default before any principal row exists.

        Both default model IDs are deliberately NULL: seeding an unproven model ID
        platform-wide would be the inert-config class (#4511) at the widest
        possible blast radius.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(f"SELECT compatibility_class, candidate_default_model_id, active_default_model_id, enforcement_posture FROM {SETTINGS}")
                )
            ).fetchall()
        await engine.dispose()

        assert len(rows) == 1, f"expected one seeded settings row, got {rows}"
        compat, candidate, active, posture = rows[0]
        assert compat == "claude-agent-sdk"
        assert candidate is None, "seeding an unproven candidate default is the inert-config class"
        assert active is None, "seeding an unproven active default is the inert-config class"
        assert posture == "report_only"


class TestActiveAliasUniqueness:
    """At most one ACTIVE alias per (org_id, alias_source, alias_id).

    Proven by insert, not by reading the index. A partial index reflects as an
    ordinary unique index on three columns, so reading its column list would
    report identically for the `UniqueConstraint` that this migration must NOT
    use — the predicate is the whole point and reflection does not show it. Only
    the insert distinguishes them.
    """

    async def test_second_active_alias_for_same_triple_is_refused(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await _insert_principal(conn, "sp-1", ORG_A)
            await _insert_alias(conn, alias_row_id="a1", canonical_id="sp-1", org_id=ORG_A)

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await _insert_alias(conn, alias_row_id="a2", canonical_id="sp-1", org_id=ORG_A)
        await engine.dispose()

    async def test_uniqueness_is_tenant_scoped(self):
        """Two tenants may each register the same external subject name.

        The alias namespaces differ per auth path and are not globally unique — an
        `agent_name` is only meaningful within its tenant. A global index here
        would make one tenant's registration block another's, which is a
        cross-tenant denial of service.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await _insert_principal(conn, "sp-a", ORG_A)
            await _insert_principal(conn, "sp-b", ORG_B)
            await _insert_alias(conn, alias_row_id="a1", canonical_id="sp-a", org_id=ORG_A, alias_id="worker-1")
            await _insert_alias(conn, alias_row_id="a2", canonical_id="sp-b", org_id=ORG_B, alias_id="worker-1")

        async with engine.connect() as conn:
            count = await conn.scalar(text(f"SELECT COUNT(*) FROM {SPA}"))
        await engine.dispose()
        assert count == 2, "same alias_id in two tenants must both be registrable"

    async def test_uniqueness_is_qualified_by_alias_source(self):
        """Without `alias_source` in the key, a Cognito client id could resolve a
        row registered for an agent name. Different namespaces, same string."""
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await _insert_principal(conn, "sp-1", ORG_A)
            await _insert_alias(conn, alias_row_id="a1", canonical_id="sp-1", org_id=ORG_A, alias_source="agent_registry", alias_id="shared-name")
            await _insert_alias(conn, alias_row_id="a2", canonical_id="sp-1", org_id=ORG_A, alias_source="cognito_m2m", alias_id="shared-name")

        async with engine.connect() as conn:
            count = await conn.scalar(text(f"SELECT COUNT(*) FROM {SPA}"))
        await engine.dispose()
        assert count == 2

    async def test_revoked_alias_frees_the_triple_for_re_registration(self):
        """This is what the partial index buys over a plain UNIQUE.

        Approved design §4.1 requires revoke-and-re-register to work, and requires
        the SQLite tests to preserve those semantics. A plain
        `UniqueConstraint(org_id, alias_source, alias_id)` would permanently burn
        the triple on first revocation. Verified on a real PostgreSQL 16 server as
        well as here — see `TestRealPostgres`.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await _insert_principal(conn, "sp-1", ORG_A)
            await _insert_alias(conn, alias_row_id="a1", canonical_id="sp-1", org_id=ORG_A)
            await conn.execute(
                text(f"UPDATE {SPA} SET is_active = 0, revoked_at = :ts, revoked_by = 'admin-1' WHERE id = 'a1'"),
                {"ts": "2026-09-19 00:00:00"},
            )
            # Re-registering the same triple must now succeed.
            await _insert_alias(conn, alias_row_id="a2", canonical_id="sp-1", org_id=ORG_A)

        async with engine.connect() as conn:
            active = await conn.scalar(text(f"SELECT COUNT(*) FROM {SPA} WHERE revoked_at IS NULL"))
            total = await conn.scalar(text(f"SELECT COUNT(*) FROM {SPA}"))
        await engine.dispose()
        assert (active, total) == (1, 2), "revoked row must survive as history while the triple is re-registrable"

    async def test_active_flag_and_revoked_at_cannot_disagree(self):
        """`ck_spa_revocation_consistent` — closes a hole in the invariant above.

        The partial index is scoped to `revoked_at IS NULL`, but every read path in
        the service layer filters on `is_active == True`. Two representations of one
        fact, independently settable, is a real gap and not a theoretical one: set
        `revoked_at` while leaving `is_active` true and the row drops out of the
        index while remaining "active" to the application. Two such rows for one
        triple then coexist, and `resolve_service_principal`'s `scalar()` resolves a
        service identity to an arbitrary one of two canonical principals.

        Verified reachable on PostgreSQL 16 before the constraint was added, which
        is why this is pinned rather than reasoned about.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await _insert_principal(conn, "sp-1", ORG_A)

        # Active but carrying a revocation timestamp — the dangerous combination.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await _insert_alias(
                    conn,
                    alias_row_id="bad1",
                    canonical_id="sp-1",
                    org_id=ORG_A,
                    is_active=True,
                    revoked_at="2026-09-19 00:00:00",
                )

        # Inactive but with no revocation timestamp — unauditable: no revocation time.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await _insert_alias(
                    conn,
                    alias_row_id="bad2",
                    canonical_id="sp-1",
                    org_id=ORG_A,
                    is_active=False,
                    revoked_at=None,
                )
        await engine.dispose()

    async def test_revoking_only_the_flag_is_refused_on_update(self):
        """The constraint must hold on UPDATE, not only on INSERT.

        A revoke implemented as `SET is_active = false` alone would leave
        `revoked_at` NULL, keeping the row inside the partial index and so blocking
        re-registration forever while the application reports it revoked.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await _insert_principal(conn, "sp-1", ORG_A)
            await _insert_alias(conn, alias_row_id="a1", canonical_id="sp-1", org_id=ORG_A)

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text(f"UPDATE {SPA} SET is_active = 0 WHERE id = 'a1'"))
        await engine.dispose()

    async def test_two_revoked_rows_for_one_triple_coexist(self):
        """Revoke/re-register/revoke again must not collide on the revoked rows."""
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await _insert_principal(conn, "sp-1", ORG_A)
            await _insert_alias(conn, alias_row_id="a1", canonical_id="sp-1", org_id=ORG_A, is_active=False, revoked_at="2026-09-19 00:00:00")
            await _insert_alias(conn, alias_row_id="a2", canonical_id="sp-1", org_id=ORG_A, is_active=False, revoked_at="2026-09-20 00:00:00")

        async with engine.connect() as conn:
            count = await conn.scalar(text(f"SELECT COUNT(*) FROM {SPA}"))
        await engine.dispose()
        assert count == 2


class TestPreferenceScopeUniqueness:
    """One row per (org_id, principal_kind, principal_id, persona_key)."""

    async def _insert_pref(
        self, conn, *, row_id: str, org_id: str = ORG_A, kind: str = "human", pid: str = "user-1", persona: str = "developer"
    ) -> None:
        await conn.execute(
            text(
                f"INSERT INTO {PREF} (id, org_id, principal_kind, principal_source, principal_id, persona_key, "
                "canonical_model_id, revision, created_at, updated_at, updated_by, updated_by_source) "
                "VALUES (:id, :org, :kind, 'self', :pid, :persona, 'model-x', 1, :ts, :ts, 'actor-1', 'self')"
            ),
            {"id": row_id, "org": org_id, "kind": kind, "pid": pid, "persona": persona, "ts": TS},
        )

    async def test_duplicate_scope_is_refused(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await self._insert_pref(conn, row_id="p1")

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await self._insert_pref(conn, row_id="p2")
        await engine.dispose()

    async def test_same_principal_different_persona_is_allowed(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await self._insert_pref(conn, row_id="p1", persona="developer")
            await self._insert_pref(conn, row_id="p2", persona="reviewer")
        async with engine.connect() as conn:
            count = await conn.scalar(text(f"SELECT COUNT(*) FROM {PREF}"))
        await engine.dispose()
        assert count == 2

    async def test_cross_tenant_same_principal_is_allowed(self):
        """Tenancy is part of the key, so the same principal id in two tenants is
        two independent preferences rather than a collision."""
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await self._insert_pref(conn, row_id="p1", org_id=ORG_A)
            await self._insert_pref(conn, row_id="p2", org_id=ORG_B)
        async with engine.connect() as conn:
            count = await conn.scalar(text(f"SELECT COUNT(*) FROM {PREF}"))
        await engine.dispose()
        assert count == 2

    async def test_invalid_principal_kind_is_refused_by_the_database(self):
        """The CHECK is the backstop for AC-03. The service layer also refuses,
        but a service-layer-only guard is bypassed by any future direct writer."""
        engine = await _bare_engine()
        await _upgrade(engine)
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await self._insert_pref(conn, row_id="p1", kind="robot")
        await engine.dispose()

    async def test_unknown_compatibility_class_is_refused(self):
        """Only the two harness classes the platform actually has.

        A settings row for a class no harness implements is an enforcement posture
        that silently governs nothing, which is the inert-config failure mode.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text(f"INSERT INTO {SETTINGS} (compatibility_class, enforcement_posture) VALUES ('invalid-class', 'report_only')"))
        await engine.dispose()

    async def test_unknown_enforcement_posture_is_refused(self):
        """An unrecognized posture must not be storable.

        If it were, the resolver's posture branch would fall through to whatever
        its `else` happens to be — which is how a class ends up unenforced while
        the row claims otherwise.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text(f"INSERT INTO {SETTINGS} (compatibility_class, enforcement_posture) VALUES ('codex-sdk', 'mandatory')"))
        await engine.dispose()


class TestApprovedVocabularies:
    """The stored vocabularies must match the approved design (§4.1).

    An under-specified CHECK does not merely mislabel a caller: it makes that
    entire caller class permanently unrecordable, so its principals can never own
    a preference. The first revision of this migration allowed `oauth_client`
    (not an approved value) and omitted `eventbridge` and `github_actions`.
    """

    def test_alias_sources_are_the_five_approved_values(self):
        assert set(ALIAS_SOURCES) == {"sa_registration", "agent_registry", "cognito_m2m", "eventbridge", "github_actions"}

    def test_oauth_client_is_not_an_approved_alias_source(self):
        """Regression pin for the value the first revision shipped."""
        assert "oauth_client" not in ALIAS_SOURCES

    def test_principal_sources_are_self_plus_the_alias_sources(self):
        assert set(PRINCIPAL_SOURCES) == {"self", *ALIAS_SOURCES}

    async def test_every_approved_alias_source_is_insertable(self):
        """Anti-vacuity: proves the CHECK admits all five, not merely that the
        Python tuple lists them."""
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await _insert_principal(conn, "sp-1", ORG_A)
            for i, source in enumerate(ALIAS_SOURCES):
                await _insert_alias(conn, alias_row_id=f"a{i}", canonical_id="sp-1", org_id=ORG_A, alias_source=source, alias_id=f"subject-{i}")
        async with engine.connect() as conn:
            count = await conn.scalar(text(f"SELECT COUNT(*) FROM {SPA}"))
        await engine.dispose()
        assert count == len(ALIAS_SOURCES)

    async def test_unapproved_alias_source_is_refused(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await _insert_principal(conn, "sp-1", ORG_A)
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await _insert_alias(conn, alias_row_id="a1", canonical_id="sp-1", org_id=ORG_A, alias_source="oauth_client")
        await engine.dispose()


class TestRoundTrip:
    """AC-01: forward, then backward, on a clean database.

    A rollback path that has never been run is not a rollback path. The story's
    rollback claim — that dropping these tables cannot change any run's effective
    model, because nothing resolves from them until PMM-07 — is only checkable if
    the downgrade actually runs.
    """

    async def test_downgrade_drops_every_table(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)
        tables = await _table_names(engine)
        await engine.dispose()
        leftover = [t for t in ALL_TABLES if t in tables]
        assert not leftover, f"downgrade left tables behind: {leftover}"

    async def test_upgrade_downgrade_upgrade_restores_the_schema(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)
        await _upgrade(engine)
        tables = await _table_names(engine)
        async with engine.connect() as conn:
            seeded = await conn.scalar(text(f"SELECT COUNT(*) FROM {SETTINGS}"))
        await engine.dispose()

        missing = [t for t in ALL_TABLES if t not in tables]
        assert not missing, f"re-upgrade did not restore: {missing}"
        assert seeded == 1, "re-upgrade must re-seed exactly one settings row, not zero and not two"

    async def test_active_alias_invariant_survives_the_round_trip(self):
        """The expression index must be recreated, not merely the tables.

        An index dropped implicitly with its table and then not recreated by the
        re-upgrade would leave the invariant silently unenforced.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)
        await _upgrade(engine)
        async with engine.begin() as conn:
            await _insert_principal(conn, "sp-1", ORG_A)
            await _insert_alias(conn, alias_row_id="a1", canonical_id="sp-1", org_id=ORG_A)
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await _insert_alias(conn, alias_row_id="a2", canonical_id="sp-1", org_id=ORG_A)
        await engine.dispose()


class TestPostgresRendering:
    """Layer 2: compile for the PostgreSQL dialect without needing a server.

    Catches dialect-specific *spelling* that SQLite would accept — `DEFAULT 1` on
    a boolean is the canonical example. Rendering in alembic's offline (`--sql`)
    mode runs the same compiler that produces what dev receives.

    **This layer is necessary but NOT sufficient, and this migration is the proof.**
    `COALESCE(CAST(revoked_at AS VARCHAR), '')` rendered here without complaint and
    was still rejected by a real server for non-immutability. A compiler checks
    syntax and types; it does not check the planner's rules about what may appear
    in an index. `TestRealPostgres` is what closes that gap.

    Kept anyway, and kept first, because it runs on every interpreter including
    3.13 where `pgserver` is unavailable — so these assertions hold even in the
    runs where layer 3 skips.
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
            MIG_056.upgrade()
        return "".join(chunks)

    def test_boolean_default_is_not_an_integer(self):
        """Regression pin for the operator-found upgrade failure.

        `is_active BOOLEAN DEFAULT 1` fails on PostgreSQL with
        `column "is_active" is of type boolean but default expression is of type
        integer` (42804). SQLite accepts it, and the acceptance suite builds
        schema with `create_all`, so CI was structurally blind to it.
        """
        ddl = self._render_postgres_ddl()
        assert "DEFAULT 1 NOT NULL" not in ddl, f"integer default on a boolean column will fail the upgrade:\n{ddl}"
        assert "is_active BOOLEAN DEFAULT true NOT NULL" in ddl, ddl

    def test_active_alias_index_uses_no_coalesce_expression(self):
        """Regression pin for the second AND third upgrade failures.

        Two different COALESCE spellings were tried here and both failed on a real
        server, for unrelated reasons:

          - `COALESCE(revoked_at, '')` — PostgreSQL cannot resolve `''` as a
            `TIMESTAMP WITH TIME ZONE`.
          - `COALESCE(CAST(revoked_at AS VARCHAR), '')` — the obvious repair, and
            still rejected: timestamptz -> text is STABLE, not IMMUTABLE, and
            `functions in index expression must be marked IMMUTABLE` (42P17).

        The partial index needs no expression over `revoked_at` at all, which is
        why it is immune to both. Asserting the *absence* of any COALESCE over
        this column is the durable pin — it fails for any future attempt to
        reintroduce an expression index here, not just the two already tried.
        """
        ddl = self._render_postgres_ddl()
        assert "COALESCE(revoked_at" not in ddl, f"uncast COALESCE over a timestamptz will fail the upgrade:\n{ddl}"
        assert "CAST(revoked_at" not in ddl, f"casting revoked_at in an index expression is not IMMUTABLE and will fail the upgrade:\n{ddl}"

    def test_active_alias_index_renders_as_a_partial_unique_index(self):
        ddl = self._render_postgres_ddl()
        expected = f"CREATE UNIQUE INDEX {ACTIVE_ALIAS_INDEX} ON {SPA} (org_id, alias_source, alias_id) WHERE revoked_at IS NULL"
        assert expected in ddl, ddl

    def test_timestamps_are_timezone_aware_on_postgres(self):
        """A naive timestamp makes `revoked_at` ambiguous across zones, and
        `revoked_at` participates in a uniqueness decision."""
        ddl = self._render_postgres_ddl()
        assert "TIMESTAMP WITH TIME ZONE" in ddl
        assert ddl.count("TIMESTAMP WITHOUT TIME ZONE") == 0

    def test_four_tables_and_one_unique_index_render(self):
        ddl = self._render_postgres_ddl()
        assert ddl.count("CREATE TABLE") == 4
        assert ddl.count("CREATE UNIQUE INDEX") == 1

    def test_preference_scope_unique_constraint_renders(self):
        """Inline in the CREATE TABLE, unlike the alias index.

        Worth asserting separately: it is the constraint that makes "one
        preference per principal per persona" true, and a UniqueConstraint that
        failed to render leaves duplicate rows with no error anywhere.
        """
        ddl = self._render_postgres_ddl()
        assert "CONSTRAINT uq_persona_model_pref_scope UNIQUE (org_id, principal_kind, principal_id, persona_key)" in ddl, ddl

    def test_the_settings_seed_renders(self):
        """The seed is an `op.execute`, so it is easy to lose in a refactor and
        leaves no schema trace when it is."""
        ddl = self._render_postgres_ddl()
        assert f"INSERT INTO {SETTINGS}" in ddl, ddl
        assert "'claude-agent-sdk'" in ddl, ddl

    def test_the_alias_foreign_key_renders(self):
        """An alias without its principal is an orphan that resolves to nothing."""
        ddl = self._render_postgres_ddl()
        assert "FOREIGN KEY(canonical_service_principal_id) REFERENCES service_principals" in ddl, ddl

    def test_no_cascade_delete_renders(self):
        """Deleting a principal must not silently delete the aliases that record
        who it was; revocation is an audited act, not a cascade."""
        ddl = self._render_postgres_ddl()
        assert "ON DELETE CASCADE" not in ddl

    def test_approved_alias_sources_render_in_the_check(self):
        ddl = self._render_postgres_ddl()
        for source in ALIAS_SOURCES:
            assert f"'{source}'" in ddl, f"{source} missing from rendered DDL"
        assert "'oauth_client'" not in ddl


class TestRealPostgres:
    """Layer 3: the real Alembic CLI against a real PostgreSQL 16 server.

    This is the only layer that satisfies AC-01 as worded — "clean upgrade,
    downgrade to predecessor, re-upgrade" — and the only one that could have
    caught the IMMUTABLE defect, which passed both cheaper layers.

    It runs the whole chain from empty to head, so it also proves this migration
    composes with its 55 predecessors rather than merely working in isolation.

    Skips (does not fail) where `pgserver` is unavailable, i.e. Python 3.13. CI's
    Test job is 3.12, so it executes there. `pg_url` comes from
    `tests/migrations/conftest.py`.
    """

    def test_clean_upgrade_to_head_succeeds(self, pg_url):
        """The operator's repro. Fails loudly with Alembic's own output.

        This is the test that would have caught all three defects. Each one made
        this exact command abort partway through the chain.
        """
        from tests.migrations.conftest_postgres import upgrade

        upgrade(pg_url, "head")

        import psycopg2

        with psycopg2.connect(pg_url) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            tables = {row[0] for row in cursor.fetchall()}
        missing = [t for t in ALL_TABLES if t not in tables]
        assert not missing, f"upgrade head did not create: {missing}"

    def test_downgrade_to_predecessor_then_re_upgrade(self, pg_url):
        """AC-01's full round trip, on the real engine.

        The downgrade is the half most likely to be wrong and least likely to be
        exercised, because nothing runs it until an incident does.
        """
        import psycopg2

        from tests.migrations.conftest_postgres import downgrade, upgrade

        upgrade(pg_url, "head")
        downgrade(pg_url, MIG_056.down_revision)

        with psycopg2.connect(pg_url) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            after_downgrade = {row[0] for row in cursor.fetchall()}
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone()[0] == MIG_056.down_revision

        leftover = [t for t in ALL_TABLES if t in after_downgrade]
        assert not leftover, f"downgrade left tables behind: {leftover}"

        upgrade(pg_url, "head")

        with psycopg2.connect(pg_url) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            restored = {row[0] for row in cursor.fetchall()}
            cursor.execute("SELECT version_num FROM alembic_version")
            # PMM-03 adds two linear successors in this integration branch, so
            # an upgrade to ``head`` must advance through 056 and finish at 058.
            assert cursor.fetchone()[0] == "058_model_probe_admission"
            cursor.execute(f"SELECT COUNT(*) FROM {SETTINGS}")
            assert cursor.fetchone()[0] == 1, "re-upgrade must re-seed exactly one settings row"

        missing = [t for t in ALL_TABLES if t not in restored]
        assert not missing, f"re-upgrade did not restore: {missing}"

    def test_is_active_default_is_boolean_true_on_the_server(self, pg_url):
        """Reads the default back out of the catalogue.

        Asserts against what PostgreSQL actually stored, not what we sent it, so
        no coercion can hide between the two.
        """
        import psycopg2

        from tests.migrations.conftest_postgres import upgrade

        upgrade(pg_url, "head")
        with psycopg2.connect(pg_url) as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT data_type, column_default FROM information_schema.columns WHERE table_name = %s AND column_name = 'is_active'",
                (SPA,),
            )
            data_type, column_default = cursor.fetchone()

        assert data_type == "boolean"
        assert column_default == "true", f"expected boolean true, got {column_default!r} — an integer default fails the upgrade (42804)"

    def test_active_alias_index_is_partial_on_the_server(self, pg_url):
        """The operator asked for this index to be validated on PostgreSQL.

        Asserts the stored `indexdef`, which is PostgreSQL's own rendering, so a
        predicate that silently failed to apply cannot read as present.
        """
        import psycopg2

        from tests.migrations.conftest_postgres import upgrade

        upgrade(pg_url, "head")
        with psycopg2.connect(pg_url) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s", (ACTIVE_ALIAS_INDEX,))
            row = cursor.fetchone()

        assert row is not None, f"{ACTIVE_ALIAS_INDEX} was not created on PostgreSQL"
        indexdef = row[0]
        assert "CREATE UNIQUE INDEX" in indexdef, indexdef
        assert "(org_id, alias_source, alias_id)" in indexdef, indexdef
        assert "WHERE (revoked_at IS NULL)" in indexdef, f"index is not partial, so revoked aliases will block re-registration: {indexdef}"

    def test_active_alias_invariant_behaves_on_the_server(self, pg_url):
        """One active alias per triple; revoke frees it; tenancy and source scope it.

        The same behavioural contract the SQLite tests assert, re-proven on the
        engine that actually runs in dev — because the two disagreed about this
        index three times already.
        """
        import psycopg2

        from tests.migrations.conftest_postgres import upgrade

        upgrade(pg_url, "head")
        insert = (
            f"INSERT INTO {SPA} (id, canonical_service_principal_id, org_id, alias_source, alias_id, registered_by) "
            "VALUES (%s, 'sp-1', %s, %s, %s, 'admin-1')"
        )
        conn = psycopg2.connect(pg_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {SP} (canonical_service_principal_id, org_id, display_name, status, approved_by) "
                    "VALUES ('sp-1', %s, 'Worker', 'active', 'admin-1')",
                    (ORG_A,),
                )
                cursor.execute(insert, ("a1", ORG_A, "agent_registry", "worker-1"))

            with pytest.raises(psycopg2.errors.UniqueViolation), conn.cursor() as cursor:
                cursor.execute(insert, ("a2", ORG_A, "agent_registry", "worker-1"))

            # A different tenant and a different alias source are both distinct keys.
            with conn.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {SP} (canonical_service_principal_id, org_id, display_name, status, approved_by) "
                    "VALUES ('sp-2', %s, 'Worker', 'active', 'admin-1')",
                    (ORG_B,),
                )
                cursor.execute(
                    f"INSERT INTO {SPA} (id, canonical_service_principal_id, org_id, alias_source, alias_id, registered_by) "
                    "VALUES ('a3', 'sp-2', %s, 'agent_registry', 'worker-1', 'admin-1')",
                    (ORG_B,),
                )
                cursor.execute(insert, ("a4", ORG_A, "cognito_m2m", "worker-1"))

            # Revoking frees the triple for re-registration.
            with conn.cursor() as cursor:
                cursor.execute(f"UPDATE {SPA} SET is_active = false, revoked_at = now(), revoked_by = 'admin-1' WHERE id = 'a1'")
                cursor.execute(insert, ("a5", ORG_A, "agent_registry", "worker-1"))

            # ...and only one may be active at a time afterwards.
            with pytest.raises(psycopg2.errors.UniqueViolation), conn.cursor() as cursor:
                cursor.execute(insert, ("a6", ORG_A, "agent_registry", "worker-1"))
        finally:
            conn.close()

    def test_unapproved_alias_source_is_refused_on_the_server(self, pg_url):
        """`oauth_client` was the value the first revision shipped."""
        import psycopg2

        from tests.migrations.conftest_postgres import upgrade

        upgrade(pg_url, "head")
        conn = psycopg2.connect(pg_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {SP} (canonical_service_principal_id, org_id, display_name, status, approved_by) "
                    "VALUES ('sp-1', %s, 'Worker', 'active', 'admin-1')",
                    (ORG_A,),
                )
            with pytest.raises(psycopg2.errors.CheckViolation), conn.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {SPA} (id, canonical_service_principal_id, org_id, alias_source, alias_id, registered_by) "
                    "VALUES ('a1', 'sp-1', %s, 'oauth_client', 'worker-1', 'admin-1')",
                    (ORG_A,),
                )
        finally:
            conn.close()

    def test_every_approved_alias_source_is_accepted_on_the_server(self, pg_url):
        """Anti-vacuity partner to the refusal test: the CHECK admits all five."""
        import psycopg2

        from tests.migrations.conftest_postgres import upgrade

        upgrade(pg_url, "head")
        conn = psycopg2.connect(pg_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {SP} (canonical_service_principal_id, org_id, display_name, status, approved_by) "
                    "VALUES ('sp-1', %s, 'Worker', 'active', 'admin-1')",
                    (ORG_A,),
                )
                for index, source in enumerate(ALIAS_SOURCES):
                    cursor.execute(
                        f"INSERT INTO {SPA} (id, canonical_service_principal_id, org_id, alias_source, alias_id, registered_by) "
                        "VALUES (%s, 'sp-1', %s, %s, %s, 'admin-1')",
                        (f"a{index}", ORG_A, source, f"subject-{index}"),
                    )
                cursor.execute(f"SELECT COUNT(*) FROM {SPA}")
                assert cursor.fetchone()[0] == len(ALIAS_SOURCES)
        finally:
            conn.close()

    def test_timestamps_are_timezone_aware_on_the_server(self, pg_url):
        """`revoked_at` decides a uniqueness question, so an ambiguous zone here is
        an ambiguous invariant."""
        import psycopg2

        from tests.migrations.conftest_postgres import upgrade

        upgrade(pg_url, "head")
        with psycopg2.connect(pg_url) as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = %s AND data_type LIKE 'timestamp%%'",
                (SPA,),
            )
            types = dict(cursor.fetchall())

        assert types, "no timestamp columns found — the query is not looking at the right table"
        naive = [name for name, kind in types.items() if kind != "timestamp with time zone"]
        assert not naive, f"timezone-naive timestamp columns: {naive}"

    def test_uniqueness_violations_are_classified_over_asyncpg(self, pg_url):
        """A lost race must be a 409 on the PRODUCTION driver, not only on SQLite.

        ``_is_scope_uniqueness_violation`` and ``_is_active_alias_uniqueness_violation``
        branch on driver-specific exception shapes, so every SQLite test of the conflict
        path proves only the SQLite branch. The production driver is asyncpg, and its
        shape is not the one it appears to be: SQLAlchemy's asyncpg dialect raises its
        own ``IntegrityError`` adapter, which carries ``sqlstate`` but **not**
        ``constraint_name`` — the real ``asyncpg.exceptions.UniqueViolationError`` sits
        beneath it on ``__cause__``.

        Reading only ``exc.orig`` therefore found no constraint name, the ``sqlstate``
        branch compared ``""`` against the expected name and failed, and a lost create
        race returned HTTP 500 instead of 409 in production while every SQLite test
        stayed green. This test drives a real duplicate INSERT through asyncpg so the
        classification is checked against the shape production actually raises.

        Scope note: ``test_pmm02_postgres_concurrency.py`` covers the same driver
        shape for ``uq_spa_active_alias`` (the alias constraint) with real racing
        transactions. This test covers ``uq_persona_model_pref_scope`` — the
        *preference* constraint behind AC-06's concurrent-create 409 — which those
        tests do not touch. Both constraints reach the same extraction helper, but a
        regression could narrow one predicate and not the other.
        """
        import asyncio

        from sqlalchemy.exc import IntegrityError
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from src.admin.persona_models.service import (
            _is_active_alias_uniqueness_violation,
            _is_scope_uniqueness_violation,
        )
        from src.shared.models.base import new_uuid
        from src.shared.models.persona_models import PersonaModelPreference
        from tests.migrations.conftest_postgres import to_async_url

        def _row():
            return PersonaModelPreference(
                id=new_uuid(),
                org_id="org-asyncpg",
                principal_kind="human",
                principal_source="self",
                principal_id="p-1",
                persona_key="developer",
                canonical_model_id="m",
                revision=1,
                updated_by="p-1",
                updated_by_source="self",
            )

        async def _exercise() -> tuple[bool, bool, bool]:
            engine = create_async_engine(to_async_url(pg_url))
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(PersonaModelPreference.__table__.create)
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with factory() as session:
                    session.add(_row())
                    await session.commit()

                # The duplicate that a lost create race produces.
                async with factory() as session:
                    session.add(_row())
                    try:
                        await session.flush()
                    except IntegrityError as exc:
                        scope = _is_scope_uniqueness_violation(exc)
                        # The same exception must NOT satisfy the alias predicate:
                        # a matching-sqlstate error on the wrong constraint is not
                        # this conflict, and reporting it as one would hide a defect.
                        alias = _is_active_alias_uniqueness_violation(exc)
                    else:
                        raise AssertionError("duplicate INSERT was accepted — no unique constraint in play")

                # An unrelated integrity failure must keep propagating, not be
                # relabelled as "someone else got there first".
                async with factory() as session:
                    bad = _row()
                    bad.principal_kind = "not_a_kind"
                    session.add(bad)
                    try:
                        await session.flush()
                    except IntegrityError as exc:
                        unrelated = _is_scope_uniqueness_violation(exc)
                    else:
                        raise AssertionError("CHECK constraint did not refuse an invalid principal_kind")
                return scope, alias, unrelated
            finally:
                await engine.dispose()

        scope, alias, unrelated = asyncio.run(_exercise())
        assert scope is True, "a real asyncpg uniqueness violation was not recognised — a lost race returns 500, not 409"
        assert alias is False, "the scope violation was also classified as an alias violation"
        assert unrelated is False, "an unrelated CHECK violation was misreported as a uniqueness conflict"


class TestNoBackfill:
    """The migration creates tables and seeds one settings row. Nothing else.

    No existing table is altered and no existing row is rewritten, which is what
    makes the additive-compatibility claim in the story checkable.
    """

    def test_upgrade_only_creates_and_seeds(self):
        source = (MIGRATIONS_DIR / MIGRATION_FILE).read_text()
        tree = ast.parse(source)
        upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        called = {
            node.func.attr
            for node in ast.walk(upgrade)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "op"
        }
        assert called, "no op.* calls found — the AST walk is not looking at the migration"
        # `execute` is the settings seed, which the story requires so that
        # resolution has a canonical default before any principal row exists.
        allowed = {"create_table", "create_index", "execute"}
        assert called <= allowed, f"upgrade() must only create/seed; it also calls {sorted(called - allowed)}"

    def test_upgrade_does_not_alter_existing_tables(self):
        source = (MIGRATIONS_DIR / MIGRATION_FILE).read_text()
        for forbidden in ("op.alter_column", "op.drop_column", "op.add_column", "op.rename_table"):
            assert forbidden not in source, f"{forbidden} makes this migration non-additive"

    def test_the_only_seed_is_the_settings_row(self):
        """An INSERT into any other table would be fabricating a preference or an
        identity that nobody authored.

        The migration interpolates its table names (`INSERT INTO {SETTINGS_TABLE}`),
        so the target is checked by resolving the migration's own constants rather
        than by looking for a literal table name that never appears in the source.
        A source-text match for `SETTINGS` would pass vacuously for an insert into
        any of the four tables.
        """
        source = (MIGRATIONS_DIR / MIGRATION_FILE).read_text()
        inserts = [line for line in source.splitlines() if "INSERT INTO" in line]
        assert len(inserts) == 1, f"expected exactly one seed INSERT, got {inserts}"

        # Map each table-name constant back to the value it holds, then confirm the
        # single INSERT names the settings one and none of the other three.
        constants = {name: getattr(MIG_056, name) for name in ("SP_TABLE", "SPA_TABLE", "PREF_TABLE", "SETTINGS_TABLE")}
        assert constants["SETTINGS_TABLE"] == SETTINGS, "migration's SETTINGS_TABLE no longer matches this test's expectation"
        referenced = {name for name, value in constants.items() if name in inserts[0] or value in inserts[0]}
        assert referenced == {"SETTINGS_TABLE"}, f"the seed INSERT must target only {SETTINGS}; it references {sorted(referenced)}"

    def test_migration_declares_no_dependencies(self):
        assert MIG_056.depends_on is None
        assert MIG_056.branch_labels is None


class TestRevisionChain:
    def test_revision_id_and_down_revision(self):
        """Chains onto the head that was real when this landed.

        Authored as `055_persona_model_prefs` onto `054_execution_tenant_guards`,
        which collided with #5150's `055_orch_environment_leases` — both took the
        same parent and main carried two heads. Renumbered to `056` chaining onto
        `055_orch_environment_leases`.

        Pinning the parent is deliberate even though
        `test_migration_leaves_exactly_one_head` asserts linearity: the head-count
        check passes for any linear arrangement, including one where a later
        rebase silently re-points this revision at a different parent. Change this
        pin only together with `down_revision`, having confirmed the new parent's
        migration is disjoint from this one.
        """
        assert MIG_056.revision == "056_persona_model_prefs"
        assert MIG_056.down_revision == "055_orch_environment_leases"

    def test_revision_id_fits_the_alembic_version_column(self):
        """`alembic_version.version_num` is VARCHAR(32); a longer id fails at apply (#4123)."""
        assert len(MIG_056.revision) <= 32

    def test_migration_leaves_exactly_one_head(self):
        """Two heads is a broken deploy, invisible until a pod runs `alembic upgrade head`.

        Asserts the *count*, not the head's name: the head advances with every
        migration that lands, and a name-pinned assertion turns every future
        migration into a spurious failure here. This is the guard that would have
        caught the `055` collision before merge rather than after.
        """
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
        assert "056_persona_model_prefs" in revisions, "this revision must still be on the chain"


class TestModelMigrationParity:
    """The migration and the models are hand-written separately, so they can drift.

    The migration is what runs against dev; the models are what every other test
    uses. When they disagree, the tests pass and the deployed database raises
    `UndefinedColumn` on the first real query. This is also what pins the model's
    `server_default=true()` to the migration's `sa.true()` — the two files had
    the same integer-default defect independently.
    """

    MODELS = (
        (PREF, PersonaModelPreference),
        (SP, ServicePrincipal),
        (SPA, ServicePrincipalAlias),
        (SETTINGS, PersonaModelPolicySetting),
    )

    async def _migrated_columns(self, table: str):
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            columns = await conn.run_sync(lambda c: sa_inspect(c).get_columns(table))
        await engine.dispose()
        return {column["name"]: column for column in columns}

    @pytest.mark.parametrize(("table", "model"), MODELS, ids=[t for t, _ in MODELS])
    async def test_column_names_match_the_model(self, table, model):
        migrated = set(await self._migrated_columns(table))
        declared = {column.name for column in model.__table__.columns}
        assert migrated == declared, f"{table} drift — migration-only: {sorted(migrated - declared)}, model-only: {sorted(declared - migrated)}"

    @pytest.mark.parametrize(("table", "model"), MODELS, ids=[t for t, _ in MODELS])
    async def test_column_types_match_the_model(self, table, model):
        from sqlalchemy.dialects import sqlite

        dialect = sqlite.dialect()
        migrated_columns = await self._migrated_columns(table)
        declared = {column.name: str(column.type.compile(dialect=dialect)) for column in model.__table__.columns}
        migrated = {name: str(column["type"].compile(dialect=dialect)) for name, column in migrated_columns.items()}
        assert migrated == declared, f"{table} column types drifted between migration and model"

    @pytest.mark.parametrize(("table", "model"), MODELS, ids=[t for t, _ in MODELS])
    async def test_nullability_matches_the_model(self, table, model):
        """Nullability drift is the dangerous half of parity: a column the model
        believes optional but the database requires fails only on the write path
        that omits it."""
        migrated_columns = await self._migrated_columns(table)
        declared = {column.name: column.nullable for column in model.__table__.columns}
        migrated = {name: column["nullable"] for name, column in migrated_columns.items()}
        assert migrated == declared, f"{table} nullability drifted between migration and model"

    async def test_boolean_default_matches_between_migration_and_model(self):
        """Both files independently shipped `1` here.

        `create_all` uses the model, the deployment uses the migration, so a
        mismatch is invisible in CI and fatal on PostgreSQL. Compare the compiled
        PostgreSQL text of both.
        """
        from sqlalchemy.dialects import postgresql

        declared = ServicePrincipalAlias.__table__.c.is_active.server_default.arg
        rendered = str(declared.compile(dialect=postgresql.dialect()))
        assert rendered.lower() == "true", f"model boolean default renders as {rendered!r}, which PostgreSQL rejects on a boolean column"
