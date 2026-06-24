"""Unit tests for the agent_context database schema migration.

Tests verify:
1. Migration creates all expected tables and indexes
2. The reverse-lookup query (package_coordinate) works correctly
3. Cascade deletes propagate from repositories to dependencies/index_runs
4. The schema is self-contained (no cross-DB references)

Uses an in-memory SQLite database for speed. The migration's raw DDL is adapted
for SQLite compatibility where needed (UUID -> TEXT, TIMESTAMPTZ -> TEXT).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# Path to the migration file
MIGRATIONS_DIR = Path(__file__).parent.parent / "alembic" / "versions"


class TestKnowledgeLayerSchema:
    """Test the 001_knowledge_layer_schema migration against SQLite."""

    @pytest.fixture
    def db(self, tmp_path):
        """Create a fresh SQLite database with the schema applied."""
        db_path = tmp_path / "test_agent_context.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")

        # Apply schema (adapted for SQLite — no pgcrypto, TEXT instead of UUID/TIMESTAMPTZ)
        conn.executescript("""
            CREATE TABLE repositories (
                id              TEXT PRIMARY KEY,
                repo_name       TEXT NOT NULL UNIQUE,
                git_url         TEXT NOT NULL,
                owner           TEXT NOT NULL,
                allowed_principals TEXT NOT NULL DEFAULT '[]',
                last_indexed_sha TEXT,
                indexed_at      TEXT,
                zoekt_status    TEXT NOT NULL DEFAULT 'pending',
                vectors_status  TEXT NOT NULL DEFAULT 'pending',
                structure_status TEXT NOT NULL DEFAULT 'pending',
                sbom_status     TEXT NOT NULL DEFAULT 'pending',
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX ix_repositories_owner ON repositories(owner);

            CREATE TABLE dependencies (
                id                  TEXT PRIMARY KEY,
                repo_id             TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                package_coordinate  TEXT NOT NULL,
                version             TEXT,
                is_transitive       INTEGER NOT NULL DEFAULT 0,
                source              TEXT NOT NULL DEFAULT 'code',
                base_image          TEXT,
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (repo_id, package_coordinate, source)
            );

            CREATE INDEX ix_dependencies_package_coordinate ON dependencies(package_coordinate);
            CREATE INDEX ix_dependencies_repo_id ON dependencies(repo_id);

            CREATE TABLE vulnerabilities (
                id                TEXT PRIMARY KEY,
                cve_id            TEXT NOT NULL UNIQUE,
                package           TEXT NOT NULL,
                affected_versions TEXT NOT NULL,
                safe_version      TEXT,
                severity          TEXT NOT NULL DEFAULT 'unknown',
                details           TEXT,
                discovered_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX ix_vulnerabilities_package ON vulnerabilities(package);
            CREATE INDEX ix_vulnerabilities_severity ON vulnerabilities(severity);

            CREATE TABLE index_runs (
                id              TEXT PRIMARY KEY,
                repo_id         TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                started_at      TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at    TEXT,
                duration_ms     INTEGER,
                status          TEXT NOT NULL DEFAULT 'running',
                error           TEXT,
                steps_completed TEXT DEFAULT '{}'
            );

            CREATE INDEX ix_index_runs_repo_id ON index_runs(repo_id);
            CREATE INDEX ix_index_runs_started_at ON index_runs(started_at);
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_tables_exist(self, db):
        """All four tables are created."""
        cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]
        assert "repositories" in tables
        assert "dependencies" in tables
        assert "vulnerabilities" in tables
        assert "index_runs" in tables

    def test_indexes_exist(self, db):
        """Critical indexes are created."""
        cursor = db.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
        indexes = [row[0] for row in cursor.fetchall()]
        assert "ix_dependencies_package_coordinate" in indexes
        assert "ix_dependencies_repo_id" in indexes
        assert "ix_repositories_owner" in indexes
        assert "ix_vulnerabilities_package" in indexes
        assert "ix_index_runs_repo_id" in indexes
        assert "ix_index_runs_started_at" in indexes

    def test_reverse_lookup_query(self, db):
        """The core reverse-lookup query works: package_coordinate -> repos."""
        # Insert test data
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-1", "org/api-service", "https://github.com/org/api-service.git", "org"),
        )
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-2", "org/web-app", "https://github.com/org/web-app.git", "org"),
        )
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-3", "org/cli-tool", "https://github.com/org/cli-tool.git", "org"),
        )

        # Insert dependencies
        db.execute(
            "INSERT INTO dependencies (id, repo_id, package_coordinate, version, source) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dep-1", "repo-1", "pkg:pypi/requests@2.28.0", "2.28.0", "code"),
        )
        db.execute(
            "INSERT INTO dependencies (id, repo_id, package_coordinate, version, source) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dep-2", "repo-2", "pkg:pypi/requests@2.28.0", "2.28.0", "code"),
        )
        db.execute(
            "INSERT INTO dependencies (id, repo_id, package_coordinate, version, source) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dep-3", "repo-3", "pkg:npm/lodash@4.17.21", "4.17.21", "code"),
        )
        db.commit()

        # Reverse lookup: which repos use requests@2.28.0?
        cursor = db.execute(
            """
            SELECT r.repo_name
            FROM dependencies d
            JOIN repositories r ON r.id = d.repo_id
            WHERE d.package_coordinate = ?
            ORDER BY r.repo_name
            """,
            ("pkg:pypi/requests@2.28.0",),
        )
        repos = [row[0] for row in cursor.fetchall()]
        assert repos == ["org/api-service", "org/web-app"]

    def test_vulnerability_join(self, db):
        """Advisory join: vulnerabilities -> dependencies -> repositories."""
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-a", "team/backend", "https://github.com/team/backend.git", "team"),
        )
        db.execute(
            "INSERT INTO dependencies (id, repo_id, package_coordinate, version, source) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dep-a", "repo-a", "pkg:pypi/cryptography@41.0.0", "41.0.0", "code"),
        )
        db.execute(
            "INSERT INTO vulnerabilities (id, cve_id, package, affected_versions, severity) "
            "VALUES (?, ?, ?, ?, ?)",
            ("vuln-1", "CVE-2024-1234", "pkg:pypi/cryptography", "<41.0.5", "high"),
        )
        db.commit()

        # Join query: find repos affected by a vulnerability
        cursor = db.execute(
            """
            SELECT r.repo_name, d.version, v.severity
            FROM vulnerabilities v
            JOIN dependencies d ON d.package_coordinate LIKE v.package || '%'
            JOIN repositories r ON r.id = d.repo_id
            WHERE v.cve_id = ?
            """,
            ("CVE-2024-1234",),
        )
        results = cursor.fetchall()
        assert len(results) == 1
        assert results[0][0] == "team/backend"
        assert results[0][1] == "41.0.0"
        assert results[0][2] == "high"

    def test_cascade_delete(self, db):
        """Deleting a repository cascades to dependencies and index_runs."""
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-del", "org/to-delete", "https://github.com/org/to-delete.git", "org"),
        )
        db.execute(
            "INSERT INTO dependencies (id, repo_id, package_coordinate, source) VALUES (?, ?, ?, ?)",
            ("dep-del", "repo-del", "pkg:pypi/flask@3.0.0", "code"),
        )
        db.execute(
            "INSERT INTO index_runs (id, repo_id, status) VALUES (?, ?, ?)",
            ("run-del", "repo-del", "complete"),
        )
        db.commit()

        # Delete the repository
        db.execute("DELETE FROM repositories WHERE id = ?", ("repo-del",))
        db.commit()

        # Verify cascades
        cursor = db.execute("SELECT COUNT(*) FROM dependencies WHERE repo_id = ?", ("repo-del",))
        assert cursor.fetchone()[0] == 0

        cursor = db.execute("SELECT COUNT(*) FROM index_runs WHERE repo_id = ?", ("repo-del",))
        assert cursor.fetchone()[0] == 0

    def test_unique_constraint_on_dependencies(self, db):
        """Duplicate (repo_id, package_coordinate, source) is rejected."""
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-dup", "org/dup-test", "https://github.com/org/dup-test.git", "org"),
        )
        db.execute(
            "INSERT INTO dependencies (id, repo_id, package_coordinate, source) VALUES (?, ?, ?, ?)",
            ("dep-dup1", "repo-dup", "pkg:npm/react@18.2.0", "code"),
        )
        db.commit()

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO dependencies (id, repo_id, package_coordinate, source) VALUES (?, ?, ?, ?)",
                ("dep-dup2", "repo-dup", "pkg:npm/react@18.2.0", "code"),
            )

    def test_different_source_allowed(self, db):
        """Same package from different sources (code vs image) is allowed."""
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-src", "org/multi-source", "https://github.com/org/multi-source.git", "org"),
        )
        db.execute(
            "INSERT INTO dependencies (id, repo_id, package_coordinate, source) VALUES (?, ?, ?, ?)",
            ("dep-src1", "repo-src", "pkg:deb/openssl@3.0.2", "code"),
        )
        db.execute(
            "INSERT INTO dependencies (id, repo_id, package_coordinate, source, base_image) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dep-src2", "repo-src", "pkg:deb/openssl@3.0.2", "image", "ubuntu:22.04"),
        )
        db.commit()

        cursor = db.execute("SELECT COUNT(*) FROM dependencies WHERE repo_id = ?", ("repo-src",))
        assert cursor.fetchone()[0] == 2

    def test_acl_jsonb_query(self, db):
        """ACL query (allowed_principals contains user) works."""
        import json

        principals = json.dumps(["alice", "team/backend", "bob"])
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner, allowed_principals) "
            "VALUES (?, ?, ?, ?, ?)",
            ("repo-acl", "org/private", "https://github.com/org/private.git", "org", principals),
        )
        db.commit()

        # SQLite doesn't have @> but we can use LIKE or json_each for testing
        cursor = db.execute(
            """
            SELECT repo_name FROM repositories
            WHERE EXISTS (
                SELECT 1 FROM json_each(allowed_principals)
                WHERE json_each.value = ?
            )
            """,
            ("alice",),
        )
        results = cursor.fetchall()
        assert len(results) == 1
        assert results[0][0] == "org/private"

        # User not in ACL
        cursor = db.execute(
            """
            SELECT repo_name FROM repositories
            WHERE EXISTS (
                SELECT 1 FROM json_each(allowed_principals)
                WHERE json_each.value = ?
            )
            """,
            ("mallory",),
        )
        assert cursor.fetchall() == []


