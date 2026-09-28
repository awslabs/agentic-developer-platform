"""Normalize organization rate-limit scopes to the admin API's spelling (#4952).

The admin API already writes 'org'. This update only changes rows written by
legacy callers using 'organization'; it is a no-op when none exist. Duplicate
scopes require an operator decision about which config to retain. Never silently
delete a configured control or choose one by database row order.

Before deploying, review all org-scope values: the corrected limiter will enforce
previously inert limits. Downgrade leaves 'org' intact, matching the pre-existing
admin API contract. No limits or quota values are changed by this migration.
"""

import sqlalchemy as sa

from alembic import op

revision = "044_ratelimit_org_type"
down_revision = "043_person_anchor_rekey"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE rate_limit_configs IN SHARE ROW EXCLUSIVE MODE"))
    duplicate = connection.execute(
        sa.text("""
        SELECT org_id, entity_id
        FROM rate_limit_configs
        WHERE entity_type IN ('org', 'organization')
        GROUP BY org_id, entity_id
        HAVING COUNT(*) > 1
        LIMIT 1
    """)
    ).first()
    if duplicate:
        raise RuntimeError(
            "Duplicate organization rate limits require review before migration 044: "
            f"org_id={duplicate.org_id}, entity_id={duplicate.entity_id}. Retain the intended config and retry."
        )
    connection.execute(sa.text("UPDATE rate_limit_configs SET entity_type = 'org' WHERE entity_type = 'organization'"))


def downgrade() -> None:
    # The old admin API also writes 'org'; there is no safe inverse rename.
    pass
