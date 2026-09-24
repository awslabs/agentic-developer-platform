"""Bind AWS verification to a fenced attempt and immutable secret version."""

import sqlalchemy as sa

from alembic import op

revision = "069_aws_verification_binding"
down_revision = "068_merge_cli_flow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Older timestamps alone do not attest a particular secret version. Leave
    # evidence null so existing connections must verify again before use.
    for name, length in (
        ("aws_verification_attempt", 36),
        ("aws_verified_version_id", 64),
        ("aws_verified_binding", 64),
    ):
        op.add_column("user_credentials", sa.Column(name, sa.String(length), nullable=True))


def downgrade() -> None:
    op.drop_column("user_credentials", "aws_verified_binding")
    op.drop_column("user_credentials", "aws_verified_version_id")
    op.drop_column("user_credentials", "aws_verification_attempt")
