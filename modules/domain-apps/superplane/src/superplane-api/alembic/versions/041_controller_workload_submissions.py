"""Preserve exact original workload specifications before POST dispatch."""

from alembic import op

revision = "041_controller_workload_submissions"
down_revision = "040_controller_cleanup_bindings"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE controller_workload_submissions (
 operation_id varchar(255) NOT NULL,
 kind varchar(32) NOT NULL,
 namespace varchar(63) NOT NULL,
 name varchar(253) NOT NULL,
 org_id varchar(255) NOT NULL,
 workspace_id varchar(255) NOT NULL,
 allocation_id varchar(255) NOT NULL,
 plan_digest varchar(64) NOT NULL,
 step_key varchar(255) NOT NULL,
 attempt_id varchar(255) NOT NULL,
 fence_token bigint NOT NULL CHECK (fence_token > 0),
 body text NOT NULL CHECK (octet_length(body)<=65536),
 body_sha256 varchar(64) NOT NULL,
 PRIMARY KEY (operation_id,kind,namespace,name)
)
""")
    op.execute("""
CREATE FUNCTION controller_workload_submission_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'original workload submission is immutable'; END;
$$ LANGUAGE plpgsql
""")
    op.execute("""
CREATE TRIGGER controller_workload_submission_immutable
 BEFORE UPDATE OR DELETE ON controller_workload_submissions
 FOR EACH ROW EXECUTE FUNCTION controller_workload_submission_immutable()
""")


def downgrade():
    op.execute("LOCK TABLE controller_workload_submissions IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM controller_workload_submissions)
THEN RAISE EXCEPTION 'original workload submissions must be preserved before rollback';
END IF; END $$""")
    op.drop_table("controller_workload_submissions")
    op.execute("DROP FUNCTION controller_workload_submission_immutable()")
