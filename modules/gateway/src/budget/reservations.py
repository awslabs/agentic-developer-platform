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

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

import redis.asyncio as redis

from src.shared.logging import get_logger
from src.shared.redis_client import create_redis_client

logger = get_logger(__name__)

_KEY_PREFIX = "budget:resv"
# A strict reservation's `pending:` marker lives this long; writers delete it on
# settlement. mark_unknown() rewrites it as already expired. A request is treated
# as abandoned (#7451) once its marker has been expired for a short settlement
# grace, so a receipt that is merely late still wins. The marker already outlives
# any provider call, so by the time it expires the request is over; while the
# flow waits, every model call of every worker in the flow is refused with
# spend_unknown, which killed in-flight developers (#7509). Deploys that restart
# the gateway mid-request are the usual source of lost settlements.
PENDING_MARKER_SECONDS = 3660
ABANDONED_PENDING_GRACE_SECONDS = 300

# Check EVERY key's headroom before incrementing ANY of them.
#
# KEYS  = one reservation hash per (entity, period) budget being enforced
# ARGV = [request_id, amount, now,
#         headroom_1, ttl_1, initialized_1, retained_1, pending_1, amount_1, ...]
#
# Issue #9536: ``amount_i`` is the amount reserved against KEYS[i]. Every target
# passes the shared request estimate except a target that carries its own
# ``reserve_usd`` (the Project meter holding a quote ceiling). The headroom
# check and the stored field both use the per-target amount.
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
-- ARGV[2] is the shared estimate; each target's own amount arrives per target.
local now = tonumber(ARGV[3])

