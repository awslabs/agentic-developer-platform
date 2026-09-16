"""
Live-denominator spend reservations for budget enforcement (Issue #4287).

## Why this exists

The budget check compares a pre-request estimate against ``budget_usage``, which
only materializes minutes later — an S3 chat-log write fires the
``budget-usage-tracker`` Lambda, which then writes the row. So the
``current_spend`` the check reads is *lagged*, and a burst of concurrent requests
all read the same stale figure. Each one individually "passes" the cap while the
burst collectively blows straight through it.

A reservation closes that gap: before a request is admitted, its estimated cost
is atomically added to a live in-flight counter, and the check compares against
``settled_postgres_spend + in_flight_reservations``. Concurrent requests now
contend on a figure that reflects each other.

## Why one Lua script over all keys

``_check_entity_budget`` runs per ``(entity, period)`` pair — up to 4 entities
(user → team → department → org) × 3 periods = 12 independent budgets per
request. A reservation must be taken against *every* pair that has a budget row,
or the cap at one level is unprotected.

Doing that as a loop of ``INCRBYFLOAT`` is not atomic: succeed on 6 keys, hit an
exhausted 7th, and the first 6 increments have leaked — permanently consuming
headroom for a request that was never admitted. So it is ONE ``EVAL`` that checks
every key's headroom *before* incrementing any of them. Nothing to roll back,
because nothing is written on the deny path.

## Why the reservation is not deleted on completion

Spend settles into Postgres asynchronously. Deleting a reservation the moment the
response lands would drop it out of the live denominator *before* it appears in
the settled one — a window in which the spend is counted nowhere and the cap
under-protects. So completion *adjusts the reservation in place* (estimate →
actual) and lets TTL reap it after settlement. That over-counts for at most one
TTL window, which is the conservative direction: caps stay sound.

## Release on crash

Adjustment happens in ``_log_usage``, which the proxy calls from a ``finally`` on
every path, so a failed request adjusts down to its real (usually ~zero) cost and
returns the headroom. ``_log_usage`` deliberately swallows exceptions, and a pod
SIGKILL skips ``finally`` entirely, so explicit release is best-effort:
**per-reservation deadlines are the real backstop**. Each hash field carries its
own expiry and expired fields are ignored when summing and pruned on write, so a
reservation from a killed pod stops consuming cap after the TTL regardless of
whether any code ran.

## Why this must never 503

Wave 1 (#4075) kept Redis off the healthy path on purpose — it was consulted only
*after* a DB read had already failed. This module puts Redis on the hot path of
every enforced request for the first time, which is a new availability surface.
A Redis blip must therefore NOT be treated as a budget-check failure: it does not
touch the DB grace window and it does not deny. It degrades to the Wave 1 DB-only
verdict (lagged denominator, still fail-closed on the ledger) and alarms. Callers
get ``None`` from :meth:`ReservationStore.reserve` to signal exactly that.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

import redis.asyncio as redis

from src.shared.logging import get_logger
from src.shared.redis_client import create_redis_client

logger = get_logger(__name__)

_KEY_PREFIX = "budget:resv"

# Check EVERY key's headroom before incrementing ANY of them.
#
# KEYS  = one reservation hash per (entity, period) budget being enforced
# ARGV = [request_id, amount, now, headroom_1, ttl_1, initialized_1, ...]
#
# Each hash maps request_id -> "<amount>:<deadline>". Storing the deadline
# per-field (rather than relying on the key TTL) is what makes an abandoned
# reservation self-healing: a request whose pod was SIGKILLed leaves a field
# behind, and every later reader treats it as gone once its deadline passes.
# The key TTL is refreshed on each write so an entity with continuous traffic
# never loses its live counter mid-period, while an idle one is reaped.
#
# Issue #4187: the TTL is PER-KEY, not one value shared across all of them. The
# hierarchy budgets (user/team/dept/org) want the short #4287 lifetime — it is a
# SIGKILL backstop for one in-flight request, and holding longer would throttle a
# tenant below their real spend. A run/chain cap wants the opposite: it must
# accumulate for the whole run, so a 120s field deadline would make it forget
# everything older than two minutes and cap nothing at all. Same Lua, same
# reconcile path, one number per target.
#
# Returns {admitted, exhausted_index} — exhausted_index is 1-based into KEYS on
# denial so the caller can attribute the denial to the right budget, or 0 when
# admitted.
_RESERVE_SCRIPT = """
local request_id = ARGV[1]
local amount = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