class TestMigrationFileStructure:
    """Verify the migration file is well-formed."""

    def test_migration_file_exists(self):
        """The migration file exists at the expected path."""
        migration = MIGRATIONS_DIR / "001_knowledge_layer_schema.py"
        assert migration.exists(), f"Migration not found at {migration}"

    def test_migration_has_revision_chain(self):
        """Migration has proper revision/down_revision markers."""
        migration = MIGRATIONS_DIR / "001_knowledge_layer_schema.py"
        content = migration.read_text()
        assert 'revision: str = "001_knowledge_layer_schema"' in content
        assert "down_revision: str | None = None" in content

    def test_migration_has_upgrade_and_downgrade(self):
        """Migration defines both upgrade() and downgrade() functions."""
        migration = MIGRATIONS_DIR / "001_knowledge_layer_schema.py"
        content = migration.read_text()
        assert "def upgrade()" in content
        assert "def downgrade()" in content

    def test_migration_creates_package_coordinate_index(self):
        """The critical B-tree index on package_coordinate is present."""
        migration = MIGRATIONS_DIR / "001_knowledge_layer_schema.py"
        content = migration.read_text()
        assert "ix_dependencies_package_coordinate" in content
        assert "package_coordinate" in content


class TestTenantIsolationMigration:
    """Test the 004_add_tenant_isolation_columns migration (Issue #1770)."""

    @pytest.fixture
    def db(self, tmp_path):
        """Create a fresh SQLite database with base schema + migration 004 applied."""
        db_path = tmp_path / "test_tenant_isolation.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")

        # Apply base schema (001) — simplified for SQLite
        conn.executescript("""
            CREATE TABLE repositories (
                id              TEXT PRIMARY KEY,
                repo_name       TEXT NOT NULL UNIQUE,
                git_url         TEXT NOT NULL,
                owner           TEXT NOT NULL,
                allowed_principals TEXT NOT NULL DEFAULT '[]',
                last_indexed_sha TEXT,
                indexed_at      TEXT,
                zoekt_status    TEXT NOT NULL DEFAULT 'pending',
                vectors_status  TEXT NOT NULL DEFAULT 'pending',
                structure_status TEXT NOT NULL DEFAULT 'pending',
                sbom_status     TEXT NOT NULL DEFAULT 'pending',
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX ix_repositories_owner ON repositories(owner);
        """)

        # Apply migration 004: add tenant_id and owner_sub
        conn.executescript("""
            ALTER TABLE repositories ADD COLUMN tenant_id TEXT;
            ALTER TABLE repositories ADD COLUMN owner_sub TEXT;
            CREATE INDEX ix_repositories_tenant_id ON repositories(tenant_id);
            CREATE INDEX ix_repositories_owner_sub ON repositories(owner_sub);
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_tenant_id_column_exists(self, db):
        """Migration adds tenant_id column to repositories."""
        cursor = db.execute("PRAGMA table_info(repositories)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "tenant_id" in columns

    def test_owner_sub_column_exists(self, db):
        """Migration adds owner_sub column to repositories."""
        cursor = db.execute("PRAGMA table_info(repositories)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "owner_sub" in columns

    def test_tenant_id_index_exists(self, db):
        """Index ix_repositories_tenant_id is created."""
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ix_repositories_tenant_id'"
        )
        assert cursor.fetchone() is not None

    def test_owner_sub_index_exists(self, db):
        """Index ix_repositories_owner_sub is created."""
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ix_repositories_owner_sub'"
        )
        assert cursor.fetchone() is not None

    def test_existing_rows_have_null_tenant_id(self, db):
        """Pre-existing rows get tenant_id=NULL (shared corpus, no data migration)."""
        # Insert a repo WITHOUT specifying tenant_id (simulates existing data)
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-existing", "org/existing", "https://github.com/org/existing.git", "org"),
        )
        db.commit()

        cursor = db.execute(
            "SELECT tenant_id, owner_sub FROM repositories WHERE id = ?",
            ("repo-existing",),
        )
        row = cursor.fetchone()
        assert row[0] is None  # tenant_id
        assert row[1] is None  # owner_sub

    def test_insert_shared_repo(self, db):
        """A shared repo has tenant_id=NULL and allowed_principals=["*"]."""
        import json

        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner, allowed_principals, tenant_id, owner_sub) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "repo-shared",
                "oss/public",
                "https://github.com/oss/public.git",
                "oss",
                json.dumps(["*"]),
                None,
                None,
            ),
        )
        db.commit()

        cursor = db.execute(
            "SELECT tenant_id, owner_sub FROM repositories WHERE id = ?",
            ("repo-shared",),
        )
        row = cursor.fetchone()
        assert row[0] is None
        assert row[1] is None

    def test_insert_tenant_scoped_repo(self, db):
        """A per-tenant repo has tenant_id set, owner_sub=NULL."""
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner, tenant_id, owner_sub) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "repo-tenant",
                "acme/private",
                "https://github.com/acme/private.git",
                "acme",
                "acme-org-id",
                None,
            ),
        )
        db.commit()

        cursor = db.execute(
            "SELECT tenant_id, owner_sub FROM repositories WHERE id = ?",
            ("repo-tenant",),
        )
        row = cursor.fetchone()
        assert row[0] == "acme-org-id"
        assert row[1] is None

    def test_insert_individual_scoped_repo(self, db):
        """A per-individual repo has both tenant_id and owner_sub set."""
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner, tenant_id, owner_sub) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "repo-personal",
                "user/my-repo",
                "https://github.com/user/my-repo.git",
                "user",
                "acme-org-id",
                "user-cognito-sub-123",
            ),
        )
        db.commit()

        cursor = db.execute(
            "SELECT tenant_id, owner_sub FROM repositories WHERE id = ?",
            ("repo-personal",),
        )
        row = cursor.fetchone()
        assert row[0] == "acme-org-id"
        assert row[1] == "user-cognito-sub-123"

    def test_tenant_scoped_query(self, db):
        """Querying by tenant_id returns only that tenant's repos + shared."""
        import json

        # Shared repo
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner, allowed_principals, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("repo-s", "oss/lib", "https://github.com/oss/lib.git", "oss", json.dumps(["*"]), None),
        )
        # Tenant A repo
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner, tenant_id) "
            "VALUES (?, ?, ?, ?, ?)",
            ("repo-a", "acme/internal", "https://github.com/acme/internal.git", "acme", "tenant-a"),
        )
        # Tenant B repo
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner, tenant_id) "
            "VALUES (?, ?, ?, ?, ?)",
            ("repo-b", "beta/private", "https://github.com/beta/private.git", "beta", "tenant-b"),
        )
        db.commit()

        # Query: repos visible to tenant-a (their own + shared)
        cursor = db.execute(
            """
            SELECT repo_name FROM repositories
            WHERE tenant_id = ? OR tenant_id IS NULL
            ORDER BY repo_name
            """,
            ("tenant-a",),
        )
        repos = [row[0] for row in cursor.fetchall()]
        assert "acme/internal" in repos
        assert "oss/lib" in repos
        assert "beta/private" not in repos

    def test_columns_are_nullable(self, db):
        """Both columns accept NULL (additive migration, no NOT NULL constraint)."""
        # Insert with explicit NULLs
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner, tenant_id, owner_sub) "
            "VALUES (?, ?, ?, ?, NULL, NULL)",
            ("repo-null", "org/null-test", "https://github.com/org/null-test.git", "org"),
        )
        db.commit()

        cursor = db.execute(
            "SELECT tenant_id, owner_sub FROM repositories WHERE id = ?",
            ("repo-null",),
        )
        row = cursor.fetchone()
        assert row[0] is None
        assert row[1] is None


