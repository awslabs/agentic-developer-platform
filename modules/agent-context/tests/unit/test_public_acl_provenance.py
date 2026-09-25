"""Real PostgreSQL regressions for ambiguous historical public ACL labels."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile

import pytest

MODULE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MODULE / "images/ingestion"))

from door.acl import CallerPrincipal, PostgresACLStore  # noqa: E402
import db  # noqa: E402


@pytest.fixture(scope="module")
def database():
    pgserver = pytest.importorskip("pgserver")
    psycopg2 = pytest.importorskip("psycopg2")
    server = pgserver.get_server(tempfile.mkdtemp(prefix="s15-public-acl-"), cleanup_mode="delete")
    try:
        with psycopg2.connect(server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE repositories (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(), repo_name text UNIQUE NOT NULL,
                git_url text, owner text, allowed_principals jsonb, tenant_id text, owner_sub text,
                acl_public_verified boolean NOT NULL DEFAULT false)""")
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture
def connection(database):
    import psycopg2

    with psycopg2.connect(database) as conn:
        with conn.cursor() as cursor:
            cursor.execute("TRUNCATE repositories")
        conn.commit()
        yield conn


def store(conn, *, scoped=True):
    class Pool:
        def getconn(self):
            return conn

        def putconn(self, ignored):
            pass

    return PostgresACLStore(Pool(), tenant_scope_enabled=scoped)


def seed(conn):
    rows = [
        ("legacy/shared", '["*"]', None, None, False),
        ("legacy/tenant", '["*"]', "one", None, False),
        ("verified/shared", '["*"]', None, None, True),
        ("verified/tenant", '["*"]', "one", None, True),
        ("private/tenant", '["alice"]', "one", None, False),
        ("private/personal", "[]", "one", "owner", False),
        ("private/other", '["alice"]', "two", None, False),
        ("legacy/mixed", '["*","alice"]', "one", None, False),
        ("unlabeled/null", None, "one", None, False),
        ("unlabeled/empty", "[]", "one", None, False),
    ]
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO repositories(repo_name, allowed_principals, tenant_id, owner_sub, acl_public_verified) VALUES (%s,%s,%s,%s,%s)",
            rows,
        )
    conn.commit()


@pytest.mark.parametrize("run_bound", [False, True])
def test_legacy_wildcard_is_quarantined_without_breaking_verified_or_private_access(
    connection, run_bound
):
    seed(connection)
    caller = CallerPrincipal(
        github_login="alice", tenant_id="one", owner_sub="owner", run_bound=run_bound
    )
    assert store(connection).get_allowed_repos(caller) == {
        "verified/shared",
        "verified/tenant",
        "private/tenant",
        "private/personal",
        "legacy/mixed",
    }


@pytest.mark.parametrize("scoped", [False, True])
def test_wildcard_cannot_be_reinterpreted_as_login_or_team(connection, scoped):
    seed(connection)
    caller = CallerPrincipal(github_login="*", github_teams=["*"], tenant_id="one")
    assert store(connection, scoped=scoped).get_allowed_repos(caller) == {
        "verified/shared",
        "verified/tenant",
    }


def test_legacy_query_requires_public_provenance_too(connection):
    seed(connection)
    assert store(connection, scoped=False).get_allowed_repos(
        CallerPrincipal(github_login="bob")
    ) == {"verified/shared", "verified/tenant"}


def test_missing_marker_schema_fails_closed_instead_of_using_legacy_query(connection):
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE repositories RENAME COLUMN acl_public_verified TO old_marker")
    try:
        with pytest.raises(Exception):
            store(connection).get_allowed_repos(
                CallerPrincipal(github_login="alice", tenant_id="one")
            )
    finally:
        connection.rollback()


