"""Delivery-loop node states and the legal transitions between them.

This module is the **single** declared vocabulary for orchestration-graph nodes
(run/stage/wave) and the **single** guarded entry point for changing a node's
state. Requirement R-N2a is explicit that a second copy of either the vocabulary
or the table anywhere is a requirement violation, not a style preference: the
existing run-status classification sets drifted three ways inside a set whose own
comment claimed drift was impossible (`activity/service.py` vs
`activity/stats_service.py`). Import from here; do not re-derive.

Pure rules — no I/O, no database, no request context. Persisting the decision
record for a rejected transition belongs to the store; this module only produces
the record.

Scope note: this is **not** the DynamoDB `webhook-events` status vocabulary
(`webhook_received`, `in_progress`, `complete`, ...). Those literals are a
different concern with different writers, and conflating the two is how the
current drift happened. Mapping between the two vocabularies is R-O1c's job.

Requirements: R-N2 (vocabulary + table), R-N2a (declared once), R-N2b (illegal
transitions rejected *and* recorded), R-N2c (`rejected`/`skipped` excluded),
R-O4b (resume), R-Q9c (human-only halt override).
"""

from dataclasses import dataclass
from enum import StrEnum


class NodeState(StrEnum):
    """The declared states of an orchestration-graph node (R-N2).

    `rejected` and `skipped` are deliberately absent (R-N2c): `rejected` is a
    frontend-only phantom with no backend writer, and `skipped` has no writer
    either. Constructing them raises `ValueError`, which is the point — a
    surface still emitting them is a bug to fix, not a state to support.
    """

    PENDING = "pending"  # Node exists on the graph; predecessors not satisfied
    READY = "ready"  # Predecessors satisfied; not yet dispatched
    RUNNING = "running"  # Dispatched; execution in flight
    AWAITING_MERGE = "awaiting_merge"  # Worker finished; merged code/checks not yet verified
    AWAITING_GATE = "awaiting_gate"  # Execution finished; human decision required
    PASSED = "passed"  # Accepted (gate approved, or evaluation green)
    REJECTED_AT_GATE = "rejected_at_gate"  # Human refused; successors stay pending
    FAILED = "failed"  # Execution failed
    HALTED = "halted"  # Defect-cycle bound exhausted (R-Q9c)
    SUPERSEDED = "superseded"  # Replaced by a newer attempt of the same node


class ActorKind(StrEnum):
    """Who is attempting a transition.

    The distinction is load-bearing, not descriptive: the recovery edges into
    `READY` are human-only so that the engine can never self-clear a halt
    (R-Q9c), and it is what makes `TERMINAL_STATES` derivable.
    """

    HUMAN = "human"  # An operator acting through an interactive control
    SERVICE = "service"  # The engine itself (tick, dispatch, evaluation)


# Engine-normal edges. A human operator has at least the engine's authority, so
# these permit both; the asymmetry lives in _HUMAN_ONLY below.
_ENGINE_OR_HUMAN = frozenset({ActorKind.SERVICE, ActorKind.HUMAN})

# Edges the engine must never take on its own.
_HUMAN_ONLY = frozenset({ActorKind.HUMAN})


# The transition table, declared as data rather than `if` branches so it can be
# asserted against and rendered. Transcribed from requirements.md §2.1; any pair
# absent from this mapping is illegal by construction.
#
# Each successor carries the actor kinds permitted to make that move. The
# amendment class (any non-terminal state -> SUPERSEDED) is first-class here:
# plan amendment is normal operation, not an exception path.
LEGAL_TRANSITIONS: dict[NodeState, dict[NodeState, frozenset[ActorKind]]] = {
    NodeState.PENDING: {
        NodeState.AWAITING_MERGE: _HUMAN_ONLY,  # Verified historical delivery, no worker
        NodeState.READY: _ENGINE_OR_HUMAN,  # Predecessors satisfied
        NodeState.SUPERSEDED: _ENGINE_OR_HUMAN,  # Amendment
    },
    NodeState.READY: {
        NodeState.AWAITING_MERGE: _HUMAN_ONLY,  # Verified historical delivery, no worker
        NodeState.RUNNING: _ENGINE_OR_HUMAN,  # Dispatch
        NodeState.SUPERSEDED: _ENGINE_OR_HUMAN,  # Amendment
    },
    NodeState.RUNNING: {
        NodeState.AWAITING_MERGE: _ENGINE_OR_HUMAN,
        NodeState.AWAITING_GATE: _ENGINE_OR_HUMAN,  # Finished; needs a human decision
        NodeState.PASSED: _ENGINE_OR_HUMAN,  # Evaluation green — no gate required
        NodeState.FAILED: _ENGINE_OR_HUMAN,  # Execution failed
        NodeState.HALTED: _ENGINE_OR_HUMAN,  # Defect-cycle bound exhausted (R-Q9c)
        NodeState.SUPERSEDED: _ENGINE_OR_HUMAN,  # Amendment
    },
    NodeState.AWAITING_MERGE: {
        NodeState.READY: _HUMAN_ONLY,  # Explicit retry when work needs correction
        NodeState.PASSED: _ENGINE_OR_HUMAN,
        NodeState.FAILED: _ENGINE_OR_HUMAN,
        NodeState.SUPERSEDED: _ENGINE_OR_HUMAN,
    },
    NodeState.AWAITING_GATE: {
        # Gate approval and refusal are human acts by definition; a service
        # actor advancing out of a gate IS the gate-skip that AC-15 forbids.
        NodeState.PASSED: _HUMAN_ONLY,  # Gate approved
        NodeState.REJECTED_AT_GATE: _HUMAN_ONLY,  # Gate refused
        NodeState.HALTED: _ENGINE_OR_HUMAN,  # Defect-cycle bound exhausted (R-Q9c)
        NodeState.SUPERSEDED: _ENGINE_OR_HUMAN,  # Amendment
    },
    # --- Terminal-to-the-engine states. Every remaining edge is human-only,
    # --- which is exactly what makes TERMINAL_STATES derivable below.
    NodeState.PASSED: {
        NodeState.SUPERSEDED: _HUMAN_ONLY,  # Only via explicit re-plan
    },
    NodeState.REJECTED_AT_GATE: {
        NodeState.READY: _HUMAN_ONLY,  # Human re-open only
    },
    NodeState.FAILED: {
        NodeState.READY: _HUMAN_ONLY,  # Resume/retry (R-O4b); increments attempts
        NodeState.RUNNING: _HUMAN_ONLY,  # Resume the same verified continuation after an outer timeout
    },
    NodeState.HALTED: {
        NodeState.READY: _HUMAN_ONLY,  # Human override only (R-Q9c)
    },
    NodeState.SUPERSEDED: {},  # Fully terminal — a superseded attempt never moves again
}


