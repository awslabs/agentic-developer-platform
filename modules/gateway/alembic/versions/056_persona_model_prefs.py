"""Persona-model preference schema, service-principal identity and platform settings.

Issue #5419 (PMM-02). Creates four tables:

1. ``service_principals`` — canonical service-principal entity with lifecycle
   (active / suspended / retired).
2. ``service_principal_aliases`` — maps external subjects to canonical IDs.
   Active-alias uniqueness via partial unique index ``WHERE revoked_at IS NULL``.
3. ``persona_model_preferences`` — one model choice per principal per persona,
   with optimistic concurrency via integer revision compare-and-set.
4. ``persona_model_policy_settings`` — per-compatibility-class platform defaults
   with candidate/active model split and enforcement posture.

**Rollback safety:** nothing resolves models from Postgres until PMM-07 ships,
so dropping these four tables cannot change any run's effective model.  Audit
rows in ``security_audit_logs`` survive (no FK).
"""

import sqlalchemy as sa

from alembic import op

revision = "056_persona_model_prefs"
down_revision = "055_orch_environment_leases"
branch_labels = None
depends_on = None

# ── Table names ──────────────────────────────────────────────────────────────

SP_TABLE = "service_principals"
SPA_TABLE = "service_principal_aliases"
PREF_TABLE = "persona_model_preferences"
SETTINGS_TABLE = "persona_model_policy_settings"

# ── Approved vocabularies (approved design §4.1) ─────────────────────────────

# The five alias sources.  Spelled once here so the three CHECK constraints
# below cannot drift from each other.
ALIAS_SOURCES = ("sa_registration", "agent_registry", "cognito_m2m", "eventbridge", "github_actions")

