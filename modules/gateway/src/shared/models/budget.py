from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, Numeric, String, UniqueConstraint
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