-- Pass 1: verify every budget has room. No writes here, so a denial cannot
-- leave a partial reservation behind.
for i = 1, #KEYS do
    local headroom = tonumber(ARGV[1 + (i * 3)])
    local strict = ARGV[3 + (i * 3)] == '1'
    if strict then
        local anchor = redis.call('HGET', KEYS[i], '__initialized__')
        if not anchor then return {-1, i} end
        local sep = string.find(anchor, ':')
        if not sep or tonumber(string.sub(anchor, 1, sep - 1)) ~= 0 or tonumber(string.sub(anchor, sep + 1)) <= now then
            return {-1, i}
        end
    end
    local in_flight = 0
    local entries = redis.call('HGETALL', KEYS[i])
    for j = 1, #entries, 2 do
        local field, value = entries[j], entries[j + 1]
        local sep = string.find(value, ':')
        local entry_amount = tonumber(string.sub(value, 1, sep - 1))
        local entry_deadline = tonumber(string.sub(value, sep + 1))
        -- A policy accumulator cannot discard usage because a field expired.
        -- Pending provider calls get a bounded observation window; a missing
        -- receipt beyond it blocks further spend until reconciliation.
        if strict and entry_deadline <= now then return {-1, i} end
        -- Skip this request's own prior reservation: a retry must replace it,
        -- not stack on top of it. Skip expired entries: their owner is gone.
        if field ~= request_id and entry_deadline > now then
            in_flight = in_flight + entry_amount
        end
    end
    if in_flight + amount > headroom then
        return {0, i}
    end
end

-- Pass 2: every budget had room, so commit to all of them.
for i = 1, #KEYS do
    local ttl = tonumber(ARGV[2 + (i * 3)])
    local strict = ARGV[3 + (i * 3)] == '1'
    local deadline = now + ttl
    -- Prune expired fields opportunistically so the hash cannot grow without
    -- bound under sustained traffic on a long period (e.g. monthly).
    local entries = redis.call('HGETALL', KEYS[i])
    for j = 1, #entries, 2 do
        local value = entries[j + 1]
        local sep = string.find(value, ':')
        if tonumber(string.sub(value, sep + 1)) <= now then
            redis.call('HDEL', KEYS[i], entries[j])
        end
    end
    redis.call('HSET', KEYS[i], request_id, amount .. ':' .. deadline)
    if strict then
        redis.call('HSET', KEYS[i], 'pending:' .. request_id, '0:' .. (now + 3660))
    end
    redis.call('EXPIRE', KEYS[i], ttl)
end

return {1, 0}
"""

# Overwrite an existing reservation's amount with the settled actual.
#
# Adjust-in-place, keyed on request_id, is what makes reconciliation idempotent:
# running it twice for the same request writes the same value twice instead of
# debiting twice. HSET (not HINCRBYFLOAT) is the whole point.
#
# Only touches a field that exists AND has not passed its own deadline. An
# expired reservation must NOT be resurrected — its spend is on its way to
# Postgres, and re-adding it here would double-count against the settled total,
# blocking the tenant below their true cap.
#
# The field deadline — not HEXISTS — is what decides that. The key TTL is
# refreshed by every write against the same budget, so on a busy entity the hash
# outlives any individual field's deadline: a field can be long expired (ignored
# by the reserve pass, correctly) and still be physically present. Reconciling on
# HEXISTS alone would hand it a fresh deadline and put the spend back into the
# live denominator. An expired field found here is pruned instead.
#
# Issue #4187: TTL is per-key here too, for the same reason as the reserve
# script — a run/chain reservation adjusted to its real cost must keep the
# run-lifetime deadline, not inherit the hierarchy's short one. Inheriting the
# short one would silently drop settled run spend out of the accumulator two
# minutes after each call, which is the cap-that-does-not-cap failure again.
#
# ARGV = [request_id, amount, now, ttl_1, ... ttl_N]
_RECONCILE_SCRIPT = """
local request_id = ARGV[1]
local amount = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local adjusted = 0

for i = 1, #KEYS do
    local existing = redis.call('HGET', KEYS[i], request_id)
    if existing then
        local sep = string.find(existing, ':')
        if tonumber(string.sub(existing, sep + 1)) > now then
            local ttl = tonumber(ARGV[3 + i])
            redis.call('HSET', KEYS[i], request_id, amount .. ':' .. (now + ttl))
            redis.call('HDEL', KEYS[i], 'pending:' .. request_id)
            redis.call('EXPIRE', KEYS[i], ttl)
            adjusted = adjusted + 1
        else
            redis.call('HDEL', KEYS[i], request_id)
        end
    end
end

