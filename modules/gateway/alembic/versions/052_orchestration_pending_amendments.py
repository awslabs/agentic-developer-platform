"""Pending amendments: where an authored amendment waits for a human's yes.

Issue #4529 (EPIC #4191). Additive — creates two tables, alters nothing, backfills
nothing.

**Why there is no backfill.** The existing `replan_requested` decisions are requests
nobody ever authored an amendment for. Inventing an `orchestration_amendment_requests`
row for each of them would queue an authoring job per historical replan the moment the
producer ships — retroactively dispatching agents for conversations that concluded
weeks ago, against plans that have since changed. A request row means "an authoring
job is owed"; asserting that about the past is asserting work nobody asked for now.
Historical replans stay as what they are: a recorded request a human can re-issue.

**Why two tables and not one.** A request is a human's ask and exists whether or not
anything is ever authored; a draft is an agent's output and exists only if authoring
succeeded. Collapsing them would make "the human asked and nothing came back" —
precisely the failure this story has to keep visible — indistinguishable from a
row with some columns unfilled.

**Why neither is a column on `orchestration_accepted_plans`.** A `status` column there
would put un-accepted, agent-authored content into the table every reader treats as the
plan of record, and one missed filter anywhere would make a proposal executable. These
tables are referenced by no graph query at all, so the separation needs no filter to
hold.

Must stay in agreement with `OrchestrationAmendmentRequest` and
`OrchestrationPendingAmendment` in `src/orchestration/models.py` — both are
hand-written, so drift is the live risk: the migration is what runs in dev, the models
are what the tests use. `tests/migrations/test_052_orchestration_pending_amendments.py`
asserts that parity.

`JSON_DOC` is spelled here identically to `models.py` and to
`029_orchestration_graph.py`, so `proposal_document` is real `JSONB` on Postgres and
plain `JSON` on SQLite where the tests run. A bare `sa.JSON` would render as `JSON` on
Postgres too and quietly forfeit JSONB on a column that holds whole plan documents.

`replan_decision_id` is deliberately NOT a foreign key to `orchestration_decisions`:
that table is append-only and enforced so at the ORM boundary, and a CASCADE into it
would be a delete path through an append-only record.

The revision id is shortened from the filename stem to stay inside the
`alembic_version.version_num` VARCHAR(32) limit that
`tests/migrations/test_revision_id_length.py` enforces.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "052_orch_pending_amend"
down_revision = "051_orch_pr_bindings"
branch_labels = None
depends_on = None

# Must stay identical to JSON_DOC in src/orchestration/models.py.
JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "orchestration_amendment_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("flow_id", sa.String(36), sa.ForeignKey("orchestration_flows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("replan_decision_id", sa.String(36), nullable=False),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("base_plan_version", sa.Integer(), nullable=True),
        sa.Column("base_plan_hash", sa.String(64), nullable=True),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("author_run_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )

    # THE idempotency invariant for the producer, enforced by the database rather
    # than by the command pass. Two concurrent ticks that both read the same pending
    # comment row can both pass an application-level "already requested?" check; only
    # one can win this index, and the loser reconciles onto the winner's row. Without
    # it, a duplicated webhook delivery queues a second authoring agent for one human
    # ask — which is the "duplicate events must reconcile to one assignment"
    # requirement, expressed where it cannot be forgotten.
    op.create_index(
        "uq_orchestration_amendment_requests_decision",
        "orchestration_amendment_requests",
        ["org_id", "replan_decision_id"],
        unique=True,
    )
    # `TenantMixin` declares `org_id` with `index=True`, so the model implies this
    # standalone index; created explicitly to keep migration/model parity exact.
    op.create_index("ix_orchestration_amendment_requests_org_id", "orchestration_amendment_requests", ["org_id"])
    # `(org_id, flow_id)`, NOT `(flow_id)`. The model spells this index in
    # `__table_args__` rather than as `index=True` on the column, because the implicit
    # name SQLAlchemy would generate for that is this same name — two indexes with one
    # name, and the second CREATE fails.
    op.create_index("ix_orchestration_amendment_requests_flow_id", "orchestration_amendment_requests", ["org_id", "flow_id"])
    # The publisher's read: which requests are still owed an authoring job.
    op.create_index("ix_orchestration_amendment_requests_state", "orchestration_amendment_requests", ["org_id", "state"])

    op.create_table(
        "orchestration_pending_amendments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("flow_id", sa.String(36), sa.ForeignKey("orchestration_flows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_id", sa.String(36), sa.ForeignKey("orchestration_amendment_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("author_run_id", sa.String(255), nullable=False),
        sa.Column("base_plan_version", sa.Integer(), nullable=True),
        sa.Column("base_plan_hash", sa.String(64), nullable=True),
        sa.Column("proposal_document", JSON_DOC, nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("accepted_by", sa.String(255), nullable=True),
        sa.Column("accepted_by_decision_id", sa.String(36), nullable=True),
        sa.Column("accepted_plan_version", sa.Integer(), nullable=True),
        sa.Column("superseded_by_draft_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Idempotent registration: the same document registered twice on one flow is one
    # draft. In the database because a fail-soft author's retry and its original can
    # both pass an application-level existence read, and two drafts of one document
    # would give a human two things to accept that mean the same thing.
    op.create_index(
        "uq_orchestration_pending_amendments_proposal",
        "orchestration_pending_amendments",
        ["org_id", "flow_id", "proposal_hash"],
        unique=True,
    )
    op.create_index("ix_orchestration_pending_amendments_org_id", "orchestration_pending_amendments", ["org_id"])
    # The acceptance path's read: this flow's drafts by status. There is deliberately
    # no separate `(flow_id)` index: it would duplicate this one's prefix, and the
    # model declares `flow_id` without `index=True` for the same reason.
    op.create_index(
        "ix_orchestration_pending_amendments_flow_state",
        "orchestration_pending_amendments",
        ["org_id", "flow_id", "state"],
    )
    # Registration's binding read: the draft owed by one authoring assignment.
    op.create_index("ix_orchestration_pending_amendments_request", "orchestration_pending_amendments", ["org_id", "request_id"])


def downgrade() -> None:
    # Unguarded, like 050's and 051's, and for a stronger reason than either: nothing
    # in these tables is a record of what a human approved. An accepted amendment's
    # authority lives in `orchestration_accepted_plans` and its `PLAN_AMENDED`
    # decision, both written by `amend_plan` and untouched by this migration, so
    # dropping these tables cannot un-accept anything. Nothing references them by
    # foreign key.
    #
    # What a downgrade DOES cost is worth stating: pending drafts are lost, so a
    # replan that had produced an un-accepted amendment reverts to "requested, not
    # authored" — the human re-issues the replan. That is the safe direction, and it
    # is why the request row is idempotent on the decision rather than on the text.
    # Drops in reverse creation order: the drafts table's FK points at the requests
    # table.
    op.drop_index("ix_orchestration_pending_amendments_request", table_name="orchestration_pending_amendments")
    op.drop_index("ix_orchestration_pending_amendments_flow_state", table_name="orchestration_pending_amendments")
    op.drop_index("ix_orchestration_pending_amendments_org_id", table_name="orchestration_pending_amendments")
    op.drop_index("uq_orchestration_pending_amendments_proposal", table_name="orchestration_pending_amendments")
    op.drop_table("orchestration_pending_amendments")

    op.drop_index("ix_orchestration_amendment_requests_state", table_name="orchestration_amendment_requests")
    op.drop_index("ix_orchestration_amendment_requests_flow_id", table_name="orchestration_amendment_requests")
    op.drop_index("ix_orchestration_amendment_requests_org_id", table_name="orchestration_amendment_requests")
    op.drop_index("uq_orchestration_amendment_requests_decision", table_name="orchestration_amendment_requests")
    op.drop_table("orchestration_amendment_requests")
