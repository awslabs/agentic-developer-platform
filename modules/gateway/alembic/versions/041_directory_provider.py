"""Admit `directory` into the user_identities.provider CHECK constraint.

Issue #4843 (T4, EPIC #4839). Design note
`docs/design-notes/4828-platform-native-org-team-user.md` §4, ruling R7.

**Why this migration must exist at all, and why it is not an edit to 009.**
`user_identities.provider` carries a Postgres CHECK constraint built from a
hand-copied tuple in `009_provider_check_constraint`, at a revision that has
already run on every deployed database. Adding a member to
`src/shared/identity/providers.py::IdentityProvider` therefore does NOT make the
value writable — the enum admits it, the database rejects it. Worse, the failure
is invisible in CI: the test database is SQLite, which does not enforce CHECK
constraints created this way, so every test passes and the first INSERT on
Postgres is the one that fails. `providers.py`'s "Adding a new channel = one line
in this set" docstring asserted otherwise and has been corrected in the same
change; the design note's blast-radius table names this exact class ("New provider
insert fails at runtime").

DROP-then-ADD rather than a second constraint: two overlapping CHECKs on one
column both have to pass, so leaving 009's in place would still reject
`directory`. The constraint NAME is reused so the schema converges on one
constraint per column no matter how many providers are added later.

`SUPPORTED_PROVIDERS` is spelled out as a literal tuple here rather than imported
from `src.shared.identity.providers`, matching 009's shape deliberately: a
migration is a snapshot of the schema at a point in time, and importing the live
enum would make an already-applied migration's meaning change every time the enum
does. 041 states what 041 does. The parity between this tuple and the enum is
asserted by `tests/migrations/test_041_directory_provider.py`, which fails if a
provider is added to the enum without a follow-up migration — the guard that makes
the two-step rule enforceable rather than merely documented.

Postgres-only, like 009. SQLite has no `ALTER TABLE ... DROP CONSTRAINT`, and the
ORM's `@validates("provider")` hook is what enforces the provider set in tests.

**Down-migration** restores the pre-041 constraint exactly. It will FAIL if any
`directory` row has been written by then, which is correct and not a defect: the
alternative is deleting a person's identity row to satisfy a rollback, silently
re-keying their person anchor. Delete or re-provider such rows deliberately first.

Revision ID: 041_directory_provider
Revises: 040_team_memberships
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "041_directory_provider"
down_revision: str | None = "040_team_memberships"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT_NAME = "ck_user_identities_provider"
TABLE = "user_identities"

# The provider set as of THIS revision. Must match
# src/shared/identity/providers.py::SUPPORTED_PROVIDERS — pinned by this
# migration's test.
SUPPORTED_PROVIDERS = ("cognito", "github", "slack", "teams", "discord", "email", "whatsapp", "directory")

# The set as of 009, restored by downgrade().
PRE_041_PROVIDERS = ("cognito", "github", "slack", "teams", "discord", "email", "whatsapp")


def _replace_provider_check(providers: tuple[str, ...]) -> None:
    """Drop the provider CHECK and recreate it over `providers`. Postgres only."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    providers_list = ", ".join(f"'{p}'" for p in providers)
    op.execute(sa.text(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME}"))
    op.execute(
        sa.text(  # nosemgrep: avoid-sqlalchemy-text
            f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT_NAME} CHECK (provider IN ({providers_list}))"
        )
    )


def upgrade() -> None:
    _replace_provider_check(SUPPORTED_PROVIDERS)


def downgrade() -> None:
    _replace_provider_check(PRE_041_PROVIDERS)
