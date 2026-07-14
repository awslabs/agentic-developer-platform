"""Unit tests for ensure_repo_exists scope propagation (Issue #3529).

Verifies that the repositories ACL row carries the scope envelope's
tenant_id and owner_sub (from the SQS message), NOT values derived from
the GitHub org name.

Without this fix, a repo registered by user 'pranavsharma1000' under
tenant 'pranavsharma1000' would get tenant_id='aws-e' (the org name)
on the repositories row — making it invisible to tenant-scoped ACL queries.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

# We can't directly import the ingestion db module because it uses psycopg2.
# Instead, we test the SQL logic directly with sqlite3 (same semantics for
# the COALESCE + INSERT ON CONFLICT pattern).


@pytest.fixture
def db_conn():
    """In-memory SQLite connection with repositories table schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE repositories (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            repo_name TEXT UNIQUE NOT NULL,
            git_url TEXT NOT NULL,
            owner TEXT NOT NULL,
            allowed_principals TEXT DEFAULT '["*"]',
            tenant_id TEXT,
            owner_sub TEXT
        )
    """)
    conn.commit()
    return conn


def ensure_repo_exists_sqlite(
    conn: sqlite3.Connection,
    org_repo: str,
    git_url: str,
    *,
    allowed_principals: list[str] | None = None,
    tenant_id: str | None = None,
    owner_sub: str | None = None,
) -> str:
    """SQLite equivalent of db.ensure_repo_exists for testing.

    Mirrors the Postgres version's INSERT ON CONFLICT logic.
    """
    owner = org_repo.split("/")[0] if "/" in org_repo else org_repo
    principals_json = json.dumps(allowed_principals if allowed_principals is not None else ["*"])

    cursor = conn.cursor()
    # SQLite doesn't have COALESCE on EXCLUDED the same way, but we can
    # simulate the same semantics with a simpler ON CONFLICT DO UPDATE
    cursor.execute(
        """
        INSERT INTO repositories (repo_name, git_url, owner, allowed_principals, tenant_id, owner_sub)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (repo_name) DO UPDATE
            SET allowed_principals = CASE
                    WHEN repositories.allowed_principals = '[]'
                      OR repositories.allowed_principals IS NULL
                    THEN excluded.allowed_principals
                    ELSE repositories.allowed_principals
                END,
                tenant_id = COALESCE(excluded.tenant_id, repositories.tenant_id),
                owner_sub = COALESCE(excluded.owner_sub, repositories.owner_sub)
        """,
        (org_repo, git_url, owner, principals_json, tenant_id, owner_sub),
    )
    conn.commit()

    cursor.execute("SELECT id FROM repositories WHERE repo_name = ?", (org_repo,))
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError(f"Repository {org_repo} not found after ensure")
    return str(row[0])


class TestEnsureRepoExistsScope:
    """Tests for scope propagation in ensure_repo_exists (Issue #3529)."""

    def test_new_repo_gets_scope_envelope_tenant_and_owner(self, db_conn):
        """New repo INSERT carries tenant_id and owner_sub from scope envelope."""
        repo_id = ensure_repo_exists_sqlite(
            db_conn,
            "aws-e/adp",
            "https://github.com/aws-e/adp",
            tenant_id="pranavsharma1000",
            owner_sub="650f093f-ecd9-4ce1-a5a9-368e02c449cf",
        )

        cursor = db_conn.cursor()
        cursor.execute(
            "SELECT tenant_id, owner_sub FROM repositories WHERE id = ?",
            (repo_id,),
        )
        row = cursor.fetchone()
        # Must be the registering user's tenant, NOT the GitHub org 'aws-e'
        assert row[0] == "pranavsharma1000"
        assert row[1] == "650f093f-ecd9-4ce1-a5a9-368e02c449cf"

    def test_existing_repo_without_scope_gets_updated(self, db_conn):
        """Existing repo with NULL tenant_id/owner_sub gets updated on re-ingest."""
        # First insert without scope (legacy behavior)
        ensure_repo_exists_sqlite(
            db_conn,
            "aws-e/adp",
            "https://github.com/aws-e/adp",
        )

        # Verify initially NULL
        cursor = db_conn.cursor()
        cursor.execute(
            "SELECT tenant_id, owner_sub FROM repositories WHERE repo_name = 'aws-e/adp'"
        )
        row = cursor.fetchone()
        assert row[0] is None
        assert row[1] is None

        # Re-ingest with scope envelope values
        ensure_repo_exists_sqlite(
            db_conn,
            "aws-e/adp",
            "https://github.com/aws-e/adp",
            tenant_id="pranavsharma1000",
            owner_sub="650f093f-ecd9-4ce1-a5a9-368e02c449cf",
        )

        cursor.execute(
            "SELECT tenant_id, owner_sub FROM repositories WHERE repo_name = 'aws-e/adp'"
        )
        row = cursor.fetchone()
        assert row[0] == "pranavsharma1000"
        assert row[1] == "650f093f-ecd9-4ce1-a5a9-368e02c449cf"

    def test_existing_scope_not_overwritten_by_none(self, db_conn):
        """COALESCE ensures existing scope is not wiped by a None re-ingest."""
        # First insert with scope
        ensure_repo_exists_sqlite(
            db_conn,
            "aws-e/adp",
            "https://github.com/aws-e/adp",
            tenant_id="pranavsharma1000",
            owner_sub="650f093f-ecd9-4ce1-a5a9-368e02c449cf",
        )

        # Re-ingest without scope (e.g., shared/public reindex path)
        ensure_repo_exists_sqlite(
            db_conn,
            "aws-e/adp",
            "https://github.com/aws-e/adp",
            tenant_id=None,
            owner_sub=None,
        )

        cursor = db_conn.cursor()
        cursor.execute(
            "SELECT tenant_id, owner_sub FROM repositories WHERE repo_name = 'aws-e/adp'"
        )
        row = cursor.fetchone()
        # Must retain the original scope values
        assert row[0] == "pranavsharma1000"
        assert row[1] == "650f093f-ecd9-4ce1-a5a9-368e02c449cf"

    def test_shared_repo_stays_null_scope(self, db_conn):
        """Shared repo (no scope envelope) → tenant_id and owner_sub stay NULL."""
        repo_id = ensure_repo_exists_sqlite(
            db_conn,
            "torvalds/linux",
            "https://github.com/torvalds/linux",
            tenant_id=None,
            owner_sub=None,
        )

        cursor = db_conn.cursor()
        cursor.execute("SELECT tenant_id, owner_sub FROM repositories WHERE id = ?", (repo_id,))
        row = cursor.fetchone()
        assert row[0] is None
        assert row[1] is None

    def test_tenant_scoped_without_owner_sub(self, db_conn):
        """Tenant-scoped repo (visibility=tenant) has tenant_id but no owner_sub."""
        repo_id = ensure_repo_exists_sqlite(
            db_conn,
            "acme/internal-tool",
            "https://github.com/acme/internal-tool",
            tenant_id="acme-corp",
            owner_sub=None,
        )

        cursor = db_conn.cursor()
        cursor.execute("SELECT tenant_id, owner_sub FROM repositories WHERE id = ?", (repo_id,))
        row = cursor.fetchone()
        assert row[0] == "acme-corp"
        assert row[1] is None


class TestEnsureRepoExistsSqlGuard:
    """String-level guards on the real ensure_repo_exists SQL (Issue #3529).

    The COALESCE ordering is load-bearing: EXCLUDED.x wins over repositories.x
    so the incoming scope envelope values take precedence. A regression that
    swaps the order would silently keep stale values from the existing row.
    """

    def test_coalesce_order_tenant_id(self):
        """SQL contains COALESCE(EXCLUDED.tenant_id, repositories.tenant_id)."""
        from pathlib import Path

        db_source = (Path(__file__).parent.parent / "images" / "ingestion" / "db.py").read_text()
        assert "COALESCE(EXCLUDED.tenant_id, repositories.tenant_id)" in db_source

    def test_coalesce_order_owner_sub(self):
        """SQL contains COALESCE(EXCLUDED.owner_sub, repositories.owner_sub)."""
        from pathlib import Path

        db_source = (Path(__file__).parent.parent / "images" / "ingestion" / "db.py").read_text()
        assert "COALESCE(EXCLUDED.owner_sub, repositories.owner_sub)" in db_source
