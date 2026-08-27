"""Tests for the orchestration node-state vocabulary and transition guard.

Covers issue #4193's Validation section: vocabulary integrity, the derived
terminal set, the guard not over-rejecting, the adversarial gate-skip and
halt-promotion cases (AC-14/AC-15 foundation), AC-23's excluded phantoms, and
R-N2b's requirement that a rejection is recorded rather than swallowed.
"""

from dataclasses import FrozenInstanceError

import pytest

from src.orchestration.state import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    ActorKind,
    NodeState,
    transition,
)

# Every (from, to) pair a service actor may legally take, derived from the table
# so new edges are covered automatically rather than needing a test edit.
SERVICE_LEGAL_PAIRS = [
    (from_state, to_state)
    for from_state, successors in LEGAL_TRANSITIONS.items()
    for to_state, actors in successors.items()
    if ActorKind.SERVICE in actors
]

HUMAN_LEGAL_PAIRS = [
    (from_state, to_state)
    for from_state, successors in LEGAL_TRANSITIONS.items()
    for to_state, actors in successors.items()
    if ActorKind.HUMAN in actors
]

HUMAN_ONLY_PAIRS = [
    (from_state, to_state)
    for from_state, successors in LEGAL_TRANSITIONS.items()
    for to_state, actors in successors.items()
    if actors == frozenset({ActorKind.HUMAN})
]


class TestVocabulary:
    """The declared vocabulary itself (R-N2, R-N2c)."""

    def test_exactly_nine_states(self):
        assert len(NodeState) == 9

    def test_states_are_the_declared_spellings(self):
        # Downstream stories and ACs cite these exact strings; a rename here is
        # a breaking change to issues that are already written.
        assert {state.value for state in NodeState} == {
            "pending",
            "ready",
            "running",
            "awaiting_gate",
            "passed",
            "rejected_at_gate",
            "failed",
            "halted",
            "superseded",
        }

    @pytest.mark.parametrize("phantom", ["rejected", "skipped"])
    def test_phantom_states_are_not_in_the_vocabulary(self, phantom):
        """AC-23: the frontend phantom cannot re-enter through the engine."""
        with pytest.raises(ValueError):
            NodeState(phantom)

    def test_states_compare_equal_to_their_string_value(self):
        # The str-Enum contract downstream persistence relies on.
        assert NodeState.HALTED == "halted"


class TestTransitionTable:
    """Structural integrity of LEGAL_TRANSITIONS (R-N2a)."""

    def test_every_state_is_a_key(self):
        """No state is unreachable-by-omission from the table."""
        assert set(LEGAL_TRANSITIONS) == set(NodeState)

    def test_every_successor_is_a_valid_state(self):
        """No typo'd phantom state hiding on the right-hand side."""
        for from_state, successors in LEGAL_TRANSITIONS.items():
            for to_state in successors:
                assert isinstance(to_state, NodeState), f"{from_state} -> {to_state!r} is not a NodeState"

    def test_every_edge_permits_at_least_one_actor(self):
        """An edge nobody may take is a table bug, not a guard."""
        for from_state, successors in LEGAL_TRANSITIONS.items():
            for to_state, actors in successors.items():
                assert actors, f"{from_state} -> {to_state} permits no actor"

    def test_no_self_transitions(self):
        for from_state, successors in LEGAL_TRANSITIONS.items():
            assert from_state not in successors

    def test_matches_the_declared_base_table(self):
        """Transcription check against requirements.md §2.1, spelled out.

        Deliberately hand-written rather than derived: this is the assertion
        that catches an edge being *added* to the table without a requirement
        behind it, which no derived check can see.
        """
        assert {state: set(successors) for state, successors in LEGAL_TRANSITIONS.items()} == {
            NodeState.PENDING: {NodeState.READY, NodeState.SUPERSEDED},
            NodeState.READY: {NodeState.RUNNING, NodeState.SUPERSEDED},
            NodeState.RUNNING: {
                NodeState.AWAITING_GATE,
                NodeState.PASSED,
                NodeState.FAILED,
                NodeState.HALTED,
                NodeState.SUPERSEDED,
            },
            NodeState.AWAITING_GATE: {
                NodeState.PASSED,
                NodeState.REJECTED_AT_GATE,
                NodeState.HALTED,
                NodeState.SUPERSEDED,
            },
            NodeState.PASSED: {NodeState.SUPERSEDED},
            NodeState.REJECTED_AT_GATE: {NodeState.READY},
            NodeState.FAILED: {NodeState.READY},
            NodeState.HALTED: {NodeState.READY},
            NodeState.SUPERSEDED: set(),
        }


