"""Resolve unused L5 draft owners to their verified workspace user.

Historical runs, bindings, setup intents and versioned settings are untouched.
"""

import sqlalchemy as sa

from alembic import op

revision = "170_l5_canonical_draft_owners"
down_revision = "169_merge_collection"
branch_labels = None
depends_on = None


def repair_drafts(connection):
    candidates = (
        connection.execute(
            sa.text("""
        SELECT c.tenant_id, c.configuration_id, c.owner_id, u.id AS canonical_id
        FROM l5_service_configurations c
        JOIN users u ON u.cognito_sub = c.owner_id AND u.org_id = c.tenant_id
        JOIN l5_service_config_versions v
          ON v.tenant_id = c.tenant_id AND v.configuration_id = c.configuration_id
         AND v.revision = c.current_revision
        WHERE c.current_revision = 1 AND v.state = 'disabled'
          AND u.user_kind = 'human' AND u.is_shadow = false AND u.id <> c.owner_id
          AND NOT EXISTS (SELECT 1 FROM users known
              WHERE known.id = c.owner_id)
          AND (SELECT count(*) FROM users matching
              WHERE matching.cognito_sub = c.owner_id AND matching.org_id = c.tenant_id) = 1
          AND NOT EXISTS (SELECT 1 FROM l5_service_bindings b
              WHERE b.tenant_id = c.tenant_id AND b.configuration_id = c.configuration_id)
          AND NOT EXISTS (SELECT 1 FROM l5_service_setup_intents s
              WHERE s.tenant_id = c.tenant_id AND s.configuration_id = c.configuration_id)
          AND NOT EXISTS (SELECT 1 FROM l5_service_runs r
              WHERE r.tenant_id = c.tenant_id AND r.configuration_id = c.configuration_id)
    """)
        )
        .mappings()
        .all()
    )
    for candidate in candidates:
        values = dict(candidate)
        # Preserve the existing request digest, configuration id and version.
        # A conflicting receipt aborts the migration rather than discarding it.
        connection.execute(
            sa.text("""
            UPDATE l5_service_config_receipts SET principal_id = :canonical_id
            WHERE tenant_id = :tenant_id AND configuration_id = :configuration_id
              AND principal_id = :owner_id
        """),
            values,
        )
        connection.execute(
            sa.text("""
            UPDATE l5_service_configurations SET owner_id = :canonical_id
            WHERE tenant_id = :tenant_id AND configuration_id = :configuration_id
              AND owner_id = :owner_id
        """),
            values,
        )
    return len(candidates)


def upgrade():
    repair_drafts(op.get_bind())


def downgrade():
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
