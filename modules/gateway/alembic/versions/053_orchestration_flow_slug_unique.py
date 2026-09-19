"""Tenant-scoped uniqueness for `orchestration_flows (org_id, slug)`.

Issue #4898 (EPIC #4191). Additive: creates ONE unique index, alters no column,
drops nothing and writes no row.

**Why this is part of the graph-address story.** Every node's address begins with
its flow's slug (`{flow.slug}/{epic}/{wave}/{node}`), and the cost readback groups
`usage_logs` by `(org_id, graph_address)`. Until #4898 nothing wrote
`graph_address`, so two same-slug flows in one tenant were a latent nuisance. The
moment addresses are actually persisted it becomes a **wrong number**: both flows'
model spend collapses into one total, with nothing in the result indicating that
two different flows were summed. A cost figure that silently merges two flows is
worse than the `unknown` it replaces, because it looks authoritative. So the
uniqueness that makes an address identify exactly one flow must land with the
write, not after it.

Cross-tenant duplicates stay legal, and that is a product requirement rather than
an oversight: two customers may each run a `delivery-loop` flow, they are
different flows, and `org_id` leads the index so they never collide.

**Why the database and not an application check.** `compile._resolve_flow` lists
the tenant's flows, looks for a matching slug and creates one when it finds none.
Under concurrent registration both callers can read "absent" and both insert —
a read-then-write race no amount of care in Python closes. Only a unique index
makes one of them lose. The application-side recovery from that loss lives in
`OrchestrationRepository.create_flow`.

**Why `upgrade()` can refuse.** Creating the index on a table that already holds
duplicate `(org_id, slug)` groups fails at the database with a message naming an
index and no rows, which tells an operator nothing actionable. So this migration
inspects first and, when it finds duplicates, raises with the tenant/slug pairs
and their counts. Refusing is the correct outcome: resolving real duplicate flows
means deciding which one is canonical and what happens to the other's history —
decisions with cost, plan and audit consequences that belong to the deployment
rollout (coordinator #5134), not to a migration running unattended.

This migration therefore NEVER deletes, merges, renames or reslugs a flow, never
resets flow state, never rewrites a policy and never reattributes historical
charges. There is no backfill of any kind: `usage_logs.graph_address` stays NULL
for every existing row, so historical flows keep reporting `unknown`, which
remains the honest answer for spend that was never attributed. Guessing an
address for a past charge would land real money in some node's total.

The revision id is shortened from the filename stem to stay inside the
`alembic_version.version_num` VARCHAR(32) limit that
`tests/migrations/test_revision_id_length.py` enforces.
"""

import sqlalchemy as sa

from alembic import op

revision = "053_flow_slug_unique"
down_revision = "052_orchestration_executions"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_orchestration_flows_org_slug"
TABLE = "orchestration_flows"


def _duplicate_groups(connection) -> list[tuple[str, str, int]]:
    """Existing `(org_id, slug)` groups with more than one flow.

    Read with the same grouping the unique index will enforce, so the report and
    the constraint cannot disagree about what counts as a duplicate.
    """
    rows = connection.execute(
        sa.text(f"SELECT org_id, slug, COUNT(*) AS n FROM {TABLE} GROUP BY org_id, slug HAVING COUNT(*) > 1 ORDER BY n DESC, org_id, slug")
    ).all()
    return [(row[0], row[1], int(row[2])) for row in rows]


def upgrade() -> None:
    # Offline (`alembic upgrade --sql`) has no database to inspect: `get_bind()`
    # yields a mock whose `execute` returns None, so the duplicate check cannot
    # run and must not be attempted — reaching into it would abort script
    # GENERATION with an AttributeError, which is a worse failure than the one
    # the check exists to improve on. Emitting only the DDL is honest here: the
    # generated script is applied later by an operator, and `CREATE UNIQUE INDEX`
    # itself still refuses to apply over duplicates. The friendly, actionable
    # report is what the normal online path below adds on top of that backstop.
    if op.get_context().as_sql:
        op.create_index(INDEX_NAME, TABLE, ["org_id", "slug"], unique=True)
        return

    connection = op.get_bind()

    duplicates = _duplicate_groups(connection)
    if duplicates:
        # Deliberately actionable and deliberately fatal. The operator gets the
        # exact tenant/slug pairs to resolve; nothing is repaired automatically,
        # because choosing a canonical flow is a data decision with cost and
        # audit consequences (see the module docstring).
        detail = ", ".join(f"org_id={org_id!r} slug={slug!r} ({count} flows)" for org_id, slug, count in duplicates)
        raise RuntimeError(
            f"cannot create {INDEX_NAME}: {len(duplicates)} duplicate (org_id, slug) group(s) already exist "
            f"in {TABLE} — {detail}. Resolve these flows explicitly before upgrading; this migration will not "
            "delete, merge or rename a flow, and will not reattribute its history. See issue #4898."
        )

    op.create_index(INDEX_NAME, TABLE, ["org_id", "slug"], unique=True)


def downgrade() -> None:
    # Unguarded, because this IS the documented rollback path: dropping the index
    # restores the previous (permissive) behaviour and destroys no data. Rolling
    # back attribution must never downgrade or drop a usage row, a flow or its
    # history — and nothing here touches any of those.
    op.drop_index(INDEX_NAME, table_name=TABLE)
