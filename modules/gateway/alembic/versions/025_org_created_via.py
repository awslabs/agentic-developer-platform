"""Add created_via provenance to organizations.

Issue #2724 (slice B): "is this GitHub org a known ADP tenant?" was
unrepresentable. The webhook auto-register path had no way to tell an org an
operator/admin deliberately onboarded from one the platform auto-created for
whoever happened to click Install on a public GitHub App — so it onboarded
everyone, copied the platform App private key into a per-tenant secret, and
handed out agent compute.

Tenant *existence* is not a usable signal because the attacker can create it:
install-callback's unauthenticated no-nonce path calls _upsert_org_tenant_shell
itself when ORG_TENANT_AUTO_CREATE is on. Provenance is the signal that is not
attacker-writable — it records WHICH path created the row:

    operator          — pre-existing rows / operator-provisioned (default)
    register_flow     — created by a nonce-authenticated ADP flow (an
                        authenticated user deliberately registered/installed)
    install_autocreate — self-created shell from the unauthenticated no-nonce
                        install callback; NOT trusted for auto-register

NOT NULL with server_default='operator' — the default IS the backfill, so every
pre-existing organization grandfathers in as trusted and existing deployments
keep routing (the reference deployment's live installations must not break).

Revision ID: 025_org_created_via
Revises: 024_budget_usage_bigint_tokens
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: I001

from alembic import op

revision: str = "025_org_created_via"
down_revision: str | None = "024_budget_usage_bigint_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add organizations.created_via, backfilling existing rows as 'operator'."""
    op.add_column(
        "organizations",
        sa.Column(
            "created_via",
            sa.String(length=32),
            nullable=False,
            server_default="operator",
        ),
    )


def downgrade() -> None:
    """Remove organizations.created_via."""
    op.drop_column("organizations", "created_via")
