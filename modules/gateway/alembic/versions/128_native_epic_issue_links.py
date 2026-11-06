"""Persist separately verified native parent links for amendment issues.

Revision ID: 128_native_epic_issue_links
Revises: 127_authoring_assignment
"""

import sqlalchemy as sa

from alembic import op

revision = "128_native_epic_issue_links"
down_revision = "127_authoring_assignment"
branch_labels = None
depends_on = None

TABLE = "orchestration_amendment_issue_bindings"
INDEX = "uq_orchestration_amendment_issue_bindings_repo_id"


def upgrade() -> None:
    conflict = op.get_bind().execute(sa.text(
        f"SELECT org_id, provider_repository_id, proposal_identity FROM {TABLE} "
        "WHERE provider_repository_id IS NOT NULL "
        "GROUP BY org_id, provider_repository_id, proposal_identity HAVING count(*) > 1 LIMIT 1"
    )).first()
    if conflict is not None:
        raise RuntimeError("Conflicting provider repository publication identities require review before activation")
    op.add_column(TABLE, sa.Column("parent_repository_id", sa.BigInteger(), nullable=True))
    op.add_column(TABLE, sa.Column("parent_issue_id", sa.BigInteger(), nullable=True))
    op.add_column(TABLE, sa.Column("link_confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        INDEX, TABLE, ["org_id", "provider_repository_id", "proposal_identity"], unique=True,
        postgresql_where=sa.text("provider_repository_id IS NOT NULL"),
        sqlite_where=sa.text("provider_repository_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_column(TABLE, "link_confirmed_at")
    op.drop_column(TABLE, "parent_issue_id")
    op.drop_column(TABLE, "parent_repository_id")
