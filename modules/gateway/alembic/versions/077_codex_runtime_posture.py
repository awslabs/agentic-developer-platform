"""Provision the native Codex runtime's model-policy settings.

The original preference migration seeded only Claude. Native Codex workers
cannot bootstrap without their own live posture row. Require an admissible
model selection; leave platform model defaults unset until separately promoted.
"""

from alembic import op
import sqlalchemy as sa

revision = "077_codex_runtime_posture"
down_revision = "076_access_request_receipts"
branch_labels = None
depends_on = None


def upgrade():
    # Preserve an already provisioned class and any audited operator choices.
    op.execute(sa.text("""
        INSERT INTO persona_model_policy_settings
            (compatibility_class, enforcement_posture)
        VALUES ('codex-sdk', 'enforcing')
        ON CONFLICT (compatibility_class) DO NOTHING
    """))


def downgrade():
    # Older gateways already understand this class. Retain its policy, which
    # may now hold audited defaults or operator changes, across code rollback.
    pass