return adjusted
"""


@dataclass(frozen=True)
class ReservationTarget:
    """One ``(entity, period)`` budget a request must reserve against.

    ``headroom_usd`` is ``budget_amount - settled_spend`` for this budget, i.e.
    what the Wave 1 DB read already told us. The reservation store adds the
    live in-flight total on top of it.
    """

    org_id: str
    entity_type: str
    entity_id: str
    period_type: str
    period_start: str
    headroom_usd: Decimal

    # Issue #4187: how long THIS budget's reservation fields live. ``None`` means
    # "use the store's default", which is what every #4287 hierarchy target does,
    # so their behaviour is unchanged.
    #
    # Run and chain scopes override it with the run lifetime. Their counter is not
    # a SIGKILL backstop for one request — it is the cap's entire denominator,
    # because run/chain budgets have no settled Postgres ledger to fall back on
    # (the budget-usage-tracker Lambda writes no run rows). Expiring it on the
    # short default would reset the run's spend to zero every two minutes.
    ttl_seconds: int | None = None
    # Accepted-policy accumulators are initialized once before any work runs.
    # Loss of that anchor or an unresolved provider receipt is unknown usage.
    require_initialization: bool = False

    def key(self) -> str:
        """Redis key for this budget's in-flight reservations.

        ``org_id`` is mandatory and comes first: the ledger is partitioned by
        attributed tenant (#4132), so a key omitting it would let two tenants
        that happen to share a ``user_id`` share a counter — cross-tenant
        leakage, where exhausting one tenant's cap denies another's traffic.

        The literal braces around ``org_id`` double as a Redis Cluster hash tag,
        so every key a single request touches lands in the same slot and the
        multi-key Lua stays valid if this is ever deployed against a cluster.

        ``period_start`` is in the key so a period rollover naturally abandons
        the previous counter instead of inheriting its total.
        """
        return f"{_KEY_PREFIX}:{{{self.org_id}}}:{self.entity_type}:{self.entity_id}:{self.period_type}:{self.period_start}"


@dataclass(frozen=True)
class ReservationOutcome:
    """Result of an attempted reservation.

    Attributes:
        admitted: ``True`` if the live denominator had room for this request.
        exhausted: Which budget ran out, when ``admitted`` is ``False``.
    """

    admitted: bool
    exhausted: ReservationTarget | None = None


@dataclass(frozen=True)
class ReservationSnapshot:
    total_usd: Decimal
    has_pending: bool


class ReservationStore:
    """Atomic in-flight spend reservations shared across all gateway processes.

    Args:
        redis_url: Redis connection URL. ``None`` disables reservations
            entirely (the caller then degrades to the Wave 1 DB-only check).
        ttl_seconds: How long an un-reconciled reservation keeps consuming cap.
            This is the SIGKILL backstop, so it must comfortably exceed p99
            request latency but stay short enough that a lost reservation is not
            a lasting denial-of-service against the tenant's own cap.
        clock: Injectable time source so tests can assert TTL expiry without
            sleeping (same idiom as ``grace_window.GraceWindow``).
        client: Injectable Redis client, for tests.
    """

    def __init__(
        self,
        redis_url: str | None,
        ttl_seconds: int,
        clock: Callable[[], float] | None = None,
        client: redis.Redis | None = None,
    ) -> None:
        self._redis_url = redis_url
        self._ttl_seconds = ttl_seconds
        self._clock = clock or time.time
        self._client = client
        self._reserve_script: redis.client.Script | None = None
        self._reconcile_script: redis.client.Script | None = None
        if client is not None:
            self._register_scripts(client)

    @property
    def enabled(self) -> bool:
        """Whether a reservation backend is configured at all."""
        return self._client is not None or bool(self._redis_url)

    def _ttl_for(self, target: ReservationTarget) -> int:
        """Resolve this target's field lifetime (Issue #4187).

        Falls back to the store default, so every #4287 caller that never sets
        ``ttl_seconds`` keeps the exact behaviour it had.
        """
        return target.ttl_seconds if target.ttl_seconds is not None else self._ttl_seconds

    def _register_scripts(self, client: redis.Redis) -> None:
        self._reserve_script = client.register_script(_RESERVE_SCRIPT)
        self._reconcile_script = client.register_script(_RECONCILE_SCRIPT)

    async def _get_client(self) -> redis.Redis:
        """Get or create the Redis client, registering the Lua scripts once."""
        if self._client is None:
            if not self._redis_url:
                raise RuntimeError("no redis_url configured")
            self._client = create_redis_client(self._redis_url, encoding="utf-8", decode_responses=True)
            self._register_scripts(self._client)
        return self._client

    async def reserve(
        self,
        request_id: str,
        amount_usd: Decimal,
        targets: list[ReservationTarget],
    ) -> ReservationOutcome | None:
        """Atomically reserve ``amount_usd`` against every target budget.

        Args:
            request_id: Idempotency key. A retry with the same id replaces its
                own prior reservation rather than stacking a second one.
            amount_usd: Estimated cost of this request.
            targets: Every ``(entity, period)`` budget being enforced, with the
                headroom the settled ledger reports for each.

        Returns:
            The reservation outcome, or ``None`` if the reservation could not be
            taken because Redis was unreachable. ``None`` is deliberately NOT a
            denial: the caller must degrade to the Wave 1 DB-only verdict rather
            than fail a request over a Redis blip.
        """
        if not targets:
            return ReservationOutcome(admitted=True)

        now = self._clock()

        # Interleaved (headroom, ttl, initialization requirement) per target.
        per_target_args: list[str | int] = []
        for target in targets:
            per_target_args.append(str(target.headroom_usd))
            per_target_args.append(self._ttl_for(target))
            per_target_args.append("1" if target.require_initialization else "0")

        try:
            client = await self._get_client()
            result = await self._reserve_script(  # type: ignore[misc]
                keys=[t.key() for t in targets],
                args=[
                    request_id,
                    str(amount_usd),
                    now,
                    *per_target_args,
                ],
                client=client,
            )
        except Exception as exc:
            # Redis is a NEW hot-path dependency here (Wave 1 only touched it
            # after a DB failure). Never convert its unavailability into a
            # request failure — see the module docstring.
            logger.warning(f"Budget reservation unavailable, degrading to settled-ledger check: {exc}")
            return None

        if int(result[0]) < 0:
            return None
        admitted = bool(int(result[0]))
        if admitted:
            return ReservationOutcome(admitted=True)

        index = int(result[1])
        return ReservationOutcome(admitted=False, exhausted=targets[index - 1])

    async def snapshot(self, target: ReservationTarget) -> ReservationSnapshot | None:
        """Read an initialized policy accumulator; absence is never zero spend."""
        try:
            entries = await (await self._get_client()).hgetall(target.key())
            now = self._clock()
            anchor = entries.get("__initialized__")
            if anchor is None:
                return None
            amount, deadline = anchor.split(":")
            if Decimal(amount) != 0 or float(deadline) <= now:
                return None
            total = Decimal(0)
            pending = False
            for field, value in entries.items():
                amount, deadline = value.split(":")
                cost = Decimal(amount)
                if not cost.is_finite() or cost < 0 or float(deadline) <= now:
                    return None
                total += cost
                pending |= field.startswith("pending:")
            return ReservationSnapshot(total, pending)
        except Exception:
            logger.warning("Policy budget snapshot unavailable")
            return None

    async def mark_unknown(self, request_id: str, target: ReservationTarget) -> None:
        """Retain the reserved upper bound and block new spend until a receipt."""
        try:
            client = await self._get_client()
            if await client.hexists(target.key(), request_id):
                await client.hset(target.key(), f"pending:{request_id}", f"0:{self._clock()}")
        except Exception:
            # The existing reservation still accounts for the request. Its
            # pending deadline will refuse future spend if this write was lost.
            logger.warning("Policy usage remains unresolved")

    async def reconcile(
        self,
        request_id: str,
        actual_usd: Decimal,
        targets: list[ReservationTarget],
    ) -> None:
        """Adjust this request's reservation from estimate to settled actual.

        Idempotent: repeated calls for the same ``request_id`` overwrite the
        same value instead of debiting again.

        The reservation is intentionally kept (not deleted) — the spend has not
        reached Postgres yet, so removing it here would drop it out of the live
        denominator before it enters the settled one. TTL reaps it after
        settlement.

        A reservation that already passed its deadline is left released rather
        than revived: by then its spend is settling into Postgres, so re-adding it
        would count it twice.

        Failures are swallowed: this runs on the response path, and the
        per-reservation deadline already bounds the damage.
        """
        if not targets:
            return

        now = self._clock()

        try:
            client = await self._get_client()
            await self._reconcile_script(  # type: ignore[misc]
                keys=[t.key() for t in targets],
                args=[request_id, str(actual_usd), now, *[self._ttl_for(t) for t in targets]],
                client=client,
            )
        except Exception as exc:
            logger.warning(f"Budget reservation reconcile failed (TTL will reap): {exc}")

    async def close(self) -> None:
        """Close the Redis client."""
        if self._client:
            await self._client.aclose()
            self._client = None
            self._reserve_script = None
            self._reconcile_script = None
