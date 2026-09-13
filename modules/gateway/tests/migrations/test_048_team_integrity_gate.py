"""Actual checkpoint SQL, refusal semantics, and operator read-only audit (#4924)."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations
from tests.migrations.conftest_postgres import run_alembic, to_async_url, upgrade

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checkpoint = _load("checkpoint_048_test", "alembic/versions/048_team_integrity_gate.py")
legacy = _load("legacy_040_test", "alembic/versions/040_team_memberships.py")
script = _load("team_audit_script_test", "scripts/audit_team_integrity.py")


def _schema(connection, *, memberships=True):
    # Minimal source schema permits broken legacy pointers. Missing-reference
    # fixtures deliberately omit FKs to reproduce damaged/imported databases.
    connection.execute(sa.text("CREATE TABLE organizations (id VARCHAR(255) PRIMARY KEY)"))
    connection.execute(sa.text("CREATE TABLE users (id VARCHAR(255) PRIMARY KEY, org_id VARCHAR(255), team_id VARCHAR(255))"))
    connection.execute(sa.text("CREATE TABLE teams (id VARCHAR(255) PRIMARY KEY, org_id VARCHAR(255))"))
    if memberships:
        connection.execute(
            sa.text(
                "CREATE TABLE team_memberships (id VARCHAR(255) PRIMARY KEY, user_id VARCHAR(255), "
                "team_id VARCHAR(255), org_id VARCHAR(255), is_primary BOOLEAN, source TEXT, external_id TEXT)"
            )
        )
    connection.execute(sa.text("INSERT INTO organizations VALUES ('org-a'), ('org-b')"))
    connection.execute(sa.text("INSERT INTO teams VALUES ('team-a', 'org-a'), ('team-a2', 'org-a'), ('team-b', 'org-b')"))
    connection.execute(sa.text("INSERT INTO users VALUES ('user-a', 'org-a', 'team-a'), ('user-b', 'org-b', 'team-b')"))
    if memberships:
        connection.execute(
            sa.text(
                "INSERT INTO team_memberships VALUES "
                "('membership-a', 'user-a', 'team-a', 'org-a', true, 'directory', 'external-1'), "
                "('membership-a2', 'user-a', 'team-a2', 'org-a', false, 'admin', NULL), "
                "('membership-b', 'user-b', 'team-b', 'org-b', true, 'admin', NULL)"
            )
        )


def _snapshot(connection):
    return {
        table: connection.execute(sa.text(f"SELECT * FROM {table} ORDER BY id")).all()
        for table in ("organizations", "users", "teams", "team_memberships")
    }


def _upgrade(connection, monkeypatch):
    monkeypatch.setattr(checkpoint, "op", Operations(MigrationContext.configure(connection)))
    checkpoint.upgrade()


@pytest.fixture(params=["sqlite", "postgresql"])
def connection(request):
    engine = sa.create_engine(request.getfixturevalue("pg_url") if request.param == "postgresql" else "sqlite://")
    with engine.begin() as connection:
        _schema(connection)
        yield connection
    engine.dispose()


def test_clean_upgrade_preserves_all_rows_and_is_idempotent(connection, monkeypatch):
    before = _snapshot(connection)
    _upgrade(connection, monkeypatch)
    _upgrade(connection, monkeypatch)
    checkpoint.downgrade()
    assert _snapshot(connection) == before
    report = checkpoint.audit(connection)
    assert report["status"] == "CLEAN"
    assert report["sources"] == {"organizations": 2, "users": 2, "teams": 3, "team_memberships": 3}
    assert not any(report["findings"].values())


@pytest.mark.parametrize(
    ("mutation", "finding"),
    [
        ("UPDATE team_memberships SET user_id = 'missing' WHERE id = 'membership-a'", "membership_missing_user"),
        ("UPDATE team_memberships SET team_id = 'missing' WHERE id = 'membership-a'", "membership_missing_team"),
        ("UPDATE team_memberships SET user_id = 'user-b' WHERE id = 'membership-a'", "membership_user_org_mismatch"),
        ("UPDATE team_memberships SET team_id = 'team-b' WHERE id = 'membership-a'", "membership_team_org_mismatch"),
        ("UPDATE team_memberships SET org_id = NULL WHERE id = 'membership-a'", "membership_user_org_mismatch"),
        ("UPDATE team_memberships SET org_id = NULL WHERE id = 'membership-a'", "membership_team_org_mismatch"),
        ("UPDATE users SET team_id = 'missing' WHERE id = 'user-a'", "pointer_missing_team"),
        ("UPDATE users SET team_id = 'team-b' WHERE id = 'user-a'", "pointer_team_org_mismatch"),
        ("UPDATE users SET org_id = NULL WHERE id = 'user-a'", "pointer_team_org_mismatch"),
    ],
)
def test_dirty_upgrade_refuses_without_mutating_any_rows(connection, monkeypatch, mutation, finding):
    connection.execute(sa.text(mutation))
    before = _snapshot(connection)
    with pytest.raises(RuntimeError, match=f"{finding}=1"):
        _upgrade(connection, monkeypatch)
    assert _snapshot(connection) == before
    report = checkpoint.audit(connection, include_ids=True)
    assert report["status"] == "INCONSISTENT"
    assert report["findings"][finding] == 1
    assert len(report["details"][finding]["rows"]) == 1


def test_no_new_primary_or_eager_membership_policy(connection, monkeypatch):
    # These states are deliberately outside this ownership-only checkpoint:
    # unmaterialized legacy pointer, empty sentinel, and primary disagreement.
    connection.execute(sa.text("INSERT INTO users VALUES ('legacy', 'org-a', 'team-a'), ('shadow', 'org-a', '')"))
    connection.execute(sa.text("UPDATE users SET team_id = 'team-a2' WHERE id = 'user-a'"))
    before = _snapshot(connection)
    _upgrade(connection, monkeypatch)
    assert _snapshot(connection) == before


def test_details_are_bounded_and_counts_are_not(connection):
    connection.execute(sa.text("UPDATE users SET team_id = 'missing'"))
    report = checkpoint.audit(connection, include_ids=True, detail_limit=1)
    assert report["findings"]["pointer_missing_team"] == 2
    assert len(report["details"]["pointer_missing_team"]["rows"]) == 1
    assert report["details"]["pointer_missing_team"]["truncated"] is True
    assert "details" not in checkpoint.audit(connection)


def test_query_error_is_not_zero_and_cannot_pass_upgrade(connection, monkeypatch):
    connection.execute(sa.text("DROP TABLE teams"))
    with pytest.raises(sa.exc.DatabaseError):
        _upgrade(connection, monkeypatch)


def test_explicit_pointer_diagnostic_never_claims_membership_validation(connection):
    connection.execute(sa.text("DROP TABLE team_memberships"))
    report = checkpoint.audit(connection, source_pointers_only=True)
    assert report["status"] == "SOURCE_POINTERS_CLEAN"
    assert report["memberships_checked"] is False
    assert "team_memberships" not in report["sources"]
    connection.execute(sa.text("UPDATE users SET team_id = 'team-b' WHERE id = 'user-a'"))
    assert checkpoint.audit(connection, source_pointers_only=True)["status"] == "INCONSISTENT"
    with pytest.raises(sa.exc.DatabaseError):
        checkpoint.audit(connection)


def test_cli_errors_are_redacted_and_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(script, "run", AsyncMock(side_effect=RuntimeError("secret://password@example")))
    assert script.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"status": "ERROR", "error_type": "RuntimeError"}


@pytest.mark.parametrize(("status", "code"), [("CLEAN", 0), ("SOURCE_POINTERS_CLEAN", 3), ("INCONSISTENT", 1)])
def test_cli_status_codes(monkeypatch, capsys, status, code):
    monkeypatch.setattr(script, "run", AsyncMock(return_value={"status": status}))
    assert script.main([]) == code
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_postgres_actual_040_backfill_then_048_refusal(pg_url, monkeypatch):
    engine = sa.create_engine(pg_url)
    try:
        with engine.begin() as connection:
            _schema(connection, memberships=False)
            connection.execute(sa.text("UPDATE users SET team_id = 'team-b' WHERE id = 'user-a'"))
            monkeypatch.setattr(legacy, "op", Operations(MigrationContext.configure(connection)))
            legacy.upgrade()
            # Demonstrate the real immutable040 defect, then reject its result.
            assert checkpoint.audit(connection)["findings"]["membership_team_org_mismatch"] == 1
            before = _snapshot(connection)
            with pytest.raises(RuntimeError, match="membership_team_org_mismatch=1"):
                _upgrade(connection, monkeypatch)
            assert _snapshot(connection) == before
    finally:
        engine.dispose()


async def test_postgres_operator_audit_uses_read_only_snapshot(pg_url):
    sync_engine = sa.create_engine(pg_url)
    with sync_engine.begin() as connection:
        _schema(connection)
        before = _snapshot(connection)
    engine = create_async_engine(to_async_url(pg_url))
    try:
        report = await script.collect(engine, include_ids=True)
        assert report["status"] == "CLEAN"
        assert report["transaction_read_only"] is True
        assert report["isolation"] == "repeatable read"
        assert report["sources"]["team_memberships"] == 3
        assert report["alembic_revisions"] == []
        with sync_engine.connect() as connection:
            assert _snapshot(connection) == before
    finally:
        await engine.dispose()
        sync_engine.dispose()


async def test_postgres_operator_transaction_rejects_writes(pg_url, monkeypatch):
    sync_engine = sa.create_engine(pg_url)
    with sync_engine.begin() as connection:
        _schema(connection)
        before = _snapshot(connection)

    def attempted_write(connection, **kwargs):
        connection.execute(sa.text("DELETE FROM users"))

    monkeypatch.setattr(script.CHECKPOINT, "audit", attempted_write)
    engine = create_async_engine(to_async_url(pg_url))
    try:
        with pytest.raises(sa.exc.DBAPIError, match="read-only transaction"):
            await script.collect(engine)
        with sync_engine.connect() as connection:
            assert _snapshot(connection) == before
    finally:
        await engine.dispose()
        sync_engine.dispose()


async def test_postgres_missing_membership_table_at_recorded_047_is_an_error(pg_url):
    engine = sa.create_engine(pg_url)
    with engine.begin() as connection:
        _schema(connection, memberships=False)
        connection.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        connection.execute(sa.text("INSERT INTO alembic_version VALUES ('047_claude_pricing_v2')"))
    async_engine = create_async_engine(to_async_url(pg_url))
    try:
        with pytest.raises(sa.exc.DBAPIError, match="team_memberships"):
            await script.collect(async_engine)
        limited = await script.collect(async_engine, source_pointers_only=True)
        assert limited["status"] == "SOURCE_POINTERS_CLEAN"
        assert limited["memberships_checked"] is False
        assert limited["alembic_revisions"] == ["047_claude_pricing_v2"]
    finally:
        await async_engine.dispose()
        engine.dispose()


def test_postgres_real_alembic_clean_upgrade_and_dirty_refusal(pg_url):
    upgrade(pg_url, checkpoint.down_revision)
    engine = sa.create_engine(pg_url)
    try:
        with engine.begin() as connection:
            # Existing seeded source data remains untouched by the real CLI.
            before = _snapshot(connection)
        upgrade(pg_url, checkpoint.revision)
        with engine.connect() as connection:
            assert _snapshot(connection) == before
        result = run_alembic(pg_url, "downgrade", checkpoint.down_revision)
        assert result.returncode == 0, result.stderr
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO users (id, org_id, team_id, email, name) "
                    "VALUES ('gate-dirty', 'not-a-real-org', 'missing-team', 'test@example.invalid', 'Test')"
                )
            )
            before = _snapshot(connection)
        result = run_alembic(pg_url, "upgrade", checkpoint.revision)
        assert result.returncode != 0
        assert "pointer_missing_team=1" in result.stderr
        with engine.connect() as connection:
            assert _snapshot(connection) == before
            assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == checkpoint.down_revision
    finally:
        engine.dispose()
