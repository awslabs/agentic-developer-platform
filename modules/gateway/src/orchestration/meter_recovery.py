"""Operator primitive for retaining a lost flow's entire historical exposure.

This restores an exhausted meter, never available headroom. The operator must
fence historical workers and establish the enforced historical ceiling first.
An immutable audit record must precede the call. Recorded receipts remain actual
spend; everything else up to that ceiling remains a separately identified hold.
Only a subsequent human budget decision can authorize additional spending.
"""

import math
import re
from decimal import Decimal
from uuid import UUID


def exhausted_inventory(*, receipts: dict[str, Decimal], ceiling: Decimal, recovery_id: str, deadline: int) -> tuple[dict[str, str], dict]:
    if not ceiling.is_finite() or ceiling <= 0 or not isinstance(deadline, int) or deadline <= 0:
        raise ValueError("Invalid historical ceiling or retention")
    UUID(recovery_id)
    total = Decimal(0)
    entries = {"__initialized__": f"0:{deadline}"}
    for request_id, cost in receipts.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", request_id) or not isinstance(cost, Decimal) or not cost.is_finite() or cost < 0:
            raise ValueError("Invalid attributed receipt")
        total += cost
        entries[request_id] = f"{cost}:{deadline}"
    if total > ceiling:
        raise ValueError("Recorded spend exceeds the established historical ceiling")
    hold = ceiling - total
    hold_id = f"recovery-{recovery_id}"
    if hold_id in entries:
        raise ValueError("Recovery identity collides with a receipt")
    entries[hold_id] = f"{hold}:{deadline}"
    entries[f"bounded:{hold_id}"] = f"0:{deadline}"
    return entries, {
        "recorded_spend_usd": str(total),
        "unreconciled_hold_usd": str(hold),
        "total_exposure_usd": str(ceiling),
        "receipt_count": len(receipts),
        "recovery_id": recovery_id,
    }


async def restore_exhausted_flow_meter(store, target, *, receipts: dict[str, Decimal], recovery_id: str) -> dict:
    """Atomically install only a missing flow meter at its FULL current ceiling.

    There is no caller-supplied smaller balance and no overwrite path. A repeated
    apply, concurrent initialization or surviving partial hash refuses the write.
    This never changes the usage ledger, policy, budget limits or flow state.
    """
    if not store._is_flow_meter(target):
        raise ValueError("An initialized flow model target is required")
    ttl = store._ttl_for(target)
    now = store._clock()
    if not math.isfinite(now) or ttl <= 0:
        raise ValueError("Invalid retention")
    entries, report = exhausted_inventory(receipts=receipts, ceiling=target.headroom_usd, recovery_id=recovery_id, deadline=math.ceil(now) + ttl)
    script = """
    if redis.call('EXISTS', KEYS[1]) ~= 0 then return 0 end
    for i = 2, #ARGV, 2 do
        redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1])
    end
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
    return 1
    """
    pairs = [item for field, value in entries.items() for item in (field, value)]
    if not await (await store._get_client()).eval(script, 1, target.key(), ttl, *pairs):
        raise ValueError("Meter exists or changed; no recovery applied")
    return report


async def release_recovery_hold(store, target, *, recovery_id: str, expected_usd: Decimal, decision_id: str) -> bool:
    """Apply an already-audited owner's release of unverified historical exposure.

    This is an administrative adjustment, not evidence of zero historic usage.
    The operator must verify the original recovery and persist the owner's
    acknowledgement first. The separate proof makes a post-write crash replayable;
    this function cannot release ordinary requests or change recorded spend.
    """
    UUID(recovery_id)
    UUID(decision_id)
    if not store._is_flow_meter(target) or not expected_usd.is_finite() or expected_usd <= 0:
        raise ValueError("A positive, reviewed recovery hold is required")
    key = target.key()
    proof = key + ":recovery-resolution:" + recovery_id
    script = """
    local prior = redis.call('GET', KEYS[2])
    local anchor = redis.call('HGET', KEYS[1], '__initialized__')
    local value = redis.call('HGET', KEYS[1], ARGV[1])
    if prior then
        local prior_amount = value and string.match(value, '^([^:]+):')
        return prior == ARGV[3] and anchor and tonumber(prior_amount) == 0 and 1 or 0
    end
    local marker = redis.call('HGET', KEYS[1], 'bounded:' .. ARGV[1])
    if not anchor or not value or not marker then return 0 end
    local anchor_amount = string.match(anchor, '^([^:]+):')
    local amount, deadline = string.match(value, '^([^:]+):([^:]+)$')
    local marker_amount, marker_deadline = string.match(marker, '^([^:]+):([^:]+)$')
    if tonumber(anchor_amount) ~= 0 or tonumber(amount) ~= tonumber(ARGV[2])
        or not tonumber(deadline) or tonumber(deadline) <= tonumber(ARGV[4])
        or tonumber(marker_amount) ~= 0 or not tonumber(marker_deadline)
        or tonumber(marker_deadline) <= tonumber(ARGV[4]) then return 0 end
    for _, prefix in ipairs({'pending:', 'unbounded:', 'retained:'}) do
        if redis.call('HEXISTS', KEYS[1], prefix .. ARGV[1]) == 1 then return 0 end
    end
    redis.call('HSET', KEYS[1], ARGV[1], '0:' .. deadline)
    redis.call('HDEL', KEYS[1], 'bounded:' .. ARGV[1])
    redis.call('SET', KEYS[2], ARGV[3], 'EX', math.max(tonumber(ARGV[5]), redis.call('TTL', KEYS[1])))
    return 1
    """
    return bool(
        await (await store._get_client()).eval(
            script, 2, key, proof, f"recovery-{recovery_id}", str(expected_usd), decision_id, store._clock(), store._ttl_for(target)
        )
    )
