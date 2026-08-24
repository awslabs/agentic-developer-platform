"""Add channel_tenant_map.installation_id, backfill it, and quarantine conflicts.

Issue #4070 (sub-EPIC #4068 ·A0): give the installation -> tenant mapping a
column of its own so uniqueness can be enforced on it.

Why a new column instead of reusing provider_scope_id
-----------------------------------------------------
``provider_scope_id`` is a per-provider ACCOUNT/WORKSPACE key that already
carries four value shapes (GitHub numeric account id or login;
``personal:<gh_account_id>:<adp_user_id>``; Slack and WhatsApp workspace ids)
and has four live readers. Slack/WhatsApp rows have no installation at all, so
the column cannot be made to mean "installation id". Two writers disagreeing
about its meaning — one storing a GitHub *account* id, the other an
*installation* id — is why ``uq_channel_tenant_map_provider_scope`` never fired
on a duplicate claim: the two values live in disjoint number spaces and so never
collided. Putting the installation id in its own column points a constraint at
the thing that actually needs to be unique, without touching any existing reader.

THIS MIGRATION DOES NOT ADD THE CONSTRAINT. The unique index lands in 027, and
the split is mechanical, not stylistic: 027 uses
``CREATE UNIQUE INDEX CONCURRENTLY``, which cannot run in the same transaction
as the DML below. Dedup must also strictly precede the constraint, or the
constraint fails on apply against existing duplicate rows.

Duplicate handling (decision D3) — quarantine, never guess
----------------------------------------------------------
* Provably-redundant duplicates (same installation, same ``org_id``, differing
  only because of the account-id/installation-id keyspace split) are collapsed.
  No owner changes, so this is safe.
* Genuine cross-tenant conflicts (different ``org_id``s claiming one
  installation) are recorded in ``installation_ownership_conflicts`` and
  **left in place**. Only GitHub can authoritatively break that tie and Alembic
  has no App credentials; any heuristic would silently re-home a paying
  customer, and a DELETE is not covered by this migration's rollback. The
  resolver reads the quarantine table and fails closed instead.

Revision ID: 026_channel_tenant_map_installation_id
Revises: 025_org_created_via
Create Date: 2026-08-23
"""

import json
import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "026_channel_tenant_map_installation_id"
down_revision: str | None = "025_org_created_via"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")


