"""Scope users.cognito_sub uniqueness to (org_id, cognito_sub). Issue #6127.

DESIGN.md 2.2/4.1: "The domain user model currently has one `org_id` and a
globally unique nullable `cognito_sub`. It must not be assumed to mirror every
ADP multi-organization user account... any projection/schema changes needed
for multi-organization users require an explicit migration and tests, not
duplicate-user creation or automatic merging."

The `ix_users_cognito_sub` index created by `007_add_users_table` is unique
across the WHOLE `users` table, so one ADP subject can hold at most one
`users` row anywhere in the system. That makes "one human, member of two
organizations" unrepresentable in this table: `POST /users/invite` (or any
future write with the same subject in a second organization) collides on the
existing row's `cognito_sub` regardless of `org_id`.

This migration narrows the constraint to `(org_id, cognito_sub)` — the same
person may hold one row per organization, and each row's role/status stays
local to that organization, matching the org-scoped `WorkspaceGrantRecord`/
`OrganizationGrantRecord` model this issue's other changes already use.

DATA-PRESERVING: every row already satisfies the old, stricter global-unique
rule, and the old rule implies the new, looser per-organization rule (if no
two rows share `cognito_sub` anywhere, no two rows sharing `cognito_sub` share
an `org_id` either). No row is rejected, merged or rewritten by this migration.
Ambiguous legacy identities are out of scope for a rename: nothing here changes
which row an existing lookup resolves to, because every existing lookup already
filters by `org_id` (`app/routers/users.py`) and none currently keys by
`cognito_sub` alone.

Revision ID: 036_users_cognito_sub_per_org
Revises: 035_controller_network_journal
Create Date: 2026-09-25
"""

from alembic import op

revision = "036_users_cognito_sub_per_org"
down_revision = "035_controller_network_journal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_users_cognito_sub", table_name="users")
    op.create_index(
        "ix_users_org_cognito_sub",
        "users",
        ["org_id", "cognito_sub"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_users_org_cognito_sub", table_name="users")
    op.create_index(
        "ix_users_cognito_sub",
        "users",
        ["cognito_sub"],
        unique=True,
    )
