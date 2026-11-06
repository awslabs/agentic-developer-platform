"""Link work-scoped Security grants to revocable owner consent."""

import sqlalchemy as sa

from alembic import op

revision = "164_l5_work_delegation_parent"
down_revision = "163_merge_connector_cost"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("credential_service_delegations") as batch:
        batch.add_column(sa.Column("parent_delegation_id", sa.String(36),
                                   sa.ForeignKey("credential_service_delegations.id", name="fk_l5_work_delegation_parent")))
    op.create_index("ix_l5_work_delegation_parent", "credential_service_delegations", ["parent_delegation_id"])


def downgrade():
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed; retain work-scoped delegation and revocation evidence")
