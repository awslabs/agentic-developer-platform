"""Persona-model preference models — Issue #5419 (PMM-02).

Four tables backing the persona-to-model mapping feature:

- ``PersonaModelPreference`` — one model choice per principal per persona.
- ``ServicePrincipal`` — canonical service-principal entity with lifecycle.
- ``ServicePrincipalAlias`` — maps an external subject to a canonical ID.
- ``PersonaModelPolicySetting`` — per-class platform defaults and posture.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text, true
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TenantMixin, new_uuid, utcnow

# ── Approved vocabularies (approved design §4.1) ─────────────────────────────
# Must stay identical to ALIAS_SOURCES / PRINCIPAL_SOURCES in
# alembic/versions/056_persona_model_prefs.py; the migration parity test asserts it.

ALIAS_SOURCES = ("sa_registration", "agent_registry", "cognito_m2m", "eventbridge", "github_actions")
PRINCIPAL_SOURCES = ("self", *ALIAS_SOURCES)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    """Render ``column IN ('a', 'b')`` for a CHECK constraint."""
    rendered = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({rendered})"


class PersonaModelPreference(Base, TenantMixin):
    """One model choice per principal per persona, tenant-scoped.

    Unique key: ``(org_id, principal_kind, principal_id, persona_key)``.
    ``principal_source`` and ``updated_by_source`` are provenance only —
    no query resolving a preference may filter or branch on either.
    """

    __tablename__ = "persona_model_preferences"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    principal_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    principal_source: Mapped[str] = mapped_column(String(32), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    persona_key: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_alias: Mapped[str | None] = mapped_column(String(128), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_by_source: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "principal_kind", "principal_id", "persona_key", name="uq_persona_model_pref_scope"),
        CheckConstraint("principal_kind IN ('human', 'service_account')", name="ck_persona_pref_principal_kind"),
        CheckConstraint(_in_list("principal_source", PRINCIPAL_SOURCES), name="ck_persona_pref_principal_source"),
        CheckConstraint(_in_list("updated_by_source", PRINCIPAL_SOURCES), name="ck_persona_pref_updated_by_src"),
    )


class ServicePrincipal(Base, TenantMixin):
    """Canonical service-principal entity — the identity row an alias resolves to.

    ``canonical_service_principal_id`` is opaque, immutable and ADP-minted.
    """

    __tablename__ = "service_principals"

    canonical_service_principal_id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    approved_by: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (CheckConstraint("status IN ('active', 'suspended', 'retired')", name="ck_service_principal_status"),)


class ServicePrincipalAlias(Base, TenantMixin):
    """Maps an external subject to a canonical service principal.

    At most one active alias per ``(org_id, alias_source, alias_id)``, enforced by
    the PARTIAL unique index ``uq_spa_active_alias`` declared below and in the
    migration. Declared here too so ``create_all``-based tests enforce the same
    invariant the migration does; see the migration for why a partial index rather
    than a unique constraint or a COALESCE expression.
    """

    __tablename__ = "service_principal_aliases"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    canonical_service_principal_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("service_principals.canonical_service_principal_id"),
        nullable=False,
        index=True,
    )
    alias_source: Mapped[str] = mapped_column(String(32), nullable=False)
    alias_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # true(), NOT "1": PostgreSQL rejects an integer default on a boolean column
    # (42804).  Must match the migration's sa.true() or the parity test fails.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    registered_by: Mapped[str] = mapped_column(String(255), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        CheckConstraint(_in_list("alias_source", ALIAS_SOURCES), name="ck_spa_alias_source"),
        # `is_active` and `revoked_at` encode the same fact and must agree; see the
        # migration for why this closes a real hole in the uniqueness invariant
        # rather than merely tidying up.
        CheckConstraint(
            "(is_active = true AND revoked_at IS NULL AND revoked_by IS NULL) OR (is_active = false AND revoked_at IS NOT NULL)",
            name="ck_spa_revocation_consistent",
        ),
        # Must stay identical to the migration's partial index, including the
        # predicate: a mismatch means create_all-based tests and the deployed
        # database disagree about how many active aliases a triple may have.
        Index(
            "uq_spa_active_alias",
            "org_id",
            "alias_source",
            "alias_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )


class PersonaModelPolicySetting(Base):
    """Per-compatibility-class platform default and enforcement posture.

    Deliberately not TenantMixin: the platform default applies across tenants.
    """

    __tablename__ = "persona_model_policy_settings"

    compatibility_class: Mapped[str] = mapped_column(String(64), primary_key=True)
    harness_contract_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_default_model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active_default_model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    posture_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    enforcement_posture: Mapped[str] = mapped_column(String(32), nullable=False, server_default="report_only")
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "compatibility_class IN ('claude-agent-sdk', 'codex-sdk')",
            name="ck_pmps_compat_class",
        ),
        CheckConstraint(
            "enforcement_posture IN ('disabled', 'report_only', 'enforcing')",
            name="ck_pmps_enforcement_posture",
        ),
    )
