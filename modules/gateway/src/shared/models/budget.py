from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, CheckConstraint, Date, DateTime, Index, Numeric, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TenantMixin, new_uuid, utcnow


class BudgetConfig(Base, TenantMixin):
    __tablename__ = "budget_configs"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)  # org/department/team/user/service_account
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    period_type: Mapped[str] = mapped_column(String(10), nullable=False)  # daily/weekly/monthly
    budget_amount_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    enforcement_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="hard")  # soft/hard
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("org_id", "entity_type", "entity_id", "period_type", name="uq_budget_config"),)


class BudgetUsage(Base, TenantMixin):
    __tablename__ = "budget_usage"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_type: Mapped[str] = mapped_column(String(10), nullable=False)
    # NUMERIC(14,6), not (10,2) — migration 030. Costs are computed to 6dp
    # (pricing.calculate_cost) and usage_logs.cost_usd already stores 6dp, so a
    # 2dp accumulator rounded every sub-cent request to $0.00: a burst of haiku
    # traffic accrued real spend while the enforced denominator stayed at zero.
    total_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False, default=0)
    # BIGINT: unbounded accumulators — a monthly row overflowed int32 at
    # ~2.1B tokens (migration 024).
    total_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    request_count: Mapped[int] = mapped_column(BigInteger, default=0)

    __table_args__ = (UniqueConstraint("org_id", "entity_type", "entity_id", "period_start", "period_type", name="uq_budget_usage"),)


class PersonBudgetConfig(Base):
    """A person's own ceiling on their total agent spend, across every org.

    Issue #4629 (#4620 · C3), design note
    ``docs/design-notes/4620-cross-org-person-budgets.md`` §4.1.

    **Deliberately NOT ``TenantMixin``.** Every other budget table is keyed
    ``org_id``-first, which is exactly why a person-wide cap has nowhere to live
    today: a person's runs execute in whichever tenant the work is in, so a cap
    stored in one partition caps nothing that happens in another (#4620). The
    precedent for a legitimately cross-partition table is ``tenant_memberships``
    (migration 021), which is also mixin-free for the same reason — the row is
    about a person, not about a tenant's data.

    Three alternatives were considered and rejected in §4.1; do not "fix" this
    table into any of them:

    * **A sentinel ``org_id``** (e.g. ``"__person__"``) on ``budget_configs``
      would put two id namespaces under one unique constraint — the identifier
      collision ``EntityType`` and #4344 exist to prevent — and every
      ``org_id``-filtered query in the budget module would either need a sentinel
      exclusion or silently read the row as some tenant's own.
    * **The person's home-org partition** recreates the bug: "home org" is
      mutable (the org switcher moves ``attributed_org_id``) and arbitrary.
    * **``parent_tenant_id`` fusion** (#2954) already exists but fuses the
      tenants entirely — one ledger, shared visibility — which is right for "one
      company, several GitHub orgs" and wrong for one person in two unrelated
      companies.

    **No aggregate spend table accompanies this one.** Person-level spend is
    *derived* by summing the existing ``root_user`` ``budget_usage`` rows across
    partitions (§4.1). A second accumulator would be a denormalised duplicate of
    the same dollars, which is the #4322 double-count family (see migration 032).
    """

    __tablename__ = "person_budget_configs"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    # The cross-org person key: ``github:<provider_user_id>``, built by
    # `src.shared.identity.person_anchor.format_person_anchor`.
    #
    # The GitHub numeric id, NOT ``users.id``, and that choice is load-bearing
    # (§3.3). ``users`` carries ``TenantMixin``, so a person independently
    # onboarded into two orgs legitimately has TWO ``users.id`` values (see
    # ``tests/shared/test_resolve_root_user_entity_id.py``) — keying on one of
    # them would produce a cap that misses the person's spend in the other org,
    # which is the inert-cap class of #4511 one layer up. ``users.id`` stays the
    # *ledger* key; this is the cross-org *join* key.
    #
    # Namespace-qualified rather than a bare numeric id, on the #4344 reasoning:
    # a bare id could alias a future provider's id space, so one person's cap
    # could govern another's spend.
    person_anchor: Mapped[str] = mapped_column(String(255), nullable=False)
    period_type: Mapped[str] = mapped_column(String(10), nullable=False)  # daily/weekly/monthly
    # NUMERIC(10,2), matching ``budget_configs.budget_amount_usd`` exactly. A cap
    # is an authored dollar figure, not an accumulator, so it does not need
    # ``budget_usage``'s 6dp (migration 030) — but it must not have LESS
    # precision than the column a client already renders caps from.
    budget_amount_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # `soft` on this table means informational: the figure is reported, and
    # nothing is denied. `hard` is reserved for #4630 (C4) and the §5.7 ruling,
    # and is NOT settable through the authoring API this migration ships with —
    # the default is the only value that can currently be written. Stored as a
    # column rather than assumed so C4 needs no migration to turn it on, and so a
    # reader can never mistake "informational" for "enforcing".
    enforcement_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="soft")  # soft/hard
    # Who wrote this row, as a canonical ``users.id``. Present for audit: the
    # authoring rules (§4.2) permit the person themselves and a platform admin,
    # and those two are indistinguishable after the fact without this column.
    #
    # NOT a ForeignKey to ``users.id``: a platform admin authoring a cap for
    # somebody in another tenant is an expected write, and this table is
    # deliberately partition-free — an FK plus ``ondelete`` would reintroduce a
    # tenant-lifecycle dependency the table exists to avoid.
    authored_by_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    # One cap per person per period. No ``org_id`` in the key — that absence IS
    # the feature: it is what makes the cap partition-free.
    __table_args__ = (UniqueConstraint("person_anchor", "period_type", name="uq_person_budget_config"),)


