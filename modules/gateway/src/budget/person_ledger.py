"""The person ledger: identity fusion and settled reads — one definition, two consumers.

Extracted from ``me_routes.py`` (review fix on #4689). These are the primitives the
#4620 ruling's load-bearing property rests on — *the displayed number IS the
enforced number* — because ``/me/budget`` composes the person envelope from them
and ``_check_person_budget`` builds its denominator from the SAME functions. As
underscore-privates of a FastAPI routes module they carried no stability contract
for their second consumer; here they are the public API of a deliberate LEAF:

* imports models and shared schemas only — no router, no service, no auth — so
  both ``me_routes`` and ``enforcement_service`` can import it at module level
  without touching their existing cycle;
* contains no mutation and no HTTP: these functions read, and return keys and
  ``Decimal`` figures.

Every function keeps its original docstring; nothing here changed behaviour.

**Issue #4690 (D1) adds the limit LADDER to this module, and here specifically for
the same reason.** "Which limit governs this person?" is now a question with four
possible answers (their own row, their team's default, their org's default, the
platform default), and both the enforcement layer and the read surface must give
the identical answer or the #4620 ruling's property breaks in a new place: a
dashboard that labels somebody "unlimited" while a 402 stops them at the platform
default is the same defect as a mismatched figure. The ladder therefore lives
beside the primitives it is built from, not in either consumer.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.identity.providers import IdentityProvider
from src.shared.models.budget import BudgetUsage, PersonBudgetConfig, PersonBudgetDefault
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.budget import EntityType, PeriodType

from .utils import CALENDAR_PERIOD_TYPES

logger = logging.getLogger("bedrockgateway.budget")

# Where an applicable person limit came from — on the wire (`/me/budget/person-cap`
# `source`) and in the text of a 402/422, so a person told they are limited can
# tell whether the number is theirs to change.
#
# `own` and `admin` are both individual `person_budget_configs` rows and differ
# only in who authored them; the split matters because it is the difference
# between "lower this yourself" and "ask a platform admin", which is the whole
# reason the ceiling rule exists (#4690).
PersonLimitSource = Literal["own", "admin", "team_default", "org_default", "platform_default"]

# The rung order, tightest scope first. The ladder walks it and stops at the first
# rung with a match, so this tuple IS the precedence rule — one grep-able place,
# rather than an ordering implied by the shape of an if/elif chain.
_DEFAULT_RUNG_ORDER: tuple[str, ...] = ("team", "org", "platform")

_SOURCE_BY_SCOPE_TYPE: dict[str, PersonLimitSource] = {
    "team": "team_default",
    "org": "org_default",
    "platform": "platform_default",
}


@dataclass(frozen=True)
class PersonLimit:
    """The one limit that governs a person for one calendar period, and where it came from.

    Frozen because every consumer of this is a reader: enforcement compares it to
    a settled denominator, the read surface renders it, and the self-service PUT
    validates against it. A mutable limit object invites a caller to "adjust" the
    figure locally, and the whole property being protected is that all three see
    the same number.

    Attributes:
        period_type: The calendar period this limit governs. Always a member of
            ``CALENDAR_PERIOD_TYPES``.
        amount: The ceiling in USD, at the ``NUMERIC(10,2)`` precision both source
            columns carry.
        enforcement_mode: ``hard`` or ``soft``, straight off the row. Defaults are
            always written ``hard``; a ``soft`` value here can only have come from
            a C3-era individual row, which stays informational until re-saved.
        source: Which rung supplied it.
        scope_label: Human-readable provenance for the 402/422 text — e.g.
            ``"platform default"``, ``"org default for acme-corp"``, or
            ``"your own limit"``. Composed here rather than at each message site so
            the denial, the header surface and the ceiling rejection cannot describe
            the same rung differently.
    """

    period_type: str
    amount: Decimal
    enforcement_mode: str
    source: PersonLimitSource
    scope_label: str
    # ISO-8601 authored-at for an INDIVIDUAL row; None for every default rung —
    # a default's timestamp is deliberately withheld from person-facing surfaces
    # (review fix on #4696: this also removes the read surface's duplicate row
    # read, whose only purpose was this field).
    updated_at: str | None = None

    @property
    def is_default(self) -> bool:
        """True when this limit came from a default rule rather than an individual row.

        The discriminator the ceiling rule turns on (#4690): a person may lower
        their own limit below an applicable default, but only a platform admin may
        author an individual row above one.
        """
        return self.source in ("team_default", "org_default", "platform_default")


async def read_settled_spend(
    db: AsyncSession,
    org_id: str,
    entity_type: EntityType,
    entity_id: str,
    period_type: PeriodType,
    period_start: date,
) -> Decimal:
    """Read settled spend with the full 5-filter predicate.

    ``(org_id, entity_type, entity_id, period_type, period_start)`` — byte-for-byte
    the predicate ``_check_entity_budget`` uses (``enforcement_service.py:1053-1064``),
    which is what makes the endpoint's figure equal the one enforcement compares
    against (FR-1.3). It is also the table's unique constraint, so this reads at
    most one row.

    Dropping any single filter changes the meaning: without ``entity_type`` it
    sums a user's direct spend together with their cloud-agent spend and the
    org total, over-reporting several times over and raising a false "over
    budget" alarm (#4328). There is deliberately no ``SUM`` here at all.

    A missing row is a true ``0`` — this period has no settled spend yet. That is
    a measurement, distinct from a failed read, which propagates as an
    infrastructure fault and becomes a ``503``.
    """
    usage = await db.scalar(
        select(BudgetUsage).where(
            and_(
                BudgetUsage.org_id == org_id,
                BudgetUsage.entity_type == entity_type.value,
                BudgetUsage.entity_id == entity_id,
                BudgetUsage.period_type == period_type.value,
                BudgetUsage.period_start == period_start,
            )
        )
    )
    return usage.total_cost_usd if usage else Decimal("0")


async def resolve_person_anchor_id(db: AsyncSession, canonical_user_id: str) -> str | None:
    """The caller's GitHub anchor id, resolved DETERMINISTICALLY.

    ``user_identities`` has no unique constraint on ``(user_id, provider)``, so a
    person can legitimately hold two GitHub rows (admin-linked second account).
    An unordered ``LIMIT 1`` here and an independent unordered pick on the
    authoring side can each choose a DIFFERENT row — a cap stored under
    ``github:A`` that enforcement looks up as ``github:B``: the #4511 inert-cap
    class (review fix on #4661). Every anchor pick — this one, the authoring
    resolver in ``shared/identity/person_anchor.py``, and the enforcement layer —
    must order identically; ``provider_user_id`` ascending is the convention.
    """
    return await db.scalar(
        select(UserIdentity.provider_user_id)
        .where(
            UserIdentity.user_id == canonical_user_id,
            UserIdentity.provider == IdentityProvider.github,
        )
        .order_by(UserIdentity.provider_user_id)
        .limit(1)
    )


async def resolve_person_identity(db: AsyncSession, canonical_user_id: str) -> tuple[str, list[str]]:
    """Fuse the caller's ``users.id`` rows across tenants, and name the anchor (§3.3).

    The cross-org join key is the **GitHub numeric id**
    (``user_identities.provider_user_id``), per the note's anchor recommendation.
    ``users.id`` stays the ledger key; the anchor is what identifies the *person*
    across partitions.

    Why the indirection is required rather than defensive: a person independently
    onboarded into two orgs can have two ``users`` rows and therefore two distinct
    ``root_user`` ledger keys. A cross-org sum over one canonical id would silently
    omit the other partition's dollars — under-reporting for precisely the
    multi-org population #4620 is about, and doing so invisibly, since a missing
    ledger row is indistinguishable from "no spend".

    Returns:
        ``(anchor, person_user_ids)``. When no GitHub identity is linked the anchor
        is ``users:<canonical id>`` and the key list is the single canonical id —
        which is correct, not a degradation: with no anchor there is no evidence of
        a second ``users`` row to fuse, and inventing one would be a guess. The
        caller's own canonical id is always present in the list, so a linked
        identity that fails to resolve can never *shrink* the read below what the
        single-partition path already covers.
    """
    anchor_id = await resolve_person_anchor_id(db, canonical_user_id)
    if not anchor_id:
        return f"users:{canonical_user_id}", [canonical_user_id]

    # No `org_id` filter, deliberately — and this is the one query in the module
    # that is *supposed* to span tenants. `_resolve_via_github_identity` filters by
    # org because it resolves a WRITE key inside one authorized target org; here the
    # question is the opposite one ("which of this person's rows exist anywhere"),
    # and the authorization is applied to the PARTITION set instead
    # (`_resolve_member_partitions`), which is where it belongs: knowing your own
    # `users.id` values discloses nothing, whereas reading a partition does.
    fused = (
        (
            await db.execute(
                select(UserIdentity.user_id).where(
                    UserIdentity.provider == IdentityProvider.github,
                    UserIdentity.provider_user_id == anchor_id,
                )
            )
        )
        .scalars()
        .all()
    )

    # Sorted for a deterministic read order, with the caller's own id unioned in so
    # the list is never smaller than the single-partition path's one key.
    return f"github:{anchor_id}", sorted({canonical_user_id} | set(fused))


async def resolve_person_subs(db: AsyncSession, person_user_ids: list[str]) -> list[str]:
    """Project the fused ``users.id`` list onto the Cognito subs their DIRECT spend uses.

    Issue #4396. The person's two ledgers are keyed in different namespaces —
    ``root_user`` by canonical ``users.id``, ``user`` by Cognito ``sub`` — so reading
    the direct half needs the subs. This is deliberately a **projection of the
    fusion already performed** by ``_resolve_person_identity``, not a second way of
    deciding who "the same person" is. One fusion, two key namespaces: a third
    independent derivation is the #4511 inert-key class, where one surface reads a
    key nothing writes.

    ``users.cognito_sub`` is **nullable** — shadow users (``POST /resolve-user``)
    and invited-but-never-signed-in users have none. Those rows are skipped rather
    than contributing a ``None``/``""`` key: a row keyed on the empty string would
    be a shared bogus ledger line every such user in the tenant collides on. A user
    with no sub has provably never signed in, so they have no direct spend to miss —
    the omission cannot under-report.

    Note there is no ``org_id`` filter and none is needed: the ids are ``users``
    primary keys already resolved from the caller's own identity, so this reads
    nothing the partition set has not already authorised. Sorted for a deterministic
    read order, exactly as the canonical-id list is.

    Returns:
        The distinct Cognito subs for this person, possibly empty when none of
        their ``users`` rows carries one.
    """
    rows = (await db.execute(select(User.cognito_sub).where(User.id.in_(person_user_ids)))).scalars().all()
    return sorted({sub for sub in rows if sub})


async def read_person_partition_spend(
    db: AsyncSession,
    org_id: str,
    person_user_ids: list[str],
    person_subs: list[str],
    period_type: PeriodType,
    period_start: date,
) -> tuple[Decimal, Decimal]:
    """One partition's settled ``(cloud, direct)`` spend for this person (#4396).

    **The single definition of "this person's spend in this partition", shared
    verbatim by the read surface and the enforcement layer.** ``/me/budget`` calls it
    to compose ``person_envelope``; ``_check_person_budget`` calls it to build its
    denominator. That sharing is the requirement, not a convenience: the ruling is
    that *the displayed number IS the enforced number*, and two independent
    summations of "the same" figure is how a dashboard and a 402 come to disagree
    about whether somebody is over their limit.

    Every dollar is still a single 5-filter row read through the unchanged
    ``_read_settled_spend`` — a widened *key set* over an unchanged predicate, never
    a relaxed predicate and never a SQL aggregate (T33 / #4328).

    The two halves are returned **separately rather than pre-summed** so callers can
    report the components (``per_org[]`` renders both, which is what makes the fused
    headline auditable) while the total stays derived in one place.

    Returns:
        ``(cloud, direct)`` — settled ``root_user`` and ``user`` spend for this
        person in this partition. Both are true ``Decimal("0")`` when no row exists:
        a measurement, not a fallback.
    """
    cloud = Decimal("0")
    for entity_id in person_user_ids:
        cloud += await read_settled_spend(db, org_id, EntityType.ROOT_USER, entity_id, period_type, period_start)

    direct = Decimal("0")
    for sub in person_subs:
        direct += await read_settled_spend(db, org_id, EntityType.USER, sub, period_type, period_start)

    return cloud, direct


async def resolve_member_partitions(db: AsyncSession, person_user_ids: list[str], active_org_id: str | None) -> list[str]:
    """Derive, server-side, which partitions this person's spend may be read from (§7.3).

    ``tenant_memberships`` is the authority: it is deliberately partition-free (no
    ``TenantMixin``, migration 021) and is the established precedent for a
    legitimately cross-tenant table. The fan-out shape mirrors
    ``src/admin/connections/routes.py:296-306``, sanctioned by
    ``docs/design-notes/3074-invisible-tenancy-per-action-resolution.md`` §1.4.

    **Two unions, both required and neither cosmetic:**

    * ``users.org_id`` — the shadow-user gap. Users auto-provisioned via
      ``POST /resolve-user`` have ``users.org_id`` set but **no** membership row
      (``src/internal/provenance_routes.py:140-147``), so a membership-only list
      silently omits partitions where their spend really accrued. Same fallback
      ``provenance_routes.py:180-190`` applies for the same reason.
    * The caller's **active** partition. It is what every figure above describes,
      so a ``per_org`` list that omitted it would disagree with ``lines`` on the
      same screen. A caller always has standing to read their own session's tenant.

    Note ``is_active`` is NOT filtered on: that flag marks which single membership
    is the caller's current workspace, and filtering by it would reduce this to the
    one partition the endpoint already read — the bug being fixed.

    Returns:
        Sorted tenant ids. This list is the entire authorization boundary of the
        cross-org read, which is why it is computed from the caller's resolved
        identity and never from request input.
    """
    membership_rows = (await db.execute(select(TenantMembership.tenant_id).where(TenantMembership.user_id.in_(person_user_ids)))).scalars().all()
    home_rows = (await db.execute(select(User.org_id).where(User.id.in_(person_user_ids)))).scalars().all()

    # `active_org_id=None` (review fix on #4696): callers deriving the DEFAULT-
    # LADDER candidates must pass None — the active org is `attributed_org_id`,
    # which is caller-influenced (#4132), and unioning it here let a header select
    # a foreign org's more generous default. Widening the SPEND denominator with
    # it stays safe (more spend counted, never less), so spend callers pass it.
    seeded = {active_org_id} if active_org_id else set()
    return sorted(seeded | {row for row in membership_rows if row} | {row for row in home_rows if row})


async def resolve_person_team_keys(db: AsyncSession, person_user_ids: list[str]) -> list[tuple[str, str]]:
    """The ``(org_id, team_id)`` pairs this person belongs to — Issue #4690.

    The team rung's matching key. Derived from the person's OWN ``users`` rows (the
    fusion ``resolve_person_identity`` already performed), so it is a projection of
    one identity decision rather than a second opinion on who the person is — the
    same discipline ``resolve_person_subs`` keeps for the direct-ledger namespace.

    **Both halves of the pair are required.** ``teams`` carries ``TenantMixin``, so a
    ``teams.id`` is unique inside its org and not globally: matching a team default
    on the team id alone would let a rule authored for one tenant's team govern a
    same-id team in an unrelated tenant. That is the #4511 wrong-key class in its
    more damaging direction — not an inert cap, but a cap that governs somebody it
    was never authored for, in a tenant whose admin cannot see it.

    ``users.team_id`` is ``NOT NULL`` in the schema but is written ``""`` by some
    provisioning paths (shadow users from ``POST /resolve-user``); an empty team id
    is dropped rather than matched, because a team default can only have been
    authored against a real ``teams.id`` and an empty string would be a shared
    bogus key every such user in the tenant collides on. Same argument
    ``resolve_person_subs`` makes for a NULL ``cognito_sub``.

    Returns:
        Sorted distinct ``(org_id, team_id)`` pairs, possibly empty. Sorted for a
        deterministic read order, exactly as the id and sub lists are.
    """
    rows = (await db.execute(select(User.org_id, User.team_id).where(User.id.in_(person_user_ids)))).all()
    return sorted({(org_id, team_id) for org_id, team_id in rows if org_id and team_id})


def _tightest(rows: list[PersonBudgetDefault]) -> PersonBudgetDefault:
    """The row a person is held to when several rules match at ONE rung.

    **The lowest amount governs** (operator ruling, 2026-09-07), because a default
    is a ceiling. The case is real rather than hypothetical: a person who belongs
    to two orgs that both carry an org default matches two rules at the org rung.
    Taking the highest — or the first row the database happened to return — would
    mean a ceiling could be escaped by joining a second, more generous org, which
    is not a ceiling.

    Ties break on ``id`` so that two rules with the same amount always yield the
    same row, and therefore the same ``scope_label`` in a denial: an operator
    reading "org default for acme" must not see "org default for globex" on the
    next request for the same reason.
    """
    return min(rows, key=lambda row: (row.budget_amount_usd, row.id))


async def resolve_person_default_limits(
    db: AsyncSession,
    org_ids: list[str],
    team_keys: list[tuple[str, str]],
) -> dict[str, PersonLimit]:
    """The DEFAULT limit applicable to this person per period — Issue #4690.

    The fallback half of the ladder: what governs a person for whom no individual
    ``person_budget_configs`` row exists. Before this, that person was unlimited,
    which is the defect — "no personal row" meant "no ceiling" even on a platform
    whose admin had set one number for everybody.

    **Per period, the tightest scope wins, and it is a first-match walk, not a
    minimum.** ``_DEFAULT_RUNG_ORDER`` is team → org → platform, and the walk stops
    at the first rung that matched: a team rule of $5,000 beats a platform rule of
    $1,000 for that person, deliberately. That is what "unless we say otherwise"
    means — a more specific rule is an override, including an override upward.
    "Lowest wins" applies only WITHIN one rung (``_tightest``), where the several
    matches are peers and none is more specific than another.

    Args:
        db: Async session bound to the gateway DB.
        org_ids: The partitions this person belongs to — pass
            ``resolve_member_partitions``' output. Server-derived, never request
            input: this list is what decides whose rules may govern the person.
        team_keys: ``(org_id, team_id)`` pairs from ``resolve_person_team_keys``.

    Returns:
        ``{period_type: PersonLimit}`` for every calendar period that has an
        applicable default, ``{}`` when none does. Non-calendar rows are filtered
        out rather than left to fault ``get_period_start_end`` (the #4328 allowlist
        discipline).
    """
    # ONE query for all three rungs. The rungs are read together rather than
    # walked with a query each because the walk needs the losers anyway to decide
    # per PERIOD — a person can match a team rule monthly and only a platform rule
    # daily, and three sequential round-trips would answer neither faster.
    predicates = [PersonBudgetDefault.scope_type == "platform"]
    if org_ids:
        predicates.append(and_(PersonBudgetDefault.scope_type == "org", PersonBudgetDefault.scope_id_org.in_(org_ids)))
    if team_keys:
        # Matched as PAIRS, deliberately not `org_id IN (...) AND team_id IN (...)`:
        # the cross-product form matches org A's rule for team T against a person
        # who is in org A and in team T *of org B* — a rule governing somebody it
        # was not authored for. Composed as explicit ANDed pairs rather than a SQL
        # row-value `IN`, which not every backend this runs on (SQLite in tests)
        # plans the same way.
        predicates.append(
            and_(
                PersonBudgetDefault.scope_type == "team",
                or_(
                    *[and_(PersonBudgetDefault.scope_id_org == org_id, PersonBudgetDefault.scope_id_team == team_id) for org_id, team_id in team_keys]
                ),
            )
        )

    rows = (
        (
            await db.execute(
                select(PersonBudgetDefault).where(
                    or_(*predicates),
                    # Calendar periods only (#4328): a run/chain period has no
                    # calendar window and `get_period_start_end` RAISES for it, so a
                    # stray row must be filtered here rather than fault the person
                    # layer. Sorted because CALENDAR_PERIOD_TYPES is a frozenset —
                    # unsorted, the generated SQL varies between runs for no reason.
                    PersonBudgetDefault.period_type.in_(sorted(CALENDAR_PERIOD_TYPES)),
                )
            )
        )
        .scalars()
        .all()
    )

    by_period_and_scope: dict[tuple[str, str], list[PersonBudgetDefault]] = {}
    for row in rows:
        by_period_and_scope.setdefault((row.period_type, row.scope_type), []).append(row)

    limits: dict[str, PersonLimit] = {}
    for period_type in {row.period_type for row in rows}:
        for scope_type in _DEFAULT_RUNG_ORDER:
            candidates = by_period_and_scope.get((period_type, scope_type))
            if not candidates:
                continue
            winner = _tightest(candidates)
            limits[period_type] = PersonLimit(
                period_type=period_type,
                amount=winner.budget_amount_usd,
                enforcement_mode=winner.enforcement_mode,
                source=_SOURCE_BY_SCOPE_TYPE[scope_type],
                scope_label=_default_scope_label(winner),
            )
            break

    return limits


def _default_scope_label(row: PersonBudgetDefault) -> str:
    """Name a default's provenance for a human reading a 402 or a 422.

    The rung ALONE is not enough on the org and team rungs: "an org default" does
    not tell a multi-org person which of their orgs set it, and that is exactly the
    thing they need to know to go ask somebody. The ids are the operator's handle
    on the row, so they are named.
    """
    if row.scope_type == "platform":
        return "platform default"
    if row.scope_type == "org":
        return f"org default for {row.scope_id_org}"
    return f"team default for {row.scope_id_team} in {row.scope_id_org}"


async def resolve_individual_person_limits(
    db: AsyncSession,
    person_anchor: str,
    self_authored_by: str | None = None,
) -> dict[str, PersonLimit]:
    """The ladder's TOP rung alone: this person's own ``person_budget_configs`` rows.

    Separated from :func:`resolve_applicable_person_limits` so the enforcement layer
    can call *just this rung* when the process-local gate has already established
    that **no default rule exists anywhere on the install**. In that state the ladder
    provably degenerates to its top rung, and resolving the person's orgs and teams
    to discover an empty set of defaults would put several queries back on the hot
    path for every request — the #4689 regression. This is that degenerate case
    named and shared, rather than a second copy of the read.

    Args:
        db: Async session bound to the gateway DB.
        person_anchor: The ``github:<id>`` key the authoring surface writes.
        self_authored_by: The caller's canonical ``users.id`` when resolving for
            that caller — see :func:`resolve_applicable_person_limits`.

    Returns:
        ``{period_type: PersonLimit}`` for each individual row, ``{}`` when none.
    """
    rows = (
        (
            await db.execute(
                select(PersonBudgetConfig).where(
                    PersonBudgetConfig.person_anchor == person_anchor,
                    # Calendar periods only — a run/chain row would fault
                    # `get_period_start_end` downstream (#4328).
                    PersonBudgetConfig.period_type.in_(sorted(CALENDAR_PERIOD_TYPES)),
                )
            )
        )
        .scalars()
        .all()
    )

    limits: dict[str, PersonLimit] = {}
    for row in rows:
        # `admin` when the row was authored by anybody other than the person this
        # ladder is being resolved for. EVERY caller — the read surface AND
        # enforcement — passes the person's own canonical id (review fix on #4696:
        # enforcement used to pass None, which interpolated 'set for you by a
        # platform administrator' into 402s for limits people set on THEMSELVES,
        # sending them to an admin who could find no such grant).
        source: PersonLimitSource = "own" if self_authored_by is not None and row.authored_by_user_id == self_authored_by else "admin"
        limits[row.period_type] = PersonLimit(
            period_type=row.period_type,
            amount=row.budget_amount_usd,
            enforcement_mode=row.enforcement_mode,
            source=source,
            scope_label="your own limit" if source == "own" else "a limit set for you by a platform administrator",
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
        )
    return limits


async def resolve_applicable_person_limits(
    db: AsyncSession,
    *,
    person_anchor: str | None,
    org_ids: list[str],
    team_keys: list[tuple[str, str]],
    self_authored_by: str | None = None,
) -> dict[str, PersonLimit]:
    """The FULL ladder: which limit governs this person, per period — Issue #4690.

    ``individual row > team default > org default > platform default``. This is the
    single definition of "this person's applicable limit", consumed by enforcement
    (``_check_person_budget``, ``_person_cap_headroom``) and by the read surface
    (``/me/budget/person-cap``). Two implementations of it would be the #4620 class
    of defect one layer up: a screen labelling somebody unlimited while a 402 stops
    them at a default they were never shown.

    **An individual row shadows the default for its own period only.** A person
    with a monthly personal limit and no daily one is still governed by a daily
    default. Resolving per period rather than per person is what makes that work,
    and the alternative ("any individual row means defaults do not apply") would
    let somebody escape a daily platform ceiling by authoring an unrelated monthly
    limit on themselves.

    Args:
        db: Async session bound to the gateway DB.
        person_anchor: The person's ``github:<id>`` key — the SAME key the authoring
            surface writes (``format_person_anchor``), never a re-derived one.
            ``None`` for a person with no linked GitHub identity: they can hold no
            individual row (it would be keyed on an anchor no ledger row carries —
            the #4511 class), but a default STILL governs them, which is the point
            of a default. Skipping the top rung is not the same as skipping the
            ladder.
        org_ids: Server-derived partitions (``resolve_member_partitions``).
        team_keys: ``(org_id, team_id)`` pairs (``resolve_person_team_keys``).
        self_authored_by: The caller's canonical ``users.id`` when this is being
            resolved FOR that caller, so an individual row can be reported as
            ``own`` rather than ``admin``. ``None`` (the enforcement path) reports
            every individual row as ``admin`` — see
            :func:`resolve_individual_person_limits`.

    Returns:
        ``{period_type: PersonLimit}``, empty when this person is governed by
        nothing at all. An empty result is the honest "unlimited" and the only case
        the enforcement layer may treat as such.
    """
    limits = await resolve_person_default_limits(db, org_ids, team_keys)

    if person_anchor:
        # The top rung overwrites the default for the periods it covers — and only
        # those, so a monthly personal limit does not lift a daily default.
        #
        # HARD rows only (review fix on #4696): a C3-era `soft` row is
        # informational — it warns and never denies — so letting it shadow a hard
        # default would silently EXEMPT its holder from a ceiling every peer is
        # denied at. A soft row still surfaces where no default governs that
        # period (today's display behavior), but a rule that enforces is never
        # displaced by one that does not.
        for period, individual in (await resolve_individual_person_limits(db, person_anchor, self_authored_by)).items():
            if individual.enforcement_mode == "hard" or period not in limits:
                limits[period] = individual

    return limits
