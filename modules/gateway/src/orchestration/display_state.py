"""The nine→five node projection and the flow status derived from it.

Issue #4869. The frontend has had this projection since #4212
(`frontend/src/utils/nodeState.ts`); the flows-list endpoint needs the same
mapping *in SQL*, because per-flow counts have to be aggregated by the database
rather than reduced in Python over every node of every flow on the page.

Two copies of a state mapping is exactly the drift `state.py` was written to end,
so this module is the backend's only copy and
`tests/orchestration/test_display_state_parity.py` asserts it agrees with the
TypeScript one key for key.

**The `FILTER` state lists are derived, never hand-listed.** `DISPLAY_TO_ENGINE`
is computed by inverting `ENGINE_TO_DISPLAY`, so adding a tenth engine state
cannot leave a bucket silently missing it. It also means `superseded` is excluded
**structurally**: it maps to `None`, so it appears in no bucket's list, so it
matches no `COUNT(*) FILTER` predicate. There is deliberately no
`state != 'superseded'` clause anywhere — a predicate is something a later edit
can drop, whereas absence from the mapping cannot be dropped by accident.

Current delivery execution can override the base state: a blocked reviewer may
leave its node `running`, and a capacity wait is queued. `progress_projection.py`
applies those facts before list, wave and graph aggregation. Historical stall
decisions remain audit records and do not add to current counts.
"""

from enum import StrEnum

from .state import NodeState

__all__ = [
    "DISPLAY_STATE_ORDER",
    "DISPLAY_TO_ENGINE",
    "ENGINE_TO_DISPLAY",
    "NODE_STATE_OVERRIDES_EXECUTION",
    "DisplayState",
    "FlowStatus",
    "derive_flow_status",
]


class DisplayState(StrEnum):
    """The five states an operator is shown. A closed vocabulary (§1.3)."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    GATE = "gate"
    STALLED = "stalled"
    COMPLETE = "complete"


class FlowStatus(StrEnum):
    """The one-word answer to "what is happening with this flow".

    An unarchived flow with no nodes is not "queued" — nothing is
    waiting on anything, the plan simply compiled to an empty graph. Rendering it
    as queued would have an operator waiting for work that will never start.
    """

    ATTENTION_NEEDED = "attention_needed"  # Rejection, failure, halt or retry stall
    AWAITING_YOU = "awaiting_you"  # A gate needs a human decision
    RUNNING = "running"  # Work is in flight
    QUEUED = "queued"  # Work exists, none of it has started
    COMPLETE = "complete"  # Every node is done
    EMPTY = "empty"  # No nodes at all
    ARCHIVED = "archived"  # Explicitly hidden work, regardless of prior node state


# The projection, verbatim from `frontend/src/utils/nodeState.ts`. Every one of
# the nine `NodeState` members appears exactly once, which the parity test
# asserts — an unmapped state would be counted into no bucket and silently
# understate a flow's size.
ENGINE_TO_DISPLAY: dict[NodeState, DisplayState | None] = {
    NodeState.PENDING: DisplayState.QUEUED,
    NodeState.READY: DisplayState.QUEUED,
    NodeState.RUNNING: DisplayState.IN_PROGRESS,
    NodeState.AWAITING_MERGE: DisplayState.IN_PROGRESS,
    NodeState.AWAITING_GATE: DisplayState.GATE,
    NodeState.PASSED: DisplayState.COMPLETE,
    NodeState.WAIVED: DisplayState.COMPLETE,  # Concluded by exception, never a test PASS
    NodeState.REJECTED_AT_GATE: DisplayState.STALLED,
    NodeState.FAILED: DisplayState.STALLED,
    NodeState.HALTED: DisplayState.STALLED,
    # Replaced by a newer attempt of the same node. Counting it would make one
    # piece of work appear twice and push a flow's total past its real node count.
    NodeState.SUPERSEDED: None,
}

# These node states own the displayed result even when an older execution is
# blocked. Their execution lineage cannot change the SQL display bucket. The
# delivery-progress card also uses this verdict except when a passed story
# still owes deployment/evaluation; plan_lineage retains that live history.
# Keep this narrower than "all non-running states": READY can show an ownership
# hold and a RUNNING node can be waiting for capacity or reviewer input.
NODE_STATE_OVERRIDES_EXECUTION = ("passed", "superseded", "failed", "halted", "rejected_at_gate", "awaiting_gate")


# Render completed work first so progress grows from left to right. Matches
# `DISPLAY_STATE_ORDER` in `nodeState.ts`; the parity test keeps the list and
# graph rollup segments in the same order.
DISPLAY_STATE_ORDER: tuple[DisplayState, ...] = (
    DisplayState.COMPLETE,
    DisplayState.IN_PROGRESS,
    DisplayState.GATE,
    DisplayState.STALLED,
    DisplayState.QUEUED,
)


def _invert() -> dict[DisplayState, tuple[str, ...]]:
    """Invert `ENGINE_TO_DISPLAY` into the state lists the SQL `FILTER`s use.

    Computed rather than written out: the two directions of a hand-maintained
    pair of dicts drift, and the drift is invisible because both halves keep
    type-checking. States mapping to `None` fall out here, which is what excludes
    `superseded` from every bucket without a predicate mentioning it.
    """
    buckets: dict[DisplayState, list[str]] = {display: [] for display in DisplayState}
    for engine, display in ENGINE_TO_DISPLAY.items():
        if display is not None:
            buckets[display].append(engine.value)
    return {display: tuple(sorted(states)) for display, states in buckets.items()}


# `{display state: the engine states that count into it}`, for `COUNT(*) FILTER`.
DISPLAY_TO_ENGINE: dict[DisplayState, tuple[str, ...]] = _invert()


def derive_flow_status(
    *,
    queued: int,
    in_progress: int,
    gate: int,
    stalled: int,
    complete: int,
) -> FlowStatus:
    """Reduce a flow's five bucket counts to one status. First match wins.

    The ordering is the whole content of this function, and it is worst-news-first
    on purpose: a flow that has a stalled node *and* running work is reported as
    needing attention, because the running work will not clear the stall and an
    operator scanning the list for problems must see it. Reporting it as
    `running` would hide the stall behind the reassuring word.

    `gate` outranks `in_progress` for the same reason: a gate is blocked on a
    human, and a human who is not told will not act.

    Both this function and the status-chip counts call it, so a chip can never
    disagree with the rows it claims to count — one derivation, two callers.
    Note that "needs me" is **not** `status in (attention_needed, awaiting_you)`:
    a flow with a stall and a gate is one row with one status but satisfies the
    needs-me filter on either ground, so that predicate is computed separately
    from `gate > 0 or stalled > 0`.
    """
    if stalled > 0:
        return FlowStatus.ATTENTION_NEEDED
    if gate > 0:
        return FlowStatus.AWAITING_YOU
    if in_progress > 0:
        return FlowStatus.RUNNING
    if queued > 0:
        return FlowStatus.QUEUED
    if complete > 0:
        # Reached only when every bucket above is zero, i.e. everything is done.
        return FlowStatus.COMPLETE
    # No nodes in any bucket. Either the flow has no nodes, or every node it has
    # is superseded — which is the same news to an operator: there is nothing
    # live here.
    return FlowStatus.EMPTY
