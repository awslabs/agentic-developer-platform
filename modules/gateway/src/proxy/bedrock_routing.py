"""Bedrock routing resolution — which AWS account *should* serve this call.

Issue #4743 (R2 · routing foundation), per the design note
``docs/design-notes/4692-per-principal-bedrock-account-routing.md`` §1.2, §2.2,
§3.4, §4.4, §7, §8.2.

**This module resolves and reports. It never signs anything.** No STS call, no
``AssumeRole``, no credential construction, no ``get_client`` argument — those are
issue #4744 (R3). Shadow mode (§8.2 phase 1) means the resolved target is written
to ``usage_logs.bedrock_account_id`` and to the log, while the request continues to
be signed with the platform account's ambient IRSA credentials exactly as before.
The whole point of the phase is that the wrong-account bug becomes **detectable
before it can cost anyone money**, so nothing here may change where a call lands.

The ladder (§1.2), first match wins, narrowest first::

    1. user      — mapping row scope_type='user'  (canonical users.id)
    2. team      — mapping row scope_type='team'  (scope_id_org + scope_id_team)
    3. org        — mapping row scope_type='org'  (scope_id_org)
    4. platform  — no mapping matched → ambient IRSA, today's behaviour

Rung 4 is the *absence* of a mapping, not a row. It is still returned as an
explicit :class:`BedrockTarget` rather than ``None`` so that shadow mode can record
"this call went to the platform account, and that was the correct answer" — which
is a different fact from "we did not look", and the audit trail needs to
distinguish them.

Three properties that are requirements rather than choices:

**Latency: the existence gate comes first (§2.2, the #4689 lesson).** Every model
call would otherwise pay an indexed lookup to learn "no mapping". A process-local
60s-TTL cached boolean answers the overwhelmingly common case — zero mappings,
which is *every* install on day one — in **zero queries**. This mirrors
``BudgetEnforcementService._person_limit_sources_exist`` deliberately, down to the
unlocked cache and the bounded-staleness trade.

**Isolation: resolution reads ``context.org_id``, never ``attributed_org_id``
(§3.4).** This is the single most important line in the module.
``attributed_org_id`` is caller-influenced by design (an internal-plane agent may
point it at the tenant that triggered a run so *billing* lands there). Keying
routing off it would let that same header choose **which AWS account gets
charged** — resurrecting #4132 as a cross-account credential-acquisition bug
rather than "only" an accounting one. Routing is a strict consumer of the
authenticated field and writes none of the attribution fields.

**Safety: an unusable destination is NO MATCH, not a failed request (§4.4).** A
destination that was never verified, or whose role cannot invoke Bedrock, is
skipped and the walk continues to the next rung. Under the eventual fail-closed
rule this matters: merely *starting* an AWS-connect flow must not be able to
reroute a principal's traffic onto an account that fails every call.

Bedrock-reaching path coverage (§7) — enumerated here because a silently partial
rollout is a spend bug of the same class as a wrong account:

===================================================== ==========================
Path                                                  Status
===================================================== ==========================
Gateway proxy (``src/proxy/service.py``)               **Covered** — resolves and
                                                       logs on every call.
Mantle passthrough (``src/proxy/mantle_service.py``)   **Capture only.** The
                                                       resolved account is logged
                                                       so the column is not
                                                       silently NULL on 100% of
                                                       OpenAI-passthrough rows,
                                                       but this path is **not
                                                       routable** by this design
                                                       (§7.2): ``SigV4MantleAuth``
                                                       is built once at app
                                                       startup, so per-request
                                                       routing there is a larger
                                                       refactor. Knowingly out of
                                                       scope, documented not
                                                       omitted.
``ADP_BEDROCK_VIA=user`` worker branch                 **Knowingly uncovered.**
                                                       The pod calls Bedrock with
                                                       the customer's own
                                                       credentials, bypassing the
                                                       gateway entirely — no
                                                       usage row at all, so
                                                       nothing to populate. On
                                                       ruling 3's shadow → enforce
                                                       → remove retirement path
                                                       (§7.1); not this issue's.
agent-factory ``sqs_consumer.py`` / ingest classifier  **Out of scope.**
                                                       Non-gateway Lambdas on the
                                                       platform account with no
                                                       principal context to
                                                       resolve against.
===================================================== ==========================
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import get_settings
from src.shared.models.bedrock_routing import BedrockAccountMapping, BedrockDestinationRegistry
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger("bedrockgateway.proxy.routing")

BedrockRoutingRung = Literal["user", "team", "org", "platform"]

# Walk order, narrowest first. `platform` is not here because it is the no-match
# branch rather than a rung with rows to read (§1.2).
_MAPPING_RUNG_ORDER: tuple[str, ...] = ("user", "team", "org")

# How long the "do ANY routing mappings exist?" verdict may be reused per process.
# Same value and same reasoning as `_PERSON_CAPS_EXISTENCE_TTL_SECONDS` (#4689):
# staleness is bounded in both directions and both directions are benign — a
# first-ever mapping starts being *observed* within the TTL rather than instantly,
# and deleting the last one wastes one query per window. In shadow mode neither
# direction can affect where a call lands, which is what makes the unlocked
# process-local cache the right trade here.
_MAPPING_EXISTENCE_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class BedrockTarget:
    """The account a call *should* be served by, and which rung decided that.

    Frozen because it is a resolution *result*: mutating one after the fact would
    make the logged account and the rung that chose it disagree, and the audit
    trail's only job is that they agree.

    ``rung`` is carried alongside the account because the account alone cannot be
    debugged. An operator reading shadow-mode output needs to know *which row* to
    fix, and "account …1234 via the team rung" is actionable where "…1234" sends
    them hunting through three scopes — the same labelled-source discipline
    #4691 applied to budget limits (§6.3).
    """

    account_id: str | None
    rung: BedrockRoutingRung
    destination_id: str | None = None
    region: str | None = None

    @property
    def is_platform(self) -> bool:
        """Is this the no-mapping fallback — today's ambient-IRSA behaviour?"""
        return self.rung == "platform"


