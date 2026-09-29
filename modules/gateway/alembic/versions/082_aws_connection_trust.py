"""Require server-issued trust IDs for personal AWS connections.

Existing rows deliberately remain NULL and must be registered again. No trust
provenance can be inferred from caller-writable secrets or scopes.
"""

import sqlalchemy as sa

from alembic import op

revision = "082_aws_connection_trust"
down_revision = "081_persona_platform_defaults"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_credentials", sa.Column("aws_external_id", sa.String(36), nullable=True))
    # Do not leave a misleading verified badge or reusable pre-upgrade evidence.
    op.execute("""
        UPDATE user_credentials
        SET aws_verified_at = NULL, aws_verification_attempt = NULL,
            aws_verified_version_id = NULL, aws_verified_binding = NULL,
            scopes = ((COALESCE(scopes::jsonb, '{}'::jsonb) - 'verified_at' - 'routing_reason')
                || '{"status":"pending","routing_capable": false}'::jsonb)::json
        WHERE credential_type = 'aws_role'
    """)


def downgrade():
    op.drop_column("user_credentials", "aws_external_id")
