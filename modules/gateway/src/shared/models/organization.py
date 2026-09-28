from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TenantMixin, new_uuid, utcnow

# Issue #2724 (slice B): organizations.created_via values — see the column
# comment below. Mirrors the webhook Lambda's copy in
# webhook-ingress/lambda/common/gateway_client.py; the two are deliberately
# separate deploy units, so the values are a wire contract between them
# (carried by POST /internal/v1/resolve-installation) and must not diverge.
CREATED_VIA_OPERATOR = "operator"
CREATED_VIA_REGISTER_FLOW = "register_flow"
CREATED_VIA_INSTALL_AUTOCREATE = "install_autocreate"

# Provenances meaning "an ADP operator or an authenticated ADP flow onboarded
# this tenant". Anything outside this set is a self-created shell, which the
# platform must not promote (no per-tenant App credentials, no routable
# identity-index row) without a deliberate open-onboarding opt-in.
TRUSTED_CREATED_VIA = frozenset({CREATED_VIA_OPERATOR, CREATED_VIA_REGISTER_FLOW})


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    aws_accounts: Mapped[dict] = mapped_column(JSON, nullable=False, default=list)
    role_mappings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Issue #375: Identity columns for tenant-identity Phase A
    # Uses JSON type in model (compatible with SQLite for tests); migration uses JSONB with GIN indexes.
    github_installation_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    cognito_client_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Issue #2952: Stable numeric GitHub org ID for org-tenant keying.
    # Nullable for pre-existing orgs created before this migration.
    github_org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Issue #2952 (D11): GitHub App ID for registry seeding.
    github_app_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Issue #2954: Nullable self-FK for multi-org-to-tenant linking (rule 3).
    # A linked org's row points at the parent tenant. Matcher resolves
    # parent_tenant_id or id.
    parent_tenant_id: Mapped[str | None] = mapped_column(
        String(255),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Issue #719: Per-tenant policy for auto-approving org members on sign-up.
    # Valid values: "auto_approve_org_members", "require_admin_approval"
    member_approval_policy: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="auto_approve_org_members",
        server_default="auto_approve_org_members",
    )
    # Issue #2724 (slice B): provenance — WHICH path created this tenant row.
    # The webhook auto-register gate reads this to decide whether an installing
    # org is a tenant an ADP operator/flow onboarded, or one the platform
    # auto-created for whoever clicked Install on a public App. Tenant
    # *existence* is attacker-creatable (install-callback's unauthenticated
    # no-nonce path upserts a shell); provenance is not.
    #   "operator"           — pre-existing / operator-provisioned (trusted)
    #   "register_flow"      — created by a nonce-authenticated ADP flow (trusted)
    #   "install_autocreate" — self-created shell from the unauthenticated
    #                          no-nonce install callback (NOT trusted)
    # Defaults to "operator" so pre-migration rows grandfather in as trusted.
    #
    # Issue #4842 (R6=a): that default is a BACKFILL, not a policy. Every writer
    # in this codebase passes ``created_via`` explicitly, so no NEW row inherits
    # trust from a column default — trust is asserted by the path that mints the
    # row. Three paths (both admin org-create services and the access-request
    # approval) previously fell through to this default and were therefore
    # trusted by accident; they now state the value. Keep it that way: a new
    # creation path that omits the argument silently mints a trusted tenant.
    created_via: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="operator",
        server_default="operator",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Department(Base, TenantMixin):
    __tablename__ = "departments"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    budget_limit: Mapped[Decimal | None] = mapped_column(Numeric(precision=15, scale=2))
    # Cognito field - replaces identity_center_group_id
    cognito_group_name: Mapped[str | None] = mapped_column(String(255))
    # Keep legacy field for backward compatibility during migration
    identity_center_group_id: Mapped[str | None] = mapped_column(String(255))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=utcnow)


class Team(Base, TenantMixin):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    department_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Keep legacy field for backward compatibility during migration
    identity_center_group_id: Mapped[str | None] = mapped_column(String(255))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=utcnow)


