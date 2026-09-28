"""Create the orchestration graph store tables.

Issue #4196 (EPIC #4191, intent #4120): durable storage for the plan accepted at
a gate and for where the loop stands against it.

Creates five tables — flows, nodes, edges, accepted plans, decisions — and
nothing else. **No existing table is altered**, so there is no backfill and no
column added to a populated table. That is the deliberate contract of this
migration, and `tests/migrations/test_029_orchestration_graph.py` asserts it.

Follows **018_agent_run_cost_traceability**'s contract, NOT the adjacent 025's:
optional columns are nullable with NO `server_default`, and the hot lookup gets a
partial index. 025 is `NOT NULL` + `server_default` where the default IS the
backfill — copying that neighbour would silently stamp a fabricated value into
every existing row. It cannot bite here (new tables have no existing rows), but
the shape is what the next migration author copies, so it is worth getting right.

Where this departs from 018 is DDL *mechanism*: 018 uses raw `op.execute` with
Postgres-only syntax, which is fine for `ALTER TABLE ... ADD COLUMN` but not for
`CREATE TABLE`. `TIMESTAMPTZ`/`JSONB`/`DEFAULT NOW()` are Postgres-only and fail
outright on SQLite, and this migration MUST execute under SQLite because the
mandated alembic-only test (AC-24) runs `upgrade()` against it. So the tables use
portable `op.create_table` with `sa.*` types — the same shape as
`008_magic_link.py` — which alembic renders to the right dialect for each
backend. Partial indexes carry both `postgresql_where` and `sqlite_where` for the
same reason.

`plan_document` uses `sa.JSON().with_variant(JSONB, "postgresql")` so it renders
as real `JSONB` on Postgres (GIN-indexable later, per the convention 016 sets
with its raw `JSONB` DDL) while still rendering as `JSON` on SQLite for the tests.
Plain `sa.JSON()` would NOT do this — it renders as `JSON` on Postgres too, which
silently gives up JSONB. Verified by rendering this migration against both
dialects.

Revision numbering note: this story was specified as `026` chaining onto
`025_org_created_via`, which was the single head when it was written. Three
migrations landed on 025 since (026_ctm_installation_id → 027 →
028_usage_cache_tokens), so chaining onto 025 now would create a SECOND HEAD and
`alembic upgrade head` fails outright on multiple heads. This chains onto the
real single head, 028. The revision id is 23 chars, inside the
`alembic_version.version_num` VARCHAR(32) ceiling that
`tests/migrations/test_revision_id_length.py` guards.

Revision ID: 029_orchestration_graph
Revises: 028_usage_cache_tokens
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Real JSONB on Postgres (GIN-indexable), plain JSON on SQLite for the tests.
# Bare sa.JSON() renders as JSON on Postgres too, quietly forfeiting JSONB.
JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

revision: str = "029_orchestration_graph"
down_revision: str | None = "028_usage_cache_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the five orchestration tables, parents before children."""
    # -- flows: the top container ------------------------------------------
    op.create_table(
        "orchestration_flows",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("intent_ref", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orchestration_flows_org_id", "orchestration_flows", ["org_id"])
    op.create_index("ix_orchestration_flows_org_id_created_at", "orchestration_flows", ["org_id", "created_at"])

    # -- nodes: story / eval / gate only. The story is the graph floor;
    # -- containers (wave/epic) are derived state, addressed by the *_ref
    # -- columns below rather than stored as rows.
    op.create_table(
        "orchestration_nodes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=255), nullable=False),
        sa.Column("flow_id", sa.String(length=36), nullable=False),
        sa.Column("epic_ref", sa.String(length=64), nullable=False),
        sa.Column("wave_ref", sa.String(length=64), nullable=False),
        sa.Column("node_ref", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("issue_ref", sa.String(length=64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["flow_id"], ["orchestration_flows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orchestration_nodes_org_id", "orchestration_nodes", ["org_id"])
    op.create_index("ix_orchestration_nodes_flow_id", "orchestration_nodes", ["flow_id"])
    op.create_index("ix_orchestration_nodes_org_id_state", "orchestration_nodes", ["org_id", "state"])
    op.create_index("ix_orchestration_nodes_flow_id_state", "orchestration_nodes", ["flow_id", "state"])
    # The graph address is an address: a duplicate means two nodes answer to the
    # same name and the cost rollup double-counts.
    op.create_index(
        "uq_orchestration_nodes_address",
        "orchestration_nodes",
        ["flow_id", "epic_ref", "wave_ref", "node_ref"],
        unique=True,
    )

    # -- edges: dependency graph, for look-ahead + parallel-branch rendering --
    op.create_table(
        "orchestration_edges",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=255), nullable=False),
        sa.Column("flow_id", sa.String(length=36), nullable=False),
        sa.Column("from_node_id", sa.String(length=36), nullable=False),
        sa.Column("to_node_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["flow_id"], ["orchestration_flows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_node_id"], ["orchestration_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_node_id"], ["orchestration_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orchestration_edges_org_id", "orchestration_edges", ["org_id"])
    op.create_index("ix_orchestration_edges_flow_id", "orchestration_edges", ["flow_id"])
    op.create_index("ix_orchestration_edges_from_node_id", "orchestration_edges", ["from_node_id"])
    op.create_index("ix_orchestration_edges_to_node_id", "orchestration_edges", ["to_node_id"])
    # A duplicate edge is not extra information; it makes fan-in/fan-out wrong.
    op.create_index(
        "uq_orchestration_edges_pair",
        "orchestration_edges",
        ["flow_id", "from_node_id", "to_node_id"],
        unique=True,
    )

    # -- accepted plans: immutable + versioned, so amendment supersedes ------
    op.create_table(
        "orchestration_accepted_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=255), nullable=False),
        sa.Column("flow_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("plan_document", JSON_DOC, nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_by_decision_id", sa.String(length=36), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["flow_id"], ["orchestration_flows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orchestration_accepted_plans_org_id", "orchestration_accepted_plans", ["org_id"])
    op.create_index("ix_orchestration_accepted_plans_flow_id", "orchestration_accepted_plans", ["flow_id"])
    op.create_index("ix_orchestration_accepted_plans_org_id_flow_id", "orchestration_accepted_plans", ["org_id", "flow_id"])
    # Two rows claiming one version makes "the accepted plan" ambiguous at
    # exactly the wrong moment.
    op.create_index(
        "uq_orchestration_accepted_plans_version",
        "orchestration_accepted_plans",
        ["flow_id", "version"],
        unique=True,
    )
    # Partial index (018's shape): "the plan in force" is the hot lookup and it is
    # always the one row per flow with superseded_at IS NULL. Both dialect keys
    # are given so the predicate survives on SQLite as well as Postgres.
    op.create_index(
        "ix_orchestration_accepted_plans_in_force",
        "orchestration_accepted_plans",
        ["flow_id"],
        postgresql_where=sa.text("superseded_at IS NULL"),
        sqlite_where=sa.text("superseded_at IS NULL"),
    )

    # -- decisions: append-only attribution ---------------------------------
    # actor_role and actor_kind are separate NOT NULL columns with NO default.
    # A default on actor_kind would let an unattributed write land looking
    # attributed — the exact ambiguity that makes the existing
    # tenant_access_requests.decided_by column unusable for this.
    op.create_table(
        "orchestration_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=255), nullable=False),
        sa.Column("flow_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("actor_role", sa.String(length=64), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["flow_id"], ["orchestration_flows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["orchestration_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orchestration_decisions_org_id", "orchestration_decisions", ["org_id"])
    op.create_index("ix_orchestration_decisions_flow_id", "orchestration_decisions", ["flow_id"])
    op.create_index("ix_orchestration_decisions_node_id", "orchestration_decisions", ["node_id"])
    op.create_index("ix_orchestration_decisions_org_id_created_at", "orchestration_decisions", ["org_id", "created_at"])
    op.create_index("ix_orchestration_decisions_flow_id_kind", "orchestration_decisions", ["flow_id", "kind"])


def downgrade() -> None:
    """Drop the five tables, children before parents.

    Reverse FK order matters: dropping orchestration_flows first would fail on
    the dependent constraints. A working downgrade is the rollback plan, so the
    migration test exercises it rather than assuming it.
    """
    op.drop_table("orchestration_decisions")
    op.drop_table("orchestration_accepted_plans")
    op.drop_table("orchestration_edges")
    op.drop_table("orchestration_nodes")
    op.drop_table("orchestration_flows")
