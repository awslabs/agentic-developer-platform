"""Shared issue ownership: one durable execution owner per issue.

Issue #5127 (EPIC #4191). Additive — creates one table, alters nothing, backfills
nothing. There is no data migration because ownership is a forward-looking
protection: a claim describes work admitted *after* this lands, and inventing
claim rows for already-running work would assert an owner nobody verified. Work
in flight at deploy time is reconciled by the admission service on its next
admission, not by this migration.

Must stay in agreement with `OrchestrationWorkClaim` in
`src/orchestration/models.py` — both are hand-written, so drift is the live risk:
the migration is what runs in dev, the models are what the tests use.
`tests/migrations/test_050_orchestration_work_claims.py` asserts that parity.

`provider_repository_id` is `BigInteger` deliberately. It holds GitHub's immutable
numeric repository id, which is a 64-bit provider integer; `String` would let
`"123"` and `123` become two owners of one repository, and `Integer` would cap a
value the provider is free to grow.
"""

import sqlalchemy as sa

from alembic import op

revision = "050_orchestration_work_claims"
down_revision = "049_bedrock_connection_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orchestration_work_claims",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("provider_repository_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("owner_kind", sa.String(32), nullable=False),
        sa.Column("owner_ref", sa.String(255), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("active_run_id", sa.String(255), nullable=True),
        sa.Column("claim_event_id", sa.String(255), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(64), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # THE invariant, enforced by the database. Two concurrent transactions can both
    # pass an application-level "is this issue free?" check; only one can win a
    # unique index. Without this the admission service would be advisory.
    op.create_index(
        "uq_orchestration_work_claims_binding",
        "orchestration_work_claims",
        ["org_id", "provider_repository_id", "issue_number"],
        unique=True,
    )
    op.create_index(
        "ix_orchestration_work_claims_org_id",
        "orchestration_work_claims",
        ["org_id"],
    )
    op.create_index(
        "ix_orchestration_work_claims_org_id_state",
        "orchestration_work_claims",
        ["org_id", "state"],
    )
    op.create_index(
        "ix_orchestration_work_claims_claim_event_id",
        "orchestration_work_claims",
        ["org_id", "claim_event_id"],
    )


def downgrade() -> None:
    # Deliberately unguarded, unlike 049's row-count check. Claim rows are
    # execution bookkeeping rather than a record of what a human approved, and
    # this migration is the documented rollback for the story: the rollback plan
    # is "disable new admissions, reconcile in-flight work, then downgrade", so a
    # raise here would block the very path the plan prescribes. Dropping the table
    # cannot orphan anything — nothing references it by foreign key.
    op.drop_index("ix_orchestration_work_claims_claim_event_id", table_name="orchestration_work_claims")
    op.drop_index("ix_orchestration_work_claims_org_id_state", table_name="orchestration_work_claims")
    op.drop_index("ix_orchestration_work_claims_org_id", table_name="orchestration_work_claims")
    op.drop_index("uq_orchestration_work_claims_binding", table_name="orchestration_work_claims")
    op.drop_table("orchestration_work_claims")