# A preference's provenance is either the principal acting on itself ('self')
# or one of the alias sources it authenticated through.
PRINCIPAL_SOURCES = ("self", *ALIAS_SOURCES)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    """Render ``column IN ('a', 'b')`` for a CHECK constraint."""
    rendered = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    # ── 1. service_principals ────────────────────────────────────────────────
    op.create_table(
        SP_TABLE,
        sa.Column("canonical_service_principal_id", sa.String(255), nullable=False),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("approved_by", sa.String(255), nullable=False),
        sa.PrimaryKeyConstraint("canonical_service_principal_id"),
        sa.CheckConstraint("status IN ('active', 'suspended', 'retired')", name="ck_service_principal_status"),
    )
    op.create_index("ix_service_principals_org_id", SP_TABLE, ["org_id"])

    # ── 2. service_principal_aliases ─────────────────────────────────────────
    op.create_table(
        SPA_TABLE,
        sa.Column("id", sa.String(255), nullable=False),
        sa.Column("canonical_service_principal_id", sa.String(255), nullable=False),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("alias_source", sa.String(32), nullable=False),
        sa.Column("alias_id", sa.String(255), nullable=False),
        # sa.true(), NOT sa.text("1"): PostgreSQL will not implicitly cast integer
        # 1 to boolean in a column default and fails the upgrade with
        # "column is of type boolean but default expression is of type integer"
        # (42804).  SQLite accepts 1, so a create_all-based test cannot catch it.
        # Every sibling Boolean server_default in 021-044 uses sa.false()/sa.true().
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("registered_by", sa.String(255), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["canonical_service_principal_id"],
            [f"{SP_TABLE}.canonical_service_principal_id"],
            name="fk_spa_principal",
        ),
        # The five approved alias sources (approved design §4.1).  'oauth_client'
        # was not one of them and is replaced by 'cognito_m2m'; 'eventbridge' and
        # 'github_actions' were missing entirely, which made those two legitimate
        # caller classes unrecordable.
        sa.CheckConstraint(_in_list("alias_source", ALIAS_SOURCES), name="ck_spa_alias_source"),
        # `is_active` and `revoked_at` encode the SAME fact, so they must agree.
        #
        # Not defensive tidiness — without this the two are independently settable
        # and the uniqueness invariant has a hole. `uq_spa_active_alias` below is
        # partial on `revoked_at IS NULL`, while every read path in
        # `src/admin/persona_models/service.py` filters on `is_active == True`.
        # Set `revoked_at` while leaving `is_active` true and the row leaves the
        # index's scope but stays "active" to the application: two such rows for
        # one (org, source, alias_id) coexist happily, and
        # `resolve_service_principal`'s `scalar()` then resolves one service
        # identity to an arbitrary one of two canonical principals. Verified
        # reachable on PostgreSQL 16 before adding this.
        #
        # Keeping `is_active` at all is redundant given `revoked_at`, but it is
        # what the ORM and the service layer already read, so the constraint is
        # the smaller and safer change: it makes the redundancy self-checking
        # rather than load-bearing.
        sa.CheckConstraint(
            "(is_active = true AND revoked_at IS NULL AND revoked_by IS NULL) OR (is_active = false AND revoked_at IS NOT NULL)",
            name="ck_spa_revocation_consistent",
        ),
    )
    op.create_index("ix_spa_principal", SPA_TABLE, ["canonical_service_principal_id"])
    op.create_index("ix_spa_org_id", SPA_TABLE, ["org_id"])
    # Active-alias uniqueness: at most one LIVE alias per (org, source, alias_id),
    # with unlimited revoked rows retained as history.
    #
    # A PARTIAL unique index, and the three constructs it was chosen over were each
    # rejected against a real PostgreSQL 16 server, not on reasoning:
    #
    #   1. `UniqueConstraint(org_id, alias_source, alias_id)` — permanently burns
    #      the triple on first revocation, so a revoked alias could never be
    #      re-registered.  Also wrong in the opposite direction on PostgreSQL,
    #      which treats NULLs in a unique constraint as distinct.
    #   2. `COALESCE(revoked_at, '')` — migration 036/037's precedent, but theirs
    #      coalesce VARCHAR columns.  revoked_at is TIMESTAMP WITH TIME ZONE, and
    #      PostgreSQL cannot resolve '' as a timestamp.
    #   3. `COALESCE(CAST(revoked_at AS VARCHAR), '')` — the obvious repair to (2),
    #      and still invalid: timestamptz -> text is STABLE, not IMMUTABLE (it
    #      depends on the TimeZone and DateStyle GUCs), and PostgreSQL refuses
    #      non-IMMUTABLE functions in an index expression —
    #      "functions in index expression must be marked IMMUTABLE" (42P17).
    #      It compiles cleanly for the PostgreSQL dialect and executes fine on
    #      SQLite, so ONLY a live upgrade catches it.
    #
    # The partial index needs no expression at all, so it sidesteps the
    # immutability rule entirely.  `WHERE revoked_at IS NULL` indexes only live
    # rows: a second active alias collides, while revoked rows are outside the
    # index and never collide with anything.  Supported by PostgreSQL and by
    # SQLite >= 3.8.0, so the invariant is enforced identically in tests and in
    # dev rather than only asserted in one of them.
    op.create_index(
        "uq_spa_active_alias",
        SPA_TABLE,
        ["org_id", "alias_source", "alias_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        sqlite_where=sa.text("revoked_at IS NULL"),
    )

    # ── 3. persona_model_preferences ─────────────────────────────────────────
    op.create_table(
        PREF_TABLE,
        sa.Column("id", sa.String(255), nullable=False),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("principal_kind", sa.String(16), nullable=False),
        sa.Column("principal_source", sa.String(32), nullable=False),
        sa.Column("principal_id", sa.String(255), nullable=False),
        sa.Column("persona_key", sa.String(64), nullable=False),
        sa.Column("canonical_model_id", sa.String(255), nullable=False),
        sa.Column("requested_alias", sa.String(128), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(255), nullable=False),
        sa.Column("updated_by_source", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "principal_kind IN ('human', 'service_account')",
            name="ck_persona_pref_principal_kind",
        ),
        sa.CheckConstraint(_in_list("principal_source", PRINCIPAL_SOURCES), name="ck_persona_pref_principal_source"),
        sa.CheckConstraint(_in_list("updated_by_source", PRINCIPAL_SOURCES), name="ck_persona_pref_updated_by_src"),
        sa.UniqueConstraint("org_id", "principal_kind", "principal_id", "persona_key", name="uq_persona_model_pref_scope"),
    )
    op.create_index("ix_persona_model_pref_org_id", PREF_TABLE, ["org_id"])
    op.create_index("ix_persona_model_pref_principal", PREF_TABLE, ["principal_id"])

    # ── 4. persona_model_policy_settings ─────────────────────────────────────
    op.create_table(
        SETTINGS_TABLE,
        sa.Column("compatibility_class", sa.String(64), nullable=False),
        sa.Column("harness_contract_revision", sa.String(64), nullable=True),
        sa.Column("candidate_default_model_id", sa.String(255), nullable=True),
        sa.Column("active_default_model_id", sa.String(255), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("posture_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enforcement_posture", sa.String(32), nullable=False, server_default="report_only"),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("compatibility_class"),
        sa.CheckConstraint(
            "compatibility_class IN ('claude-agent-sdk', 'codex-sdk')",
            name="ck_pmps_compat_class",
        ),
        sa.CheckConstraint(
            "enforcement_posture IN ('disabled', 'report_only', 'enforcing')",
            name="ck_pmps_enforcement_posture",
        ),
    )

    # Seed the Claude compatibility class.  Both default model IDs are NULL:
    # the candidate is pending live invocation proof, and seeding an unproven
    # value would be the inert-config class at platform scale.
    op.execute(sa.text(f"INSERT INTO {SETTINGS_TABLE} (compatibility_class, enforcement_posture) VALUES ('claude-agent-sdk', 'report_only')"))


def downgrade() -> None:
    """Drop all four tables.

    Safe because nothing resolves from these tables until PMM-07: dropping them
    cannot change any run's effective model.  Audit rows in security_audit_logs
    survive (no FK cascade).
    """
    op.drop_table(PREF_TABLE)
    op.drop_table(SPA_TABLE)
    op.drop_table(SP_TABLE)
    op.drop_table(SETTINGS_TABLE)
