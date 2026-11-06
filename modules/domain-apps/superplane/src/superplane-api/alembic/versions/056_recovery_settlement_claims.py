"""Preserve each accepted recovery claim across an authorized operation resume."""

from alembic import op

revision = "056_recovery_settlement_claims"
down_revision = "055_sso_provider_ownership"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("LOCK TABLE operation_settlement_receipts IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "ALTER TABLE operation_settlement_receipts ADD COLUMN claim_fence_token bigint"
    )
    # The accepted payload is retained byte for byte. Only its existing claim
    # identity is indexed; malformed historical receipts refuse migration.
    op.execute("""UPDATE operation_settlement_receipts
        SET claim_fence_token=(payload_json::jsonb#>>'{claim,fence_token}')::bigint""")
    op.execute("""ALTER TABLE operation_settlement_receipts
        ALTER COLUMN claim_fence_token SET NOT NULL,
        ADD CONSTRAINT operation_settlement_positive_claim CHECK(claim_fence_token>0),
        DROP CONSTRAINT operation_settlement_receipts_operation_id_key,
        ADD CONSTRAINT uq_operation_settlement_claim UNIQUE(operation_id,claim_fence_token)
    """)


def downgrade():
    op.execute("LOCK TABLE operation_settlement_receipts IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM operation_settlement_receipts
                  GROUP BY operation_id HAVING count(*)>1) THEN
            RAISE EXCEPTION 'cannot discard accepted recovery claim history';
        END IF;
    END $$""")
    op.execute("""ALTER TABLE operation_settlement_receipts
        DROP CONSTRAINT uq_operation_settlement_claim,
        ADD CONSTRAINT operation_settlement_receipts_operation_id_key UNIQUE(operation_id),
        DROP COLUMN claim_fence_token
    """)
