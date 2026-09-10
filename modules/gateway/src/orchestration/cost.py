"""Cost by graph address — three-valued, one grouped query, no DynamoDB.

Issue #4207 (EPIC #4191, intent #4120).

Two problems are fixed here, and they are the same problem seen from two sides:
a number that is not known must never be rendered as a number.

**1. The 30-day cliff (R-O6c).** Aggregation used to enumerate runs from the
DynamoDB webhook-events table and then query Postgres with
`WHERE agent_run_id IN (...)` (`activity/cost_service.py`). DynamoDB rows expire
at 30 days, so on day 31 an EPIC's total silently became partial with nothing
saying so. Here aggregation is **one Postgres query grouped by graph address**,
with no DynamoDB dependency at all — the ledger row carries its own address
(migration 030), so nothing needs to be enumerated first. `test_cost.py` asserts
the query count, so regressing to enumerate-then-`IN` fails CI rather than
quietly reintroducing the cliff.

**2. Absence rendered as zero (R-N5).** `usage_logs.cost_usd` is
`Numeric(10, 6)` **`nullable=False`** (`shared/models/usage.py`), so a missing
cost is never NULL — it is **row-nonexistence**. And `SUM` over zero rows returns
`0` in SQL, indistinguishable from a genuine zero. Three-valued cost therefore
has to be **computed from the row count**, not read out of the sum:

    known          — rows exist and total > 0
    none_incurred  — rows exist and total == 0 (a verified zero)
    unknown        — no rows at all (we do not know; NOT free)

`CostStatus.UNKNOWN` always carries a `reason`, because "unknown" with no
explanation reads as a bug. The legitimate reasons are real: a non-gateway
Bedrock path (`ADP_BEDROCK_VIA=direct`) writes no usage row, chat logging
can be disabled, and a node may simply not have run yet.

**`usage_logs` is the only cost source (R-O6a).** `budget_usage`
(`shared/models/budget.py`) is a per-(entity, period) rolling accumulator: it
carries **no graph address and no run identifier**, so it cannot answer "what did
this node cost" at any address. It is a budget-enforcement counter, not a ledger.
`test_cost.py` asserts at source level that it appears in no cost-read query
here, so a future change reintroducing it fails CI.

(Its `total_cost_usd` was also `Numeric(10, 2)` and rounded away the sub-cent
long tail; #4287/#4291 widened it to `Numeric(14, 6)`. That fixes the rounding
but not the addressability gap above, which is the reason this module never reads
it.)

**The join key — this is the trap this module exists to get right.**
`usage_logs.agent_run_id` holds the DynamoDB **`event_id`**, which is the
agent-worker's `message_id`:

    entrypoint.py:834   os.environ["ADP_MESSAGE_ID"] = message_id
    sigv4-proxy.ts:35   const AGENT_RUN_ID = process.env.ADP_MESSAGE_ID
                        → injected as the X-Agent-RunId header
    proxy/routes.py:141 request.headers.get("x-agent-runid")
                        → contextvar → usage_logs.agent_run_id

The DynamoDB attribute literally **named** `run_id` is something else entirely:
it is the KEDA job/pod name (`entrypoint.py:1498  run_id=_keda_job_name`), which
the UI labels "Run / Job ID". Joining on the plausible-sounding name yields
**zero rows, silently, and a $0.00 EPIC** — a wrong number that looks like a
right one. `assert_join_key_is_event_id` makes that mistake raise instead, and
AC-19 tests it, because the failure mode is otherwise invisible.

**Every figure carries a scope label** (R-N5c). These totals are agent-run
Bedrock costs only: they exclude CodeBuild, EKS compute, NAT, storage, and every
other infrastructure line. A figure that silently excludes non-run cost reads as
a total, and someone will make a budget decision on it.

**Tenant isolation.** Every query filters on `org_id`, and the flow is resolved
under the caller's org before any ledger read. An address from another org
returns no rows — never another tenant's costs.
"""

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.usage import UsageLog

from .models import OrchestrationFlow, OrchestrationNode

logger = logging.getLogger("bedrockgateway.orchestration.cost")

