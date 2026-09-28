"""The gate-placement guidance an author reads matches what the engine does (#4529).

The AIDLC persona/skill tell an authoring agent where to put a plan's human stops.
`modules/agent-factory/agent-worker-image/tests/test_gate_placement_guidance.py`
pins that the guidance *reaches the worker image*. This file pins the other half:
that the guidance is **true of this code**.

Both halves are needed and neither implies the other. Before #4529 the skill said
registration inserts "a gate at every wave boundary" for a gateless proposal. That
text staged into the image perfectly — the staging test would have passed — and it
was false, because the transform is off unless an operator opts in. An author who
believed it shipped a plan that ran every wave, including a deploy wave, with no
human stop after acceptance.

So the failure mode this file exists for is *drift*, in either direction:

  - Someone flips `gate_every_wave_enabled`'s default to True, or renames the flag,
    and the skill's "OFF by default / do not rely on it" becomes stale advice that
    over-gates every plan.
  - Someone relaxes `insert_wave_gates`'s all-or-nothing condition into a top-up,
    and the skill's "declare all of them" becomes needless work.

A doc that is merely out of date is worse than a missing doc here, because an author
follows it confidently. These assertions read the real functions (and the real
markdown) so that a change to either one that contradicts the other fails a test
instead of silently misleading the next authoring run.

Deliberately NOT asserted: prose or wording. The staging test owns "is the guidance
present"; this file owns "is it accurate". The claims are located by the flag name
and by short factual phrases, and each is checked against behaviour, not text.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from src.orchestration import registration
from src.orchestration.proposal import LoopProposal, NodeKind, validate_proposal

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SKILL = _REPO_ROOT / "modules/agent-factory/skills/aidlc-emit-issues/SKILL.md"
_PERSONA = _REPO_ROOT / "modules/agent-factory/rules/personas/aidlc.md"


def _flat(text: str) -> str:
    """Whitespace-normalised, because these are hard-wrapped markdown documents."""
    return " ".join(text.split())


@pytest.fixture(scope="module")
def skill_text() -> str:
    assert _SKILL.exists(), f"the emission skill moved; update this test: {_SKILL}"
    return _flat(_SKILL.read_text())


@pytest.fixture(scope="module")
def persona_text() -> str:
    assert _PERSONA.exists(), f"the aidlc persona moved; update this test: {_PERSONA}"
    return _flat(_PERSONA.read_text())


class TestTheDocumentedFlagIsTheRealFlag:
    """The skill names an env var so an author can check their environment. A name
    that does not resolve to anything sends them to look for a variable that has no
    effect, and they conclude their waves are gated when nothing reads it."""

    def test_the_flag_name_in_the_skill_is_the_one_the_code_reads(self, skill_text: str) -> None:
        assert registration.AUTONOMY_FLAG_ENV in skill_text, (
            f"the skill must name {registration.AUTONOMY_FLAG_ENV!r}; if the flag was renamed, update the skill and the persona too"
        )

    def test_the_helper_named_in_the_skill_exists(self, skill_text: str) -> None:
        """The skill cites `gate_every_wave_enabled()` by name as the thing that
        returns False when unset. A stale citation is an author's dead end."""
        assert "gate_every_wave_enabled()" in skill_text
        assert callable(registration.gate_every_wave_enabled)


class TestOffByDefaultIsTrue:
    """The load-bearing claim: an unset environment does not gate your waves."""

    def test_unset_means_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(registration.AUTONOMY_FLAG_ENV, raising=False)
        assert registration.gate_every_wave_enabled() is False, (
            "the skill tells authors this returns False when unset and to declare "
            "their own gates. If the default changed, the guidance must change with it."
        )

    def test_both_documents_state_the_default(self, skill_text: str, persona_text: str) -> None:
        # Paired with the behavioural assertion above so the docs cannot keep saying
        # "off by default" after the default flips.
        assert "OFF by default" in skill_text
        assert "OFF by default" in persona_text


