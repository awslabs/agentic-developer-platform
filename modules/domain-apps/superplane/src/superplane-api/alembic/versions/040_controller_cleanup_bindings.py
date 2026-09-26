"""Deduplicate separately approved cleanup of a fenced cancelled allocation."""

from alembic import op

revision = "040_controller_cleanup_bindings"
down_revision = "039_controller_node_commands"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE controller_cleanup_bindings (
 source_operation_id varchar(255) PRIMARY KEY,
 org_id varchar(255) NOT NULL,
 workspace_id varchar(255) NOT NULL,
 deployment_id varchar(36) NOT NULL,
 allocation_id varchar(255) NOT NULL,
 source_plan_digest varchar(64) NOT NULL,
 source_fence bigint NOT NULL CHECK (source_fence > 0),
 claim_holder varchar(255) NOT NULL,
 claim_attempt_id varchar(255) NOT NULL,
 claim_subject varchar(255) NOT NULL,
 cancel_requested_at timestamptz NOT NULL,
 cancel_requested_by varchar(255) NOT NULL,
 request_id varchar(36) NOT NULL,
 request_payload text NOT NULL,
 plan_digest varchar(64) NOT NULL,
 approval_id varchar(255) NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(org_id,workspace_id,deployment_id),
 UNIQUE(org_id,workspace_id,allocation_id),
 UNIQUE(org_id,workspace_id,request_id)
)
""")


def downgrade():
    op.execute("LOCK TABLE controller_cleanup_bindings IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM controller_cleanup_bindings)
THEN RAISE EXCEPTION 'cleanup ownership evidence must be preserved before rollback';
END IF; END $$""")
    op.drop_table("controller_cleanup_bindings")
