"""One allowance for a whole accepted delivery, shared by every descendant (#5128).

`execution_policy.PolicyLimits.max_spend_usd` is the number a plan owner authorized
for a delivery. This module is what makes that number *bind* when several agents are
admitted at the same instant — the case the settled ledger alone cannot bound.

## Why the settled ledger is not enough on its own

`policy_admission._observed_spend` reads `usage_logs`, which only materializes
minutes after a model call (the `budget-usage-tracker` Lambda writes the row). So a
burst of concurrent admissions all read the same stale total, each one individually
fits under the cap, and collectively they blow straight through it. That is #4287's
finding restated for the engine's fan-out, and it is why a `SUM(cost_usd) <= cap`
check is not an implementation of this requirement.

The reservation plane closes it: before an action is admitted, the most it could
cost is added to a live in-flight counter under a multi-key Lua `EVAL`, and the
comparison becomes `settled + in_flight + this_action <= cap`. Concurrent admissions
now contend on a figure that reflects each other. **Reserved plus settled**, which is
the phrase the issue uses, is literally these two terms.

## Why this reuses the run/chain machinery instead of adding a second one

`budget/reservations.py` already provides exactly this primitive — atomic
all-or-nothing reservation across N keys, per-target TTL, idempotent reconcile — and
the issue is explicit that there is to be no second billing service. So a flow scope
is a new `EntityType` value and a new key, not new enforcement code. Concretely this
module contributes three things and nothing else: which key, how much, and when to
release.

## The three properties that make this a *flow* allowance

1. **Stable.** The key is `flow_budget_binding(flow_id)`, a pure function of a
   server-issued id that never changes. A new run id cannot address a different
   ledger, and a restart re-derives the same one. Contrast the two existing lifetime
   scopes, both of which reset here: `RUN` is per-run by definition, and `CHAIN`
   keys on `correlation_id`, which engine dispatch sets to a **fresh per-attempt run
   id** — so on the engine's own dispatch path a chain scope would mint a new
   allowance on every retry.
2. **Shared.** Every developer, reviewer, repair and evaluation admission under the
   flow reserves against that one key, so a fan-out contends with itself.
3. **Not resettable by the work it bounds.** Nothing a worker writes is read here.
   The amount comes from the accepted policy, the settled total from `usage_logs`,
   and the key from the flow id.

## Unavailable reservations block policy-governed admissions

A Redis fault returns `None` from `ReservationStore.reserve`. An accepted bounded
policy cannot permit new work without its concurrent holds, so this module
refuses that admission. Disabling reservations or omitting the backend has the
same result. Policy-less plans retain their existing admission path. Unknown
settled spend is independently refused by `policy_admission`.

## Why `src.budget` is imported inside functions, not at module scope

This is not a style preference and must not be "tidied up". `src/budget/__init__.py`
eagerly imports `routes.py`, which imports `src.auth`, whose `middleware.py` builds
an `AuthService()` at module scope and raises without `BG_TOKEN_SECRET_KEY`. This
module is reached from `dispatch_pass` → `policy_admission`, and therefore from
`tick_handler` — the orchestration-tick Lambda, which has no web-session secret and
should never be given one to satisfy an import side effect.

A module-level `from src.budget.reservations import ...` here crashes that Lambda on
**every** invocation, which stops all engine dispatch while looking like an idle
tick. That is #4527 exactly, and `tests/orchestration/test_tick_handler_import.py`
pins it. `policy_admission` defers its `src.admin` imports for the same reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from src.shared.schemas.budget import EntityType, PeriodType

from .execution_policy import ExecutionPolicy, flow_budget_binding

if TYPE_CHECKING:
    from src.budget.reservations import ReservationStore, ReservationTarget

logger = logging.getLogger(__name__)

__all__ = [
    "FlowReservation",
    "admission_cost_usd",
    "admission_request_id",
    "flow_reservation_target",
    "get_flow_reservations",
    "release_flow_admission",
    "reserve_flow_admission",
]

# `period_start` for a lifetime-scoped ledger. Matches the literal
# `enforcement_service._scope_targets` uses for the run and chain scopes, because it
# lands in the Redis key and two spellings of "lifetime" would be two ledgers.
_LIFETIME = "lifetime"


def admission_request_id(node_id: str) -> str:
    """The reservation field one node's in-flight action occupies.

    Keyed on the **node**, not on the attempt. Both are stable identifiers, so this
    is a choice about what a hold means rather than about idempotency, and the node
    is the correct subject for two reasons:

    * A node has at most one action in flight at a time — the engine dispatches it,
      and it is `running` until observed. So one field per node is exactly the set of
      concurrent holds, and `max_concurrent_actions` (checked separately by the rule)
      bounds how many exist.
    * A retry supersedes its predecessor rather than adding to it. Keyed on the
      attempt, attempt 2 would open a **second** field while attempt 1's hold sat
      there until the 24h TTL — so a repair loop would consume the flow's allowance
      in holds for work that is no longer running. Keyed on the node, the retry
      replaces its own hold, which is what the reserve script's same-`request_id`
      rule already does.

    Prefixed so the value cannot collide with a proxy request id in the same hash.
    The hash is per-flow, so a collision would need a `usage_logs` request id equal
    to a node id, but the namespace is free and an unprefixed id would be relying on
    that coincidence not happening.
    """
    return f"orchnode:{node_id}"


def admission_cost_usd(policy: ExecutionPolicy) -> Decimal:
    """The most one admitted action can add to the flow's total.

    A reservation has to be taken *before* the work runs, and the engine has no
    per-node cost estimate — an agent run's cost is not knowable in advance. So this
    reserves the largest amount the platform will actually let one run spend: the
    per-run cap from `budget/config.py`, which `enforcement_service` enforces
    independently on every model call inside that run.

    Reserving the worst case is what makes the guarantee unconditional rather than
    probabilistic. With an optimistic estimate, N concurrent admissions can each
    reserve a fraction of what they go on to spend, and the flow cap is exceeded by
    work that was admitted against a number nobody was holding to. The cost of the
    conservative choice is that a flow admits fewer actions concurrently than it
    might strictly afford; the cost of the optimistic one is that the cap does not
    hold, which is the whole point of the feature.

    Never clamp to the policy total: a smaller hold does not lower the actual run
    cap. If this worst case cannot fit, admission must be refused.
    """
    from src.budget.config import budget_config

    return budget_config.budget_run_cap_usd


def flow_reservation_target(
    *,
    org_id: str,
    flow_id: str,
    policy: ExecutionPolicy,
    settled_usd: Decimal,
) -> ReservationTarget:
    """The single reservation key a flow's whole delivery contends on.

    `headroom_usd` is `max_spend_usd - settled_usd` — a **remainder**, not the full
    cap. This is the one place this scope differs from run and chain, and the
    difference is not cosmetic: those two have no settled Postgres ledger at all (the
    usage tracker writes no run rows), so their live Redis total is the entire
    denominator and their headroom must be the whole cap. A flow *does* have a
    settled ledger — `usage_logs.graph_address` prefixed by the flow slug, which
    `policy_admission` reads through `cost.get_cost_by_address`. Passing the full cap
    here would discard that half of the total and let a flow spend its allowance
    twice: once settled, once again in flight.

    Clamped at zero rather than allowed to go negative. A negative headroom would be
    a nonsensical string in the Lua comparison, and the over-cap case is already a
    block in `authorize_action` (`SPEND_LIMIT_EXCEEDED`) — reaching this function
    with settled spend above the cap means the caller skipped the rule, and zero
    headroom denies everything, which is the right behaviour for that mistake.

    `org_id` comes first in the key and doubles as the Redis Cluster hash tag, so two
    tenants cannot share a counter (see `ReservationTarget.key`). It must be the
    node's own server-read `org_id`, never a caller-supplied tenant.
    """
    from src.budget.config import budget_config
    from src.budget.reservations import ReservationTarget

    headroom = policy.limits.max_spend_usd - settled_usd
    return ReservationTarget(
        org_id=org_id,
        entity_type=EntityType.FLOW.value,
        entity_id=flow_budget_binding(flow_id),
        # Lifetime, not a calendar period. A delivery spans whatever it spans, and a
        # monthly rollover would hand a long flow a second full allowance.
        period_type=PeriodType.RUN.value,
        period_start=_LIFETIME,
        headroom_usd=headroom if headroom > 0 else Decimal(0),
        # The run-lifetime TTL, for the same reason the run scope uses it: on the
        # short #4287 backstop this counter would forget everything older than two
        # minutes, and an accumulator that forgets is not an accumulator. It is a
        # backstop against a reservation whose owner died, not the primary release —
        # `release_flow_admission` is that.
        ttl_seconds=budget_config.budget_run_cap_ttl_seconds,
    )


@dataclass(frozen=True)
class FlowReservation:
    """The outcome of trying to reserve headroom for one admission.

    `degraded` True identifies unavailable enforcement and always accompanies a
    refusal. It lets the caller distinguish an outage from an exhausted limit.

    `target` is carried back for logging and assertion, not as the input to the
    release. `enforcement_service` has to stash its run/chain targets (#4323) because
    their keys derive from a run binding that only exists on the request path; a flow
    target derives from `(org_id, flow_id)` alone, both of which the releasing caller
    reads from the node's own row. `flow_reservation_target` is therefore the single
    definition of the key for both directions, which is what makes the released key
    byte-match the reserved one — rather than a second construction that could differ
    silently and leak the hold until TTL.
    """

    admitted: bool
    degraded: bool = False
    target: ReservationTarget | None = None


_reservations: ReservationStore | None = None


def get_flow_reservations() -> ReservationStore:
    """The process-wide reservation store, built lazily from config.

    Module-level rather than per-call because `ReservationStore` holds a Redis
    connection and the tick dispatches many nodes per invocation; a fresh client per
    node would open a connection per node.

    Reads the same two `budget_config` fields `enforcement_service._get_reservations`
    reads, so the engine and the proxy point at one Redis and one key space. They are
    deliberately NOT two stores with two settings: a flow counter that lived somewhere
    the proxy could not see would be a second denominator, which is not a live figure
    at all.
    """
    global _reservations
    if _reservations is None:
        from src.budget.config import budget_config
        from src.budget.reservations import ReservationStore
        from src.shared.config import get_settings

        redis_url = get_settings().redis_url if budget_config.budget_reservation_backend == "redis" else None
        _reservations = ReservationStore(
            redis_url=redis_url,
            # The store default only applies to targets that set no TTL of their own;
            # every target this module builds sets one explicitly. Passed for
            # completeness so the store is never constructed with an implicit zero.
            ttl_seconds=budget_config.budget_reservation_ttl_seconds,
        )
    return _reservations


async def reserve_flow_admission(
    *,
    org_id: str,
    flow_id: str,
    policy: ExecutionPolicy,
    settled_usd: Decimal,
    node_id: str,
    store: ReservationStore | None = None,
) -> FlowReservation:
    """Hold headroom for one node's action against the flow's shared allowance.

    The hold is idempotent on the node (see :func:`admission_request_id`), so a
    dispatch pass that runs twice over the same ready node holds the amount once
    rather than stacking a second copy. A fresh id per call would exhaust the flow's
    allowance in holds without a single action having run — a flow that stops
    dispatching while its ledger reads near zero.

    Disabled, missing or unavailable reservations refuse new policy-governed work.
    """
    from src.budget.config import budget_config

    target = flow_reservation_target(org_id=org_id, flow_id=flow_id, policy=policy, settled_usd=settled_usd)
    request_id = admission_request_id(node_id)

    if not budget_config.budget_reservation_enabled:
        return FlowReservation(admitted=False, degraded=True, target=target)

    resolved = store if store is not None else get_flow_reservations()
    if not resolved.enabled:
        return FlowReservation(admitted=False, degraded=True, target=target)

    outcome = await resolved.reserve(request_id, admission_cost_usd(policy), [target])

    if outcome is None:
        logger.warning(
            "orchestration flow budget: reservation unavailable for flow %s (org %s) — refusing new admission",
            flow_id,
            org_id,
        )
        return FlowReservation(admitted=False, degraded=True, target=target)

    if outcome.admitted:
        return FlowReservation(admitted=True, target=target)

    logger.warning(
        "orchestration flow budget: flow %s (org %s) has no headroom for a further action "
        "(policy allows $%s, settled $%s, worst case per action $%s) — refusing",
        flow_id,
        org_id,
        policy.limits.max_spend_usd,
        settled_usd,
        admission_cost_usd(policy),
    )
    return FlowReservation(admitted=False, target=target)


async def release_flow_admission(
    *,
    org_id: str,
    flow_id: str,
    policy: ExecutionPolicy,
    settled_usd: Decimal,
    node_id: str,
    store: ReservationStore | None = None,
) -> None:
    """Release one node's hold once its spend has reached the settled ledger.

    This is what "reconciliation restores only valid headroom" means here.
    `reserve_flow_admission` holds the per-run ceiling because an agent run's real
    cost is not knowable in advance; once the node has a settled `usage_logs` figure,
    that pessimistic hold is no longer the best information available and keeping it
    would count the node twice. Without this call a flow would admit only
    `max_spend_usd / run_cap` actions in its entire life no matter how little each
    one actually cost.

    **Released to zero, not adjusted to the actual cost** — and this is the one place
    this scope deliberately departs from `enforcement_service`'s run/chain reconcile,
    so the reasoning is spelled out. That path adjusts estimate → actual because its
    scopes have *no settled Postgres ledger*: the live Redis figure is their entire
    denominator, so dropping a completed run's spend would lose it altogether. A flow
    is the opposite case. Its denominator is `settled + in_flight`, where the settled
    half is read from `usage_logs` by `policy_admission._observed_spend`. Writing
    the actual cost into the live half as well would count the same dollars in both
    terms and shrink the flow's allowance by every completed action's cost a second
    time — a flow that stops dispatching at half its authorized spend.

    So the ordering property this relies on is explicit: a hold is released **only**
    once the node's cost is visible in the settled ledger, which is what the caller
    checks. Release before that and there is a window where the spend counts in
    neither term; release after, and it counts in exactly one.

    `settled_usd` is a figure read from `usage_logs`, never a total an agent reported
    about itself — a self-reported number would let a run release a hold larger than
    the spend it had really incurred, which is the "model-reported totals" the issue
    rules out.

    Idempotent on the node id, which is load-bearing rather than incidental: the
    caller sweeps **every** settled node on **every** admission, so a long-lived flow
    re-releases the same node many times. `HSET` of the same zero is what makes that
    free. Failures are swallowed by `ReservationStore.reconcile` and the per-target
    deadline bounds a missed release, so a cache write can never fail an admission
    that the rule already permitted.
    """
    from src.budget.config import budget_config

    if not budget_config.budget_reservation_enabled:
        return

    resolved = store if store is not None else get_flow_reservations()
    if not resolved.enabled:
        return

    target = flow_reservation_target(org_id=org_id, flow_id=flow_id, policy=policy, settled_usd=settled_usd)
    await resolved.reconcile(admission_request_id(node_id), Decimal(0), [target])
