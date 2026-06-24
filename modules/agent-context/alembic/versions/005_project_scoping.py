"""Add projects and project_repositories tables for M:N project scoping.

Issue #1784 (Story A of E9 #1728): creates the project registry that backs
project-scoped queries. Additive-only — no behaviour change until the Door
filter (Story B) consumes these tables.

Schema (per design-1728-project-scoping.md §2):
  - projects: user-owned project containers
  - project_repositories: M:N join linking projects to repositories

Revision ID: 005_project_scoping
Revises: 004_add_tenant_isolation_columns
Create Date: 2026-06-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "005_project_scoping"
down_revision: str = "004_add_tenant_isolation_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create projects and project_repositories tables with indexes."""
    # -------------------------------------------------------------------------
    # projects — user-owned project containers
    # -------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE projects (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_sub   VARCHAR(128) NOT NULL,
            name        VARCHAR(256) NOT NULL,
            tenant_id   VARCHAR(128),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT uq_projects_owner_name UNIQUE (owner_sub, name)
        )
    """)

    op.create_index("ix_projects_owner_sub", "projects", ["owner_sub"])
    op.create_index(
        "ix_projects_tenant_id",
        "projects",
        ["tenant_id"],
        postgresql_where="tenant_id IS NOT NULL",
    )

    # -------------------------------------------------------------------------
    # project_repositories — M:N join (composite PK, both FKs cascade)
    # -------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE project_repositories (
            project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            repo_id     UUID NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            PRIMARY KEY (project_id, repo_id)
        )
    """)

    op.create_index("ix_project_repositories_repo_id", "project_repositories", ["repo_id"])


def downgrade() -> None:
    """Drop project_repositories then projects (dependency order)."""
    op.drop_index("ix_project_repositories_repo_id", table_name="project_repositories")
    op.drop_table("project_repositories")

    op.drop_index("ix_projects_tenant_id", table_name="projects")
    op.drop_index("ix_projects_owner_sub", table_name="projects")
    op.drop_table("projects")
