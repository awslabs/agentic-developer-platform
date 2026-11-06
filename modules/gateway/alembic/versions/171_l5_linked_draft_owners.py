"""Repair unused L5 drafts whose login has a verified workspace placement.

Revision 170 handles direct users.cognito_sub mappings. Onboarded workspace
accounts may instead have the platform-owned cognito/org_placement identity.
Keep established runs, bindings, settings and ambiguous owners unchanged.
"""

import sqlalchemy as sa

from alembic import op

revision = "171_l5_linked_draft_owners"
down_revision = "170_l5_canonical_draft_owners"
branch_labels = None
depends_on = None


def repair_drafts(connection):
    query = """
        SELECT c.tenant_id, c.configuration_id, c.owner_id, u.id AS canonical_id
        FROM l5_service_configurations c
        JOIN user_identities i ON i.provider = 'cognito'
          AND i.provider_user_id = c.owner_id AND i.org_id = c.tenant_id
          AND i.verification_method = 'org_placement'
        JOIN users u ON u.id = i.user_id AND u.org_id = c.tenant_id
        JOIN l5_service_config_versions v
          ON v.tenant_id = c.tenant_id AND v.configuration_id = c.configuration_id
         AND v.revision = c.current_revision
        WHERE c.current_revision = 1 AND v.state = 'disabled'
          AND u.user_kind = 'human' AND u.is_shadow = false AND u.id <> c.owner_id
          AND (u.cognito_sub IS NULL OR u.cognito_sub = c.owner_id)
          AND NOT EXISTS (SELECT 1 FROM users known WHERE known.id = c.owner_id)
          AND NOT EXISTS (SELECT 1 FROM users direct
              WHERE direct.cognito_sub = c.owner_id AND direct.org_id = c.tenant_id
                AND direct.id <> u.id)
          AND (SELECT count(*) FROM user_identities matching
              WHERE matching.provider = 'cognito'
                AND matching.provider_user_id = c.owner_id
                AND matching.org_id = c.tenant_id) = 1
          AND NOT EXISTS (SELECT 1 FROM tenant_memberships membership
              WHERE membership.user_id = u.id AND membership.tenant_id = c.tenant_id
                AND membership.revoked_at IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM l5_service_bindings b
              WHERE b.tenant_id = c.tenant_id AND b.configuration_id = c.configuration_id)
          AND NOT EXISTS (SELECT 1 FROM l5_service_setup_intents s
              WHERE s.tenant_id = c.tenant_id AND s.configuration_id = c.configuration_id)
          AND NOT EXISTS (SELECT 1 FROM l5_service_runs r
              WHERE r.tenant_id = c.tenant_id AND r.configuration_id = c.configuration_id)
    """
    if connection.dialect.name == "postgresql":
        # Fence configuration edits and identity/membership changes through the
        # migration transaction. No migration-time provider or credential access.
        connection.execute(sa.text("LOCK TABLE tenant_memberships IN SHARE MODE"))
        query += " FOR UPDATE OF c, u, i"
    candidates = connection.execute(sa.text(query)).mappings().all()
    for candidate in candidates:
        values = dict(candidate)
        # Retain the original idempotency key and digest. A conflicting receipt
        # aborts the transaction instead of deleting or merging request history.
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
                  AND owner_id = :owner_id AND current_revision = 1
            """),
            values,
        )
    return len(candidates)


def upgrade():
    repair_drafts(op.get_bind())


def downgrade():
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
