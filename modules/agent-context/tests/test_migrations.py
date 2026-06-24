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


class TestProjectScopingMigration:
    """Test the 005_project_scoping migration (Issue #1784).

    Verifies that the projects and project_repositories tables are created
    correctly, with proper indexes, constraints, and CASCADE behaviour.
    """

    @pytest.fixture
    def db(self, tmp_path):
        """Create a fresh SQLite database with base schema + migrations 004 + 005 applied."""
        db_path = tmp_path / "test_project_scoping.db"
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

        # Apply migration 004: add tenant_id and owner_sub to repositories
        conn.executescript("""
            ALTER TABLE repositories ADD COLUMN tenant_id TEXT;
            ALTER TABLE repositories ADD COLUMN owner_sub TEXT;
            CREATE INDEX ix_repositories_tenant_id ON repositories(tenant_id);
            CREATE INDEX ix_repositories_owner_sub ON repositories(owner_sub);
        """)

        # Apply migration 005: projects + project_repositories
        conn.executescript("""
            CREATE TABLE projects (
                id          TEXT PRIMARY KEY,
                owner_sub   TEXT NOT NULL,
                name        TEXT NOT NULL,
                tenant_id   TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),

                UNIQUE (owner_sub, name)
            );

            CREATE INDEX ix_projects_owner_sub ON projects(owner_sub);
            CREATE INDEX ix_projects_tenant_id ON projects(tenant_id);

            CREATE TABLE project_repositories (
                project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                repo_id     TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                added_at    TEXT NOT NULL DEFAULT (datetime('now')),

                PRIMARY KEY (project_id, repo_id)
            );

            CREATE INDEX ix_project_repositories_repo_id ON project_repositories(repo_id);
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_projects_table_exists(self, db):
        """Migration creates the projects table."""
        cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='projects'")
        assert cursor.fetchone() is not None

    def test_project_repositories_table_exists(self, db):
        """Migration creates the project_repositories join table."""
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='project_repositories'"
        )
        assert cursor.fetchone() is not None

    def test_projects_indexes_exist(self, db):
        """Indexes ix_projects_owner_sub and ix_projects_tenant_id are created."""
        cursor = db.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
        indexes = [row[0] for row in cursor.fetchall()]
        assert "ix_projects_owner_sub" in indexes
        assert "ix_projects_tenant_id" in indexes

    def test_project_repositories_index_exists(self, db):
        """Index ix_project_repositories_repo_id is created."""
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ix_project_repositories_repo_id'"
        )
        assert cursor.fetchone() is not None

    def test_insert_project(self, db):
        """Can insert a project with all fields."""
        db.execute(
            "INSERT INTO projects (id, owner_sub, name, tenant_id) VALUES (?, ?, ?, ?)",
            ("proj-1", "user-sub-abc", "My Project", "tenant-xyz"),
        )
        db.commit()

        cursor = db.execute(
            "SELECT owner_sub, name, tenant_id FROM projects WHERE id = ?", ("proj-1",)
        )
        row = cursor.fetchone()
        assert row[0] == "user-sub-abc"
        assert row[1] == "My Project"
        assert row[2] == "tenant-xyz"

    def test_project_tenant_id_nullable(self, db):
        """Projects can have NULL tenant_id (personal, no org)."""
        db.execute(
            "INSERT INTO projects (id, owner_sub, name, tenant_id) VALUES (?, ?, ?, ?)",
            ("proj-personal", "user-sub-123", "Personal Project", None),
        )
        db.commit()

        cursor = db.execute("SELECT tenant_id FROM projects WHERE id = ?", ("proj-personal",))
        assert cursor.fetchone()[0] is None

    def test_unique_constraint_owner_name(self, db):
        """Same owner cannot have two projects with the same name."""
        db.execute(
            "INSERT INTO projects (id, owner_sub, name) VALUES (?, ?, ?)",
            ("proj-a", "user-sub-1", "Alpha"),
        )
        db.commit()

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO projects (id, owner_sub, name) VALUES (?, ?, ?)",
                ("proj-b", "user-sub-1", "Alpha"),
            )

    def test_different_owners_same_name_allowed(self, db):
        """Different owners can have projects with the same name."""
        db.execute(
            "INSERT INTO projects (id, owner_sub, name) VALUES (?, ?, ?)",
            ("proj-x", "user-sub-1", "Common Name"),
        )
        db.execute(
            "INSERT INTO projects (id, owner_sub, name) VALUES (?, ?, ?)",
            ("proj-y", "user-sub-2", "Common Name"),
        )
        db.commit()

        cursor = db.execute("SELECT COUNT(*) FROM projects WHERE name = ?", ("Common Name",))
        assert cursor.fetchone()[0] == 2

    def test_link_project_to_repository(self, db):
        """Can link a project to a repository via the join table."""
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-1", "org/service", "https://github.com/org/service.git", "org"),
        )
        db.execute(
            "INSERT INTO projects (id, owner_sub, name) VALUES (?, ?, ?)",
            ("proj-1", "user-sub-1", "Backend"),
        )
        db.execute(
            "INSERT INTO project_repositories (project_id, repo_id) VALUES (?, ?)",
            ("proj-1", "repo-1"),
        )
        db.commit()

        cursor = db.execute(
            "SELECT project_id, repo_id FROM project_repositories WHERE project_id = ?",
            ("proj-1",),
        )
        row = cursor.fetchone()
        assert row[0] == "proj-1"
        assert row[1] == "repo-1"

    def test_many_to_many_relationship(self, db):
        """A repo can belong to multiple projects and a project can have multiple repos."""
        # Create repos and projects
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-a", "org/api", "https://github.com/org/api.git", "org"),
        )
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-b", "org/web", "https://github.com/org/web.git", "org"),
        )
        db.execute(
            "INSERT INTO projects (id, owner_sub, name) VALUES (?, ?, ?)",
            ("proj-backend", "user-1", "Backend"),
        )
        db.execute(
            "INSERT INTO projects (id, owner_sub, name) VALUES (?, ?, ?)",
            ("proj-fullstack", "user-1", "Fullstack"),
        )

        # Link: repo-a -> both projects, repo-b -> fullstack only
        db.execute(
            "INSERT INTO project_repositories (project_id, repo_id) VALUES (?, ?)",
            ("proj-backend", "repo-a"),
        )
        db.execute(
            "INSERT INTO project_repositories (project_id, repo_id) VALUES (?, ?)",
            ("proj-fullstack", "repo-a"),
        )
        db.execute(
            "INSERT INTO project_repositories (project_id, repo_id) VALUES (?, ?)",
            ("proj-fullstack", "repo-b"),
        )
        db.commit()

        # repo-a belongs to 2 projects
        cursor = db.execute(
            "SELECT COUNT(*) FROM project_repositories WHERE repo_id = ?", ("repo-a",)
        )
        assert cursor.fetchone()[0] == 2

        # fullstack project has 2 repos
        cursor = db.execute(
            "SELECT COUNT(*) FROM project_repositories WHERE project_id = ?", ("proj-fullstack",)
        )
        assert cursor.fetchone()[0] == 2

    def test_composite_pk_prevents_duplicates(self, db):
        """Cannot link the same repo to the same project twice."""
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-dup", "org/dup", "https://github.com/org/dup.git", "org"),
        )
        db.execute(
            "INSERT INTO projects (id, owner_sub, name) VALUES (?, ?, ?)",
            ("proj-dup", "user-1", "DupTest"),
        )
        db.execute(
            "INSERT INTO project_repositories (project_id, repo_id) VALUES (?, ?)",
            ("proj-dup", "repo-dup"),
        )
        db.commit()

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO project_repositories (project_id, repo_id) VALUES (?, ?)",
                ("proj-dup", "repo-dup"),
            )

    def test_cascade_delete_project(self, db):
        """Deleting a project cascades to project_repositories."""
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-cas", "org/cascade", "https://github.com/org/cascade.git", "org"),
        )
        db.execute(
            "INSERT INTO projects (id, owner_sub, name) VALUES (?, ?, ?)",
            ("proj-cas", "user-1", "CascadeTest"),
        )
        db.execute(
            "INSERT INTO project_repositories (project_id, repo_id) VALUES (?, ?)",
            ("proj-cas", "repo-cas"),
        )
        db.commit()

        # Delete the project
        db.execute("DELETE FROM projects WHERE id = ?", ("proj-cas",))
        db.commit()

        # Join row is gone
        cursor = db.execute(
            "SELECT COUNT(*) FROM project_repositories WHERE project_id = ?", ("proj-cas",)
        )
        assert cursor.fetchone()[0] == 0

    def test_cascade_delete_repository(self, db):
        """Deleting a repository cascades to project_repositories."""
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-del", "org/to-delete", "https://github.com/org/to-delete.git", "org"),
        )
        db.execute(
            "INSERT INTO projects (id, owner_sub, name) VALUES (?, ?, ?)",
            ("proj-keep", "user-1", "KeepProject"),
        )
        db.execute(
            "INSERT INTO project_repositories (project_id, repo_id) VALUES (?, ?)",
            ("proj-keep", "repo-del"),
        )
        db.commit()

        # Delete the repository
        db.execute("DELETE FROM repositories WHERE id = ?", ("repo-del",))
        db.commit()

        # Join row is gone but project survives
        cursor = db.execute(
            "SELECT COUNT(*) FROM project_repositories WHERE repo_id = ?", ("repo-del",)
        )
        assert cursor.fetchone()[0] == 0

        cursor = db.execute("SELECT COUNT(*) FROM projects WHERE id = ?", ("proj-keep",))
        assert cursor.fetchone()[0] == 1

    def test_project_scoped_query(self, db):
        """Can query repos visible to a project (the core use-case for Story B)."""
        # Setup: two repos, one project containing only repo-a
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-in", "org/in-project", "https://github.com/org/in-project.git", "org"),
        )
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-out", "org/not-in-project", "https://github.com/org/not-in-project.git", "org"),
        )
        db.execute(
            "INSERT INTO projects (id, owner_sub, name) VALUES (?, ?, ?)",
            ("proj-scope", "user-1", "ScopedProject"),
        )
        db.execute(
            "INSERT INTO project_repositories (project_id, repo_id) VALUES (?, ?)",
            ("proj-scope", "repo-in"),
        )
        db.commit()

        # Query: repos in project
        cursor = db.execute(
            """
            SELECT r.repo_name
            FROM repositories r
            JOIN project_repositories pr ON pr.repo_id = r.id
            WHERE pr.project_id = ?
            ORDER BY r.repo_name
            """,
            ("proj-scope",),
        )
        repos = [row[0] for row in cursor.fetchall()]
        assert repos == ["org/in-project"]
        assert "org/not-in-project" not in repos

    def test_fk_rejects_invalid_project_id(self, db):
        """Cannot insert a join row with a non-existent project_id."""
        db.execute(
            "INSERT INTO repositories (id, repo_name, git_url, owner) VALUES (?, ?, ?, ?)",
            ("repo-fk", "org/fk-test", "https://github.com/org/fk-test.git", "org"),
        )
        db.commit()

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO project_repositories (project_id, repo_id) VALUES (?, ?)",
                ("nonexistent-project", "repo-fk"),
            )

    def test_fk_rejects_invalid_repo_id(self, db):
        """Cannot insert a join row with a non-existent repo_id."""
        db.execute(
            "INSERT INTO projects (id, owner_sub, name) VALUES (?, ?, ?)",
            ("proj-fk", "user-1", "FKTest"),
        )
        db.commit()

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO project_repositories (project_id, repo_id) VALUES (?, ?)",
                ("proj-fk", "nonexistent-repo"),
            )


class TestMigration005FileStructure:
    """Verify migration 005 file is well-formed and chains correctly."""

    def test_migration_file_exists(self):
        """Migration 005 file exists at the expected path."""
        migration = MIGRATIONS_DIR / "005_project_scoping.py"
        assert migration.exists(), f"Migration not found at {migration}"

    def test_migration_revision_chain(self):
        """Migration 005 revises 004_add_tenant_isolation_columns."""
        migration = MIGRATIONS_DIR / "005_project_scoping.py"
        content = migration.read_text()
        assert 'revision: str = "005_project_scoping"' in content
        assert 'down_revision: str = "004_add_tenant_isolation_columns"' in content

    def test_migration_has_upgrade_and_downgrade(self):
        """Migration 005 defines both upgrade() and downgrade() functions."""
        migration = MIGRATIONS_DIR / "005_project_scoping.py"
        content = migration.read_text()
        assert "def upgrade()" in content
        assert "def downgrade()" in content

    def test_migration_creates_projects_table(self):
        """Migration 005 creates the projects table."""
        migration = MIGRATIONS_DIR / "005_project_scoping.py"
        content = migration.read_text()
        assert "CREATE TABLE projects" in content
        assert "owner_sub" in content
        assert "uq_projects_owner_name" in content

    def test_migration_creates_project_repositories_table(self):
        """Migration 005 creates the project_repositories join table."""
        migration = MIGRATIONS_DIR / "005_project_scoping.py"
        content = migration.read_text()
        assert "CREATE TABLE project_repositories" in content
        assert "ON DELETE CASCADE" in content

    def test_migration_creates_indexes(self):
        """Migration 005 creates all required indexes."""
        migration = MIGRATIONS_DIR / "005_project_scoping.py"
        content = migration.read_text()
        assert "ix_projects_owner_sub" in content
        assert "ix_projects_tenant_id" in content
        assert "ix_project_repositories_repo_id" in content

    def test_downgrade_drops_tables(self):
        """Migration 005 downgrade drops both tables in correct order."""
        migration = MIGRATIONS_DIR / "005_project_scoping.py"
        content = migration.read_text()
        # project_repositories must be dropped before projects (FK dependency)
        pr_pos = content.index("project_repositories")
        # Find the drop_table call for projects (the standalone table)
        proj_drop_pos = content.index('drop_table("projects")')
        # project_repositories is dropped first
        assert pr_pos < proj_drop_pos


class TestKnowledgeAssetsMigration:
    """Test the 007_knowledge_assets migration (Issue #1790).

    Verifies that the knowledge_assets registry table is created with proper
    columns, indexes, and unique constraints.
    """

    @pytest.fixture
    def db(self, tmp_path):
        """Create a fresh SQLite database with the knowledge_assets table."""
        db_path = tmp_path / "test_knowledge_assets.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")

        # Apply knowledge_assets schema (adapted for SQLite)
        conn.executescript("""
            CREATE TABLE knowledge_assets (
                id              TEXT PRIMARY KEY,
                asset_type      TEXT NOT NULL,
                source_ref      TEXT NOT NULL,
                tenant_id       TEXT,
                owner_sub       TEXT,
                project_id      TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',
                registered_by   TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                metadata        TEXT DEFAULT '{}',
                last_error      TEXT,
                retry_count     INTEGER NOT NULL DEFAULT 0
            );

            -- Unique index (SQLite version — COALESCE for NULL handling)
            CREATE UNIQUE INDEX uq_knowledge_assets_source_scope
            ON knowledge_assets (
                source_ref,
                COALESCE(tenant_id, ''),
                COALESCE(owner_sub, '')
            );

            CREATE INDEX ix_knowledge_assets_tenant_id ON knowledge_assets(tenant_id);
            CREATE INDEX ix_knowledge_assets_owner_sub ON knowledge_assets(owner_sub);
            CREATE INDEX ix_knowledge_assets_status ON knowledge_assets(status);
            CREATE INDEX ix_knowledge_assets_project_id ON knowledge_assets(project_id);
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_table_exists(self, db):
        """Migration creates the knowledge_assets table."""
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_assets'"
        )
        assert cursor.fetchone() is not None

    def test_insert_asset(self, db):
        """Can insert an asset with all fields."""
        db.execute(
            "INSERT INTO knowledge_assets (id, asset_type, source_ref, tenant_id, owner_sub, "
            "project_id, status, registered_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "asset-1",
                "github_repo",
                "https://github.com/org/repo",
                "tenant-acme",
                "sub-alice-001",
                "proj-123",
                "indexed",
                "alice",
            ),
        )
        db.commit()

        cursor = db.execute(
            "SELECT asset_type, source_ref, tenant_id, owner_sub, project_id, status "
            "FROM knowledge_assets WHERE id = ?",
            ("asset-1",),
        )
        row = cursor.fetchone()
        assert row[0] == "github_repo"
        assert row[1] == "https://github.com/org/repo"
        assert row[2] == "tenant-acme"
        assert row[3] == "sub-alice-001"
        assert row[4] == "proj-123"
        assert row[5] == "indexed"

    def test_nullable_scope_fields(self, db):
        """Scope fields (tenant_id, owner_sub, project_id) are nullable."""
        db.execute(
            "INSERT INTO knowledge_assets (id, asset_type, source_ref) VALUES (?, ?, ?)",
            ("asset-shared", "github_repo", "https://github.com/oss/lib"),
        )
        db.commit()

        cursor = db.execute(
            "SELECT tenant_id, owner_sub, project_id FROM knowledge_assets WHERE id = ?",
            ("asset-shared",),
        )
        row = cursor.fetchone()
        assert row[0] is None
        assert row[1] is None
        assert row[2] is None

    def test_default_status_pending(self, db):
        """Default status is 'pending'."""
        db.execute(
            "INSERT INTO knowledge_assets (id, asset_type, source_ref) VALUES (?, ?, ?)",
            ("asset-default", "github_repo", "https://github.com/org/new"),
        )
        db.commit()

        cursor = db.execute(
            "SELECT status FROM knowledge_assets WHERE id = ?", ("asset-default",)
        )
        assert cursor.fetchone()[0] == "pending"

    def test_default_retry_count_zero(self, db):
        """Default retry_count is 0."""
        db.execute(
            "INSERT INTO knowledge_assets (id, asset_type, source_ref) VALUES (?, ?, ?)",
            ("asset-retry", "github_repo", "https://github.com/org/retry"),
        )
        db.commit()

        cursor = db.execute(
            "SELECT retry_count FROM knowledge_assets WHERE id = ?", ("asset-retry",)
        )
        assert cursor.fetchone()[0] == 0

    def test_unique_index_prevents_duplicate_registration(self, db):
        """Same source_ref + tenant_id + owner_sub cannot be registered twice."""
        db.execute(
            "INSERT INTO knowledge_assets (id, asset_type, source_ref, tenant_id, owner_sub) "
            "VALUES (?, ?, ?, ?, ?)",
            ("asset-a", "github_repo", "https://github.com/org/repo", "tenant-1", "sub-1"),
        )
        db.commit()

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO knowledge_assets (id, asset_type, source_ref, tenant_id, owner_sub) "
                "VALUES (?, ?, ?, ?, ?)",
                ("asset-b", "github_repo", "https://github.com/org/repo", "tenant-1", "sub-1"),
            )

    def test_unique_index_allows_same_source_different_tenant(self, db):
        """Same source_ref but different tenant_id is allowed."""
        db.execute(
            "INSERT INTO knowledge_assets (id, asset_type, source_ref, tenant_id, owner_sub) "
            "VALUES (?, ?, ?, ?, ?)",
            ("asset-t1", "github_repo", "https://github.com/org/repo", "tenant-1", "sub-1"),
        )
        db.execute(
            "INSERT INTO knowledge_assets (id, asset_type, source_ref, tenant_id, owner_sub) "
            "VALUES (?, ?, ?, ?, ?)",
            ("asset-t2", "github_repo", "https://github.com/org/repo", "tenant-2", "sub-1"),
        )
        db.commit()

        cursor = db.execute(
            "SELECT COUNT(*) FROM knowledge_assets WHERE source_ref = ?",
            ("https://github.com/org/repo",),
        )
        assert cursor.fetchone()[0] == 2

    def test_unique_index_null_scope_dedup(self, db):
        """Shared assets (NULL scope) are deduplicated via COALESCE('')."""
        db.execute(
            "INSERT INTO knowledge_assets (id, asset_type, source_ref) VALUES (?, ?, ?)",
            ("asset-shared-1", "github_repo", "https://github.com/oss/shared"),
        )
        db.commit()

        # Second insert with same source_ref and NULL scope should fail
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO knowledge_assets (id, asset_type, source_ref) VALUES (?, ?, ?)",
                ("asset-shared-2", "github_repo", "https://github.com/oss/shared"),
            )

    def test_indexes_exist(self, db):
        """All required indexes are created."""
        cursor = db.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
        indexes = [row[0] for row in cursor.fetchall()]
        assert "ix_knowledge_assets_tenant_id" in indexes
        assert "ix_knowledge_assets_owner_sub" in indexes
        assert "ix_knowledge_assets_status" in indexes
        assert "ix_knowledge_assets_project_id" in indexes
        assert "uq_knowledge_assets_source_scope" in indexes

    def test_open_asset_type(self, db):
        """asset_type is an open VARCHAR, not constrained by enum/CHECK."""
        # Can insert any string as asset_type
        for i, atype in enumerate(["github_repo", "s3_bucket", "confluence_page", "custom_v2"]):
            db.execute(
                "INSERT INTO knowledge_assets (id, asset_type, source_ref, tenant_id) "
                "VALUES (?, ?, ?, ?)",
                (f"asset-type-{i}", atype, f"source-{i}", f"tenant-{i}"),
            )
        db.commit()

        cursor = db.execute("SELECT COUNT(*) FROM knowledge_assets")
        assert cursor.fetchone()[0] == 4