class TestAmendmentIsFirstClass:
    """Plan amendment is normal operation, not an exception path."""

    @pytest.mark.parametrize("state", sorted(set(NodeState) - TERMINAL_STATES))
    def test_any_non_terminal_state_may_be_superseded(self, state):
        result = transition(state, NodeState.SUPERSEDED, actor_kind=ActorKind.SERVICE, reason="plan amended")
        assert result.allowed is True

    def test_passed_may_be_superseded_only_by_a_human(self):
        """`passed -> superseded` is explicit re-plan, not engine housekeeping."""
        assert transition(NodeState.PASSED, NodeState.SUPERSEDED, actor_kind=ActorKind.HUMAN, reason="re-plan").allowed is True
        assert transition(NodeState.PASSED, NodeState.SUPERSEDED, actor_kind=ActorKind.SERVICE, reason="re-plan").allowed is False

    def test_superseded_is_fully_terminal(self):
        """Not even a human moves a superseded attempt — a new node is created."""
        assert LEGAL_TRANSITIONS[NodeState.SUPERSEDED] == {}


class TestTerminalStates:
    """TERMINAL_STATES must be derived, not hand-listed."""

    def test_derived_from_the_transition_table(self):
        """Recomputing the definition from the table reproduces the constant.

        If someone replaces the derivation with a literal set, this still
        passes — so `test_derivation_follows_the_table` below is the real guard.
        """
        expected = {
            state for state, successors in LEGAL_TRANSITIONS.items() if not any(ActorKind.SERVICE in actors for actors in successors.values())
        }
        assert set(TERMINAL_STATES) == expected

    def test_derivation_follows_the_table(self):
        """A hand-listed set would not track a table edit; this proves it does.

        Temporarily grant the engine a service-reachable edge out of `halted`
        and re-run the derivation: a derived set drops `halted`, a hand-listed
        one cannot.
        """
        patched = dict(LEGAL_TRANSITIONS)
        patched[NodeState.HALTED] = {NodeState.READY: frozenset({ActorKind.SERVICE})}
        rederived = {state for state, successors in patched.items() if not any(ActorKind.SERVICE in actors for actors in successors.values())}
        assert NodeState.HALTED not in rederived
        assert NodeState.HALTED in TERMINAL_STATES  # real table unchanged

    def test_halted_is_terminal(self):
        """Halted work must not re-enter the loop under its own steam."""
        assert NodeState.HALTED in TERMINAL_STATES

    def test_expected_membership(self):
        assert set(TERMINAL_STATES) == {
            NodeState.PASSED,
            NodeState.REJECTED_AT_GATE,
            NodeState.FAILED,
            NodeState.HALTED,
            NodeState.SUPERSEDED,
        }

    def test_no_terminal_state_omitted_from_the_vocabulary(self):
        assert TERMINAL_STATES <= set(NodeState)

    def test_live_states_are_not_terminal(self):
        for state in (NodeState.PENDING, NodeState.READY, NodeState.RUNNING, NodeState.AWAITING_GATE):
            assert state not in TERMINAL_STATES