# Derived, never hand-listed. A hand-maintained parallel set is precisely how the
# existing status sets drifted, so the only safe definition is a computed one.
#
# "Terminal" means terminal *to the engine*: no successor is reachable by a
# service actor. `FAILED`, `HALTED` and `REJECTED_AT_GATE` remain
# human-recoverable via the edges above while still being terminal here — the
# engine cannot move them, so it cannot resurrect halted work into the loop.
TERMINAL_STATES: frozenset[NodeState] = frozenset(
    state for state, successors in LEGAL_TRANSITIONS.items() if not any(ActorKind.SERVICE in actors for actors in successors.values())
)


@dataclass(frozen=True)
class TransitionResult:
    """The outcome of one transition attempt — legal or not.

    A rejected attempt is returned, not raised, so the caller always has
    something to persist (R-N2b): under RULING 5 recorded rejections are the
    primary detector for off-plan agent activity, and an exception that unwinds
    the stack loses the evidence.
    """

    allowed: bool
    from_state: NodeState
    to_state: NodeState
    actor_kind: ActorKind
    reason: str
    # The node's state after the attempt: `to_state` when allowed, otherwise
    # None. Callers must not assume the attempt succeeded.
    new_state: NodeState | None = None
    # Non-empty exactly when `allowed` is False, so deviation-visibility always
    # has something to render.
    rejection_reason: str | None = None


def transition(from_state: NodeState, to_state: NodeState, *, actor_kind: ActorKind, reason: str) -> TransitionResult:
    """Guard one node state change. Every engine state change goes through here.

    Args:
        from_state: The node's current state.
        to_state: The state being requested.
        actor_kind: Who is attempting it. Human-only edges reject `SERVICE`.
        reason: Why, for the decision record. Carried through verbatim.

    Returns:
        A `TransitionResult`. On success `allowed` is True and `new_state` is
        `to_state`; on rejection `allowed` is False, `new_state` is None, and
        `rejection_reason` is populated and non-empty.

    Raises:
        ValueError: If `from_state`/`to_state` is not a declared `NodeState` or
            `actor_kind` is not a declared `ActorKind`. Coercing here makes this
            function the one place an undeclared literal (`rejected`, `skipped`)
            can be caught on write (R-N2c).
    """
    from_state = NodeState(from_state)
    to_state = NodeState(to_state)
    actor_kind = ActorKind(actor_kind)

    permitted_actors = LEGAL_TRANSITIONS[from_state].get(to_state)

    if permitted_actors is None:
        return TransitionResult(
            allowed=False,
            from_state=from_state,
            to_state=to_state,
            actor_kind=actor_kind,
            reason=reason,
            rejection_reason=f"no legal transition from '{from_state}' to '{to_state}'",
        )

    if actor_kind not in permitted_actors:
        allowed_names = ", ".join(sorted(actor.value for actor in permitted_actors))
        return TransitionResult(
            allowed=False,
            from_state=from_state,
            to_state=to_state,
            actor_kind=actor_kind,
            reason=reason,
            rejection_reason=f"transition from '{from_state}' to '{to_state}' requires actor_kind in ({allowed_names}); got '{actor_kind}'",
        )

    return TransitionResult(
        allowed=True,
        from_state=from_state,
        to_state=to_state,
        actor_kind=actor_kind,
        reason=reason,
        new_state=to_state,
    )