-- An authenticated policy meter already records which requests still await
-- trusted usage. Read those markers atomically with the cap check. They only
-- classify a DENIAL as transient; they never reduce the admitted denominator.
local pending_requests = {}
local retained_targets = {}
for i = 1, #KEYS do
    -- A finalized Project request cannot acquire a second spending allowance.
    if redis.call('EXISTS', KEYS[i] .. ':settled:' .. request_id) == 1 then return {-2, i} end
    -- An off-mode request has no upper bound. ON cannot count it as zero,
    -- even for a legacy run or organization budget without a strict anchor.
    local fields = redis.call('HKEYS', KEYS[i])
    local retained = {}
    for j = 1, #fields do
        if string.sub(fields[j], 1, 10) == 'unbounded:' then return {-2, i} end
        if string.sub(fields[j], 1, 9) == 'retained:' then
            local id = string.sub(fields[j], 10)
            local value = redis.call('HGET', KEYS[i], id)
            if not value or id == request_id then return {-1, i} end
            retained[id] = true
        end
    end
    retained_targets[i] = retained
    if ARGV[i * 6] == '1' then
        -- The anchor marks an initialized meter; its deadline is bookkeeping,
        -- not an expiry (the hash TTL governs the meter's lifetime).
        local anchor = redis.call('HGET', KEYS[i], '__initialized__')
        if not anchor then return {-1, i} end
        local sep = string.find(anchor, ':')
        if not sep or tonumber(string.sub(anchor, 1, sep - 1)) ~= 0 then
            return {-1, i}
        end
        local entries = redis.call('HGETALL', KEYS[i])
        local unsettled = {}
        for j = 1, #entries, 2 do
            local field = entries[j]
            if string.sub(field, 1, 8) == 'pending:' then
                pending_requests[string.sub(field, 9)] = true
                unsettled[string.sub(field, 9)] = true
            end
        end
        for j = 1, #entries, 2 do
            local field, value = entries[j], entries[j + 1]
            local split = string.find(value, ':')
            if field ~= '__initialized__' and tonumber(string.sub(value, split + 1)) <= now then
                -- An expired pending marker, or an expired reservation whose
                -- outcome is still pending, is unknown usage: unavailable, never
                -- a retryable cap wait. A retained failed bound is a known upper
                -- bound (counted while live), so like a settled reservation it is
                -- pruned on the next write and ignored once it ages out.
                if string.sub(field, 1, 8) == 'pending:' or unsettled[field] then return {-1, i} end
            end
        end
    end
end

-- Pass 1: verify every budget has room. No writes here, so a denial cannot
-- leave a partial reservation behind. A definitive cap takes precedence over
-- temporary contention at another level of the same hierarchy.
local waiting_index = 0
local waiting_total = 0
for i = 1, #KEYS do
    local headroom = tonumber(ARGV[(i * 6) - 2])
    local amount_i = tonumber(ARGV[3 + (i * 6)])
    local in_flight, awaiting_usage, settled = 0, 0, 0
    local entries = redis.call('HGETALL', KEYS[i])
    for j = 1, #entries, 2 do
        local field, value = entries[j], entries[j + 1]
        local sep = string.find(value, ':')
        local entry_amount = tonumber(string.sub(value, 1, sep - 1))
        local entry_deadline = tonumber(string.sub(value, sep + 1))
        -- A retry replaces its own reservation; expired non-policy entries
        -- retain their existing TTL semantics.
        if field ~= request_id and (entry_deadline > now or retained_targets[i][field]) then
            in_flight = in_flight + entry_amount
            if pending_requests[field] then
                awaiting_usage = awaiting_usage + entry_amount
            else
                settled = settled + entry_amount
            end
        end
    end
    if in_flight + amount_i > headroom then
        if awaiting_usage > 0 and settled + amount_i <= headroom then
            if waiting_index == 0 then waiting_index = i; waiting_total = in_flight end
        else
            return {0, i, 0, tostring(in_flight)}
        end
    end
end
if waiting_index > 0 then return {0, waiting_index, 1, tostring(waiting_total)} end

-- Pass 2: every budget had room, so commit to all of them.
for i = 1, #KEYS do
    local ttl = tonumber(ARGV[(i * 6) - 1])
    local strict = ARGV[i * 6] == '1'
    local retain = ARGV[1 + (i * 6)] == '1'
    local amount_i = tonumber(ARGV[3 + (i * 6)])
    local deadline = now + ttl
    -- Prune expired fields opportunistically so the hash cannot grow without
    -- bound under sustained traffic on a long period (e.g. monthly).
    local entries = redis.call('HGETALL', KEYS[i])
    for j = 1, #entries, 2 do
        local value = entries[j + 1]
        local sep = string.find(value, ':')
        -- The anchor is never pruned: its deadline is bookkeeping, not an expiry.
        local field = entries[j]
        local retained_id = string.match(field, '^retained:(.+)$') or string.match(field, '^pending:(.+)$')
        if field ~= '__initialized__' and not retained_targets[i][field]
            and not (retained_id and retained_targets[i][retained_id])
            and tonumber(string.sub(value, sep + 1)) <= now then
            redis.call('HDEL', KEYS[i], entries[j])
        end
    end
    redis.call('HSET', KEYS[i], request_id, amount_i .. ':' .. deadline)
    if strict then
        redis.call('HSET', KEYS[i], 'pending:' .. request_id, '0:' .. (now + tonumber(ARGV[2 + (i * 6)])))
    end
    if retain then
        redis.call('HSET', KEYS[i], 'retained:' .. request_id, '0:' .. deadline)
    end
    if retain or next(retained_targets[i]) then
        redis.call('PERSIST', KEYS[i])
    elseif redis.call('TTL', KEYS[i]) < ttl then
        redis.call('EXPIRE', KEYS[i], ttl)
    end
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
    if existing and redis.call('HEXISTS', KEYS[i], 'retained:' .. request_id) == 0 then
        local sep = string.find(existing, ':')
        if tonumber(string.sub(existing, sep + 1)) > now then
            local ttl = tonumber(ARGV[3 + i])
            redis.call('HSET', KEYS[i], request_id, amount .. ':' .. (now + ttl))
            redis.call('HDEL', KEYS[i], 'pending:' .. request_id)
            redis.call('HDEL', KEYS[i], 'bounded:' .. request_id)
            redis.call('HDEL', KEYS[i], 'unbounded:' .. request_id)
            local retained = false
            for _, field in ipairs(redis.call('HKEYS', KEYS[i])) do
                if string.sub(field, 1, 9) == 'retained:' then retained = true end
            end
            if not retained and redis.call('TTL', KEYS[i]) < ttl then redis.call('EXPIRE', KEYS[i], ttl) end
            adjusted = adjusted + 1
        elseif redis.call('HEXISTS', KEYS[i], '__initialized__') == 0 then
            -- Strict accounting is not a disposable settlement-lag cache.
            -- A late finalizer must not delete the only retained charge.
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
    # All-in native obligations cannot restore headroom by elapsed time or an
    # ordinary partial-usage finalizer. A trusted complete settlement must own
    # their eventual reduction. The immutable marker is written by reserve Lua.
    retain_until_settlement: bool = False
    pending_marker_seconds: int = PENDING_MARKER_SECONDS
    # Issue #9536: the amount THIS target reserves when it differs from the
    # request estimate every other target reserves. ``None`` means "the shared
    # amount". A Project meter holds a provider quote's full-context ceiling so
    # an unknown outcome can be charged at that ceiling, while the run, chain,
    # flow and hierarchy caps admit against the request-sized estimate; before
    # this field the ceiling was pushed into the shared estimate and a 25 USD run
    # cap refused a reviewer's first 0.5 USD request because its ceiling was 30.
    reserve_usd: Decimal | None = None

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
        awaiting_usage: Denied solely by active policy request holds, so a retry
            may fit after trusted settlement. Never permission to discount spend.
    """

    admitted: bool
    usage_unavailable: bool = False
    exhausted: ReservationTarget | None = None
    awaiting_usage: bool = False
    current_total_usd: Decimal | None = None


@dataclass(frozen=True)
class ReservationSnapshot:
    total_usd: Decimal
    has_pending: bool
    request_amounts: dict[str, Decimal] | None = None


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

        # Model callers can outlive a gateway rollout. Upgrade old deadlines at
        # their actual spending boundary too, not only at node admission.
        for target in targets:
            if self._is_flow_meter(target) and not await self.extend_flow_retention(target):
                return None

        now = self._clock()

        # Interleaved (headroom, ttl, initialization, retained hold, pending
        # marker, amount) per target. The amount is the shared estimate unless the
        # target carries its own ``reserve_usd`` (#9536).
        per_target_args: list[str | int] = []
        for target in targets:
            per_target_args.append(str(target.headroom_usd))
            per_target_args.append(self._ttl_for(target))
            per_target_args.append("1" if target.require_initialization else "0")
            per_target_args.append("1" if target.retain_until_settlement else "0")
            per_target_args.append(target.pending_marker_seconds)
            per_target_args.append(str(target.reserve_usd if target.reserve_usd is not None else amount_usd))

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

        if int(result[0]) == -2:
            return ReservationOutcome(admitted=False, usage_unavailable=True, exhausted=targets[int(result[1]) - 1])
        if int(result[0]) < 0:
            return None
        admitted = bool(int(result[0]))
        if admitted:
            return ReservationOutcome(admitted=True)

        index = int(result[1])
        return ReservationOutcome(
            admitted=False,
            exhausted=targets[index - 1],
            awaiting_usage=len(result) > 2 and bool(int(result[2])),
            current_total_usd=Decimal(result[3].decode() if isinstance(result[3], bytes) else result[3]) if len(result) > 3 else None,
        )

    async def live_request_amounts(self, target: ReservationTarget) -> dict[str, Decimal] | None:
        """Read-only usage display; missing/expired counters have no displayed total."""
        entries = await (await self._get_client()).hgetall(target.key())
        result = {}
        now = self._clock()
        for request_id, value in entries.items():
            if request_id == "__initialized__" or request_id.startswith(("pending:", "bounded:", "unbounded:", "retained:")):
                continue
            amount, deadline = map(Decimal, value.split(":"))
            if not amount.is_finite() or amount < 0 or not deadline.is_finite():
                return None
            if deadline > now or f"retained:{request_id}" in entries:
                result[request_id] = amount
        return result or None

    async def retain_maximum(self, request_id: str, amount: Decimal, targets: list[ReservationTarget]) -> None:
        """Raise unknown request exposure to its ceiling without renewing any TTL."""
        if not targets or not amount.is_finite() or amount < 0:
            return
        script = """
        for i = 1, #KEYS do
            local value = redis.call('HGET', KEYS[i], ARGV[1])
            if value then
                local cost, deadline = string.match(value, '^([^:]+):([^:]+)$')
                if tonumber(deadline) > tonumber(ARGV[3]) and tonumber(cost) < tonumber(ARGV[2]) then
                    redis.call('HSET', KEYS[i], ARGV[1], ARGV[2] .. ':' .. deadline)
                end
            end
        end
        return 1
        """
        await (await self._get_client()).eval(script, len(targets), *[target.key() for target in targets], request_id, str(amount), self._clock())

    async def observe(self, request_id: str, targets: list[ReservationTarget]) -> bool:
        """Record an unbounded call while enforcement is off, without cap checks.

        The zero field is a placeholder, never a usage receipt: the expired
        pending marker makes strict reads UNKNOWN until actual usage settles.
        Preserve all earlier holds and unknown entries, including expired ones.
        Missing initialization is never manufactured by this path.
        """
        if not targets:
            return True
        script = """
        local now = tonumber(ARGV[2])
        for i = 1, #KEYS do
            local ttl = tonumber(ARGV[1 + 2 * i])
            if redis.call('HEXISTS', KEYS[i], 'retained:' .. ARGV[1]) == 1
                or redis.call('EXISTS', KEYS[i] .. ':settled:' .. ARGV[1]) == 1 then return 0 end
        end
        for i = 1, #KEYS do
            local ttl = tonumber(ARGV[1 + 2 * i])
            redis.call('HSET', KEYS[i], ARGV[1], '0:' .. (now + ttl))
            redis.call('HSET', KEYS[i], 'unbounded:' .. ARGV[1], '0:' .. now)
            if ARGV[2 + 2 * i] == '1' then
                redis.call('HSET', KEYS[i], 'retained:' .. ARGV[1], '0:' .. now)
                redis.call('PERSIST', KEYS[i])
            end
            local retained = false
            for _, field in ipairs(redis.call('HKEYS', KEYS[i])) do
                if string.sub(field, 1, 9) == 'retained:' then retained = true end
            end
            if not retained and redis.call('TTL', KEYS[i]) < ttl then redis.call('EXPIRE', KEYS[i], ttl) end
        end
        return 1
        """
        try:
            client = await self._get_client()
            return bool(
                await client.eval(
                    script,
                    len(targets),
                    *[t.key() for t in targets],
                    request_id,
                    self._clock(),
                    *[value for t in targets for value in (self._ttl_for(t), int(t.retain_until_settlement))],
                )
            )
        except Exception:
            logger.warning("Budget observation unavailable while enforcement is off")
            return False

    async def snapshot(
        self, target: ReservationTarget, *, existing_scope: bool = False, include_requests: bool = False
    ) -> ReservationSnapshot | None:
        """Read an accumulator without changing it; absence is never zero spend.

        Legacy run/chain scopes predate initialization anchors. An explicit
        existing-scope read may inspect a nonempty lifetime hash, but cannot
        treat a missing hash as zero or relax a policy/flow accumulator.
        """
        if existing_scope and (target.entity_type not in {"run", "chain"} or target.period_type != "run" or target.period_start != "lifetime"):
            return None
        try:
            entries = await (await self._get_client()).hgetall(target.key())
            now = self._clock()
            if existing_scope:
                markers = ("pending:", "bounded:", "unbounded:", "retained:")
                charges = {field for field in entries if field != "__initialized__" and not field.startswith(markers)}
                if not charges or any(field.startswith("unbounded:") for field in entries):
                    return None
                for field, value in entries.items():
                    amount, deadline = map(Decimal, value.split(":"))
                    if not amount.is_finite() or amount < 0 or not deadline.is_finite() or deadline < 0:
                        return None
                    if field.startswith(markers) and (amount != 0 or field.split(":", 1)[1] not in charges):
                        return None
            anchor = entries.get("__initialized__")
            if anchor is None and not (existing_scope and entries):
                return None
            if anchor is not None:
                amount, _deadline = anchor.split(":")
                if Decimal(amount) != 0:
                    # The anchor's deadline is bookkeeping, not an expiry: the hash TTL
                    # governs the meter's lifetime (mirrors the Lua gate).
                    return None
            unsettled = {field.split(":", 1)[1] for field in entries if field.startswith(("pending:", "retained:"))}
            total = Decimal(0)
            pending = False
            requests = {} if include_requests else None
            for field, value in entries.items():
                if field == "__initialized__":
                    continue
                amount, deadline = value.split(":")
                cost = Decimal(amount)
                if not cost.is_finite() or cost < 0:
                    return None
                if float(deadline) <= now:
                    # Unknown usage stays unavailable; a settled reservation or a
                    # retained failed bound (a known upper bound) that aged past its
                    # TTL window is ignored (pruned on the next write).
                    if field.startswith(("pending:", "unbounded:")) or field in unsettled:
                        return None
                    continue
                total += cost
                pending |= field.startswith("pending:")
                if requests is not None and not field.startswith(("pending:", "bounded:", "unbounded:", "retained:")):
                    requests[field] = cost
            return ReservationSnapshot(total, pending, requests)
        except Exception:
            logger.warning("Policy budget snapshot unavailable")
            return None

    @staticmethod
    def _is_flow_meter(target: ReservationTarget) -> bool:
        return (
            target.require_initialization
            and target.entity_type == "flow"
            and target.entity_id.endswith(":models")
            and target.period_type == "run"
            and target.period_start == "lifetime"
        )

    async def extend_flow_retention(self, target: ReservationTarget, *, expected_entries: dict[str, str] | None = None) -> bool:
        """Keep an existing flow's complete denominator through its policy window.

        Older flows can still carry the former two-day field and key deadlines.
        Extend amounts in place, atomically with concurrent finalizers. Receipt
        deadlines and unknown-usage markers are deliberately unchanged: keeping
        accounting must never turn an overdue request into a fresh pending one.
        A missing or malformed meter is refused, never initialized from usage.
        """
        if not self._is_flow_meter(target):
            return False
        script = """
        local key, now, ttl = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2])
        if ARGV[3] ~= '' then
            local expected = cjson.decode(ARGV[3])
            local count = 0
            for field, value in pairs(expected) do
                count = count + 1
                if redis.call('HGET', key, field) ~= value then return 0 end
            end
            if redis.call('HLEN', key) ~= count then return 0 end
        end
        local anchor = redis.call('HGET', key, '__initialized__')
        if not anchor then return 0 end
        local function parse(value)
            local a, d = string.match(value, '^([^:]+):([^:]+)$')
            a, d = tonumber(a), tonumber(d)
            if not a or not d or a ~= a or d ~= d or a < 0 or d < 0
                or a == math.huge or d == math.huge then return nil end
            return a, d
        end
        local a = parse(anchor)
        if a ~= 0 then return 0 end
        local entries = redis.call('HGETALL', key)
        local latest = now + ttl
        local renew = false
        local renewal_floor = latest - math.min(86400, ttl / 10)
        local retained = false
        -- Validate the whole hash before the first write.
        for i = 1, #entries, 2 do
            local field, value = entries[i], entries[i + 1]
            local amount, deadline = parse(value)
            if not amount then return 0 end
            local id = string.match(field, '^pending:(.+)$')
                or string.match(field, '^bounded:(.+)$')
                or string.match(field, '^unbounded:(.+)$')
                or string.match(field, '^retained:(.+)$')
            if id and (amount ~= 0 or redis.call('HEXISTS', key, id) == 0) then return 0 end
            if string.sub(field, 1, 9) == 'retained:' then retained = true end
            if not id or string.sub(field, 1, 8) == 'bounded:' then
                latest = math.max(latest, deadline)
                if deadline < renewal_floor then renew = true end
            end
        end
        for i = 1, #entries, 2 do
            local field, value = entries[i], entries[i + 1]
            -- Preserve pending/unknown/retained marker times byte-for-byte.
            if renew and string.sub(field, 1, 8) ~= 'pending:'
                and string.sub(field, 1, 10) ~= 'unbounded:'
                and string.sub(field, 1, 9) ~= 'retained:' then
                local amount, deadline = string.match(value, '^([^:]+):([^:]+)$')
                if tonumber(deadline) < latest then
                    redis.call('HSET', key, field, amount .. ':' .. latest)
                end
            end
        end
        local current_ttl = redis.call('TTL', key)
        if retained then
            redis.call('PERSIST', key)
        elseif current_ttl >= 0 and current_ttl < math.ceil(latest - now) then
            redis.call('EXPIRE', key, math.ceil(latest - now))
        end
        return 1
        """
        try:
            return bool(
                await (await self._get_client()).eval(
                    script,
                    1,
                    target.key(),
                    self._clock(),
                    self._ttl_for(target),
                    json.dumps(expected_entries) if expected_entries is not None else "",
                )
            )
        except Exception:
            logger.warning("Flow accounting retention unavailable; existing amounts retained")
            return False

    async def retain_failed_bound(self, request_id: str, target: ReservationTarget) -> bool:
        """Keep the entire admitted bound after HTTP 5xx or a stream interruption.

        This is NOT an actual-cost receipt or a release. The request's original
        amount and deadline remain unchanged. A late trusted receipt may still
        replace it. Missing/expired/unbounded accounting remains unavailable.
        The marker distinguishes this conservative charge from settled usage.
        """
        if not target.require_initialization or request_id == "__initialized__":
            return False
        script = """
        local key, id, now, grace = KEYS[1], ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3])
        local anchor = redis.call('HGET', key, '__initialized__')
        local entry = redis.call('HGET', key, id)
        if not anchor or not entry or redis.call('HEXISTS', key, 'unbounded:' .. id) == 1 then return 0 end
        local a, d = string.match(anchor, '^([^:]+):([^:]+)$')
        if tonumber(a) ~= 0 or not tonumber(d) then return 0 end
        local amount, deadline = string.match(entry, '^([^:]+):([^:]+)$')
        if not tonumber(amount) or tonumber(amount) < 0 or not tonumber(deadline) or tonumber(deadline) <= now then return 0 end
        -- A trusted receipt that won the race must never become estimated again.
        if redis.call('HEXISTS', key, 'pending:' .. id) == 0 then return 1 end
        redis.call('HSET', key, 'bounded:' .. id, '0:' .. deadline)
        redis.call('HDEL', key, 'pending:' .. id)
        return 1
        """
        try:
            return bool(await (await self._get_client()).eval(script, 1, target.key(), request_id, self._clock()))
        except Exception:
            logger.warning("Failed provider request bound could not be retained")
            return False

    async def unresolved_requests(self, target: ReservationTarget) -> list[str]:
        """IDs only; the caller must obtain actual amounts from trusted receipts."""
        entries = await (await self._get_client()).hgetall(target.key())
        return sorted({field.split(":", 1)[1] for field in entries if field.startswith(("pending:", "bounded:"))})

    async def reconcile_receipt(self, request_id: str, actual_usd: Decimal, target: ReservationTarget, *, lifetime_receipt: bool = False) -> bool:
        """Retry a durable receipt without resetting deadlines or reviving a lost meter.

        A live finalizer may have settled first. Only unresolved, unexpired fields
        can change; expired strict accounting remains unknown instead of disappearing.
        """
        legacy = lifetime_receipt and target.entity_type in {"run", "chain"} and target.period_type == "run" and target.period_start == "lifetime"
        if not actual_usd.is_finite() or actual_usd < 0 or not (target.require_initialization or legacy):
            return False
        script = """
        local key, id, now = KEYS[1], ARGV[1], tonumber(ARGV[3])
        local anchor = redis.call('HGET', key, '__initialized__')
        local value = redis.call('HGET', key, id)
        if redis.call('HEXISTS', key, 'retained:' .. id) == 1 then return 0 end
        if not value or redis.call('HEXISTS', key, 'unbounded:' .. id) == 1 then return 0 end
        if ARGV[4] ~= '1' and not anchor then return 0 end
        local a, d = string.match(anchor or '0:0', '^([^:]+):([^:]+)$')
        local amount, deadline = string.match(value, '^([^:]+):([^:]+)$')
        if tonumber(a) ~= 0 or not tonumber(d)
          or not tonumber(amount) or tonumber(amount) < 0 or not tonumber(deadline) or tonumber(deadline) <= now then return 0 end
        if ARGV[4] ~= '1' and redis.call('HEXISTS', key, 'pending:' .. id) == 0
            and redis.call('HEXISTS', key, 'bounded:' .. id) == 0 then return 0 end
        redis.call('HSET', key, id, ARGV[2] .. ':' .. deadline)
        redis.call('HDEL', key, 'pending:' .. id, 'bounded:' .. id)
        return 1
        """
        return bool(
            await (await self._get_client()).eval(script, 1, target.key(), request_id, str(actual_usd), self._clock(), "1" if legacy else "0")
        )

    async def expired_pending_requests(self, target: ReservationTarget) -> list[str]:
        """Request ids whose strict `pending:` marker has been expired for a full grace period while the reservation is live.

        These are the requests that never settled: no usage, no receipt, and a
        marker the writer would have deleted on settlement. Each one makes
        :meth:`snapshot` unavailable for the whole meter until it is dealt with.
        A marker that expired recently (including one mark_unknown() just wrote)
        is still waiting for its receipt and is not reported.
        """
        entries = await (await self._get_client()).hgetall(target.key())
        now = self._clock()
        cutoff = now - ABANDONED_PENDING_GRACE_SECONDS
        expired: list[str] = []
        for field, value in entries.items():
            if not field.startswith("pending:"):
                continue
            request_id = field.split(":", 1)[1]
            marker_deadline = value.split(":")[1]
            reservation = entries.get(request_id)
            if float(marker_deadline) <= cutoff and reservation is not None and float(reservation.split(":")[1]) > now:
                expired.append(request_id)
        return sorted(expired)

    async def settle_expired_pending(self, request_id: str, target: ReservationTarget) -> bool:
        """Charge a request whose usage never arrived at its full reserved bound (#7451).

        A strict marker that expired with no receipt used to leave the meter
        unreadable forever: snapshot() refuses any expired field, reconcile_receipt()
        refuses expired fields, and nothing else touched the marker. The engine then
        refused every story of the flow with budget_unavailable / spend_unknown and
        no operator route existed.

        Settling at the bound is the conservative direction the accounting already
        prefers: the reservation field keeps its admitted amount (never lowered), the
        marker is removed so the meter reads again, and the caller records a
        BudgetAccountingGap for audit. Only a marker expired for longer than
        ABANDONED_PENDING_GRACE_SECONDS on a LIVE reservation qualifies; a live or
        recently expired marker still awaits its receipt and is untouched.
        """
        if not target.require_initialization or request_id == "__initialized__":
            return False
        script = """
        local key, id, now, grace = KEYS[1], ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3])
        local anchor = redis.call('HGET', key, '__initialized__')
        local entry = redis.call('HGET', key, id)
        local marker = redis.call('HGET', key, 'pending:' .. id)
        if redis.call('HEXISTS', key, 'retained:' .. id) == 1 then return 0 end
        if not anchor or not entry or not marker or redis.call('HEXISTS', key, 'unbounded:' .. id) == 1 then return 0 end
        local a, d = string.match(anchor, '^([^:]+):([^:]+)$')
        if tonumber(a) ~= 0 or not tonumber(d) then return 0 end
        local amount, deadline = string.match(entry, '^([^:]+):([^:]+)$')
        if not tonumber(amount) or tonumber(amount) < 0 or not tonumber(deadline) or tonumber(deadline) <= now then return 0 end
        local _, marker_deadline = string.match(marker, '^([^:]+):([^:]+)$')
        if not tonumber(marker_deadline) or tonumber(marker_deadline) > now - grace then return 0 end
        redis.call('HDEL', key, 'pending:' .. id, 'bounded:' .. id)
        return 1
        """
        return bool(await (await self._get_client()).eval(script, 1, target.key(), request_id, self._clock(), ABANDONED_PENDING_GRACE_SECONDS))

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

    async def initialize_project_meter(self, target: ReservationTarget, balance: Decimal) -> None:
        if target.entity_type != "project" or not balance.is_finite() or balance < 0:
            raise ValueError("Invalid Project opening balance")
        await (await self._get_client()).eval(
            """
            if redis.call('HEXISTS', KEYS[1], '__initialized__') == 0 then
                if redis.call('HLEN', KEYS[1]) > 0 then return redis.error_reply('Project meter lacks an anchor') end
                redis.call('HSET', KEYS[1], '__initialized__', '0:1000000000000000',
                           '__settled__', ARGV[1] .. ':1000000000000000')
            end
            redis.call('PERSIST', KEYS[1])
            return 1
        """,
            1,
            target.key(),
            str(balance),
        )

    async def release_project_receipt(self, request_id: str, actual: Decimal, target: ReservationTarget) -> None:
        """Called only after durable Project debit is verified by its owner."""
        if (
            target.entity_type != "project"
            or not target.require_initialization
            or not target.retain_until_settlement
            or not actual.is_finite()
            or actual < 0
        ):
            raise ValueError("A Project settlement target is required")
        await (await self._get_client()).eval(
            """
            if redis.call('HEXISTS', KEYS[1], '__initialized__') == 0 then return 0 end
            local proof = KEYS[1] .. ':settled:' .. ARGV[1]
            local prior = redis.call('GET', proof)
            if not prior and redis.call('HEXISTS', KEYS[1], 'retained:' .. ARGV[1]) == 0 then return 0 end
            local delta = tonumber(ARGV[2]) - tonumber(prior or '0')
            if delta < 0 then return redis.error_reply('Project settlement cannot decrease') end
            local value = redis.call('HGET', KEYS[1], '__settled__') or '0:0'
            local amount = tonumber(string.match(value, '^([^:]+):'))
            redis.call('HSET', KEYS[1], '__settled__', tostring(amount + delta) .. ':1000000000000000')
            redis.call('SET', proof, ARGV[2])
            redis.call('HDEL', KEYS[1], ARGV[1], 'pending:' .. ARGV[1],
                       'retained:' .. ARGV[1], 'bounded:' .. ARGV[1], 'unbounded:' .. ARGV[1])
            redis.call('PERSIST', KEYS[1])
            return 1
        """,
            1,
            target.key(),
            request_id,
            str(actual),
        )

    async def close(self) -> None:
        """Close the Redis client."""
        if self._client:
            await self._client.aclose()
            self._client = None
            self._reserve_script = None
            self._reconcile_script = None
