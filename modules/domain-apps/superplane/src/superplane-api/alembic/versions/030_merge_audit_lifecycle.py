"""Join published lifecycle control and deployment/audit schema histories."""

revision = "030_merge_audit_lifecycle"
down_revision = (
    "029_lifecycle_control_registry",
    "029_add_event_principal_outcome",
)
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