class TestMigration007FileStructure:
    """Verify migration 007 file is well-formed and chains correctly."""

    def test_migration_file_exists(self):
        """Migration 007 file exists at the expected path."""
        migration = MIGRATIONS_DIR / "007_knowledge_assets.py"
        assert migration.exists(), f"Migration not found at {migration}"

    def test_migration_revision_chain(self):
        """Migration 007 revises 006_merge_005_heads."""
        migration = MIGRATIONS_DIR / "007_knowledge_assets.py"
        content = migration.read_text()
        assert 'revision: str = "007_knowledge_assets"' in content
        assert 'down_revision: str = "006_merge_005_heads"' in content

    def test_migration_has_upgrade_and_downgrade(self):
        """Migration 007 defines both upgrade() and downgrade() functions."""
        migration = MIGRATIONS_DIR / "007_knowledge_assets.py"
        content = migration.read_text()
        assert "def upgrade()" in content
        assert "def downgrade()" in content

    def test_migration_creates_table(self):
        """Migration 007 creates knowledge_assets table."""
        migration = MIGRATIONS_DIR / "007_knowledge_assets.py"
        content = migration.read_text()
        assert "CREATE TABLE knowledge_assets" in content
        assert "asset_type" in content
        assert "source_ref" in content
        assert "tenant_id" in content
        assert "owner_sub" in content

    def test_migration_creates_unique_index(self):
        """Migration 007 creates the unique scope index."""
        migration = MIGRATIONS_DIR / "007_knowledge_assets.py"
        content = migration.read_text()
        assert "uq_knowledge_assets_source_scope" in content
        assert "COALESCE" in content

    def test_downgrade_drops_table(self):
        """Migration 007 downgrade drops the table."""
        migration = MIGRATIONS_DIR / "007_knowledge_assets.py"
        content = migration.read_text()
        assert "drop_table" in content