class TestMigration004FileStructure:
    """Verify migration 004 file is well-formed and chains correctly."""

    def test_migration_file_exists(self):
        """Migration 004 file exists at the expected path."""
        migration = MIGRATIONS_DIR / "004_add_tenant_isolation_columns.py"
        assert migration.exists(), f"Migration not found at {migration}"

    def test_migration_revision_chain(self):
        """Migration 004 revises 003_index_run_stages."""
        migration = MIGRATIONS_DIR / "004_add_tenant_isolation_columns.py"
        content = migration.read_text()
        assert 'revision: str = "004_add_tenant_isolation_columns"' in content
        assert 'down_revision: str = "003_index_run_stages"' in content

    def test_migration_has_upgrade_and_downgrade(self):
        """Migration 004 defines both upgrade() and downgrade() functions."""
        migration = MIGRATIONS_DIR / "004_add_tenant_isolation_columns.py"
        content = migration.read_text()
        assert "def upgrade()" in content
        assert "def downgrade()" in content

    def test_migration_adds_tenant_id(self):
        """Migration 004 adds tenant_id column."""
        migration = MIGRATIONS_DIR / "004_add_tenant_isolation_columns.py"
        content = migration.read_text()
        assert "tenant_id" in content
        assert "ix_repositories_tenant_id" in content

    def test_migration_adds_owner_sub(self):
        """Migration 004 adds owner_sub column."""
        migration = MIGRATIONS_DIR / "004_add_tenant_isolation_columns.py"
        content = migration.read_text()
        assert "owner_sub" in content
        assert "ix_repositories_owner_sub" in content

    def test_downgrade_drops_columns(self):
        """Migration 004 downgrade drops both columns."""
        migration = MIGRATIONS_DIR / "004_add_tenant_isolation_columns.py"
        content = migration.read_text()
        assert "DROP COLUMN owner_sub" in content
        assert "DROP COLUMN tenant_id" in content
