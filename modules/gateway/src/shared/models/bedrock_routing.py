"""Bedrock account-routing models — where a principal's model calls should land.

Issue #4743 (R2 · routing foundation). ORM mirror of migration
``037_bedrock_account_routing``, per the design note
``docs/design-notes/4692-per-principal-bedrock-account-routing.md`` §1.1b, §4.2.

Two tables because a mapping and a connection are two different things (§1.1): a
mapping names a *scope*, a destination carries the *account and role*. Keeping
them apart is what makes "a team's mapping must not point at one person's
personal credential" a checkable constraint on the reference rather than a
convention.

Neither class carries ``TenantMixin``. ``TenantMixin.org_id`` is
``nullable=False`` (``base.py``), and the platform rung has no tenant at all —
the same reason migration 036 gives for ``person_budget_defaults``. The org and
team rungs *declare* their tenant in ``scope_id_org`` instead of *living in* a
partition.

Every constraint below is duplicated in the migration by hand. The parity is
asserted by ``tests/migrations/test_037_bedrock_account_routing.py``, because a
CHECK present in only one of the two means the tests pass against a schema the
database does not have.
"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid, utcnow

# The rungs a mapping row may declare, narrowest first — the resolver's walk
# order. ``platform`` is deliberately absent: rung 4 is the ABSENCE of a mapping
# (§1.2), so a platform row would be a second, contradictory way to say
# "ambient IRSA" and the ladder would then have two answers for one question.
MAPPING_SCOPE_TYPES: tuple[str, ...] = ("user", "team", "org")


class BedrockDestinationRegistry(Base):
    """A place a routed Bedrock call could land: an account plus an assumable role.

    Platform-scoped by design (design ruling 4b) — the pool of destinations a
    platform admin picks from spans tenants, so this table cannot be tenant-scoped.

    **``is_platform_registered`` is an explicit boolean, not a NULL
    ``owner_org_id``.** This is the non-obvious column and it is load-bearing
    (§4.2 requirement 2). An admin may register a destination for an account
    nobody has linked yet; that row has no owning tenant. Encoding it as a NULL
    tenant column would make "deliberately platform-wide" indistinguishable from
    "the writer forgot to set the tenant" — which is how cross-tenant leaks get
    written by well-meaning code. A row must say which it is, and
    ``ck_bedrock_destination_ownership`` refuses the incoherent combinations.

    **``routing_capable`` defaults False and means what it says.** Every role the
    current AWS-connect flow creates attaches only ``ReadOnlyAccess``, which
    excludes ``bedrock:InvokeModel`` (§5.0) — so no existing connection can serve
    a routed call. #4742 (R1) ships the ``aws_role_v2`` template and the
    capability probe that flips this. Defaulting True would advertise every
    connected account as a usable destination when none is.
    """

    __tablename__ = "bedrock_destination_registry"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    # String(12), byte-for-byte usage_logs.bedrock_account_id: shadow mode copies
    # this column into that one, and a width difference between them would
    # truncate silently at exactly the moment an operator is auditing where a
    # call went.
    account_id: Mapped[str] = mapped_column(String(12), nullable=False)
    role_arn: Mapped[str] = mapped_column(String(2048), nullable=False)
    # The `user_credentials` row this destination came from, when it came from a
    # tenant's own AWS-connect flow; NULL for an admin-registered one. No FK:
    # deleting a connection a mapping still references must be a checked action,
    # not a cascade — under fail-closed (§2.5) a silent removal turns every mapped
    # principal's traffic into an outage, whereas a dangling reference the
    # resolver skips merely falls through to the next rung.
    credential_id: Mapped[str | None] = mapped_column(String(36), nullable=True, default=None)
    # NULL only for platform-registered rows; the CHECK ties the two together.
    owner_org_id: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    is_platform_registered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    routing_capable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # When the destination last passed a real test assume-role. NULL means never
    # proven, and the resolver refuses those (§4.4) rather than deprioritising
    # them — an unproven destination fails every call, so under fail-closed
    # merely *starting* a connect flow must not be able to reroute traffic.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    region: Mapped[str] = mapped_column(String(32), nullable=False, default="us-east-1")
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    # Canonical ``users.id`` of the platform admin who registered it (the #4647
    # audit-column contract), not ``TokenContext.user_id`` — that is a Cognito sub
    # on the ordinary JWT path, and persisting it raw mixes two id namespaces in
    # one audit column.
    registered_by_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        # A destination is EITHER platform-registered with no owning tenant, OR
        # linked by exactly one tenant. Enforced in the database rather than by
        # the writer, because the failure mode of the incoherent combination is a
        # cross-tenant routing decision.
        CheckConstraint(
            "(is_platform_registered = true AND owner_org_id IS NULL) OR (is_platform_registered = false AND owner_org_id IS NOT NULL)",
            name="ck_bedrock_destination_ownership",
        ),
        # Non-unique: one AWS account may legitimately appear twice — once
        # platform-wide, once linked by the tenant that owns it — with different
        # role ARNs and different provenance.
        Index("ix_bedrock_destination_account_id", "account_id"),
    )

    @property
    def is_usable_for_routing(self) -> bool:
        """May this destination serve a routed call at all?

        Both halves are required and they fail for different reasons: an unverified
        destination has never been proven assumable, and a non-routing-capable one
        is a role that assumes fine but cannot invoke Bedrock (§5.0) — the inert
        mapping of the #4511 class. Checking only one would let the other through.

        Read by the resolver, which treats a non-usable destination as *no match*
        rather than as a match it then rejects: a broken user-rung destination must
        fall through to the team rung, not fail the request.
        """
        return self.routing_capable and self.verified_at is not None


class BedrockConnectionGrant(Base):
    """An explicit platform-admin grant to reuse a connection for org Bedrock calls.

    The original vault credential keeps its owner and secret. This grant authorizes
    only a routing destination; it does not share the credential with agent tools.
    Kept separately so adding the feature does not change existing routing rows.
    """

    __tablename__ = "bedrock_connection_grants"
    __table_args__ = (UniqueConstraint("credential_id", "org_id", name="uq_bedrock_connection_grant_scope"),)

    destination_id: Mapped[str] = mapped_column(String(255), ForeignKey("bedrock_destination_registry.id", ondelete="CASCADE"), primary_key=True)
    # Keep the grant visible/removable if its source is deleted, just like the
    # registry's credential reference. The signer rejects a missing credential.
    credential_id: Mapped[str] = mapped_column(String(36), nullable=False)
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class BedrockAccountMapping(Base):
    """A rule: "this scope's Bedrock calls go to that destination."

    One row per scope, on migration 036's shape. ``scope_type`` is **stored, not
    inferred** from which columns are NULL, so a future department rung is a new
    value rather than a re-reading of existing rows.

    **Two scope columns, not one packed id.** A ``teams.id`` is unique only inside
    its org (``Team`` carries ``TenantMixin``), so the team rung needs both; a
    packed ``"org:team"`` string would put two identifier namespaces in one
    column, the #4344 collision class.

    ``scope_id_user`` is the canonical ``users.id``, the same namespace as
    ``authored_by_user_id`` — never a Cognito sub (#4647). A mapping written with a
    sub resolves for nobody.
    """

    __tablename__ = "bedrock_account_mappings"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id_org: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    scope_id_team: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    scope_id_user: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    # Points at BedrockDestinationRegistry.id. A reference, not an inlined account
    # id — that is design ruling 4a in DDL: an account number alone is unusable,
    # since the platform needs an assumable role in the destination account.
    destination_id: Mapped[str] = mapped_column(String(255), nullable=False)
    authored_by_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        # One mapping per scope. An expression index over COALESCE, NOT a
        # UniqueConstraint: in Postgres NULLs compare *distinct* inside a unique
        # constraint, so `UNIQUE (scope_type, scope_id_org, scope_id_team,
        # scope_id_user)` would accept TWO rows for one scope. The rung would then
        # hold two destination accounts and **which one bills depends on row
        # order** — the wrong-account bug, installed at the schema level.
        Index(
            "uq_bedrock_account_mapping_scope",
            "scope_type",
            text("COALESCE(scope_id_org, '')"),
            text("COALESCE(scope_id_team, '')"),
            text("COALESCE(scope_id_user, '')"),
            unique=True,
        ),
        # A row must describe the rung it claims. Without this, an `org` row with a
        # NULL `scope_id_org` is a rule matching every tenant via a NULL comparison
        # nobody wrote, and a `user` row carrying a stray team id reads as
        # team-scoped to a human and user-scoped to the ladder.
        CheckConstraint(
            "(scope_type = 'user' AND scope_id_user IS NOT NULL AND scope_id_org IS NULL AND scope_id_team IS NULL) "
            "OR (scope_type = 'team' AND scope_id_user IS NULL AND scope_id_org IS NOT NULL AND scope_id_team IS NOT NULL) "
            "OR (scope_type = 'org' AND scope_id_user IS NULL AND scope_id_org IS NOT NULL AND scope_id_team IS NULL)",
            name="ck_bedrock_account_mapping_scope",
        ),
        # The reverse lookup the authoring API needs before deleting a destination
        # ("does any mapping still reference this?"). Under fail-closed, deleting a
        # referenced destination is an outage (§8.3), so that check must be cheap
        # enough to always run.
        Index("ix_bedrock_account_mapping_destination", "destination_id"),
    )
