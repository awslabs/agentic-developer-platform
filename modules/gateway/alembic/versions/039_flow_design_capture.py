"""Add description and design_history to orchestration_flows.

Issue #4885 (child of #4869, EPIC #4191). A flow row carries the plan's *shape*
— steps, waves, dependencies — and nothing about the design conversation that
produced it. The flows list (#4869) can therefore say what a plan is doing but
not what it is *for*, nor which of the five AIDLC design gates ran. These two
columns are the capture point, written once at registration.

**Both nullable, no server default, and NO backfill. That is the contract.**

`NULL` means "we do not know", and it must stay reachable, because for every flow
that exists today it is the *honest* value: they were registered before anything
recorded a design history, and no amount of inference would recover one. A
synthetic default would be worse than absence in the specific way that matters —
absence renders as nothing at all on the card, while a fabricated history renders
as a real record of gates that never happened, and it looks authoritative. So
there is no `UPDATE` statement in `upgrade()`, and that absence is the contract
rather than an oversight. `tests/migrations/test_039_flow_design_capture.py`
asserts a pre-existing row comes out byte-identical with both columns null.

This follows 031's shape (`ADD COLUMN ... NULL`, no `server_default`) and
deliberately not 025's (`NOT NULL` + `server_default`, where the default IS the
backfill).

Nullability is also what keeps registration writable across the rollout: gateway
pods running the pre-039 image INSERT into `orchestration_flows` without these
columns, and a `NOT NULL` column with no default would fail every one of those
in-flight INSERTs — turning a routine deploy into a registration outage.

No index on either column. Neither is a lookup key: both ride a row already
being fetched by `list_flows_page_with_aggregates`, so an index would cost writes
to serve a query nobody issues. `description` is deliberately not searchable —
`q` filters title / slug / intent_ref, and widening it is not this issue.

`design_history` uses the SAME `JSON_DOC` dialect variant as
`029_orchestration_graph.py` and `models.py`, so it is real `JSONB` on Postgres
and plain `JSON` on SQLite where the tests run. A bare `sa.JSON()` renders as
`JSON` on Postgres too and would quietly forfeit JSONB — and would disagree with
the model, which is the drift these three declarations exist to prevent. Its
shape is validated by Pydantic at write time (`proposal.DesignHistory`), not by
the database: the canonical stage names live in `rules/personas/aidlc.md` and a
CHECK constraint over them would need a migration every time that list changed.

Revision ID is 24 chars, inside the `alembic_version.version_num` VARCHAR(32)
ceiling that `tests/migrations/test_revision_id_length.py` guards. SQLite does
not enforce VARCHAR length, so an over-long id passes every test and overflows on
Postgres only — checked by hand as well as by that guard.

Revision ID: 039_flow_design_capture
Revises: 038_cli_auth_requests
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "039_flow_design_capture"
down_revision: str | None = "038_cli_auth_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must stay identical to JSON_DOC in `src/orchestration/models.py` and in
# `029_orchestration_graph.py`. Three declarations of one type; they must agree.
JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """Add both nullable columns.

    No UPDATE statement anywhere in this function. That absence is the
    no-backfill contract — see the module docstring.
    """
    op.add_column("orchestration_flows", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("orchestration_flows", sa.Column("design_history", JSON_DOC, nullable=True))


def downgrade() -> None:
    """Drop both columns.

    This IS the documented rollback plan, so the migration test exercises it
    rather than assuming it works. Safe by construction: both columns are
    nullable, nothing was backfilled, and no pre-existing row was modified, so
    dropping them cannot lose data that predates the migration.

    Note the application rollback does NOT need this: the read path treats both
    columns as nullable, so reverting the code leaves them harmlessly populated.
    """
    op.drop_column("orchestration_flows", "design_history")
    op.drop_column("orchestration_flows", "description")
