"""Normalize saved Product environment references without advancing source revisions."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op
from src.products.configuration import normalize_environments

revision = "141_normalize_environments"
down_revision = "140_merge_adapter_sources"
branch_labels = None
depends_on = None


def upgrade():
    projects = sa.table(
        "projects",
        sa.column("tenant_id", sa.String()),
        sa.column("id", sa.String()),
        sa.column("configuration", sa.JSON().with_variant(JSONB(), "postgresql")),
    )
    connection = op.get_bind()
    for tenant_id, project_id, configuration in connection.execute(sa.select(projects.c.tenant_id, projects.c.id, projects.c.configuration)):
        normalized = normalize_environments(configuration or {})
        if normalized != configuration:
            connection.execute(
                sa.update(projects)
                .where(projects.c.tenant_id == tenant_id, projects.c.id == project_id)
                .values(configuration=normalized)
            )


def downgrade():
    raise RuntimeError("Environment normalization retains source revisions and cannot safely be reversed")
