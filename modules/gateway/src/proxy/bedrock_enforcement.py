"""Is routing ENFORCED for this call, and if so, what does the proxy sign with?

Issue #4744 (#4692 · R3 · routing enforcement), per the design note
``docs/design-notes/4692-per-principal-bedrock-account-routing.md`` §2.5, §8.2, §8.3.

This is the seam between R2's *observation* and R3's *action*, and it is a separate
module from both on purpose. ``bedrock_routing`` answers "where should this go" and its
docstring promises it never signs anything — a promise R2 backs with a test asserting
the module's source contains no ``assume_role``. ``bedrock_signing`` answers "get me
those credentials". This module answers the third question, the only one that is a
*policy* question: **is this org allowed to have that answer acted upon yet?**

The decision (§8.2) is two-gated, and both gates must be open:

1. ``BG_BEDROCK_ROUTING_ENFORCE`` — the environment master switch, off by default, set
   by whoever deploys. Shadow mode (R2) remains the global default.
2. ``organizations.settings["bedrock_routing_enforce"]`` — the per-org opt-in, set by an
   operator *after* reviewing that org's shadow-mode evidence.

They are separate because they answer different questions on different timescales and
are operated by different people: "is this code path live in this environment" versus
"has this tenant been signed off". Collapsing them into one would mean the only way to
stop a bad rollout is editing tenant rows one at a time — and under fail-closed a bad
rollout is an outage, so §8.3 requires the fast lever to exist and to work without a
deploy. Flag 1 is that lever.

**No migration.** The per-org flag lives in the existing ``organizations.settings``
JSON column, which is why this issue ships no schema change. A dedicated column would
be a migration for a boolean that is expected to be temporary — §8.2 phase 3 is
"default enforce", at which point the per-org opt-in is retired rather than backfilled.

**Latency.** Enforcement adds a per-org read to the hot path, so it rides the same
existence-gate discipline as R2 (§2.2, the #4689 lesson): a short-TTL process-local
cache, and it is only ever consulted **after** the resolver has returned a non-platform
target. An install with no mappings resolves to the platform rung and never reaches this
module, so it stays at the zero-extra-queries cost R2 established. The ordering is the
optimization: check the cheap cached global flag, then resolve, then check the per-org
flag only if there is something to enforce.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.proxy.bedrock_routing import BedrockTarget, bedrock_routing_resolver
from src.proxy.bedrock_signing import DestinationCredentials, bedrock_destination_signer
from src.shared.config import get_settings
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger("bedrockgateway.proxy.enforcement")

# The key inside `organizations.settings`. Wire contract with whatever operator tooling
# flips it (R4's admin panel, or a psql UPDATE until then), so it must not be renamed.
ORG_SETTING_ENFORCE = "bedrock_routing_enforce"

# Same TTL and same reasoning as R2's mapping-existence gate and #4689's person-cap
# gate: bounded staleness in both directions, and both directions are survivable.
#
# The asymmetry worth stating, because it is the opposite of R2's: here a stale read
# has real consequences. Stale-ON for up to 60s after an operator disables enforcement
# means a mapped principal's calls keep being routed for up to a minute. That is
# acceptable for the *disable* direction only because the true emergency lever is the
# environment flag, which is read from settings per request and is not cached here at
# all — so an operator halting a bad rollout does not wait on this TTL.
_ENFORCE_FLAG_TTL_SECONDS = 60.0


class BedrockEnforcementGate:
    """Caches the per-org enforcement opt-in. One instance per process.

    Shared per process (see :data:`bedrock_enforcement_gate`) for the same reason the
    resolver is: the cache lives on the instance, so a per-request instance would turn
    the gate into one query per model call — the hot-path regression it exists to
    prevent.
    """

    def __init__(self) -> None:
        # org_id -> (enforced, monotonic_stamp). Per-org rather than global because
        # phase 2 is explicitly a per-org rollout, so unlike R2's existence gate there
        # is no single boolean to collapse it to. Unlocked and process-local, matching
        # the established pattern.
        self._cache: dict[str, tuple[bool, float]] = {}

    def clear(self) -> None:
        """Drop every cached verdict. For tests and for an operator-triggered reload."""
        self._cache.clear()

    async def is_enforced_for_org(self, session: AsyncSession, org_id: str | None) -> bool:
        """Has this org been opted in to routing enforcement?

        Both gates are checked here, environment flag first — it is a pure settings read
        with no I/O, so when enforcement is off in an environment this costs nothing and
        touches no database.

        Returns False for a caller with no ``org_id``. Such a principal cannot have been
        deliberately opted in by an operator (there is no row to opt in), and defaulting
        to *not enforced* means the call is served by the platform account exactly as it
        is on main. Note this is the safe direction only because the alternative would
        be failing calls that main serves — it does not risk a wrong bill, since not
        enforcing means not routing.
        """
        settings = get_settings()
        if not settings.bedrock_routing_enforce:
            return False
        if not org_id:
            return False

        now = time.monotonic()
        cached = self._cache.get(org_id)
        if cached is not None and now - cached[1] < _ENFORCE_FLAG_TTL_SECONDS:
            return cached[0]

        enforced = await self._read_org_flag(session, org_id)
        self._cache[org_id] = (enforced, now)
        return enforced

    @staticmethod
    async def _read_org_flag(session: AsyncSession, org_id: str) -> bool:
        """Read ``organizations.settings["bedrock_routing_enforce"]``.

        Selects the single JSON column, not the whole ORM entity: this runs on the hot
        path, and ``Organization`` carries a wide row (several JSON columns) that this
        decision has no use for.

        Anything other than a literal ``True`` is False, including the string
        ``"true"``. Strict because this flag's ON state moves real money to a different
        AWS account: a truthiness check would let ``"false"`` — a plausible thing for a
        hand-written UPDATE to put in a JSON column — read as enabled, which is the
        wrong-account bug arriving via a typo. An operator who writes a string gets the
        safe behaviour plus the warning below, rather than silent enforcement.
        """
        from src.shared.models.organization import Organization

        raw = (await session.execute(select(Organization.settings).where(Organization.id == org_id))).scalar_one_or_none()
        if not isinstance(raw, dict):
            return False

        value = raw.get(ORG_SETTING_ENFORCE, False)
        if value is True:
            return True
        if value not in (False, None):
            logger.warning(
                "Ignoring non-boolean Bedrock routing enforcement flag; enforcement stays OFF",
                extra={"org_id": org_id, "value_type": type(value).__name__},
            )
        return False


bedrock_enforcement_gate = BedrockEnforcementGate()


@dataclass(frozen=True)
class RoutingDecision:
    """What the proxy should sign this request with, and what to record for it.

    ``credentials is None`` means "use the ambient platform client" — the case for
    every unmapped call and every non-enforced org, byte-identical to main.

    ``target`` is carried so the settlement path can record the account **actually**
    used without resolving a second time. It is None when enforcement is not active at
    all, which is the signal to ``_log_usage`` that R2's shadow observation still owns
    the column. Distinguishing the two is what keeps an enforced request from paying
    for two ladder walks.
    """

    target: BedrockTarget | None = None
    credentials: DestinationCredentials | None = None

    @property
    def is_enforced(self) -> bool:
        return self.target is not None


async def resolve_routing_decision(context: TokenContext) -> RoutingDecision:
    """Decide what this request signs with. The single enforcement entry point.

    Ordering is deliberate and is the latency design (§2.2):

    1. Environment flag — a settings read, no I/O. Off ⇒ return immediately, and the
       hot path is exactly main's.
    2. Resolve the ladder (R2), which is itself gated by the cached
       "do any mappings exist" boolean — zero queries for an install with no mappings.
    3. Per-org opt-in — consulted **only** if a non-platform target was resolved, so
       orgs with no mapping never pay for it.
    4. Assume the destination role, cached per the §2.3 identity tuple.

    Step 3 after step 2 is the non-obvious ordering, and it is worth stating why: the
    reverse (check opt-in, then resolve) would query the org row on every request from
    an enforced org even when that request has no mapping and enforcement is therefore
    irrelevant to it.

    Raises:
        BedrockAccountUnavailableError: When a mapped destination cannot serve the call
            — the fail-closed outcome (ruling 1). Deliberately **not** caught here:
            this propagates to the caller and becomes a 502 naming the account, the
            cause and the fix.

    On an *infrastructure* failure (database unreachable, so the mapping cannot be
    read) the exception also propagates, surfacing as the proxy's ordinary 500. That is
    a conscious choice with a real cost, so it is stated rather than buried: when the
    environment flag is on, a database outage fails model calls that main would have
    served. The alternative — treating "cannot determine routing" as "not routed" —
    is precisely the silent fallback to the platform account ruling 1 forbids, since a
    mapped org's traffic would be billed to the platform with a 200 response and no
    signal. Availability is recoverable; a misdirected bill is not. The fast remedy is
    the environment flag (§8.3), which is read per request and needs no deploy.
    """
    settings = get_settings()
    if not settings.bedrock_routing_enforce:
        return RoutingDecision()

    from src.shared.database import get_session_factory

    session_factory = get_session_factory()
    async with session_factory() as session:
        target = await bedrock_routing_resolver.resolve(session, context)

        # No mapping matched: today's behaviour, no per-org read, no assume. Returned
        # as a target (not None) so the settlement path records "platform, and that was
        # the correct answer" without walking the ladder again.
        if target.is_platform:
            return RoutingDecision(target=target)

        if not await bedrock_enforcement_gate.is_enforced_for_org(session, context.org_id):
            # Resolved to a real destination, but this org has not been opted in. That
            # is shadow mode for this org: observe, do not act. Returning target=None
            # hands the column back to R2's observation path so the recorded value
            # keeps meaning "would have gone here", not "went here".
            return RoutingDecision()

        user_id = await bedrock_routing_resolver.resolve_canonical_user_id(session, context)
        credentials = await bedrock_destination_signer.get_credentials(
            session,
            target,
            # Session tag only, for CloudTrail attribution in the destination account.
            # Canonical `users.id`, never the Cognito sub in `context.user_id` — the
            # repo's #4647 id-namespace contract, and what the connect flow's
            # UserSessionTag is set to. Falls back to the raw principal when there is
            # no canonical row (service accounts), which is an honest audit value
            # rather than an empty tag.
            user_id=user_id or context.user_id or "unknown",
        )
        return RoutingDecision(target=target, credentials=credentials)