__all__ = [
    "COST_SCOPE_LABEL",
    "AggregateCost",
    "CostStatus",
    "JoinKeyError",
    "NodeCost",
    "UnknownReason",
    "assert_join_key_is_event_id",
    "escape_like",
    "get_cost_by_address",
    "get_cost_by_address_prefixes",
    "get_flow_cost",
]


def escape_like(value: str) -> str:
    """Escape LIKE metacharacters in `value`, for use with `escape="\\\\"`.

    **Load-bearing, not defensive.** `_` is a legal character in a flow slug *and*
    the single-character LIKE wildcard, so an unescaped prefix `loop_4645` also
    matches `loop-4645` — a different flow — and the two flows' costs merge into
    one figure with nothing indicating it happened. `%` is worse: it widens the
    match past the subtree entirely.

    Declared once and shared by every LIKE this package builds (the address-prefix
    cost queries here, the `q` search in `repository.py`). A second copy is a
    second chance for someone to simplify it away in one place only.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# R-N5c: stamped onto every figure this module returns. A cost figure without its
# scope reads as "what this cost", not "what this cost in Bedrock tokens".
COST_SCOPE_LABEL = "agent run costs only; excludes build/infra"

# The DynamoDB attribute named `run_id` is the KEDA job/pod name — derived from
# the ScaledJob name plus a random suffix, so the real values are
# `agent-gateway-worker-abc12`, `chat-agent-worker-xyz98`, `arc-runner-...`
# (`tests/e2e/test_keda_scaler.py:21`, `agent/k8s/chat-scaledjob.yaml:78`).
#
# Matched on the `worker`/`runner` SEGMENT rather than a `^agent-` prefix: the
# deployed ScaledJobs are `agent-gateway-worker` and `chat-agent-worker`, neither
# of which a prefix anchor catches — a guard that misses the real names is worse
# than no guard, because it advertises protection it does not provide.
#
# False positives are implausible in the other direction: an `event_id` is a
# uuid4 (`spawn_persona.py:543`), which is hex-and-hyphens and contains no such
# word.
_KEDA_JOB_NAME = re.compile(r"(?:^|-)(?:worker|runner)(?:-|$)", re.IGNORECASE)


class CostStatus(StrEnum):
    """Three-valued cost. The whole point is that `UNKNOWN` is not a number.

    `NONE_INCURRED` and `UNKNOWN` both total zero dollars and mean opposite
    things: the first is a measured zero, the second is an absent measurement.
    Collapsing them is the bug this story exists to end, so they are distinct
    members rather than a nullable amount.
    """

    KNOWN = "known"  # Rows exist, total > 0
    NONE_INCURRED = "none_incurred"  # Rows exist, total == 0 — a verified zero
    UNKNOWN = "unknown"  # No rows — we do not know. NOT free.


class UnknownReason(StrEnum):
    """Why a figure is `UNKNOWN`. Required, because bare "unknown" reads as a bug.

    Each member is a real, documented path to having no usage row — none of them
    are "shouldn't happen" placeholders.
    """

    NO_USAGE_ROWS = "no_usage_rows"  # Addressed, but nothing logged against it
    NOT_STARTED = "not_started"  # Node has not run yet
    NOT_COSTABLE = "not_costable"  # Gate/eval node with no model calls to bill
    NON_GATEWAY_PATH = "non_gateway_path"  # ADP_BEDROCK_VIA=direct: no row


class JoinKeyError(ValueError):
    """Raised when a cost query is handed KEDA job names instead of event ids.

    This is deliberately loud. The alternative — joining on the wrong key — is a
    successful query returning zero rows, which every formatter downstream
    renders as `$0.00`. A wrong number that looks right is worse than an
    exception, so this refuses to run rather than return a plausible lie.
    """


def assert_join_key_is_event_id(run_ids: list[str]) -> None:
    """Fail loudly if `run_ids` look like KEDA job names rather than event ids.

    AC-19. `usage_logs.agent_run_id` == DynamoDB `event_id` (the worker's
    `message_id`). The attribute *named* `run_id` is the KEDA pod name
    (`entrypoint.py:1498`), which the UI labels "Run / Job ID" — so the wrong key
    is the one with the more convincing name. Joining on it matches nothing and
    silently reports a $0.00 EPIC.

    Raises:
        JoinKeyError: If any value looks like a KEDA job name.
    """
    # `search`, not `match`: the real names are `agent-gateway-worker-abc12` and
    # `chat-agent-worker-xyz98`, where the giveaway segment is in the MIDDLE.
    # `match` anchors at position 0 and would let both through — a guard that
    # misses the deployed names is worse than none, because it advertises
    # protection it does not provide.
    offenders = [run_id for run_id in run_ids if run_id and _KEDA_JOB_NAME.search(run_id)]
    if offenders:
        raise JoinKeyError(
            "cost queries join usage_logs.agent_run_id == DynamoDB `event_id` "
            "(the agent-worker message_id). These look like KEDA job names, i.e. "
            "the DynamoDB attribute named `run_id`, which is the pod name and "
            f"matches no usage_logs row: {offenders[:3]!r}. Joining on it would "
            "return zero rows and report $0.00 as though the work were free."
        )


@dataclass(frozen=True)
class NodeCost:
    """Cost for one graph address, three-valued.

    `amount_usd` is `None` for anything but `KNOWN`. That is enforced in
    `__post_init__` rather than left to callers: an `UNKNOWN` carrying `0` is
    exactly the shape that gets formatted as `$0.00` three layers away.
    """

    address: str
    status: CostStatus
    amount_usd: Decimal | None = None
    total_tokens: int = 0
    call_count: int = 0
    reason: UnknownReason | None = None
    scope: str = COST_SCOPE_LABEL

    def __post_init__(self) -> None:
        if self.status is CostStatus.UNKNOWN:
            if self.amount_usd is not None:
                raise ValueError("an UNKNOWN cost must not carry an amount — that is how absence becomes $0.00")
            if self.reason is None:
                raise ValueError("an UNKNOWN cost must carry a reason; bare 'unknown' reads as a bug")
        elif self.amount_usd is None:
            raise ValueError(f"a {self.status.value} cost must carry an amount")


@dataclass(frozen=True)
class AggregateCost:
    """A rollup over many addresses — an EPIC, a wave, or a whole flow.

    `partial` is the aggregate-level counterpart of `UNKNOWN` (R-O6d, AC-21). An
    aggregate is partial when any member node is `UNKNOWN`, because the total then
    excludes an unmeasured contribution and is a **lower bound**, not a total.
    Rendering it as a total is how decisions get made on wrong numbers.
    """

    address: str
    status: CostStatus
    amount_usd: Decimal | None = None
    total_tokens: int = 0
    call_count: int = 0
    node_count: int = 0
    unknown_node_count: int = 0
    partial: bool = False
    reason: UnknownReason | None = None
    nodes: tuple[NodeCost, ...] = field(default_factory=tuple)
    scope: str = COST_SCOPE_LABEL

    def __post_init__(self) -> None:
        if self.status is CostStatus.UNKNOWN and self.amount_usd is not None:
            raise ValueError("an UNKNOWN aggregate must not carry an amount")
        if self.unknown_node_count > 0 and not self.partial:
            raise ValueError("an aggregate containing an UNKNOWN node is partial by definition")


def _classify(total: Decimal | None, call_count: int, *, absent_reason: UnknownReason) -> tuple[CostStatus, Decimal | None, UnknownReason | None]:
    """Turn (sum, row count) into a three-valued status.

    The row count is load-bearing and the sum alone is not sufficient: `SUM` over
    zero rows returns `0` (or NULL, depending on dialect), which is
    indistinguishable from a real zero. `call_count == 0` is the only signal that
    separates "no measurement" from "measured zero".
    """
    if call_count == 0:
        return CostStatus.UNKNOWN, None, absent_reason
    amount = Decimal(total or 0)
    if amount == 0:
        return CostStatus.NONE_INCURRED, amount, None
    return CostStatus.KNOWN, amount, None


async def get_cost_by_address(
    db: AsyncSession,
    *,
    org_id: str,
    address_prefix: str,
) -> list[NodeCost]:
    """Cost per graph address under `address_prefix`, in ONE grouped query.

    This is the query that removes the DynamoDB dependency (R-O6c): it groups
    `usage_logs` by `graph_address` directly, so no run enumeration precedes it
    and there is no 30-day horizon on the result. Addresses with no rows are
    simply absent from the return — the caller decides whether that is
    `NOT_STARTED` or `NO_USAGE_ROWS`, since only the caller knows which nodes were
    expected.

    Args:
        address_prefix: A graph-address prefix, e.g. `flow/epic` for an EPIC
            rollup or the full `flow/epic/wave/node` for one node. Matched with
            `LIKE prefix/%` plus an exact match, so `epic-1` never matches
            `epic-10`.

    Note: `org_id` is filtered in SQL, not in Python. An address from another
    tenant returns no rows rather than that tenant's costs.
    """
    # Escape LIKE metacharacters: an address containing `%` would otherwise widen
    # the match past its own subtree and pull in unrelated nodes' costs. The
    # expression moved to `escape_like` verbatim when #4869 needed the same rule
    # for a list of prefixes — same behaviour, one copy instead of two.
    escaped = escape_like(address_prefix)

    query = (
        select(
            UsageLog.graph_address,
            func.sum(UsageLog.cost_usd).label("total_cost_usd"),
            func.sum(UsageLog.input_tokens + UsageLog.output_tokens).label("total_tokens"),
            func.count(UsageLog.id).label("call_count"),
        )
        .where(
            UsageLog.org_id == org_id,
            UsageLog.graph_address.is_not(None),
            # Exact match OR descendant. The trailing `/` is what stops
            # `flow/epic-1` from matching `flow/epic-10`.
            (UsageLog.graph_address == address_prefix) | (UsageLog.graph_address.like(f"{escaped}/%", escape="\\")),
        )
        .group_by(UsageLog.graph_address)
    )

    rows = (await db.execute(query)).all()

    costs: list[NodeCost] = []
    for row in rows:
        status, amount, reason = _classify(row.total_cost_usd, int(row.call_count or 0), absent_reason=UnknownReason.NO_USAGE_ROWS)
        costs.append(
            NodeCost(
                address=row.graph_address,
                status=status,
                amount_usd=amount,
                total_tokens=int(row.total_tokens or 0),
                call_count=int(row.call_count or 0),
                reason=reason,
            )
        )
    return costs


async def get_cost_by_address_prefixes(
    db: AsyncSession,
    *,
    org_id: str,
    address_prefixes: list[str],
) -> list[NodeCost]:
    """Cost per graph address under ANY of `address_prefixes`, in ONE grouped query.

    Issue #4869. The flows list needs a cost figure per flow for a whole page of
    flows, and `usage_logs.graph_address` is `{flow.slug}/{epic}/{wave}/{node}` —
    so a flow's slug *is* its address prefix and one `or_()` of prefix predicates
    covers the page. Callers attribute each returned address back to its flow by
    splitting on the first `/` (`proposal.split_address`).

    This is a sibling of `get_cost_by_address`, not a replacement, and it is
    deliberately **not** built on `get_flow_cost`: that function is node-shaped
    (it needs the node list per flow to distinguish `NOT_STARTED` from
    `NOT_COSTABLE`) and has a live caller whose semantics must not shift.

    Each disjunct carries its own `prefix/` separator, so the boundary that stops
    `loop-4645` matching `loop-46450` survives the `or_()` — the escaping and the
    trailing `/` are per-prefix, not applied once to the whole clause.

    Args:
        address_prefixes: Flow slugs (or any address prefix). Empty returns `[]`
            without a query — an empty `or_()` is `false`, but not issuing the
            statement at all keeps the endpoint's query count honest.

    Note: `org_id` is filtered in SQL. A prefix belonging to another tenant
    returns no rows rather than that tenant's costs — which matters more here than
    in the single-prefix version, since two tenants may run identically-named
    flows and the page's prefixes come from a list.
    """
    if not address_prefixes:
        return []

    clauses = [
        # Identical escaping and boundary to `get_cost_by_address` — `_` is a legal
        # slug character *and* a single-char LIKE wildcard, so without the escape
        # `loop_4645` matches `loop-4645` and two flows' costs merge into one
        # figure. The equality disjunct is kept because that one does use the index.
        (UsageLog.graph_address == prefix) | (UsageLog.graph_address.like(f"{escape_like(prefix)}/%", escape="\\"))
        for prefix in address_prefixes
    ]

    query = (
        select(
            UsageLog.graph_address,
            func.sum(UsageLog.cost_usd).label("total_cost_usd"),
            func.sum(UsageLog.input_tokens + UsageLog.output_tokens).label("total_tokens"),
            func.count(UsageLog.id).label("call_count"),
        )
        .where(
            UsageLog.org_id == org_id,
            UsageLog.graph_address.is_not(None),
            or_(*clauses),
        )
        .group_by(UsageLog.graph_address)
    )

    rows = (await db.execute(query)).all()

    costs: list[NodeCost] = []
    for row in rows:
        status, amount, reason = _classify(row.total_cost_usd, int(row.call_count or 0), absent_reason=UnknownReason.NO_USAGE_ROWS)
        costs.append(
            NodeCost(
                address=row.graph_address,
                status=status,
                amount_usd=amount,
                total_tokens=int(row.total_tokens or 0),
                call_count=int(row.call_count or 0),
                reason=reason,
            )
        )
    return costs


async def get_flow_cost(
    db: AsyncSession,
    *,
    org_id: str,
    flow: OrchestrationFlow,
    nodes: list[OrchestrationNode],
) -> AggregateCost:
    """Roll a flow's cost up from its nodes, labelling partial totals.

    Exactly **two** queries run regardless of node count: one for the flow's
    ledger rows (grouped by address) and none per node — the nodes are already in
    hand from the caller's store read. Adding a per-node query here would
    reintroduce the N+1 that the grouped query exists to remove.

    A node with no ledger row becomes `UNKNOWN`, never `0` (AC-22), and its
    presence makes the aggregate `partial` (AC-21): the total is then a lower
    bound, because an unmeasured contribution is missing from it.
    """
    measured = {cost.address: cost for cost in await get_cost_by_address(db, org_id=org_id, address_prefix=flow.slug)}

    node_costs: list[NodeCost] = []
    for node in nodes:
        address = f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"
        existing = measured.get(address)
        if existing is not None:
            node_costs.append(existing)
            continue
        # No ledger row. Why matters to whoever reads this: a gate or eval node
        # has nothing to bill, while a story node that has not run yet may still
        # cost money later. Both are UNKNOWN, but they are not the same news.
        node_costs.append(
            NodeCost(
                address=address,
                status=CostStatus.UNKNOWN,
                reason=UnknownReason.NOT_COSTABLE if node.kind in ("gate", "eval") else UnknownReason.NOT_STARTED,
            )
        )

    known = [cost for cost in node_costs if cost.status is not CostStatus.UNKNOWN]
    unknown_count = sum(1 for cost in node_costs if cost.status is CostStatus.UNKNOWN)

    if not known:
        # Nothing measured anywhere. This is UNKNOWN, not a zero total — an
        # untouched flow has not been established to be free.
        return AggregateCost(
            address=flow.slug,
            status=CostStatus.UNKNOWN,
            node_count=len(node_costs),
            unknown_node_count=unknown_count,
            partial=unknown_count > 0,
            reason=UnknownReason.NO_USAGE_ROWS,
            nodes=tuple(node_costs),
        )

    total = sum((cost.amount_usd or Decimal(0) for cost in known), Decimal(0))
    status = CostStatus.KNOWN if total > 0 else CostStatus.NONE_INCURRED

    return AggregateCost(
        address=flow.slug,
        status=status,
        amount_usd=total,
        total_tokens=sum(cost.total_tokens for cost in known),
        call_count=sum(cost.call_count for cost in known),
        node_count=len(node_costs),
        unknown_node_count=unknown_count,
        # AC-21: any UNKNOWN member means the total omits an unmeasured
        # contribution, so it is a lower bound and must be labelled as one.
        partial=unknown_count > 0,
        nodes=tuple(node_costs),
    )
