"""Permit registered instance-qualified GitLab identities for protected roots."""

from alembic import op

revision = "059_gitlab_identity_provider"
down_revision = "058_model_probe_admission"
branch_labels = None
depends_on = None

PREVIOUS_PROVIDERS = ("cognito", "github", "slack", "teams", "discord", "email", "whatsapp", "directory")
SUPPORTED_PROVIDERS = (*PREVIOUS_PROVIDERS, "gitlab")


def _replace(providers):
    if op.get_bind().dialect.name != "postgresql":
        return
    values = ", ".join(f"'{provider}'" for provider in providers)
    op.execute("ALTER TABLE user_identities DROP CONSTRAINT IF EXISTS ck_user_identities_provider")
    op.execute(f"ALTER TABLE user_identities ADD CONSTRAINT ck_user_identities_provider CHECK (provider IN ({values}))")


def upgrade():
    _replace(SUPPORTED_PROVIDERS)


def downgrade():
    # Refuse rollback if GitLab identities exist; never delete human identity.
    _replace(PREVIOUS_PROVIDERS)