def test_generic_wildcard_registration_does_not_claim_source_verification(connection):
    db.ensure_repo_exists(
        connection, "fixture/repo", "https://github.com/fixture/repo", allowed_principals=["*"]
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT allowed_principals, acl_public_verified FROM repositories")
        assert cursor.fetchone() == (["*"], False)
    assert store(connection).get_allowed_repos(CallerPrincipal(github_login="reader")) == set()


def test_trusted_rederivation_enables_public_then_revokes_it(connection):
    repo = "fixture/repo"
    db.ensure_repo_exists(
        connection,
        repo,
        "https://github.com/" + repo,
        allowed_principals=["*"],
        public_verified=True,
    )
    assert store(connection).get_allowed_repos(CallerPrincipal(github_login="reader")) == {repo}
    db.ensure_repo_exists(
        connection, repo, "https://github.com/" + repo
    )  # SBOM lookup preserves it.
    assert store(connection).get_allowed_repos(CallerPrincipal(github_login="reader")) == {repo}
    db.ensure_repo_exists(connection, repo, "https://github.com/" + repo, allowed_principals=[])
    assert store(connection).get_allowed_repos(CallerPrincipal(github_login="reader")) == set()
    with connection.cursor() as cursor:
        cursor.execute("SELECT acl_public_verified FROM repositories")
        assert cursor.fetchone() == (False,)


def test_public_flag_without_authoritative_acl_is_rejected(connection):
    with pytest.raises(ValueError):
        db.ensure_repo_exists(
            connection,
            "fixture/repo",
            "https://github.com/fixture/repo",
            allowed_principals=["alice"],
            public_verified=True,
        )


def load_backfill():
    spec = importlib.util.spec_from_file_location(
        "s15_public_backfill", MODULE / "scripts/backfill_repo_acls.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_marks_only_observed_public_and_rollback_does_not_reopen_legacy(
    connection, monkeypatch, tmp_path
):
    import repo_acl

    backfill = load_backfill()
    db.ensure_repo_exists(
        connection, "fixture/repo", "https://github.com/fixture/repo", allowed_principals=["*"]
    )
    monkeypatch.setattr(repo_acl, "resolve_allowed_principals", lambda *a, **kw: ["*"])
    plan = [
        backfill.classify(row, token="synthetic-test-token")
        for row in backfill.find_legacy_rows(connection)
    ]
    journal = tmp_path / "journal.json"
    backfill.apply_changes(connection, plan, deny_unknown=False, journal_path=str(journal))
    assert store(connection).get_allowed_repos(CallerPrincipal(github_login="reader")) == {
        "fixture/repo"
    }
    backfill.rollback(connection, str(journal), apply=True)
    assert store(connection).get_allowed_repos(CallerPrincipal(github_login="reader")) == set()


def test_unknown_backfill_stays_quarantined_even_when_row_is_not_rewritten(connection, monkeypatch):
    import repo_acl

    backfill = load_backfill()
    db.ensure_repo_exists(
        connection, "fixture/repo", "https://github.com/fixture/repo", allowed_principals=["*"]
    )
    monkeypatch.setattr(repo_acl, "resolve_allowed_principals", lambda *a, **kw: [])
    plan = [
        backfill.classify(row, token="synthetic-test-token")
        for row in backfill.find_legacy_rows(connection)
    ]
    assert backfill.apply_changes(connection, plan, deny_unknown=False) == []
    assert store(connection).get_allowed_repos(CallerPrincipal(github_login="reader")) == set()
    assert backfill.verify(connection) is False


def test_migration_quarantines_existing_labels_and_downgrade_removes_unsafe_wildcards(
    connection, monkeypatch
):
    import psycopg2
    import sqlalchemy as sa

    MigrationContext = pytest.importorskip("alembic.migration").MigrationContext
    from alembic.operations import Operations

    seed(connection)
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE repositories DROP COLUMN acl_public_verified")
    connection.commit()
    spec = importlib.util.spec_from_file_location(
        "s15_marker_migration", MODULE / "alembic/versions/014_verified_public_acl.py"
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine(
        "postgresql+psycopg2://", creator=lambda: psycopg2.connect(connection.dsn)
    )
    try:
        with engine.begin() as sql_conn:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sql_conn)))
            migration.upgrade()
            assert (
                sql_conn.execute(
                    sa.text("SELECT count(*) FROM repositories WHERE acl_public_verified")
                ).scalar_one()
                == 0
            )
            sql_conn.execute(
                sa.text(
                    "UPDATE repositories SET acl_public_verified=true WHERE repo_name='verified/shared'"
                )
            )
            migration.downgrade()
            rows = dict(
                sql_conn.execute(
                    sa.text("SELECT repo_name, allowed_principals FROM repositories")
                ).all()
            )
            assert rows["legacy/shared"] == []
            assert rows["legacy/mixed"] == ["alice"]
            assert rows["verified/shared"] == ["*"]
            assert rows["private/tenant"] == ["alice"]
            migration.upgrade()  # Restore the shared disposable test schema.
    finally:
        engine.dispose()


def test_backfill_does_not_overwrite_concurrently_verified_public_row(connection, monkeypatch):
    import repo_acl

    backfill = load_backfill()
    db.ensure_repo_exists(
        connection, "fixture/repo", "https://github.com/fixture/repo", allowed_principals=["*"]
    )
    monkeypatch.setattr(repo_acl, "resolve_allowed_principals", lambda *a, **kw: [])
    plan = [
        backfill.classify(row, token="fixture") for row in backfill.find_legacy_rows(connection)
    ]
    db.ensure_repo_exists(
        connection,
        "fixture/repo",
        "https://github.com/fixture/repo",
        allowed_principals=["*"],
        public_verified=True,
    )
    assert backfill.apply_changes(connection, plan, deny_unknown=True) == []
    assert store(connection).get_allowed_repos(CallerPrincipal(github_login="reader")) == {
        "fixture/repo"
    }


def test_old_rollback_journal_cannot_promote_wildcard(connection, tmp_path):
    import json

    repo_id = db.ensure_repo_exists(
        connection, "fixture/repo", "https://github.com/fixture/repo", allowed_principals=[]
    )
    journal = tmp_path / "old-journal.json"
    journal.write_text(
        json.dumps(
            [
                {
                    "id": repo_id,
                    "repo_name": "fixture/repo",
                    "previous_acl": ["*"],
                    "new_acl": [],
                }
            ]
        )
    )
    load_backfill().rollback(connection, str(journal), apply=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT allowed_principals, acl_public_verified FROM repositories")
        assert cursor.fetchone() == (["*"], False)
    assert store(connection).get_allowed_repos(CallerPrincipal(github_login="reader")) == set()