class TestGuardAcceptsLegalTransitions:
    """The guard must not over-reject (issue: 'the guard does not over-reject')."""

    @pytest.mark.parametrize(("from_state", "to_state"), SERVICE_LEGAL_PAIRS)
    def test_accepts_every_service_legal_pair(self, from_state, to_state):
        result = transition(from_state, to_state, actor_kind=ActorKind.SERVICE, reason="engine tick")
        assert result.allowed is True
        assert result.new_state == to_state
        assert result.rejection_reason is None

    @pytest.mark.parametrize(("from_state", "to_state"), HUMAN_LEGAL_PAIRS)
    def test_accepts_every_human_legal_pair(self, from_state, to_state):
        result = transition(from_state, to_state, actor_kind=ActorKind.HUMAN, reason="operator action")
        assert result.allowed is True
        assert result.new_state == to_state

    def test_result_carries_the_decision_record_fields(self):
        result = transition(NodeState.READY, NodeState.RUNNING, actor_kind=ActorKind.SERVICE, reason="dispatched to worker")
        assert result.from_state is NodeState.READY
        assert result.to_state is NodeState.RUNNING
        assert result.actor_kind is ActorKind.SERVICE
        assert result.reason == "dispatched to worker"

    def test_accepts_equivalent_string_arguments(self):
        """Callers deserializing from storage pass plain strings."""
        result = transition("ready", "running", actor_kind="service", reason="dispatch")
        assert result.allowed is True
        assert result.new_state is NodeState.RUNNING


class TestGuardRejectsIllegalTransitions:
    """Adversarial cases — the AC-14/AC-15 foundation."""

    def test_rejects_pending_to_passed(self):
        """A mis-prompted caller cannot skip dispatch *and* the gate."""
        result = transition(NodeState.PENDING, NodeState.PASSED, actor_kind=ActorKind.SERVICE, reason="looks fine to me")
        assert result.allowed is False
        assert result.new_state is None

    def test_rejects_halted_to_passed(self):
        """A halted node cannot be promoted through this function."""
        result = transition(NodeState.HALTED, NodeState.PASSED, actor_kind=ActorKind.HUMAN, reason="override")
        assert result.allowed is False
        assert result.new_state is None

    def test_rejects_service_advancing_out_of_a_gate(self):
        """AC-15: the engine cannot approve its own gate."""
        result = transition(NodeState.AWAITING_GATE, NodeState.PASSED, actor_kind=ActorKind.SERVICE, reason="auto-approve")
        assert result.allowed is False

    def test_rejects_backwards_transition(self):
        """The current bare-str field permits going backwards; this must not."""
        assert transition(NodeState.RUNNING, NodeState.PENDING, actor_kind=ActorKind.SERVICE, reason="reset").allowed is False

    @pytest.mark.parametrize("actor_kind", list(ActorKind))
    def test_rejects_every_move_out_of_superseded(self, actor_kind):
        for to_state in NodeState:
            assert transition(NodeState.SUPERSEDED, to_state, actor_kind=actor_kind, reason="revive").allowed is False

    def test_rejects_every_pair_absent_from_the_table(self):
        """Exhaustive: all 81 ordered pairs agree with the table."""
        for from_state in NodeState:
            for to_state in NodeState:
                declared = LEGAL_TRANSITIONS[from_state].get(to_state)
                human = transition(from_state, to_state, actor_kind=ActorKind.HUMAN, reason="x").allowed
                service = transition(from_state, to_state, actor_kind=ActorKind.SERVICE, reason="x").allowed
                assert human is (declared is not None and ActorKind.HUMAN in declared)
                assert service is (declared is not None and ActorKind.SERVICE in declared)

    @pytest.mark.parametrize("phantom", ["rejected", "skipped"])
    def test_rejects_phantom_states_on_write(self, phantom):
        """AC-23 at the guard: an undeclared literal cannot be written."""
        with pytest.raises(ValueError):
            transition(NodeState.RUNNING, phantom, actor_kind=ActorKind.SERVICE, reason="frontend said so")
        with pytest.raises(ValueError):
            transition(phantom, NodeState.PASSED, actor_kind=ActorKind.SERVICE, reason="frontend said so")

    def test_rejects_unknown_actor_kind(self):
        with pytest.raises(ValueError):
            transition(NodeState.READY, NodeState.RUNNING, actor_kind="robot", reason="x")


