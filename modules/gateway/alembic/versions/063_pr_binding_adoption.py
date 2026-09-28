"""Represent historical delivery without inventing a dispatched run."""

import sqlalchemy as sa

from alembic import op

revision = "063_pr_binding_adoption"
down_revision = "062_persona_usage_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("orchestration_pr_bindings") as batch:
        batch.alter_column("run_id", existing_type=sa.String(255), nullable=True)


def downgrade() -> None:
    # Refuse downgrade while real historical adoptions exist. Never manufacture
    # run identities or discard their provenance to satisfy the old constraint.
    with op.batch_alter_table("orchestration_pr_bindings") as batch:
        batch.alter_column("run_id", existing_type=sa.String(255), nullable=False)
