"""Add graph_address to usage_logs — cost answerable by graph address.

Issue #4207 (EPIC #4191, intent #4120): give the usage ledger a graph address so
"what did this EPIC cost?" is ONE grouped Postgres query instead of
enumerate-runs-from-DynamoDB-then-`IN`.

Why this column exists at all: cost aggregation today lists runs from a DynamoDB
table whose rows expire at 30 days, then queries Postgres for those run ids. On
day 31 an EPIC's total silently becomes partial with nothing saying so. Stamping
the address onto the ledger row removes the DynamoDB dependency, which fixes the
cliff at its cause rather than widening the TTL.

**Follows 018_agent_run_cost_traceability's contract, NOT the adjacent 025's.**
`ADD COLUMN ... NULL`, no `server_default`, and a partial index
`WHERE graph_address IS NOT NULL`. 025 is `NOT NULL` + `server_default`, where
the default IS the backfill — copying that neighbour here would stamp a
fabricated graph address onto every historical usage row, and a fabricated
address is worse than a null one: null reads as "not addressed", while a
fabricated one reads as real and lands that row in some EPIC's total.

**Explicit no-backfill contract: existing rows stay null.** Pre-feature Bedrock
calls genuinely have no graph address, and rows written by non-gateway paths
(`ADP_BEDROCK_VIA=direct|user`) never will. `tests/migrations/
test_031_usage_graph_address.py` asserts pre-existing rows are byte-identical
after `upgrade()`.

Nullability is also what keeps the usage hot path writable during rollout: the
gateway pods running the pre-031 image INSERT without this column, and a
`NOT NULL` column with no default would fail every one of those in-flight
INSERTs. Because `_log_usage` swallows exceptions, that failure returns HTTP 200
with no usage row at all — unmetered and unbilled, with no alarm. The same
reasoning 028 records for its cache counters applies with more force here,
because this migration touches a populated table.

The partial index is 018's shape and serves the grouped rollup's prefix scan.
`postgresql_where` and `sqlite_where` are both given so the predicate survives on
SQLite, where the tests run — 018 used raw `op.execute` with a Postgres-only
`CREATE INDEX ... WHERE`, which is fine for Postgres but not executable under the
SQLite-backed migration test this story is required to ship.

Revision numbering note: this story was specified as `027` chaining onto `026`,
neither of which is right in this repo. `027` is already taken by
`027_installation_tenant_uniqueness` and `026` is
`026_channel_tenant_map_installation_id`, an unrelated migration; the store story
(#4196) landed as **029**, not 026, and `030` was taken by
`030_budget_usage_cost_precision` (#4287) while this story was in flight.
Chaining onto a stale number creates a SECOND HEAD and `alembic upgrade head`
then fails outright for everyone. This chains onto the real single head,
030_budget_usage_cost_precision. The revision id is 24 chars, inside the
`alembic_version.version_num` VARCHAR(32) ceiling that
`tests/migrations/test_revision_id_length.py` guards.

Revision ID: 031_usage_graph_address
Revises: 030_budget_usage_cost_precision
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "031_usage_graph_address"
down_revision: str | None = "030_budget_usage_cost_precision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `flow/epic/wave/node`, four segments of up to 64 chars plus three separators.
# 512 leaves headroom without pretending the column is free-text.
_ADDRESS_LEN = 512


def upgrade() -> None:
    """Add the nullable graph_address column and its partial index.

    No UPDATE statement anywhere in this function: that absence is the
    no-backfill contract, not an oversight.
    """
    op.add_column(
        "usage_logs",
        sa.Column("graph_address", sa.String(length=_ADDRESS_LEN), nullable=True),
    )
    # Partial index (018's shape): only addressed rows are ever scanned by the
    # rollup, and the vast majority of historical rows are null. Indexing the
    # nulls would pay for millions of entries no cost query will ever read.
    op.create_index(
        "ix_usage_logs_graph_address",
        "usage_logs",
        ["graph_address"],
        postgresql_where=sa.text("graph_address IS NOT NULL"),
        sqlite_where=sa.text("graph_address IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the index and column.

    This IS the documented rollback plan, so it is exercised by the migration
    test rather than assumed to work. Safe by construction: the column is
    nullable, nothing was backfilled, and no existing row was modified, so
    dropping it cannot lose data that predates the migration.
    """
    op.drop_index("ix_usage_logs_graph_address", table_name="usage_logs")
    op.drop_column("usage_logs", "graph_address")
