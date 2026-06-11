"""Create Knowledge Layer schema: repositories, dependencies, vulnerabilities, index_runs.

Issue #1355: Initial schema for the agent_context database. Provides:
- Repository catalog with per-step indexing status and ACL
- Dependency reverse-index (package_coordinate -> repos)
- Vulnerability advisory records
- Append-only index run log for observability

Revision ID: 001_knowledge_layer_schema
Revises: None
Create Date: 2026-06-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "001_knowledge_layer_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all Knowledge Layer tables and indexes."""
    # pgcrypto for gen_random_uuid() — idempotent, safe without superuser on RDS
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # -------------------------------------------------------------------------
    # repositories — catalog of indexed repos with ACL and per-step status
    # -------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE repositories (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            repo_name         VARCHAR(512) NOT NULL UNIQUE,
            git_url           VARCHAR(1024) NOT NULL,
            owner             VARCHAR(256) NOT NULL,
            allowed_principals JSONB NOT NULL DEFAULT '[]'::jsonb,
            last_indexed_sha  VARCHAR(40),
            indexed_at        TIMESTAMPTZ,
            zoekt_status      VARCHAR(32) NOT NULL DEFAULT 'pending',
            vectors_status    VARCHAR(32) NOT NULL DEFAULT 'pending',
            structure_status  VARCHAR(32) NOT NULL DEFAULT 'pending',
            sbom_status       VARCHAR(32) NOT NULL DEFAULT 'pending',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.create_index("ix_repositories_owner", "repositories", ["owner"])

    # -------------------------------------------------------------------------
    # dependencies — reverse-index: package_coordinate -> repos
    # -------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE dependencies (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            repo_id             UUID NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            package_coordinate  VARCHAR(512) NOT NULL,
            version             VARCHAR(128),
            is_transitive       BOOLEAN NOT NULL DEFAULT FALSE,
            source              VARCHAR(16) NOT NULL DEFAULT 'code',
            base_image          VARCHAR(512),
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_dependencies_repo_pkg_source
                UNIQUE (repo_id, package_coordinate, source)
        )
    """)

    # The critical index: reverse lookup "which repos use package X?"
    op.create_index(
        "ix_dependencies_package_coordinate",
        "dependencies",
        ["package_coordinate"],
    )
    # FK index for efficient cascade deletes
    op.create_index("ix_dependencies_repo_id", "dependencies", ["repo_id"])

    # -------------------------------------------------------------------------
    # vulnerabilities — known advisories matched against indexed packages
    # -------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE vulnerabilities (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            cve_id            VARCHAR(64) NOT NULL UNIQUE,
            package           VARCHAR(512) NOT NULL,
            affected_versions VARCHAR(512) NOT NULL,
            safe_version      VARCHAR(128),
            severity          VARCHAR(16) NOT NULL DEFAULT 'unknown',
            details           JSONB,
            discovered_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.create_index("ix_vulnerabilities_package", "vulnerabilities", ["package"])
    op.create_index("ix_vulnerabilities_severity", "vulnerabilities", ["severity"])

    # -------------------------------------------------------------------------
    # index_runs — append-only observability log
    # -------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE index_runs (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            repo_id         UUID NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at    TIMESTAMPTZ,
            duration_ms     INTEGER,
            status          VARCHAR(32) NOT NULL DEFAULT 'running',
            error           TEXT,
            steps_completed JSONB DEFAULT '{}'::jsonb
        )
    """)

    op.create_index("ix_index_runs_repo_id", "index_runs", ["repo_id"])
    op.create_index("ix_index_runs_started_at", "index_runs", ["started_at"])


def downgrade() -> None:
    """Drop all Knowledge Layer tables in reverse dependency order."""
    op.drop_index("ix_index_runs_started_at", table_name="index_runs")
    op.drop_index("ix_index_runs_repo_id", table_name="index_runs")
    op.drop_table("index_runs")

    op.drop_index("ix_vulnerabilities_severity", table_name="vulnerabilities")
    op.drop_index("ix_vulnerabilities_package", table_name="vulnerabilities")
    op.drop_table("vulnerabilities")

    op.drop_index("ix_dependencies_repo_id", table_name="dependencies")
    op.drop_index("ix_dependencies_package_coordinate", table_name="dependencies")
    op.drop_table("dependencies")

    op.drop_index("ix_repositories_owner", table_name="repositories")
    op.drop_table("repositories")

    # Note: not dropping pgcrypto — other extensions may depend on it.