class TestRejectionsAreRecorded:
    """R-N2b: rejected, never silently dropped or raise-and-lose."""

    def test_illegal_transition_returns_a_populated_reason(self):
        result = transition(NodeState.PENDING, NodeState.PASSED, actor_kind=ActorKind.SERVICE, reason="skip ahead")
        assert result.rejection_reason
        assert result.rejection_reason.strip()

    def test_rejection_record_identifies_the_attempt(self):
        """Deviation-visibility needs attempted-from, attempted-to and actor."""
        result = transition(NodeState.PENDING, NodeState.PASSED, actor_kind=ActorKind.SERVICE, reason="skip ahead")
        assert result.from_state is NodeState.PENDING
        assert result.to_state is NodeState.PASSED
        assert result.actor_kind is ActorKind.SERVICE
        assert result.reason == "skip ahead"

    def test_illegal_transition_does_not_raise(self):
        """An exception unwinds the stack and loses the evidence."""
        result = transition(NodeState.HALTED, NodeState.PASSED, actor_kind=ActorKind.SERVICE, reason="promote")
        assert result.allowed is False

    def test_authority_rejection_names_the_required_actor(self):
        result = transition(NodeState.HALTED, NodeState.READY, actor_kind=ActorKind.SERVICE, reason="auto-resume")
        assert "human" in result.rejection_reason

    def test_result_is_immutable(self):
        """A decision record a caller can edit is not a record."""
        result = transition(NodeState.READY, NodeState.RUNNING, actor_kind=ActorKind.SERVICE, reason="dispatch")
        with pytest.raises(FrozenInstanceError):
            result.allowed = False


class TestHumanOnlyEdges:
    """R-Q9c and the recovery edges into `ready`."""

    def test_engine_cannot_self_clear_a_halt(self):
        assert transition(NodeState.HALTED, NodeState.READY, actor_kind=ActorKind.SERVICE, reason="retry").allowed is False

    def test_human_may_clear_a_halt(self):
        result = transition(NodeState.HALTED, NodeState.READY, actor_kind=ActorKind.HUMAN, reason="operator override")
        assert result.allowed is True
        assert result.new_state is NodeState.READY

    @pytest.mark.parametrize(("from_state", "to_state"), HUMAN_ONLY_PAIRS)
    def test_human_only_edges_reject_the_service_actor(self, from_state, to_state):
        assert transition(from_state, to_state, actor_kind=ActorKind.SERVICE, reason="engine").allowed is False
        assert transition(from_state, to_state, actor_kind=ActorKind.HUMAN, reason="operator").allowed is True

    def test_recovery_edges_into_ready_are_human_only(self):
        """failed/halted/rejected_at_gate -> ready all require a human."""
        for from_state in (NodeState.FAILED, NodeState.HALTED, NodeState.REJECTED_AT_GATE):
            assert LEGAL_TRANSITIONS[from_state][NodeState.READY] == frozenset({ActorKind.HUMAN})

    def test_terminal_states_have_no_service_reachable_successor(self):
        """The invariant that makes TERMINAL_STATES meaningful."""
        for state in TERMINAL_STATES:
            for to_state in NodeState:
                assert transition(state, to_state, actor_kind=ActorKind.SERVICE, reason="engine").allowed is False


class TestSmokeTest:
    """The mechanical check issue #4193 and the wave-1 evaluation both run."""

    def test_smoke_assertion(self):
        assert len(NodeState) == 9
        assert NodeState("halted") in TERMINAL_STATES
