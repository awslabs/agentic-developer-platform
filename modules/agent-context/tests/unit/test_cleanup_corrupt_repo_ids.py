"""Unit tests for the corrupt repo_id cleanup (Issue #2864).

Tests the DELETE predicates from scripts/cleanup_corrupt_repo_ids.py against an
in-memory SQLite database:
- Corrupt repositories rows (repo_name starts with http/git@) are deleted
- Corrupt knowledge_assets rows (double-prefixed source_ref) are deleted
- The HEALTHY octocat/Hello-World rows are NEVER touched
- Cleanup is idempotent

Uses SQLite (LIKE semantics match Postgres closely enough for these predicates).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# Predicates under test (kept in sync with the script).
from importlib.util import module_from_spec, spec_from_file_location

SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "cleanup_corrupt_repo_ids.py"


def _load_predicates() -> tuple[str, str]:
    """Load the SQL predicate constants from the cleanup script without importing db."""
    spec = spec_from_file_location("cleanup_corrupt_repo_ids", SCRIPT_PATH)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CORRUPT_REPO_NAME_SQL, module.CORRUPT_SOURCE_REF_SQL


CORRUPT_REPO_NAME_SQL, CORRUPT_SOURCE_REF_SQL = _load_predicates()


class TestCleanupPredicates:
    """Verify the DELETE predicates hit only corrupt rows."""

    @pytest.fixture
    def db(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE repositories (
                id TEXT PRIMARY KEY,
                repo_name TEXT NOT NULL UNIQUE,
                git_url TEXT NOT NULL
            );
            CREATE TABLE knowledge_assets (
                id TEXT PRIMARY KEY,
                asset_type TEXT NOT NULL,
                source_ref TEXT NOT NULL
            );
        """)
        # Healthy repo row + its corrupt double-prefixed twin.
        conn.executemany(
            "INSERT INTO repositories (id, repo_name, git_url) VALUES (?, ?, ?)",
            [
                ("r-good", "octocat/Hello-World", "https://github.com/octocat/Hello-World"),
                # Legit orgs whose name merely STARTS with 'http' must never match.
                ("r-httpie", "httpie/cli", "https://github.com/httpie/cli"),
                ("r-httporg", "http-party/node-http-proxy", "https://github.com/http-party/node-http-proxy"),
                (
                    "r-bad",
                    "https://github.com/octocat/Hello-World",
                    "https://github.com/https://github.com/octocat/Hello-World",
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO knowledge_assets (id, asset_type, source_ref) VALUES (?, ?, ?)",
            [
                ("a-good", "repo", "https://github.com/octocat/Hello-World"),
                (
                    "a-bad",
                    "repo",
                    "https://github.com/https://github.com/octocat/Hello-World",
                ),
                # A url-type asset that legitimately points at github.com must not match.
                ("a-url", "url", "https://github.com/octocat/Hello-World/issues"),
            ],
        )
        conn.commit()
        yield conn
        conn.close()

    def _apply(self, db):
        db.execute(f"DELETE FROM repositories WHERE {CORRUPT_REPO_NAME_SQL}")
        db.execute(f"DELETE FROM knowledge_assets WHERE {CORRUPT_SOURCE_REF_SQL}")
        db.commit()

    def test_corrupt_repository_deleted(self, db):
        self._apply(db)
        rows = db.execute("SELECT id FROM repositories ORDER BY id").fetchall()
        assert [r[0] for r in rows] == ["r-good", "r-httpie", "r-httporg"]

    def test_org_names_starting_with_http_survive(self, db):
        """Regression: 'httpie/cli'-style orgs must never match the DELETE predicate."""
        self._apply(db)
        rows = db.execute(
            "SELECT repo_name FROM repositories WHERE id IN ('r-httpie', 'r-httporg') ORDER BY id"
        ).fetchall()
        assert [r[0] for r in rows] == ["httpie/cli", "http-party/node-http-proxy"]

    def test_corrupt_asset_deleted(self, db):
        self._apply(db)
        rows = db.execute("SELECT id FROM knowledge_assets ORDER BY id").fetchall()
        # a-good (healthy repo) and a-url (url type) survive; a-bad is deleted.
        assert [r[0] for r in rows] == ["a-good", "a-url"]

    def test_healthy_rows_untouched(self, db):
        self._apply(db)
        assert (
            db.execute("SELECT repo_name FROM repositories WHERE id = 'r-good'").fetchone()[0]
            == "octocat/Hello-World"
        )
        assert (
            db.execute("SELECT source_ref FROM knowledge_assets WHERE id = 'a-good'").fetchone()[0]
            == "https://github.com/octocat/Hello-World"
        )

    def test_cleanup_is_idempotent(self, db):
        self._apply(db)
        first = db.execute("SELECT id FROM repositories ORDER BY id").fetchall()
        self._apply(db)
        second = db.execute("SELECT id FROM repositories ORDER BY id").fetchall()
        assert first == second


class TestScriptStructure:
    """Sanity checks on the script file itself."""

    def test_script_exists(self):
        assert SCRIPT_PATH.exists()

    def test_has_dry_run_and_apply(self):
        content = SCRIPT_PATH.read_text()
        assert "--apply" in content
        assert "--verify" in content
        assert "def apply_cleanup" in content
