"""Durable network effects and allocation dependencies under shared execution."""

from alembic import op

revision = "035_controller_network_journal"
down_revision = "034_provider_request_region"
branch_labels = None
depends_on = None

DDL = """
CREATE TABLE controller_network_completion (
 operation_id text PRIMARY KEY,
 allocation_id text NOT NULL,
 plan_digest text NOT NULL,
 compute_region text NOT NULL,
 completed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE controller_network_resources (
 resource_key text PRIMARY KEY,
 org_id text NOT NULL,
 descriptor text NOT NULL,
 generation bigint NOT NULL DEFAULT 1,
 state text NOT NULL CHECK(state IN ('intended','present','delete_intended','absent')),
 owned boolean NOT NULL,
 provider_reference text,
 created_by_operation text NOT NULL
);
CREATE TABLE controller_network_members (
 resource_key text NOT NULL REFERENCES controller_network_resources(resource_key),
 allocation_id text NOT NULL,
 org_id text NOT NULL,
 workspace_id text NOT NULL,
 cluster_id text NOT NULL,
 membership_generation varchar(64) NOT NULL CHECK(membership_generation ~ '^[a-f0-9]{64}$'),
 source_operation_id text NOT NULL,
 source_plan_digest text NOT NULL,
 released_at timestamptz,
 PRIMARY KEY(resource_key, allocation_id)
);
CREATE TABLE controller_network_effects (
 resource_key text NOT NULL REFERENCES controller_network_resources(resource_key),
 generation bigint NOT NULL,
 action text NOT NULL,
 operation_id text NOT NULL,
 attempt_id text NOT NULL,
 fence_token bigint NOT NULL,
 descriptor text NOT NULL,
 result text,
 intended_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 confirmed_at timestamptz,
 PRIMARY KEY(resource_key,generation,action)
);
"""


def upgrade():
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    # Dropping evidence while resources remain would erase cleanup ownership.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM controller_network_resources WHERE state <> 'absent')
      THEN RAISE EXCEPTION 'network resources must be reconciled before downgrade'; END IF;
    END $$""")
    op.drop_table("controller_network_completion")
    op.drop_table("controller_network_effects")
    op.drop_table("controller_network_members")
    op.drop_table("controller_network_resources")