class User(Base, TenantMixin):
    __tablename__ = "users"
    __table_args__ = (
        # Issue #700: prevent duplicate canonical rows for the same Cognito identity.
        # Partial unique index — only enforced for non-NULL cognito_sub values.
        Index(
            "uq_users_cognito_sub",
            "cognito_sub",
            unique=True,
            postgresql_where=text("cognito_sub IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    team_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str | None] = mapped_column(String(64))
    # Cognito fields - replaces identity_center_user_id
    cognito_sub: Mapped[str | None] = mapped_column(String(255), index=True)
    cognito_username: Mapped[str | None] = mapped_column(String(255))
    # Keep legacy field for backward compatibility during migration
    identity_center_user_id: Mapped[str | None] = mapped_column(String(255))
    # Issue #446: shadow users are auto-provisioned from channel_tenant_map;
    # they can receive agent messages but cannot log into the ADP UI.
    is_shadow: Mapped[bool] = mapped_column(default=False, server_default="false", nullable=False)
    # Issue #780: Bot identity discriminator — 'human' (default) or 'bot'.
    user_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="human", server_default="human")
    # Issue #780: Agent slug for bots (e.g. 'agent-developer'). NULL for humans.
    bot_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=utcnow)


class TeamMembership(Base, TenantMixin):
    """Many-to-many user<->team membership, with at most one primary per user per org.

    Issue #4840 (EPIC #4839), design note `4828-platform-native-org-team-user.md`
    §2.1 (ruling R2). Additive and reversible: ``users.team_id`` remains the
    denormalized *primary-team pointer*, so the Cognito pre-token Lambda and every
    existing ``custom:team_id`` consumer keep working untouched. This table is the
    authority for the full set; ``users.team_id`` is a cache of the primary, and
    the two are kept in step by ``src/admin/team_memberships.py``.

    NOT to be confused with ``tenant_memberships`` (migration 021,
    ``src/shared/models/onboarding.py``), which is one grain UP (user<->org) and is
    the live authority for *org role* in ``admin/access_control.py``. The names
    differ by one character; the grains, and the questions they answer, do not
    overlap. A row here confers team membership only — never authority.

    The one-primary-per-user-per-org rule is enforced by
    ``uq_team_memberships_one_primary``, a PostgreSQL-only partial unique index
    created by migration 040 behind a dialect guard. It is deliberately **NOT
    declared here**, exactly as ``TenantMembership`` (migration 021) omits its own
    ``WHERE is_active`` index: SQLAlchemy emits ``postgresql_where`` only on
    PostgreSQL, so ``create_all()`` on SQLite — how the test suite builds its schema
    — would render it as a *plain* unique index on ``(user_id, org_id)`` and reject
    a user's second, non-primary team, breaking the very feature this table exists
    for. (This is not hypothetical: declaring it here made
    ``test_second_non_primary_team_is_accepted`` fail with
    ``UNIQUE constraint failed: team_memberships.user_id, team_memberships.org_id``.)

    The consequence is that the SQLite suite does NOT enforce one-primary at the DB
    level, so the invariant ALSO lives in the application layer — see
    ``src/admin/team_memberships.py`` — and the real Postgres DDL is asserted in
    ``tests/migrations/test_040_team_memberships.py``.
    """

    __tablename__ = "team_memberships"
    __table_args__ = (UniqueConstraint("user_id", "team_id", name="uq_team_memberships_user_team"),)

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    team_id: Mapped[str] = mapped_column(String(255), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member", server_default="member")
    is_primary: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=text("false"))
    # Provenance: 'admin' (a human/API assignment) or 'ad_sync' etc. once Wave 2
    # directory sync lands. Kept a plain string rather than an enum so a new
    # source needs no migration.
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="admin", server_default="admin")
    # Directory-system identity for synced rows (e.g. an AD group member id).
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # server_default matters here (and is why this mirrors TenantMembership rather
    # than the other models in this file): migration 040's backfill is a raw
    # INSERT ... SELECT that bypasses the ORM, so a Python-side default alone would
    # violate NOT NULL for every backfilled row.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=utcnow)


class ServiceAccount(Base, TenantMixin):
    __tablename__ = "service_accounts"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    department_id: Mapped[str] = mapped_column(String(255), nullable=False)
    team_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    iam_role_arn: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