class BedrockRoutingResolver:
    """Resolves the ladder in §1.2. Read-only, and it never signs a request.

    One instance is shared per process (see ``bedrock_routing_resolver`` at the
    bottom of this module) because the existence-gate cache lives on the instance
    and a per-request instance would defeat it entirely — the gate would then cost
    one query per call, which is precisely the hot-path regression it exists to
    prevent.
    """

    def __init__(self) -> None:
        # ``(mappings_exist, monotonic_stamp)``. Deliberately per-process and
        # unlocked: a stale read is bounded by the TTL and harmless in shadow mode.
        self._mappings_exist_cache: tuple[bool, float] | None = None

    def _platform_target(self) -> BedrockTarget:
        """Rung 4: no mapping matched, so the platform account serves the call.

        The account id comes from configuration rather than a live
        ``sts:get_caller_identity`` call, because this runs on the hot path and an
        STS round trip per request is exactly the unbounded work §2.2 forbids.

        When it is unset the account id is **None**, which persists as NULL and
        honestly means "not captured" — never a fabricated placeholder. The repo's
        null-discipline is explicit about this distinction for ``client_tool`` and
        the cache-token counters, and the same logic applies here: a made-up
        account id in an audit column is worse than an absent one, because it reads
        as evidence.
        """
        account_id = get_settings().platform_bedrock_account_id or None
        return BedrockTarget(account_id=account_id, rung="platform")

    async def _any_mappings_exist(self, session: AsyncSession) -> bool:
        """Does ANY routing mapping exist at all? Cached per process for 60s.

        The single most important latency decision in the design (§2.2) and the
        reason the feature can ship default-on-safe: an install with no mappings —
        every install on day one — performs **zero** queries per model call, not
        "one cheap query".

        The gate is naturally *global* rather than per-org because the mapping
        table has no ``org_id`` (§1.1b), which is strictly cheaper than a per-org
        boolean and collapses to one cached flag for the whole process.
        """
        now = time.monotonic()
        cached = self._mappings_exist_cache
        if cached is not None and now - cached[1] < _MAPPING_EXISTENCE_TTL_SECONDS:
            return cached[0]
        exists = bool((await session.execute(select(select(BedrockAccountMapping.id).exists()))).scalar())
        self._mappings_exist_cache = (exists, now)
        return exists

    @staticmethod
    async def resolve_canonical_user_id(session: AsyncSession, context: TokenContext) -> str | None:
        """Map the token's principal to a canonical ``users.id``, or None.

        Issue #4744: renamed from ``_resolve_canonical_user_id`` and made part of this
        class's public surface. The enforcement path needs the same canonical id — to
        send as a CloudTrail session tag on the destination assume — and importing it
        under a private name would be a second module depending on this one's internals.
        Same #4647 id-namespace contract, one implementation.

        ``scope_id_user`` stores a canonical ``users.id`` (#4647), but
        ``TokenContext.user_id`` holds a Cognito *sub* on the ordinary JWT path.
        Comparing the two directly would make every user-rung mapping silently
        never fire — the inert-config class (#4511) with no visible symptom.

        Matches on ``cognito_sub`` OR ``id``, the same two-step lookup
        ``AccessControlService`` uses: IAM/service-account callers already carry a
        canonical id in ``user_id``, so keying only on ``cognito_sub`` would
        resolve those callers to nothing.

        **Called only after the existence gate passes**, so an install with no
        mappings never pays for it — which is what keeps the day-one cost at zero
        queries. Returns None when the principal has no canonical row at all
        (legitimate for service accounts, §7.2); the user rung is then skipped,
        which is a correct miss rather than a wrong match.
        """
        # Local import: `organization` is a heavy model module and the hot path
        # should not carry it when the flag is off or no mapping exists.
        from src.shared.models.organization import User

        if not context.user_id:
            return None
        return (
            await session.execute(select(User.id).where(or_(User.cognito_sub == context.user_id, User.id == context.user_id)).limit(1))
        ).scalar_one_or_none()

    async def resolve(self, session: AsyncSession, context: TokenContext, *, user_id: str | None = None) -> BedrockTarget:
        """Walk the ladder and return the account that *should* serve this call.

        Args:
            session: Async session bound to the gateway DB.
            context: The authenticated request context. ``org_id`` and ``team_id``
                are read from it; ``attributed_org_id`` is **never** read (§3.4),
                and neither is ``attributed_user_id`` — both are caller-influenced
                or attribution-owned, and routing is a strict consumer of the
                authenticated fields only.
            user_id: The caller's canonical ``users.id`` when the caller already
                knows it, which skips the lookup below. Left unset by the proxy
                paths, which do not resolve it.

        Returns:
            Always a :class:`BedrockTarget`, never None — the platform rung is an
            answer, not an absence.
        """
        if not await self._any_mappings_exist(session):
            return self._platform_target()

        if user_id is None:
            user_id = await self.resolve_canonical_user_id(session, context)

        # ONE query for all three rungs, resolved most-specific-first in Python
        # (§2.2). Read together rather than walked with a query each: three
        # sequential round trips would answer no faster and would triple the
        # hot-path cost for the install that has any mapping at all.
        predicates = []
        if user_id:
            predicates.append(
                and_(
                    BedrockAccountMapping.scope_type == "user",
                    BedrockAccountMapping.scope_id_user == user_id,
                )
            )
        # The team rung needs BOTH ids, matched as a pair. `scope_id_team == team_id`
        # alone would match another org's rule for a same-named team — a `teams.id`
        # is unique only inside its org, so the pair is the identity (§1.1b).
        if context.org_id and context.team_id:
            predicates.append(
                and_(
                    BedrockAccountMapping.scope_type == "team",
                    BedrockAccountMapping.scope_id_org == context.org_id,
                    BedrockAccountMapping.scope_id_team == context.team_id,
                )
            )
        if context.org_id:
            predicates.append(
                and_(
                    BedrockAccountMapping.scope_type == "org",
                    BedrockAccountMapping.scope_id_org == context.org_id,
                )
            )
        if not predicates:
            # No identity to match on at all (no canonical user id, no org). The
            # platform rung is the honest answer rather than an unfiltered query,
            # which would match every mapping in the table.
            return self._platform_target()

        rows = (
            (
                await session.execute(
                    select(BedrockAccountMapping, BedrockDestinationRegistry)
                    .join(
                        BedrockDestinationRegistry,
                        BedrockDestinationRegistry.id == BedrockAccountMapping.destination_id,
                    )
                    .where(or_(*predicates))
                )
            )
            .tuples()
            .all()
        )

        by_rung: dict[str, list[tuple[BedrockAccountMapping, BedrockDestinationRegistry]]] = {}
        for mapping, destination in rows:
            by_rung.setdefault(mapping.scope_type, []).append((mapping, destination))

        for rung in _MAPPING_RUNG_ORDER:
            for mapping, destination in by_rung.get(rung, []):
                if not destination.is_usable_for_routing:
                    # Skip, do not fail (§4.4). An unverified or non-routing-capable
                    # destination must fall through to the next rung — otherwise
                    # merely starting a connect flow would take a principal's model
                    # access down once enforcement lands.
                    logger.warning(
                        "Bedrock routing skipped an unusable destination",
                        extra={
                            "rung": rung,
                            "mapping_id": mapping.id,
                            "destination_id": destination.id,
                            "routing_capable": destination.routing_capable,
                            "verified": destination.verified_at is not None,
                        },
                    )
                    continue
                return BedrockTarget(
                    account_id=destination.account_id,
                    rung=rung,  # type: ignore[arg-type]
                    destination_id=destination.id,
                    region=destination.region,
                )

        return self._platform_target()