class PersonBudgetDefault(Base):
    """A DEFAULT person limit at platform, org or team scope — Issue #4690 (D1).

    ``PersonBudgetConfig`` above stores *one person's* limit. This table stores a
    **rule**: "everybody, unless we say otherwise". One row governs current and
    future members of its scope, which is what makes it a default rather than a
    bulk write — a platform admin who typed a limit into every person's row would
    still have every future joiner start unlimited.

    **Defaults are CEILINGS** (operator ruling, 2026-09-07). Two consequences,
    both load-bearing and both enforced by the ladder in
    ``src/budget/person_ledger.py`` rather than by this table:

    * A person may set a **lower** personal limit on themselves; they may not use
      the self-service path to raise themselves above the applicable default.
      Only a platform admin may author an individual row above it.
    * When a person matches several rules at the SAME rung — a member of two orgs
      that both carry an org default — the **LOWEST** amount governs. A ceiling
      that could be escaped by joining a second, more generous org would not be
      one.

    **Deliberately NOT ``TenantMixin``**, for the same reason ``PersonBudgetConfig``
    is not: the platform rung has no tenant at all, and a person's applicable rule
    is resolved across every partition they belong to. The scope is carried in
    explicit columns instead, so a row states which rung it is on rather than
    leaving a reader to infer it from a NULL.

    **Two nullable scope columns, not one polymorphic id.** ``scope_id_org`` and
    ``scope_id_team`` are separate because a team rung needs BOTH (a team id is
    only unique inside its org — ``teams`` carries ``TenantMixin``), and a single
    packed ``"org:team"`` string would be a second identifier namespace inside one
    column, the #4344 collision class. ``ck_person_budget_default_scope`` pins the
    shape so a row can never claim a scope its columns do not describe.

    **The uniqueness key is an expression index, not a ``UniqueConstraint``.** In
    Postgres NULLs compare distinct inside a unique constraint, so
    ``UNIQUE (scope_type, scope_id_org, scope_id_team, period_type)`` would happily
    accept TWO platform defaults for the same period — a rung silently holding two
    conflicting numbers, where which one governs depends on row order. Indexing
    ``COALESCE(col, '')`` gives the intended "one rule per (scope, period)" and is
    enforced by the database rather than by whichever writer remembers to check.
    """

    __tablename__ = "person_budget_defaults"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    # Which rung: ``platform`` | ``org`` | ``team``. Stored rather than derived
    # from which scope columns are NULL, so a row is self-describing and a future
    # rung (department — explicitly left room for in #4690's non-goals) is an added
    # value here rather than a re-reading of existing rows.
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # NULL for the platform rung. Set for ``org`` and ``team``.
    scope_id_org: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Set for the ``team`` rung ONLY, alongside ``scope_id_org``: a ``teams.id``
    # is unique inside its org, not globally, so a team rule that named only the
    # team id would match same-named teams in unrelated tenants — the #4511
    # wrong-person class, in the direction that governs somebody who was never
    # meant to be governed.
    scope_id_team: Mapped[str | None] = mapped_column(String(255), nullable=True)
    period_type: Mapped[str] = mapped_column(String(10), nullable=False)  # daily/weekly/monthly
    # NUMERIC(10,2), matching ``person_budget_configs.budget_amount_usd`` exactly:
    # the ladder compares the two and reports whichever applies, so differing
    # precision would be a silent rounding difference between "your limit" and
    # "the default you are held to".
    budget_amount_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # ``hard`` from the first row, unlike ``person_budget_configs``' historical
    # ``soft`` default: there is no pre-enforcement generation of these rows to keep
    # a promise to. A default authored to bound everybody and silently not enforcing
    # would be the inert-cap class (#4511) at platform scale.
    enforcement_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="hard")  # hard
    # The canonical ``users.id`` of the platform admin who authored it (the #4647
    # audit-column contract). Not ``TokenContext.user_id``, which is a Cognito sub
    # on the ordinary JWT path — persisting it raw mixes two id namespaces in one
    # audit column. No FK, for the same tenant-lifecycle reason as
    # ``PersonBudgetConfig.authored_by_user_id``.
    authored_by_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        # One rule per (scope, period). See the class docstring for why this is an
        # expression index over COALESCE rather than a UniqueConstraint.
        Index(
            "uq_person_budget_default",
            "scope_type",
            text("COALESCE(scope_id_org, '')"),
            text("COALESCE(scope_id_team, '')"),
            "period_type",
            unique=True,
        ),
        # A row must describe the rung it claims. Without this, an ``org`` row with
        # a NULL ``scope_id_org`` is a rule that matches every org's members via a
        # NULL comparison nobody wrote — and a ``platform`` row carrying a stray
        # org id is a platform rule that reads as tenant-scoped to a human and as
        # platform-wide to the ladder.
        CheckConstraint(
            "(scope_type = 'platform' AND scope_id_org IS NULL AND scope_id_team IS NULL) "
            "OR (scope_type = 'org' AND scope_id_org IS NOT NULL AND scope_id_team IS NULL) "
            "OR (scope_type = 'team' AND scope_id_org IS NOT NULL AND scope_id_team IS NOT NULL)",
            name="ck_person_budget_default_scope",
        ),
        CheckConstraint(
            "enforcement_mode = 'hard'",
            name="ck_person_budget_default_hard",
        ),
    )


class BudgetSettlementReceipt(Base):
    """Claim and all budget debits commit together; historical rows are not inferred."""

    __tablename__ = "budget_settlement_receipts"
    allocation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    org_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)


class BudgetPricingCorrection(Base):
    """One audited incident credit per request; original debit stays replayable."""

    __tablename__ = "budget_pricing_corrections"
    org_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    correction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    credit_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    original_decision: Mapped[dict] = mapped_column(JSON, nullable=False)
    corrected_decision: Mapped[dict] = mapped_column(JSON, nullable=False)
    allocation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
