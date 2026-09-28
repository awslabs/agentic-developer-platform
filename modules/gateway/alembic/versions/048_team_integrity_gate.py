"""Refuse upgrades with inconsistent team ownership or legacy team pointers.

Issue #4924. Applied migration 040 is immutable. This forward checkpoint detects
the inconsistent memberships it can create from cross-org legacy pointers, and
also rejects invalid nonempty pointers that 040 skips. It does not repair data,
choose a primary team, create missing memberships, or change runtime enforcement.
An operator must resolve any reported placement before retrying the upgrade.

The queries are frozen here so future application changes cannot change what an
upgrade accepts. The read-only operator audit imports this module for the same
predicates. All counts come from one SQL statement/snapshot. This is a checkpoint,
not a constraint against later writes; no lock or data mutation is introduced.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "048_team_integrity_gate"
down_revision: str | None = "047_claude_pricing_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEMBERSHIP_FROM = """
FROM team_memberships tm
LEFT JOIN users u ON u.id = tm.user_id
LEFT JOIN teams t ON t.id = tm.team_id
"""
POINTER_FROM = "FROM users u LEFT JOIN teams t ON t.id = u.team_id"

# Missing references and present-but-mismatched ownership are separate classes.
# IS DISTINCT FROM is deliberately null-safe, unlike <>.
MEMBERSHIP_PREDICATES = {
    "membership_missing_user": "u.id IS NULL",
    "membership_missing_team": "t.id IS NULL",
    "membership_user_org_mismatch": "u.id IS NOT NULL AND tm.org_id IS DISTINCT FROM u.org_id",
    "membership_team_org_mismatch": "t.id IS NOT NULL AND tm.org_id IS DISTINCT FROM t.org_id",
}
POINTER_PREDICATES = {
    "pointer_missing_team": "u.team_id <> '' AND t.id IS NULL",
    "pointer_team_org_mismatch": "u.team_id <> '' AND t.id IS NOT NULL AND u.org_id IS DISTINCT FROM t.org_id",
}


def audit(connection, *, include_ids: bool = False, detail_limit: int = 100, source_pointers_only: bool = False) -> dict:
    """Read counts, and optionally bounded internal-ID details; never write.

    ``source_pointers_only`` is an explicitly limited operator diagnostic. It
    cannot produce CLEAN even if all pointers are valid. Migration upgrade and
    the default operator audit always require the full schema. Query/schema
    errors propagate; they are never converted to zero findings.
    """
    if not 1 <= detail_limit <= 10000:
        raise ValueError("detail_limit must be between 1 and 10000")
    memberships_checked = not source_pointers_only
    sources = ["organizations", "users", "teams"]
    if memberships_checked:
        sources.append("team_memberships")

    cases = {key: (POINTER_FROM, predicate) for key, predicate in POINTER_PREDICATES.items()}
    if memberships_checked:
        cases.update({key: (MEMBERSHIP_FROM, predicate) for key, predicate in MEMBERSHIP_PREDICATES.items()})

    # The identifiers and predicates are migration-local literals, never input.
    queries = [f"SELECT 'source_{table}' AS name, COUNT(*) AS value FROM {table}" for table in sources]
    queries += [f"SELECT '{name}' AS name, COUNT(*) AS value {from_sql} WHERE {predicate}" for name, (from_sql, predicate) in cases.items()]
    counts = dict(connection.execute(sa.text(" UNION ALL ".join(queries))).tuples().all())
    findings = {name: counts[name] for name in cases}
    report = {
        "status": "INCONSISTENT" if any(findings.values()) else "CLEAN" if memberships_checked else "SOURCE_POINTERS_CLEAN",
        "memberships_checked": memberships_checked,
        "sources": {table: counts[f"source_{table}"] for table in sources},
        "findings": findings,
    }
    if include_ids:
        details = {}
        for name, (from_sql, predicate) in cases.items():
            columns = (
                "tm.id AS membership_id, tm.user_id, tm.team_id, tm.org_id, u.org_id AS user_org_id, t.org_id AS team_org_id"
                if name in MEMBERSHIP_PREDICATES
                else "u.id AS user_id, u.team_id, u.org_id AS user_org_id, t.org_id AS team_org_id"
            )
            order = "tm.id" if name in MEMBERSHIP_PREDICATES else "u.id"
            query = sa.text(f"SELECT {columns} {from_sql} WHERE {predicate} ORDER BY {order} LIMIT :detail_limit")
            rows = connection.execute(query, {"detail_limit": detail_limit}).mappings().all()
            details[name] = {"rows": [dict(row) for row in rows], "truncated": counts[name] > detail_limit}
        report["details"] = details
    return report


def upgrade() -> None:
    report = audit(op.get_bind())
    if report["status"] != "CLEAN":
        counts = ", ".join(f"{name}={count}" for name, count in report["findings"].items() if count)
        raise RuntimeError(
            f"Team integrity checkpoint refused upgrade: {counts}. This checkpoint changed no rows. "
            "Run the candidate checkout's scripts/audit_team_integrity.py --include-ids and follow docs/runbooks/gateway-migrations.md; "
            "an explicit operator placement decision is required."
        )


def downgrade() -> None:
    """No schema/data was changed; downgrading only removes the version marker."""