def _installation_id_expr(dialect: str) -> str:
    """SQL extracting installation_id out of the legacy ``metadata`` JSON blob.

    ``->>`` is Postgres-only and the test suite runs SQLite, so the SQLite branch
    uses ``json_extract``. Without this split the migration could not be tested.
    """
    if dialect == "postgresql":
        return "metadata->>'installation_id'"
    return "json_extract(metadata, '$.installation_id')"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    inspector = sa.inspect(bind)

    # -- 1. Add the column + lookup index (idempotent) ----------------------
    # Inspect-first so a partial prior apply (e.g. DDL committed outside a
    # transaction that alembic's version bump then rolled back) re-runs cleanly.
    existing_cols = {c["name"] for c in inspector.get_columns("channel_tenant_map")}
    if "installation_id" not in existing_cols:
        op.add_column("channel_tenant_map", sa.Column("installation_id", sa.String(64), nullable=True))
    if "ownership_disputed" not in existing_cols:
        # Denormalized conflict flag, consumed by 027's partial index predicate.
        # It exists as a column because a Postgres partial-index predicate may
        # only reference columns of the indexed table — a subquery against
        # installation_ownership_conflicts would be rejected outright.
        op.add_column(
            "channel_tenant_map",
            sa.Column("ownership_disputed", sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    existing_indexes = {i["name"] for i in inspector.get_indexes("channel_tenant_map")}
    if "ix_channel_tenant_map_installation_id" not in existing_indexes:
        op.create_index("ix_channel_tenant_map_installation_id", "channel_tenant_map", ["installation_id"])

    # -- 2. Quarantine table (idempotent) ----------------------------------
    if "installation_ownership_conflicts" not in inspector.get_table_names():
        op.create_table(
            "installation_ownership_conflicts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("installation_id", sa.String(64), nullable=False),
            sa.Column("org_id", sa.String(255), nullable=False),
            sa.Column("source", sa.String(64), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("installation_id", "org_id", name="uq_installation_ownership_conflicts_claim"),
        )
        op.create_index(
            "ix_installation_ownership_conflicts_installation_id",
            "installation_ownership_conflicts",
            ["installation_id"],
        )

    # -- 3. Backfill from the legacy metadata blob -------------------------
    # install_callback has populated metadata->installation_id since migration
    # 013, so this recovers ownership for every row written by that path.
    expr = _installation_id_expr(dialect)
    op.execute(
        sa.text(f"""
            UPDATE channel_tenant_map
               SET installation_id = {expr}
             WHERE provider = 'github'
               AND installation_id IS NULL
               AND {expr} IS NOT NULL
        """)  # noqa: S608 - expr is a dialect literal chosen above, not user input
    )

    # -- 4. Backfill from the second writer's representation ----------------
    # organizations.github_installation_ids is the other record of ownership.
    # Read and matched in Python: JSONB array containment is spelled differently
    # on each dialect, and the org set is small.
    orgs = bind.execute(sa.text("SELECT id, github_installation_ids FROM organizations")).fetchall()

    # installation_id -> {org_id, ...}, union of BOTH representations. This is
    # what tells provably-redundant duplicates apart from real conflicts.
    claims: dict[str, set[str]] = {}
    claim_sources: dict[tuple[str, str], str] = {}

    for org_id, raw_ids in orgs:
        ids = raw_ids
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except (TypeError, ValueError):
                ids = []
        for install_id in ids or []:
            key = str(install_id)
            claims.setdefault(key, set()).add(org_id)
            claim_sources.setdefault((key, org_id), "organizations.github_installation_ids")

    rows = bind.execute(
        sa.text("SELECT id, org_id, installation_id FROM channel_tenant_map WHERE provider = 'github' AND installation_id IS NOT NULL")
    ).fetchall()
    for _row_id, org_id, install_id in rows:
        key = str(install_id)
        claims.setdefault(key, set()).add(org_id)
        claim_sources[(key, org_id)] = "channel_tenant_map"

    # -- 5. Collapse provably-redundant duplicates -------------------------
    # Same installation AND same org_id across multiple rows: no owner change,
    # so this is safe to collapse. Keep the oldest row (stable, and it carries
    # the original created_at); this is a within-tenant choice only and can
    # never re-home anyone.
    collapsed = 0
    for install_id, owners in claims.items():
        if len(owners) != 1:
            continue
        dupe_rows = bind.execute(
            sa.text("""
                SELECT id FROM channel_tenant_map
                 WHERE provider = 'github'
                   AND installation_id = :iid
                   AND org_id = :org_id
                 ORDER BY created_at ASC, id ASC
            """),
            {"iid": install_id, "org_id": next(iter(owners))},
        ).fetchall()
        for (row_id,) in dupe_rows[1:]:
            bind.execute(sa.text("DELETE FROM channel_tenant_map WHERE id = :id"), {"id": row_id})
            collapsed += 1

    # -- 6. Quarantine genuine cross-tenant conflicts (D3) -----------------
    # Recorded, NOT resolved. Nothing is deleted and no owner is reassigned; the
    # resolver returns AMBIGUOUS for these and fails closed until an operator
    # runs scripts/resolve_installation_conflicts.py, which can ask GitHub.
    if dialect == "postgresql":
        uuid_expr = "gen_random_uuid()::text"
    else:
        uuid_expr = (
            "lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-4' || "
            "substr(hex(randomblob(2)),2) || '-' || "
            "substr('89ab', abs(random()) % 4 + 1, 1) || "
            "substr(hex(randomblob(2)),2) || '-' || hex(randomblob(6)))"
        )

    quarantined = 0
    for install_id, owners in sorted(claims.items()):
        if len(owners) < 2:
            continue
        for org_id in sorted(owners):
            already = bind.execute(
                sa.text("SELECT 1 FROM installation_ownership_conflicts WHERE installation_id = :iid AND org_id = :org_id"),
                {"iid": install_id, "org_id": org_id},
            ).fetchone()
            if already:
                continue
            bind.execute(
                sa.text(  # noqa: S608 - uuid_expr is a dialect literal chosen above
                    f"INSERT INTO installation_ownership_conflicts (id, installation_id, org_id, source) VALUES ({uuid_expr}, :iid, :org_id, :source)"
                ),
                {
                    "iid": install_id,
                    "org_id": org_id,
                    "source": claim_sources.get((install_id, org_id), "unknown"),
                },
            )
            quarantined += 1
        # Flag every mapping row for this installation so 027's partial index
        # skips it. Without this the constraint would fail to build on precisely
        # the deployments that already have a conflict.
        bind.execute(
            sa.text("UPDATE channel_tenant_map SET ownership_disputed = true WHERE provider = 'github' AND installation_id = :iid"),
            {"iid": install_id},
        )
        logger.warning(
            "026: installation %s is claimed by %d tenants (%s) — QUARANTINED, not resolved. "
            "Ownership must be settled with scripts/resolve_installation_conflicts.py before it can be used.",
            install_id,
            len(owners),
            sorted(owners),
        )

    # Logged so a revert can be reasoned about — the issue's rollback requirement.
    logger.info(
        "026 complete: %d redundant duplicate row(s) collapsed, %d conflicting claim(s) quarantined across %d installation(s).",
        collapsed,
        quarantined,
        sum(1 for owners in claims.values() if len(owners) > 1),
    )


def downgrade() -> None:
    """Drop the column, its index, and the quarantine table.

    The backfill is derivable from ``metadata`` and
    ``organizations.github_installation_ids``, both untouched here, so dropping
    the column loses no information. The collapse of redundant duplicates in
    step 5 is NOT reversible — which is exactly why genuine conflicts were
    quarantined rather than deleted.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "installation_ownership_conflicts" in inspector.get_table_names():
        op.drop_table("installation_ownership_conflicts")

    existing_indexes = {i["name"] for i in inspector.get_indexes("channel_tenant_map")}
    if "ix_channel_tenant_map_installation_id" in existing_indexes:
        op.drop_index("ix_channel_tenant_map_installation_id", table_name="channel_tenant_map")

    existing_cols = {c["name"] for c in inspector.get_columns("channel_tenant_map")}
    if "installation_id" in existing_cols:
        op.drop_column("channel_tenant_map", "installation_id")
    if "ownership_disputed" in existing_cols:
        op.drop_column("channel_tenant_map", "ownership_disputed")
