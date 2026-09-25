"""Original-instance native bootstrap command intents and bounded evidence."""

from alembic import op

revision = "039_controller_node_commands"
down_revision = "038_cluster_grant_scopes"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE controller_node_commands (
 reference text PRIMARY KEY,
 operation_id text NOT NULL,
 org_id text NOT NULL,
 workspace_id text NOT NULL,
 allocation_id text NOT NULL,
 plan_digest text NOT NULL,
 step_key text NOT NULL,
 purpose text NOT NULL CHECK (purpose IN ('node-bootstrap','node-api-dns-tls')),
 instance_id text NOT NULL,
 region text NOT NULL,
 account_id text NOT NULL,
 contract text NOT NULL,
 contract_sha256 varchar(64) NOT NULL,
 state text NOT NULL DEFAULT 'prepared'
   CHECK (state IN ('prepared','dispatching','accepted','running','succeeded','failed','uncertain')),
 dispatched_at timestamptz,
 observation_deadline timestamptz,
 command_id text,
 result text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(operation_id,instance_id,purpose),
 CHECK ((dispatched_at IS NULL) = (observation_deadline IS NULL)),
 CHECK (command_id IS NULL OR dispatched_at IS NOT NULL)
);
CREATE INDEX ix_controller_node_commands_allocation
 ON controller_node_commands(org_id,workspace_id,allocation_id);
""")


def downgrade():
    op.execute("LOCK TABLE controller_node_commands IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM controller_node_commands)
THEN RAISE EXCEPTION 'native command evidence must be preserved before rollback';
END IF; END $$""")
    op.drop_table("controller_node_commands")
