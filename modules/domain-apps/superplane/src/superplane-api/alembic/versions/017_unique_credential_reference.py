"""Make credential registration idempotent per organization.

Revision ID: 017_unique_credential_reference
Revises: 016_add_organization_grants
"""

import sqlalchemy as sa
from alembic import op

revision = "017_unique_credential_reference"
down_revision = "016_add_organization_grants"
branch_labels = None
depends_on = None


class DuplicateCredentialReferencesError(RuntimeError):
    """Existing rows need an operator-selected canonical registration."""


def _duplicate_references() -> list[dict]:
    if op.get_context().as_sql:
        return []
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id, org_id, adp_credential_id, provider, friendly_name, "
                "credential_type, status FROM credential_registry "
                "ORDER BY org_id, adp_credential_id, id"
            )
        )
        .mappings()
    )
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (str(row["org_id"]), str(row["adp_credential_id"]))
        grouped.setdefault(key, []).append(dict(row))
    return [
        {"org_id": org_id, "reference": reference, "rows": duplicates}
        for (org_id, reference), duplicates in grouped.items()
        if len(duplicates) > 1
    ]


def _refuse_duplicates(duplicates: list[dict]) -> None:
    if not duplicates:
        return
    summaries = []
    for duplicate in duplicates:
        metadata = {
            (
                row["provider"],
                row["friendly_name"],
                row["credential_type"],
                row["status"],
            )
            for row in duplicate["rows"]
        }
        kind = "identical metadata" if len(metadata) == 1 else "conflicting metadata"
        ids = ", ".join(str(row["id"]) for row in duplicate["rows"])
        summaries.append(
            f"org={duplicate['org_id']} reference={duplicate['reference']} ({kind}; record ids: {ids})"
        )
    raise DuplicateCredentialReferencesError(
        "Cannot add uq_credential_registry_org_adp_credential because duplicate registrations exist: "
        + "; ".join(summaries)
        + ". For each group, choose the authoritative record with the tenant owner, repoint "
        "cluster_vault_assignments and credential_audit_log rows to that record, delete only the "
        "confirmed duplicate registry rows, and rerun the migration. No schema or data was changed."
    )


def upgrade() -> None:
    _refuse_duplicates(_duplicate_references())
    op.create_unique_constraint(
        "uq_credential_registry_org_adp_credential",
        "credential_registry",
        ["org_id", "adp_credential_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_credential_registry_org_adp_credential",
        "credential_registry",
        type_="unique",
    )