# Process-wide instance, mirroring `budget_enforcement_service`. The existence-gate
# cache lives on it, so sharing one instance is what makes the gate work at all.
bedrock_routing_resolver = BedrockRoutingResolver()


async def resolve_shadow_target(context: TokenContext, *, user_id: str | None = None) -> BedrockTarget | None:
    """Resolve the would-be target for shadow-mode logging. Never raises.

    The shape the proxy's metering path calls. It owns its own session because it
    is invoked from ``_log_usage``, which runs in a ``finally`` and already opens
    its own session for the usage write — reusing that one would tie the routing
    read to the lifetime of a transaction it has no business in.

    **Every failure is swallowed and returns None.** Shadow mode is an observation,
    and an observation that can fail a model call is worse than no observation.
    This matches the surrounding metering code, which swallows exactly the same way
    for the same reason. None means "not resolved" and persists as NULL — which is
    honestly distinguishable from a resolved platform account.

    Returns None (without touching the database) when shadow mode is off, so the
    flag is a genuine off switch and not merely a change of destination.
    """
    settings = get_settings()
    if not settings.bedrock_routing_shadow_mode:
        return None
    try:
        # Imported here rather than at module scope: `src.shared.database` pulls in
        # the engine/session machinery, and the proxy hot path should not carry that
        # import cost when the flag is off.
        from src.shared.database import get_session_factory

        session_factory = get_session_factory()
        async with session_factory() as session:
            return await bedrock_routing_resolver.resolve(session, context, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 - shadow observation must never break the proxy
        logger.warning(
            "Bedrock routing shadow resolution failed",
            extra={"error": str(exc), "org_id": context.org_id},
        )
        return None
