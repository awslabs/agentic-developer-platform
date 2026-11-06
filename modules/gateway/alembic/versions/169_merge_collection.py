"""Join collection installation authority and fenced service work histories."""

revision = "169_merge_collection"
down_revision = ("167_connector_installation", "168_merge_security_wa")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
