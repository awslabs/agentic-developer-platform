"""Unit tests for db.py — Postgres connection helper and upsert logic.

Tests cover:
- upsert_dependencies: batch insert, conflict handling, rollback on error
- ensure_repo_exists: insert-on-conflict-do-nothing + fetch id
- update_repo_sbom_status: partial updates

Uses mocks for psycopg2 (no live DB required for unit tests).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add ingestion scripts to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "images" / "ingestion"))

from sbom_parser import DependencyRecord


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_conn():
    """Mock psycopg2 connection with cursor."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    conn.autocommit = False
    return conn


@pytest.fixture
def sample_records():
    """Sample dependency records for testing."""
    return [
        DependencyRecord(
            package_url="pkg:pypi/requests@2.31.0",
            package_name="requests",
            package_version="2.31.0",
            package_ecosystem="pypi",
            source="code",
            resolution_source="lockfile",
            is_transitive=False,
            component_type="library",
        ),
        DependencyRecord(
            package_url="pkg:npm/express@4.18.2",
            package_name="express",
            package_version="4.18.2",
            package_ecosystem="npm",
            source="code",
            resolution_source="lockfile",
            is_transitive=False,
            component_type="library",
        ),
    ]


# ---------------------------------------------------------------------------
# upsert_dependencies tests
# ---------------------------------------------------------------------------


class TestUpsertDependencies:
    """Test dependency upsert logic."""

    def test_empty_records_returns_zero(self, mock_conn):
        import db

        result = db.upsert_dependencies(mock_conn, "repo-uuid-123", [])
        assert result == 0
        mock_conn.cursor.assert_not_called()

    def test_inserts_records(self, mock_conn, sample_records):
        import db

        result = db.upsert_dependencies(mock_conn, "repo-uuid-123", sample_records)
        assert result == 2
        mock_conn.commit.assert_called_once()

    def test_uses_correct_sql_params(self, mock_conn, sample_records):
        import db

        db.upsert_dependencies(mock_conn, "repo-uuid-123", sample_records)
        cursor = mock_conn.cursor.return_value
        # executemany should be called with the batch
        call_args = cursor.executemany.call_args
        sql = call_args[0][0]
        rows = call_args[0][1]

        assert "INSERT INTO dependencies" in sql
        assert "ON CONFLICT" in sql
        assert len(rows) == 2
        # First row: repo_id, package_coordinate, version, is_transitive, source, base_image
        assert rows[0] == (
            "repo-uuid-123",
            "pkg:pypi/requests@2.31.0",
            "2.31.0",
            False,
            "code",
            None,
        )

    def test_rollback_on_error(self, mock_conn, sample_records):
        import db

        cursor = mock_conn.cursor.return_value
        cursor.executemany.side_effect = Exception("DB error")

        with pytest.raises(Exception, match="DB error"):
            db.upsert_dependencies(mock_conn, "repo-uuid-123", sample_records)

        mock_conn.rollback.assert_called_once()

    def test_batching(self, mock_conn):
        """Test that large record sets are batched."""
        import db

        # Create 250 records
        records = [
            DependencyRecord(
                package_url=f"pkg:pypi/pkg{i}@1.0.0",
                package_name=f"pkg{i}",
                package_version="1.0.0",
                package_ecosystem="pypi",
                source="code",
                resolution_source="lockfile",
                is_transitive=False,
                component_type="library",
            )
            for i in range(250)
        ]

        result = db.upsert_dependencies(mock_conn, "repo-uuid-123", records, batch_size=100)
        assert result == 250
        cursor = mock_conn.cursor.return_value
        # Should be called 3 times: 100 + 100 + 50
        assert cursor.executemany.call_count == 3


# ---------------------------------------------------------------------------
# ensure_repo_exists tests
# ---------------------------------------------------------------------------


class TestEnsureRepoExists:
    """Test repository row creation/lookup."""

    def test_returns_existing_repo_id(self, mock_conn):
        import db

        cursor = mock_conn.cursor.return_value
        cursor.fetchone.return_value = ("uuid-abc-123",)

        result = db.ensure_repo_exists(mock_conn, "aws-e/adp", "https://github.com/aws-e/adp")
        assert result == "uuid-abc-123"
        mock_conn.commit.assert_called()

    def test_inserts_with_correct_owner(self, mock_conn):
        import db

        cursor = mock_conn.cursor.return_value
        cursor.fetchone.return_value = ("uuid-new",)

        db.ensure_repo_exists(mock_conn, "myorg/myrepo", "https://github.com/myorg/myrepo")
        insert_call = cursor.execute.call_args_list[0]
        params = insert_call[0][1]
        assert params == ("myorg/myrepo", "https://github.com/myorg/myrepo", "myorg")

    def test_raises_on_missing_row(self, mock_conn):
        import db

        cursor = mock_conn.cursor.return_value
        cursor.fetchone.return_value = None

        with pytest.raises(RuntimeError, match="not found after ensure"):
            db.ensure_repo_exists(mock_conn, "org/repo", "https://github.com/org/repo")


# ---------------------------------------------------------------------------
# update_repo_sbom_status tests
# ---------------------------------------------------------------------------


class TestUpdateRepoSbomStatus:
    """Test SBOM status updates on the repositories table."""

    def test_no_update_when_all_none(self, mock_conn):
        import db

        db.update_repo_sbom_status(mock_conn, "repo-uuid")
        mock_conn.cursor.assert_not_called()

    def test_updates_source_status(self, mock_conn):
        import db

        db.update_repo_sbom_status(mock_conn, "repo-uuid", source_status="complete")
        cursor = mock_conn.cursor.return_value
        cursor.execute.assert_called_once()
        sql = cursor.execute.call_args[0][0]
        assert "sbom_status" in sql
        assert "updated_at" in sql
        mock_conn.commit.assert_called_once()
