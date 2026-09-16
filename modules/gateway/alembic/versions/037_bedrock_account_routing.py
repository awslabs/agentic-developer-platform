"""Create the Bedrock account-routing tables — destination registry + mapping rules.

Issue #4743 (R2 · routing foundation), per the merged design note
``docs/design-notes/4692-per-principal-bedrock-account-routing.md`` §1.1b, §4.2.

**Two tables, because a mapping and a connection are two different things**
(design ruling 4a, §1.1). A mapping names a *scope* and points at a
*destination*; a destination carries the account id and the assumable role. They
are kept apart so "this team's mapping must not point at one person's personal
credential" (ruling 6) is expressible as a constraint on the reference rather
than as a convention about a column.

Neither table carries ``org_id``/``TenantMixin``, for the reason migration 036
states verbatim for ``person_budget_defaults``: the platform rung has no tenant
at all. ``TenantMixin.org_id`` is ``nullable=False``
(``src/shared/models/base.py:15``), so a platform-scoped row is *unrepresentable*
in a tenant-scoped table — which is exactly why this migration exists instead of
reusing ``user_credentials``. The org/team rungs name their tenant explicitly in
``scope_id_org``, a scope the row *declares* rather than a partition it *lives
in*.

``bedrock_destination_registry`` — where a routed call could land
-----------------------------------------------------------------

  - **``is_platform_registered`` is an explicit boolean, NOT a NULL
    ``owner_org_id`` meaning "allowed everywhere"** (§4.2 requirement 2). This is
    the non-obvious column here and it is load-bearing. A platform admin may
    register a destination for the case where nobody has linked the desired
    account yet; such a row has no owning tenant. Encoding that as a NULL tenant
    column is how cross-tenant leaks get written by well-meaning code — a reader
    (and a query) cannot distinguish "deliberately platform-wide" from "the
    writer forgot to set the tenant". A row must *say* which it is, and
    ``ck_bedrock_destination_ownership`` refuses the two incoherent
    combinations.
  - **``routing_capable`` is stored and defaults false.** Issue #4742 (R1) ships
    the ``aws_role_v2`` template and the capability probe that sets it; until
    then no destination is routing-capable, which is the truthful state — every
    role the connect flow creates today attaches only ``ReadOnlyAccess`` and so
    cannot invoke Bedrock at all (§5.0). Defaulting true would mark every
    existing connection as a usable destination when none is. The resolver
    filters on this column, so R1 wiring is a one-site change rather than a
    retrofit.
  - **``account_id`` is ``String(12)``**, byte-for-byte
    ``usage_logs.bedrock_account_id`` (``src/shared/models/usage.py:26``) — the
    shadow-mode write copies one column into the other, and a width difference
    between them would truncate silently at exactly the moment an operator is
    trying to audit where a call went.
  - **``credential_id`` has no FK to ``user_credentials``.** Deleting a
    connection that a mapping still references must be a checked, reported
    action rather than a cascade: under the design's fail-closed rule (§2.5,
    §8.3) silently removing a destination converts every mapped principal's
    traffic into an outage. A dangling reference the resolver skips is the safer
    failure, and it is the one this shape produces.

``bedrock_account_mappings`` — which scope goes where
-----------------------------------------------------

Built on migration 036's shape, which is a close fit because #4690 hit the same
platform-rung problem. Every choice below is asserted by
``tests/migrations/test_037_bedrock_account_routing.py``:

  - **``scope_type`` is stored, not inferred** from which scope columns are NULL,
    so a future department rung is a new value rather than a re-reading of
    existing rows.
  - **Two nullable scope columns, not one packed id.** A ``teams.id`` is unique
    only inside its org (``Team`` carries ``TenantMixin``,
    ``src/shared/models/organization.py``), so the team rung needs both; a packed
    ``"org:team"`` string would put two identifier namespaces in one column, the
    #4344 collision class.
  - **``scope_id_user`` is the canonical ``users.id``**, the same id namespace as
    ``authored_by_user_id`` — never a Cognito sub, which is what
    ``TokenContext.user_id`` holds on the ordinary JWT path (#4647). Mixing the
    two namespaces in one column makes a mapping that resolves for nobody.
  - **Uniqueness is a UNIQUE EXPRESSION INDEX over ``COALESCE(col, '')``, NOT a
    ``UniqueConstraint``.** Same trap 036 documents, with a worse consequence
    here: in Postgres NULLs compare *distinct* inside a unique constraint, so the
    obvious ``UNIQUE (scope_type, scope_id_org, scope_id_team, scope_id_user)``
    accepts **two** rows for one scope. For a budget that means two conflicting
    numbers; for routing it means **two destination accounts for one scope, and
    which one bills depends on row order** — the wrong-account bug, installed at
    the schema level. Coalescing inside the index has the database enforce "one
    mapping per scope" rather than whichever writer remembers to check.
  - **``ck_bedrock_account_mapping_scope`` pins the shape per rung.** Without it
    an ``org`` row with a NULL ``scope_id_org`` is a rule matching every tenant
    through a NULL comparison nobody wrote, and a ``user`` row carrying a stray
    team id reads as team-scoped to a human and user-scoped to the ladder.
  - **No ``platform`` rung value.** Rung 4 is the *absence* of a mapping (§1.2),
    not a row — so ``platform`` is deliberately not in the CHECK's allowed set.
    A platform row would be a second, contradictory way to express the fallback.
  - **No FK on ``authored_by_user_id``**, same tenant-lifecycle reason as 034/036.

No index on ``scope_id_org`` alone: every ladder read is by ``scope_type`` first,
so the unique index's leading column already seeks it, and the table holds one
row per scope — tiny by construction (§2.2).

**No column is added to ``usage_logs``.** ``bedrock_account_id`` has existed since
``001_initial_schema.py`` and is plumbed through ``UsageService.log_request``
already; it has simply never been written. Shadow mode populates it (§3.5) — a
migration for it would be a no-op at best and a conflict at worst.

**Purely additive.** Nothing is backfilled and no existing row changes meaning: an
install with no rows here behaves exactly as it does today (ambient IRSA, the
platform account), which is what makes the rollback "stop reading them, then
drop them".

Revision id is 27 chars, inside the ``alembic_version.version_num`` VARCHAR(32)
ceiling ``tests/migrations/test_revision_id_length.py`` guards (#4123). Chains
onto the real single head, ``036_person_budget_defaults``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "037_bedrock_account_routing"
down_revision: str | Sequence[str] | None = "036_person_budget_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REGISTRY_TABLE = "bedrock_destination_registry"
REGISTRY_ACCOUNT_INDEX = "ix_bedrock_destination_account_id"
REGISTRY_OWNERSHIP_CHECK = "ck_bedrock_destination_ownership"

MAPPING_TABLE = "bedrock_account_mappings"
MAPPING_UNIQUE_INDEX = "uq_bedrock_account_mapping_scope"
MAPPING_DESTINATION_INDEX = "ix_bedrock_account_mapping_destination"
MAPPING_SCOPE_CHECK = "ck_bedrock_account_mapping_scope"

# A destination is EITHER platform-registered (no owning tenant, usable by any
# scope an admin names) OR linked by exactly one tenant. Spelled as two explicit
# disjuncts rather than compressed, so each is a shape a reader can check against
# the authoring API's scope check (§4.2 requirement 1).
_OWNERSHIP_SHAPE = (
    "(is_platform_registered = true AND owner_org_id IS NULL) OR (is_platform_registered = false AND owner_org_id IS NOT NULL)"
)

# One expression per rung, narrowest first, matching the ladder's walk order in
# `src/proxy/bedrock_routing.py`. `platform` is absent on purpose: rung 4 is the
# absence of a mapping, not a row (§1.2).
_SCOPE_SHAPE = (
    "(scope_type = 'user' AND scope_id_user IS NOT NULL AND scope_id_org IS NULL AND scope_id_team IS NULL) "
    "OR (scope_type = 'team' AND scope_id_user IS NULL AND scope_id_org IS NOT NULL AND scope_id_team IS NOT NULL) "
    "OR (scope_type = 'org' AND scope_id_user IS NULL AND scope_id_org IS NOT NULL AND scope_id_team IS NULL)"
)


def upgrade() -> None:
    """Create the registry and mapping tables. See the module docstring for every shape choice."""
    op.create_table(
        REGISTRY_TABLE,
        sa.Column("id", sa.String(length=255), nullable=False),
        # Same width as usage_logs.bedrock_account_id — shadow mode copies this
        # column into that one.
        sa.Column("account_id", sa.String(length=12), nullable=False),
        sa.Column("role_arn", sa.String(length=2048), nullable=False),
        # The `user_credentials` row this destination was derived from, when it
        # came from a tenant's own AWS-connect flow. NULL for a destination a
        # platform admin registered directly. No FK — see the module docstring.
        sa.Column("credential_id", sa.String(length=36), nullable=True),
        # Nullable: a platform-registered destination has no owning tenant. The
        # CHECK below is what stops that NULL from being read as "allowed
        # everywhere" by accident.
        sa.Column("owner_org_id", sa.String(length=255), nullable=True),
        sa.Column("is_platform_registered", sa.Boolean(), nullable=False, server_default=sa.false()),
        # Set by #4742's capability probe once an `aws_role_v2` destination proves
        # it can invoke Bedrock. False until then, because no role the current
        # connect flow creates can (§5.0).
        sa.Column("routing_capable", sa.Boolean(), nullable=False, server_default=sa.false()),
        # When the destination last passed a real test assume-role. NULL means
        # never proven; the resolver refuses those (§4.4).
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("region", sa.String(length=32), nullable=False, server_default="us-east-1"),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("registered_by_user_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_OWNERSHIP_SHAPE, name=REGISTRY_OWNERSHIP_CHECK),
    )
    # Non-unique: the same AWS account may legitimately be registered twice —
    # once platform-wide and once linked by the tenant that owns it — and the two
    # rows carry different role ARNs and different provenance. Indexed because
    # the authoring API looks destinations up by account id.
    op.create_index(REGISTRY_ACCOUNT_INDEX, REGISTRY_TABLE, ["account_id"])

    op.create_table(
        MAPPING_TABLE,
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        # Nullable per rung, shapes pinned by the CHECK below. NOT foreign keys —
        # these rows are rules about scopes, and an `ondelete` would tie a
        # governance decision to a tenant lifecycle (same reasoning as 034/036).
        sa.Column("scope_id_org", sa.String(length=255), nullable=True),
        sa.Column("scope_id_team", sa.String(length=255), nullable=True),
        # Canonical `users.id`, never a Cognito sub. See the module docstring.
        sa.Column("scope_id_user", sa.String(length=255), nullable=True),
        sa.Column("destination_id", sa.String(length=255), nullable=False),
        sa.Column("authored_by_user_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_SCOPE_SHAPE, name=MAPPING_SCOPE_CHECK),
    )
    # NOT a UniqueConstraint — Postgres treats NULLs as distinct inside one, which
    # would let two mappings coexist for the same scope and make the billed
    # account depend on row order. See the module docstring.
    op.create_index(
        MAPPING_UNIQUE_INDEX,
        MAPPING_TABLE,
        [
            "scope_type",
            sa.text("COALESCE(scope_id_org, '')"),
            sa.text("COALESCE(scope_id_team, '')"),
            sa.text("COALESCE(scope_id_user, '')"),
        ],
        unique=True,
    )
    # The reverse lookup the authoring API needs before deleting a destination:
    # "does any mapping still reference this?" Under fail-closed, deleting a
    # referenced destination is an outage, so that check must be cheap enough to
    # always run (§8.3).
    op.create_index(MAPPING_DESTINATION_INDEX, MAPPING_TABLE, ["destination_id"])


def downgrade() -> None:
    """Drop both tables.

    Trivially reversible because nothing references them: no FK points at either,
    no existing row's meaning depends on them, and with them gone the resolver's
    existence gate simply finds no mappings — which is the pre-#4743 behaviour,
    where every call used the platform account's ambient IRSA credentials. The
    operational rollback is still "revert the PR"; this exists so
    ``alembic downgrade`` walks past the revision cleanly.

    Mappings drop first: they are the table that references destinations, so this
    order holds even if a future revision adds the FK this one deliberately omits.
    """
    op.drop_index(MAPPING_DESTINATION_INDEX, table_name=MAPPING_TABLE)
    op.drop_index(MAPPING_UNIQUE_INDEX, table_name=MAPPING_TABLE)
    op.drop_table(MAPPING_TABLE)
    op.drop_index(REGISTRY_ACCOUNT_INDEX, table_name=REGISTRY_TABLE)
    op.drop_table(REGISTRY_TABLE)