class TestAllOrNothingIsTrue:
    """The second claim: declaring one gate suppresses the transform entirely, so an
    author must declare every gate they want rather than seeding one."""

    def _proposal(self, *, with_gate: bool) -> LoopProposal:
        flow = "conformance-flow"
        epic = "epic-1"

        def addr(rest: str) -> str:
            return f"{flow}/{epic}/{rest}"

        nodes = [
            {"address": addr("wave-1/story-a"), "kind": "story", "title": "Wave 1 story", "issue_ref": "1"},
            {"address": addr("wave-1/eval"), "kind": "eval", "title": "Wave 1 evaluation"},
            {"address": addr("wave-2/story-b"), "kind": "story", "title": "Wave 2 story", "issue_ref": "2"},
            {"address": addr("wave-2/eval"), "kind": "eval", "title": "Wave 2 evaluation"},
            {"address": addr("wave-3/story-c"), "kind": "story", "title": "Wave 3 story", "issue_ref": "3"},
            {"address": addr("wave-3/eval"), "kind": "eval", "title": "Wave 3 evaluation"},
        ]
        edges = [
            {"from_address": addr("wave-1/story-a"), "to_address": addr("wave-1/eval")},
            {"from_address": addr("wave-1/eval"), "to_address": addr("wave-2/story-b")},
            {"from_address": addr("wave-2/story-b"), "to_address": addr("wave-2/eval")},
            {"from_address": addr("wave-2/eval"), "to_address": addr("wave-3/story-c")},
            {"from_address": addr("wave-3/story-c"), "to_address": addr("wave-3/eval")},
        ]
        if with_gate:
            # Exactly the shape Step 7f documents: a gate guarding entry to a wave,
            # with the inbound edge rerouted through it.
            nodes.append({"address": addr("wave-3/deploy-gate"), "kind": "gate", "title": "Human gate: approve deploy"})
            edges = [e for e in edges if e["to_address"] != addr("wave-3/story-c")]
            edges += [
                {"from_address": addr("wave-2/eval"), "to_address": addr("wave-3/deploy-gate")},
                {"from_address": addr("wave-3/deploy-gate"), "to_address": addr("wave-3/story-c")},
            ]
        proposal = LoopProposal.model_validate(
            {
                "flow_slug": flow,
                "title": "Conformance",
                "org_id": "org-conformance",
                "spec_revision": "r1",
                "nodes": nodes,
                "edges": edges,
            }
        )
        # The premise: whatever we assert about the transform, the input the skill
        # tells authors to write is itself a well-formed document.
        assert validate_proposal(proposal) == []
        return proposal

    def test_the_documented_gate_shape_is_valid(self) -> None:
        """Guards the guidance's example, not the transform.

        If Step 7f's node/edge shape did not validate, every author following it
        would get a rejection at registration — the worst possible outcome for
        guidance whose whole purpose is to get gates declared.
        """
        self._proposal(with_gate=True)

    def test_declaring_one_gate_suppresses_the_transform(self) -> None:
        proposal = self._proposal(with_gate=True)
        result = registration.insert_wave_gates(proposal)
        gates = [n for n in result.nodes if n.kind == NodeKind.GATE.value]
        assert len(gates) == 1, (
            "the skill tells authors the transform is all-or-nothing and that "
            "declaring one gate does not top up the rest. If it now tops up, the "
            "'declare all of them' instruction is wrong and must be rewritten."
        )

    def test_a_gateless_proposal_is_the_case_the_transform_acts_on(self) -> None:
        """The contrast case. Without it, the test above would also pass if
        `insert_wave_gates` had become a no-op for every input, which would make the
        skill's all-or-nothing warning true-but-vacuous rather than accurate."""
        proposal = self._proposal(with_gate=False)
        assert [n for n in proposal.nodes if n.kind == NodeKind.GATE.value] == []
        result = registration.insert_wave_gates(proposal)
        assert [n for n in result.nodes if n.kind == NodeKind.GATE.value], (
            "insert_wave_gates no longer gates a gateless proposal at all; the skill's description of the transform is now wrong"
        )

    def test_the_condition_is_still_a_presence_check_on_declared_gates(self) -> None:
        """Pins *why* it is all-or-nothing, at the source level.

        The behavioural tests above would keep passing if the condition were
        rewritten to, say, skip only fully-gated proposals — a change that would make
        'declaring one does not top up the rest' false for the three-wave plan an
        author is most likely to write, while still passing a one-gate example.
        """
        source = inspect.getsource(registration.insert_wave_gates)
        assert "if any(" in source and "GATE" in source

    def test_both_documents_state_the_all_or_nothing_rule(self, skill_text: str) -> None:
        assert "all-or-nothing" in skill_text
        assert "declaring one does not top up the rest" in skill_text


class TestTheAcceptanceGateIsNotAWaveGate:
    """The conflation that let the original gap hide: the acceptance gate genuinely
    is always inserted, so 'a gate is always added' sounds reassuring and is not."""

    def test_the_two_gate_refs_are_distinct(self) -> None:
        assert registration.ACCEPTANCE_GATE_REF != registration.WAVE_GATE_REF

    def test_the_skill_scopes_the_acceptance_gate_to_whether_the_plan_runs(self, skill_text: str) -> None:
        assert "may this plan run at all" in skill_text
