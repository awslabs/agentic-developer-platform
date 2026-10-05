"""Immutable source-finalizer evidence for opt-in staged cleanup."""

from alembic import op

revision = "042_controller_cleanup_snapshots"
down_revision = "041_controller_workload_submissions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE controller_cleanup_snapshots (
 snapshot_id varchar(64) PRIMARY KEY,
 source_operation_id text NOT NULL UNIQUE,
 org_id text NOT NULL,
 workspace_id text NOT NULL,
 allocation_id text NOT NULL,
 source_plan_digest varchar(64) NOT NULL,
 body text NOT NULL CHECK (octet_length(body)<=131072),
 body_sha256 varchar(64) NOT NULL,
 sealed_revision text NOT NULL,
 report_digest varchar(64) NOT NULL,
 enumeration_binding text NOT NULL,
 report_observations text NOT NULL,
 attempt_id text NOT NULL,
 executor_id text NOT NULL,
 fence_token bigint NOT NULL CHECK (fence_token>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(org_id,workspace_id,allocation_id)
)
""")
    op.execute("""
CREATE FUNCTION controller_cleanup_snapshot_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'original cleanup snapshot is immutable'; END;
$$ LANGUAGE plpgsql
""")
    op.execute("""
CREATE TRIGGER controller_cleanup_snapshot_immutable
 BEFORE UPDATE OR DELETE ON controller_cleanup_snapshots
 FOR EACH ROW EXECUTE FUNCTION controller_cleanup_snapshot_immutable()
""")


def downgrade():
    op.execute("LOCK TABLE controller_cleanup_snapshots IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM controller_cleanup_snapshots)
THEN RAISE EXCEPTION 'original cleanup snapshots must be preserved before rollback';
END IF; END $$""")
    op.drop_table("controller_cleanup_snapshots")
    op.execute("DROP FUNCTION controller_cleanup_snapshot_immutable()")
