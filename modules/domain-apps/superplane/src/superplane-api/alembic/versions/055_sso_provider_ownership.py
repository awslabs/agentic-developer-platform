"""Keep a separately reconciled, exact Cognito provider owner binding."""

import sqlalchemy as sa
from alembic import op

revision = "055_sso_provider_ownership"
down_revision = "054_unstarted_bootstrap"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "organizations",
        sa.Column("sso_cognito_provider_name", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_organizations_sso_cognito_provider_name",
        "organizations",
        ["sso_cognito_provider_name"],
        unique=True,
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM organizations WHERE sso_cognito_provider_name IS NOT NULL)"
            )
        )
        .scalar()
    ):
        raise RuntimeError(
            "reconcile or preserve SSO provider bindings before downgrade"
        )
    op.drop_index(
        "ix_organizations_sso_cognito_provider_name", table_name="organizations"
    )
    op.drop_column("organizations", "sso_cognito_provider_name")
