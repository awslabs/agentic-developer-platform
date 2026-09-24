"""Record WHO acted and WHETHER it was allowed, on the events table (#5673, A17).

WHAT WAS WRONG WITH THE OLD SHAPE

The events table had one actor column, `user_id`, and the audit middleware wrote
`str(org_id)` into it. So every row named the ORGANIZATION that owned the action and
nothing named the person who performed it: "who ran this provisioning action" was
unanswerable from the audit trail by construction, not by accident of missing data.

The table also had no place to record a refusal. The middleware only wrote rows for
successful responses, so the schema never needed an outcome column -- and an audit
trail that cannot represent "this attempt was denied" cannot show a tenant-isolation
probe, which is the main thing it exists to show.

THE THREE CHANGES, AND WHY EACH IS SHAPED THIS WAY

* `principal` -- the acting subject, separate from the tenant. Added as a NEW column
  rather than by redefining `user_id`, because the two columns mean different things
  and existing rows genuinely hold a tenant identifier in `user_id`. Rewriting those
  rows to move the value would assert that the old value was a principal, which it was
  not. NULL therefore means "a row written before this migration", which is the marker
  that keeps historical rows distinguishable from new ones.

* `outcome` -- ALLOWED, DENIED or ERROR. Deliberately NULLABLE with NO server default. A
  default of 'allowed' would be the more convenient choice and is the wrong one: it
  would silently assert that every pre-existing row was an allowed attempt. They were
  all successes, so that happens to be true today, but it writes an inference into data
  and a later reader cannot tell the inference from a recorded fact. NULL says "this row
  predates outcome recording", which is what is actually known.

* `org_id` -- relaxed to NULLABLE. This is the change that makes unauthenticated
  attempts recordable at all. A request rejected before any identity was established has
  no tenant, and the old NOT NULL constraint left only two options: invent a tenant, or
  drop the record. The middleware chose to drop it, which is finding
  f-93177516-24d1-451e-bfd1-ddd2e3c60048. Relaxing the constraint is what turns "cannot
  be recorded" into "recorded as unattributed".

WHY THIS IS SAFE TO APPLY BEFORE THE NEW IMAGE ROLLS

Every change is additive or a constraint relaxation. The currently-deployed code does
not reference `principal` or `outcome` and always supplies `org_id`, so it keeps working
unchanged against this schema. That ordering is deliberate: the migration can be applied
ahead of the image, and a rollback to the previous image does not require reversing it.

Downgrade refuses if unattributed audit records exist. Preserve/export that evidence
through a separately authorized operational procedure before restoring NOT NULL.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers
revision = "023_add_event_principal_outcome"
down_revision = "022_deployment_namespace_quota"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add principal/outcome and allow unattributed audit rows."""
    op.add_column("events", sa.Column("principal", sa.String(255), nullable=True))
    op.add_column("events", sa.Column("outcome", sa.String(16), nullable=True))

    # The constraint relaxation that makes an unauthenticated attempt recordable.
    op.alter_column(
        "events",
        "org_id",
        existing_type=postgresql.UUID(),
        nullable=True,
    )

    op.create_index(
        "ix_events_principal_created_at", "events", ["principal", "created_at"]
    )


def downgrade() -> None:
    """Reverse only when no unattributed audit evidence would be lost."""
    op.drop_index("ix_events_principal_created_at", table_name="events")

    # Restoring NOT NULL must refuse while unattributed evidence exists.
    # Export/preservation and cleanup require a separate operator decision.
    op.alter_column(
        "events",
        "org_id",
        existing_type=postgresql.UUID(),
        nullable=False,
    )

    op.drop_column("events", "outcome")
    op.drop_column("events", "principal")
