"""Detect a loop that stopped moving, and bound how many times a defect may cycle.

Issue #4211 (EPIC #4191, intent #4120). This is the detection half of the story
whose delivery half is `notify.py`.

Two different failures, deliberately given two different outcomes:

**A stall** is a node that has been `running` longer than it plausibly should. It
becomes `failed` — human-recoverable, with `attempts` preserved so the cycle bound
below still applies when someone resumes it.

**A halt** is a defect that has cycled past its allowed number of attempts. It
becomes `halted`, which is terminal for the engine.

They are not merged into one state because they need different responses: a stall
is "go look at why this node is wedged", a halt is "this work is not converging,
stop paying for it". Which one occurred is recorded on the decision row
(`NODE_STALLED` / `NODE_HALTED`), so it is queryable rather than inferred from a
state that means both.

--------------------------------------------------------------------------------
The threshold must sit BELOW the agent pod's own deadline (R-O4a)
--------------------------------------------------------------------------------

This is the original bug and the reason the story exists. The agent pod is killed
by Kubernetes at `activeDeadlineSeconds`. If the engine's stall threshold were
*longer* than that, the pod would always die first, the node would sit in
`running` forever, and the engine would never call it stalled — no diagnosis, no
notification, indefinitely. The threshold is therefore **derived from** the pod
deadline rather than picked independently of it, and `StallConfig` refuses to
construct if the derived value is not strictly below it. `test_stall.py` pins that
relationship so raising the deadline later cannot silently invert it.

The deadline itself lives in Terraform — `agent_pod_deadline_seconds` in
`modules/agent-factory/webhook-ingress/infra/variables.tf`, consumed only by the
KEDA ScaledJob's `activeDeadlineSeconds` in `scaledjob.tf`. It is not exposed to
Python anywhere, so `AGENT_POD_DEADLINE_SECONDS` below mirrors it. A mirror is a
drift risk and is called out as one; it is still strictly better than the status
quo, which is the same number written in English in a docstring
(`src/activity/liveness.py:6`) where nothing can assert against it at all.

The existing 24 h staleness cutoff (`ACTIVE_STALENESS_HOURS` in
`src/activity/liveness.py`) is **not** reused: 24 h is four times the pod deadline,
so it is exactly the inverted relationship described above.

The default fraction is high (0.9) on purpose. "Healthy long-running work is
halted; the engine becomes the thing that breaks the loop" is one of this story's
own listed bug classes, and a conservative threshold is how that is avoided — a
6 h pod that is legitimately working for 3 h must not be declared stalled.

--------------------------------------------------------------------------------
The cycle bound must sit strictly BELOW MAX_CHAIN_DEPTH
--------------------------------------------------------------------------------

Two limits, and the ordering is the whole point. Exceeding the cycle bound yields
`halted`: a state with a recorded reason that an operator can diagnose. Exceeding
`MAX_CHAIN_DEPTH` (8, in `webhook-ingress/lambda/common/spawn_persona.py`) yields
an opaque dispatch-guard refusal. If the cycle bound were the looser of the two,
**every** runaway defect would surface as the undiagnosable one. So the bound is
configurable, defaults to 5, and a config with `bound >= MAX_CHAIN_DEPTH` is
rejected at construction rather than tolerated.

The bound is also tunable per environment via `ORCH_DEFECT_CYCLE_BOUND` (issue
#4403), read by `StallConfig.from_env`. That reader routes through this same
constructor rather than validating separately, so an env value outside `1 <= n < 8`
is rejected by the invariant above and falls back to the default — the knob cannot
be used to invert the ordering, and a typo in a Deployment env var cannot disable
halting or crash the tick.

--------------------------------------------------------------------------------
`halted` is terminal for the engine
--------------------------------------------------------------------------------

Only an explicit human override (`halted -> ready`) resumes a halted node, and
`state.py` already restricts that edge to `actor_kind="human"` (R-Q9c). This module
adds no path that clears a halt — it never proposes `READY` as a target at all, and
`test_stall.py` asserts that at source level as well as behaviourally. A bound the
engine can lift is a decorative bound.

--------------------------------------------------------------------------------
Detection proposes; `transition()` decides
--------------------------------------------------------------------------------

There is no state UPDATE in this module. Every state change is routed through
`apply_guarded_transition` in `tick.py`, which is already the single audited seam:
it consults `transition()` for authority, records a rejection as a decision row,
and guards the write with `UPDATE ... WHERE id = :id AND state = :observed_state`
for concurrency. Reusing it rather than writing a second one is required by the
story ("do not invent a second one") and is also what keeps the notify-once
guarantee cheap — see below.

**Notify-once is structural, not a flag.** A notification is emitted only when that
conditional UPDATE matched exactly one row. Two overlapping ticks therefore produce
exactly one notification: the loser matches zero rows and notifies nobody. A later
tick finds the node in `failed`/`halted`, which is not a candidate state, so it is
never re-examined. There is no "notified" column to keep in sync and no dedupe
table to expire.

Tenant isolation: every read is filtered by the `org_id` of the node being
considered, counts are broken out per org, and each notification carries its own
org — a stall in one org never notifies another.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DecisionKind, OrchestrationDecision, OrchestrationNode
from .notify import Notification, NotificationError, NotifyConfig, notify
from .state import TERMINAL_STATES, ActorKind, NodeState
from .tick import _ITEM_BACKSTOP, _PAGE_SIZE, apply_guarded_transition

logger = logging.getLogger("bedrockgateway.orchestration.stall")

# Mirror of Terraform's `agent_pod_deadline_seconds` (6 h), the hard kill applied
# to an agent pod by the KEDA ScaledJob's `activeDeadlineSeconds`. See the module
# docstring for why this is a mirror and what asserts against it.
#
# If this is ever raised, `StallConfig` recomputes the threshold from it
# automatically — that is the point of deriving rather than hardcoding. If it is
# *lowered* below the configured absolute threshold, construction fails loudly.
AGENT_POD_DEADLINE_SECONDS = 21_600

# Mirror of `MAX_CHAIN_DEPTH` in
# `modules/agent-factory/webhook-ingress/lambda/common/spawn_persona.py`. That
# module is Lambda-side code with no import path from the gateway image, so the
# value is restated here; `test_stall.py` pins it to 8 so a change on either side
# fails a test rather than silently reordering the two limits.
MAX_CHAIN_DEPTH = 8

# Default cycle bound. Five attempts at the same defect is the point at which more
# attempts stop being evidence of progress.
#
# Raised from 3 by issue #4403: at 3, defects that would have converged on a 4th or
# 5th autonomous attempt were being parked in front of a human instead, which is the
# interruption this platform exists to avoid. 5 keeps two attempts of headroom below
# `MAX_CHAIN_DEPTH`, so the diagnosable `halted` still wins the race described above.
DEFAULT_DEFECT_CYCLE_BOUND = 5

# Per-environment override for the bound (issue #4403). An integer in `1..7`; see
# `StallConfig.from_env` for the fail-closed parse. Absent is the normal case — the
# default above applies with nothing set.
DEFECT_CYCLE_BOUND_ENV = "ORCH_DEFECT_CYCLE_BOUND"

# The threshold as a fraction of the pod deadline. Strictly below 1.0 by
# construction, which is what makes the R-O4a invariant hold for *any* deadline
# rather than for one particular pair of numbers. 0.9 of 6 h is 5.4 h.
DEFAULT_THRESHOLD_FRACTION = 0.9

# The actor this module transitions as. SERVICE, never HUMAN — which is precisely
# what makes the `halted -> ready` edge in `state.py` unreachable from here.
_STALL_ACTOR = ActorKind.SERVICE
_STALL_ACTOR_ID = "system:orchestration-stall-detector"
_STALL_ACTOR_ROLE = "engine"


# States a node can be stalled *in*. Only `running`: a node in `awaiting_gate` is
# waiting on a human by design, and calling that a stall would make the engine
# report every unreviewed gate as a fault. Terminal states are excluded by
# construction here and re-asserted in `_is_candidate_state`.
STALLABLE_STATES: frozenset[NodeState] = frozenset({NodeState.RUNNING})

# States a node can be halted *from*. `awaiting_gate` is included because a defect
# that cycled its bound and is now parked at a gate has still exhausted its budget,
# and `state.py` permits the edge for a service actor.
HALTABLE_STATES: frozenset[NodeState] = frozenset({NodeState.RUNNING, NodeState.AWAITING_GATE})


class StallConfigError(ValueError):
    """A stall/halt configuration that violates one of the two ordering invariants.

    Raised at construction — "rejected at load", not tolerated and warned about.
    A config that inverts either ordering does not degrade detection, it disables
    the diagnosable outcome entirely, so there is no safe way to run with one.
    """


@dataclass(frozen=True)
class StallConfig:
    """Engine configuration for stall and halt detection.

    Both invariants are enforced here rather than at the call site, so there is
    exactly one place either can be violated and it fails closed.

    Args:
        pod_deadline_seconds: The agent pod's hard kill, in seconds.
        threshold_fraction: Fraction of the deadline at which `running` becomes a
            stall. Must be strictly between 0 and 1.
        threshold_seconds: Explicit absolute override. When given it is used
            verbatim instead of the derived value — and still checked against the
            deadline, so an override cannot buy its way past R-O4a.
        defect_cycle_bound: Attempts allowed before a defect halts.

    Raises:
        StallConfigError: If the derived/overridden threshold is not strictly below
            the pod deadline (R-O4a), or if `defect_cycle_bound` is not strictly
            below `MAX_CHAIN_DEPTH`.
    """

    pod_deadline_seconds: int = AGENT_POD_DEADLINE_SECONDS
    threshold_fraction: float = DEFAULT_THRESHOLD_FRACTION
    threshold_seconds: int | None = None
    defect_cycle_bound: int = DEFAULT_DEFECT_CYCLE_BOUND

    def __post_init__(self) -> None:
        if self.pod_deadline_seconds <= 0:
            raise StallConfigError(f"pod_deadline_seconds must be positive; got {self.pod_deadline_seconds}")

        if self.threshold_seconds is None and not 0.0 < self.threshold_fraction < 1.0:
            # A fraction of 1.0 or more derives a threshold at or above the
            # deadline, which is the exact inversion this story exists to prevent.
            raise StallConfigError(
                f"threshold_fraction must be strictly between 0 and 1 so the threshold stays below the pod deadline; got {self.threshold_fraction}"
            )

        if self.defect_cycle_bound < 1:
            raise StallConfigError(f"defect_cycle_bound must be at least 1; got {self.defect_cycle_bound}")

        # --- Invariant 1 (R-O4a): the threshold is strictly below the deadline.
        # Checked against the *effective* threshold, so an absolute override is
        # covered as well as the derived value.
        if self.stall_threshold_seconds >= self.pod_deadline_seconds:
            raise StallConfigError(
                f"stall threshold ({self.stall_threshold_seconds}s) must be STRICTLY BELOW the agent pod "
                f"deadline ({self.pod_deadline_seconds}s): otherwise the pod is killed before the engine "
                "ever calls the node stalled, and the node stays 'running' forever with no diagnosis (R-O4a)"
            )

        # --- Invariant 2: the cycle bound is strictly below the chain-depth guard,
        # so the diagnosable `halted` always fires before the opaque one.
        if self.defect_cycle_bound >= MAX_CHAIN_DEPTH:
            raise StallConfigError(
                f"defect_cycle_bound ({self.defect_cycle_bound}) must be STRICTLY BELOW MAX_CHAIN_DEPTH "
                f"({MAX_CHAIN_DEPTH}): otherwise the chain-depth guard trips first and a runaway defect "
                "surfaces as an opaque dispatch failure instead of a diagnosable 'halted'"
            )

    @property
    def stall_threshold_seconds(self) -> int:
        """The effective threshold: the override if given, else derived."""
        if self.threshold_seconds is not None:
            return self.threshold_seconds
        return int(self.pod_deadline_seconds * self.threshold_fraction)

    @classmethod
    def from_env(cls) -> StallConfig:
        """Build from the process environment, falling back to the defaults (#4403).

        Only `defect_cycle_bound` is env-tunable. The threshold is deliberately not:
        it is *derived* from the pod deadline precisely so the two cannot be set
        independently of each other, and an env knob would reintroduce the drift the
        derivation removes.

        **Never raises.** This runs on the tick path, where the alternative to a
        usable config is no detection pass at all — so a bad value degrades to the
        default rather than taking the tick down. Two ways it can be bad, both
        handled the same way and both logged loudly:

        - unparseable (`"x"`, `""`) — `ValueError` from `int`
        - out of range (`0`, `9`) — `StallConfigError` from the invariants above,
          which is why the parse routes through the real constructor instead of
          re-checking the bounds here. There is one validation point, not two.

        Falling back is safe in the direction that matters: the default is inside
        both invariants, so a typo cannot disable halting or let the opaque
        chain-depth guard trip first.
        """
        raw = (os.environ.get(DEFECT_CYCLE_BOUND_ENV) or "").strip()
        if not raw:
            return cls()

        try:
            return cls(defect_cycle_bound=int(raw))
        except (ValueError, StallConfigError) as exc:
            logger.warning(
                "orchestration stall: %s=%r is not a usable defect cycle bound (%s); using default %d",
                DEFECT_CYCLE_BOUND_ENV,
                raw,
                exc,
                DEFAULT_DEFECT_CYCLE_BOUND,
            )
            return cls()


@dataclass
class StallReport:
    """What one detection pass found. Every field exists to be emitted as a metric.

    `success` is False if anything went wrong — including a notification that could
    not be delivered. Detection that ran but told nobody is not a success (R-Q9d),
    which is why `notifications_failed` counts against it.
    """

    nodes_examined: int = 0
    stalls_detected: int = 0
    halts_detected: int = 0
    notifications_sent: int = 0
    # A delivery failure. Counted and forces non-success — never swallowed (R-NF3).
    notifications_failed: int = 0
    errors: int = 0
    # Guarded writes that matched 0 rows because a concurrent pass got there first.
    lost_races: int = 0
    transitions_rejected: int = 0
    pages_read: int = 0
    truncated: bool = False
    per_org: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """False if anything failed, including delivery."""
        return self.errors == 0 and self.notifications_failed == 0

    def _org(self, org_id: str) -> dict[str, int]:
        return self.per_org.setdefault(
            org_id,
            {
                "nodes_examined": 0,
                "stalls_detected": 0,
                "halts_detected": 0,
                "notifications_sent": 0,
                "notifications_failed": 0,
                "errors": 0,
                "lost_races": 0,
                "transitions_rejected": 0,
            },
        )

    def record(self, org_id: str, key: str, amount: int = 1) -> None:
        """Increment a counter both in total and for one org."""
        setattr(self, key, getattr(self, key) + amount)
        self._org(org_id)[key] += amount


@dataclass(frozen=True)
class _Candidate:
    """A node as observed, with the state we observed it in.

    `observed_state` is carried rather than re-read at write time: guarding the
    write on a freshly re-read value would reintroduce the read-then-write race the
    guard exists to close.
    """

    node_id: str
    org_id: str
    flow_id: str
    observed_state: str
    attempts: int
    # When this node entered its current state. `updated_at` is set by
    # `apply_guarded_transition` on every state write, so for a `running` node it
    # is the moment it started running. `created_at` is the fallback for a node
    # that has never been updated.
    since: datetime


def _is_candidate_state(raw_state: str) -> NodeState | None:
    """Coerce and screen one observed state. None means "never flag this node".

    Terminal states are rejected here as well as being excluded by the query. The
    query is an optimisation; this is the guarantee. An undeclared literal is also
    rejected rather than raising: a node carrying a value outside the vocabulary
    must not be transitioned on the strength of a guess.
    """
    try:
        state = NodeState(raw_state)
    except ValueError:
        logger.error("orchestration stall: node carries undeclared state %r; not flagging", raw_state)
        return None

    if state in TERMINAL_STATES:
        # Includes `halted` itself, which is what stops a halted node being
        # re-examined, re-halted or re-notified on every subsequent pass.
        return None

    if state not in STALLABLE_STATES and state not in HALTABLE_STATES:
        return None

    return state


async def _fetch_candidate_page(session: AsyncSession, *, after_id: str | None, limit: int) -> list[_Candidate]:
    """One keyset page of nodes that could be stalled or halted, ordered by id.

    Keyset (`id > :after_id`) rather than OFFSET, matching the tick: pagination
    stays correct while rows are being updated underneath it, and stays
    index-served as the table grows. `ix_orchestration_nodes_org_id_state` serves
    the state predicate.
    """
    watched = sorted({state.value for state in STALLABLE_STATES | HALTABLE_STATES})

    stmt = select(
        OrchestrationNode.id,
        OrchestrationNode.org_id,
        OrchestrationNode.flow_id,
        OrchestrationNode.state,
        OrchestrationNode.attempts,
        OrchestrationNode.updated_at,
        OrchestrationNode.created_at,
    ).where(OrchestrationNode.state.in_(watched))

    if after_id is not None:
        stmt = stmt.where(OrchestrationNode.id > after_id)

    stmt = stmt.order_by(OrchestrationNode.id).limit(limit)

    candidates: list[_Candidate] = []
    for row in (await session.execute(stmt)).all():
        node_id, org_id, flow_id, state, attempts, updated_at, created_at = row
        since = updated_at or created_at
        candidates.append(
            _Candidate(
                node_id=node_id,
                org_id=org_id,
                flow_id=flow_id,
                observed_state=state,
                attempts=attempts or 0,
                # SQLite returns naive datetimes; normalise so the arithmetic
                # against an aware `now` cannot raise on one backend and work on
                # the other.
                since=since if since.tzinfo is not None else since.replace(tzinfo=UTC),
            )
        )
    return candidates


def _record_decision(
    session: AsyncSession,
    candidate: _Candidate,
    *,
    kind: DecisionKind,
    reason: str,
    to_state: NodeState,
) -> None:
    """Append the decision row that makes a stall or a halt diagnosable.

    An append to an append-only table, not a state change — the state change is
    `apply_guarded_transition`'s job. Recording *why* is what distinguishes a halt
    from "the node just stopped": AC-8 requires the reason, not merely the state.
    """
    session.add(
        OrchestrationDecision(
            org_id=candidate.org_id,
            flow_id=candidate.flow_id,
            node_id=candidate.node_id,
            kind=kind.value,
            actor_id=_STALL_ACTOR_ID,
            actor_role=_STALL_ACTOR_ROLE,
            actor_kind=_STALL_ACTOR.value,
            reason=reason,
            from_state=candidate.observed_state,
            to_state=to_state.value,
        )
    )


def _deliver(
    notification: Notification,
    report: StallReport,
    *,
    notify_config: NotifyConfig | None,
) -> None:
    """Send one notification, counting a failure rather than hiding it.

    The exception is caught so that one undeliverable notification cannot stop the
    remaining nodes from being detected — but it is counted into
    `notifications_failed`, which forces `report.success` to False, and logged with
    its traceback. That is the difference between containment and silent
    degradation (R-NF3): the pass keeps going *and* the failure is visible.
    """
    try:
        notify(notification, notify_config)
    except NotificationError:
        logger.exception(
            "orchestration stall: FAILED to deliver %s notification for node %s (org %s) — detection succeeded but nobody was told",
            notification.event,
            notification.node_id,
            notification.org_id,
        )
        report.record(notification.org_id, "notifications_failed")
        return

    report.record(notification.org_id, "notifications_sent")


async def _propose(
    session: AsyncSession,
    candidate: _Candidate,
    report: StallReport,
    *,
    to_state: NodeState,
    kind: DecisionKind,
    reason: str,
    detail: dict[str, str | int | float | None],
    detected_counter: str,
    notify_config: NotifyConfig | None,
) -> None:
    """Propose one transition, record it, and notify exactly once if it took.

    The ordering is the notify-once guarantee: the notification is sent only when
    the conditional UPDATE matched exactly one row. A concurrent pass that already
    moved this node matches zero rows and notifies nobody, so overlapping ticks
    produce one notification between them rather than one each.
    """
    rows, allowed = await apply_guarded_transition(
        session,
        node_id=candidate.node_id,
        org_id=candidate.org_id,
        flow_id=candidate.flow_id,
        observed_state=candidate.observed_state,
        to_state=to_state,
        reason=reason,
    )

    if not allowed:
        # The authority guard refused. Already recorded as a decision row inside
        # the seam; counted here so it reaches the metrics.
        report.record(candidate.org_id, "transitions_rejected")
        return

    if rows != 1:
        # Allowed but no row matched: a concurrent pass moved this node between our
        # read and our write. A no-op is correct, and notifying here is exactly the
        # duplicate the once-only requirement forbids.
        report.record(candidate.org_id, "lost_races")
        logger.info("orchestration stall: node %s already moved by a concurrent pass; no-op", candidate.node_id)
        return

    _record_decision(session, candidate, kind=kind, reason=reason, to_state=to_state)
    await session.flush()
    report.record(candidate.org_id, detected_counter)

    logger.warning(
        "orchestration stall: node %s (org %s, flow %s) %s -> %s: %s",
        candidate.node_id,
        candidate.org_id,
        candidate.flow_id,
        candidate.observed_state,
        to_state.value,
        reason,
    )

    _deliver(
        Notification(
            org_id=candidate.org_id,
            flow_id=candidate.flow_id,
            node_id=candidate.node_id,
            event=kind.value,
            summary=reason,
            detail=detail,
        ),
        report,
        notify_config=notify_config,
    )


async def _examine(
    session: AsyncSession,
    candidate: _Candidate,
    report: StallReport,
    *,
    now: datetime,
    config: StallConfig,
    notify_config: NotifyConfig | None,
) -> None:
    """Decide whether one node is halted, stalled, or healthy.

    Halt is evaluated **before** stall. A node that has both exhausted its cycle
    bound and been running too long is halted, not failed: halting is the more
    specific finding and the one that stops further spend, and treating it as a
    stall would let it be resumed straight back into the cycle it just exhausted.
    """
    state = _is_candidate_state(candidate.observed_state)
    if state is None:
        return

    elapsed = int((now - candidate.since).total_seconds())

    if state in HALTABLE_STATES and candidate.attempts >= config.defect_cycle_bound:
        await _propose(
            session,
            candidate,
            report,
            to_state=NodeState.HALTED,
            kind=DecisionKind.NODE_HALTED,
            reason=(
                f"defect-cycle bound exhausted: {candidate.attempts} attempt(s) at a bound of "
                f"{config.defect_cycle_bound}; halting rather than cycling again"
            ),
            detail={
                "attempts": candidate.attempts,
                "defect_cycle_bound": config.defect_cycle_bound,
                "observed_state": candidate.observed_state,
            },
            detected_counter="halts_detected",
            notify_config=notify_config,
        )
        return

    if state is NodeState.RUNNING:
        managed, worker_since = await _continuation_clock(session, candidate)
        if managed:
            # The execution runner owns a completed worker's next phase. Applying
            # the initial developer's pod deadline here kills review/merge work.
            if worker_since is None:
                return
            elapsed = int((now - max(candidate.since, worker_since)).total_seconds())

    if state in STALLABLE_STATES and elapsed > config.stall_threshold_seconds:
        await _propose(
            session,
            candidate,
            report,
            to_state=NodeState.FAILED,
            kind=DecisionKind.NODE_STALLED,
            reason=(
                f"stalled: {elapsed}s in '{candidate.observed_state}' exceeds the "
                f"{config.stall_threshold_seconds}s threshold (agent pod deadline {config.pod_deadline_seconds}s)"
            ),
            detail={
                "elapsed_seconds": elapsed,
                "stall_threshold_seconds": config.stall_threshold_seconds,
                "pod_deadline_seconds": config.pod_deadline_seconds,
                "attempts": candidate.attempts,
            },
            detected_counter="stalls_detected",
            notify_config=notify_config,
        )


async def _continuation_clock(session, candidate):
    """Measure the current assigned worker, never a previous worker's start."""
    from .models import OrchestrationAcceptedPlan, OrchestrationExecution, OrchestrationWorkClaim
    from .run_reports import OrchestrationRunReport

    row = (
        await session.execute(
            select(OrchestrationRunReport)
            .join(OrchestrationWorkClaim, OrchestrationWorkClaim.active_run_id == OrchestrationRunReport.run_id)
            .join(OrchestrationExecution, OrchestrationExecution.claim_id == OrchestrationWorkClaim.id)
            .join(OrchestrationAcceptedPlan, OrchestrationAcceptedPlan.flow_id == OrchestrationExecution.flow_id)
            .where(
                OrchestrationRunReport.org_id == candidate.org_id,
                OrchestrationRunReport.flow_id == candidate.flow_id,
                OrchestrationRunReport.node_id == candidate.node_id,
                OrchestrationRunReport.attempt == candidate.attempts,
                OrchestrationWorkClaim.org_id == candidate.org_id,
                OrchestrationWorkClaim.owner_kind == "engine_flow",
                OrchestrationWorkClaim.owner_ref == candidate.flow_id,
                OrchestrationWorkClaim.state == "held",
                OrchestrationWorkClaim.generation == OrchestrationExecution.claim_generation,
                OrchestrationExecution.org_id == candidate.org_id,
                OrchestrationExecution.node_id == candidate.node_id,
                OrchestrationExecution.cycle == candidate.attempts,
                OrchestrationExecution.status.not_in({"concluded", "superseded"}),
                OrchestrationAcceptedPlan.org_id == candidate.org_id,
                OrchestrationAcceptedPlan.version == OrchestrationExecution.accepted_plan_version,
                OrchestrationAcceptedPlan.superseded_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False, None
    if (row.terminal_receipt or {}).get("outcome") in {"complete", "failed"}:
        return True, None
    from .review_recovery import pending_recovery_for_report

    recovery = await pending_recovery_for_report(session, row)
    if recovery is not None:
        return True, recovery.created_at.replace(tzinfo=UTC) if recovery.created_at.tzinfo is None else recovery.created_at
    since = row.created_at
    started = (row.worker_receipt or {}).get("recorded_at")
    if started:
        try:
            since = datetime.fromisoformat(started.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False, None
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return True, since


async def detect_stalls(
    session: AsyncSession,
    now: datetime | None = None,
    *,
    config: StallConfig | None = None,
    notify_config: NotifyConfig | None = None,
) -> StallReport:
    """One detection pass. Find stalled and halted nodes, move them, notify once.

    Pure over the session in the same sense as `run_tick`: no AWS beyond the
    notification publish, no request context, no commit. The caller owns the
    transaction, so detection and the tick's own transitions land together or not
    at all.

    Args:
        session: The session to read and write through. Not committed.
        now: The instant to measure elapsed time against. Injected rather than
            read from the clock so the threshold tests are deterministic.
        config: Thresholds and bounds. Defaults are the derived ones, and
            construction fails closed on an invariant violation.
        notify_config: Where to deliver. Read from the environment when omitted.

    Returns:
        A `StallReport`. `success` is False if any node errored **or** any
        notification failed to deliver.

    Never raises for a per-node failure: it records the error and continues, so one
    bad node cannot stop every other flow from being diagnosed. The failure still
    surfaces via `success` and the error count (R-NF3).
    """
    config = config or StallConfig()
    now = now or datetime.now(UTC)
    report = StallReport()
    after_id: str | None = None

    while report.nodes_examined < _ITEM_BACKSTOP:
        remaining = _ITEM_BACKSTOP - report.nodes_examined
        try:
            page = await _fetch_candidate_page(session, after_id=after_id, limit=min(_PAGE_SIZE, remaining))
        except Exception:
            # A page we cannot read is a real failure, not an empty page. Reporting
            # success here would be the silent stall this module exists to end.
            logger.exception("orchestration stall: failed to fetch candidate page after id=%s", after_id)
            report.errors += 1
            break

        if not page:
            break

        report.pages_read += 1

        for candidate in page:
            report.record(candidate.org_id, "nodes_examined")
            try:
                await _examine(session, candidate, report, now=now, config=config, notify_config=notify_config)
            except Exception:
                # Per-node containment: log, count, force non-success, keep going.
                logger.exception(
                    "orchestration stall: failed to examine node %s (org %s)",
                    candidate.node_id,
                    candidate.org_id,
                )
                report.record(candidate.org_id, "errors")

        after_id = page[-1].node_id

        if len(page) < _PAGE_SIZE:
            break
    else:
        report.truncated = True
        logger.warning("orchestration stall: stopped at the %d-node backstop; more candidates remain", _ITEM_BACKSTOP)

    return report
