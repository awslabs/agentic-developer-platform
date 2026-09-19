"""Gate-placement authoring guidance reaches the RUNTIME instructions (#4529).

The AIDLC persona proposes where a plan's human stops go. That guidance is only real
if it reaches the worker, and the worker does not read `modules/agent-factory/`: it
reads a flat tree assembled by `stage-personas.sh` at image build time (Dockerfile
stage 2). So every assertion here runs the **real staging script** over the **real
source trees** and then reads the **staged** copies — asserting on the source files
would pass for a guidance change that the image never ships.

Why this file exists at all, rather than trusting a source-level grep: the two
things it pins are both facts that were WRONG in the checked-in text before #4529,
and both were wrong in the direction of a missing human stop.

  1. `ORCHESTRATION_AUTONOMY_GATE_EVERY_WAVE` defaults to **off**
     (`registration.gate_every_wave_enabled` returns False when unset). The skill
     previously told authors registration "inserts ... a gate at every wave
     boundary", so an author who followed it and declared no gates shipped a plan
     that runs every wave — including a deploy wave — with no human stop after
     acceptance.
  2. `insert_wave_gates` is **all-or-nothing**: it no-ops entirely if the proposal
     declares any gate. So "declare one gate and the default tops up the rest" is
     false, and an author who believes it under-gates a plan by declaring exactly
     the one gate they were most sure about.

Both are load-bearing for a human's oversight of an autonomous plan, which is why
they are pinned here rather than left to review.

The assertions are deliberately about *substance* (the off-by-default fact, the
all-or-nothing fact, the code-and-tests-is-not-a-gate rule, the reversibility
heuristics, the amendment path) and not about exact prose. A wording change should
not fail this file; deleting the warning should.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
STAGE_SCRIPT = HERE.parent / "stage-personas.sh"
MODULE_ROOT = HERE.parents[1]


@pytest.fixture(scope="module")
def staged(tmp_path_factory) -> Path:
    """The real source trees, put through the real staging script once.

    Module-scoped because staging the whole persona/skill tree is the expensive part
    and every test here reads the same output. Read-only by construction: no test
    writes into the staged tree.
    """
    root = tmp_path_factory.mktemp("stage-gate-placement")
    source, stage = root / "source", root / "stage"
    shutil.copytree(MODULE_ROOT / "rules/personas", source / "agent-factory/personas")
    shutil.copytree(MODULE_ROOT / "skills", source / "agent-factory/skills")
    result = subprocess.run(
        ["bash", str(STAGE_SCRIPT), str(source), str(stage)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return stage


def flat(text: str) -> str:
    """Every run of whitespace collapsed to one space.

    Prose assertions run against this rather than the raw file because these are
    hard-wrapped markdown documents: a sentence an author must not delete is routinely
    split mid-phrase by a line break, and a raw substring check would then fail for a
    reflow while passing for a deletion — exactly backwards. Assertions about
    *structure* (table header rows, JSON keys) stay on the raw text, where the line
    breaks are meaningful.
    """
    return " ".join(text.split())


@pytest.fixture(scope="module")
def skill_text(staged: Path) -> str:
    """The staged emission skill, whitespace-normalised — what the worker loads."""
    path = staged / "skills" / "aidlc-emit-issues" / "SKILL.md"
    assert path.exists(), f"the emission skill did not reach the staged tree: {sorted((staged / 'skills').iterdir())}"
    return flat(path.read_text())


@pytest.fixture(scope="module")
def skill_raw(staged: Path) -> str:
    """The staged emission skill, verbatim. For structural assertions only."""
    return (staged / "skills" / "aidlc-emit-issues" / "SKILL.md").read_text()


@pytest.fixture(scope="module")
def persona_text(staged: Path) -> str:
    """The staged AIDLC persona, whitespace-normalised — what the worker loads."""
    path = staged / "personas" / "aidlc.md"
    assert path.exists(), f"the aidlc persona did not reach the staged tree: {sorted((staged / 'personas').glob('*.md'))}"
    return flat(path.read_text())


@pytest.fixture(scope="module")
def persona_raw(staged: Path) -> str:
    """The staged AIDLC persona, verbatim. For structural assertions only."""
    return (staged / "personas" / "aidlc.md").read_text()


class TestTheStagedTreeIsWhatWeThinkItIs:
    """Guards the harness itself. A silently-empty staged tree would make every
    substantive assertion below vacuous, so the fixtures' own premises are checked."""

    def test_the_staged_skill_is_not_a_stub(self, skill_text: str) -> None:
        assert "Step 7e" in skill_text
        assert "proposal.json" in skill_text

    def test_the_staged_persona_is_not_a_stub(self, persona_text: str) -> None:
        assert "loop-proposal" in persona_text
        assert "aidlc-gate:" in persona_text

    def test_every_staged_aidlc_persona_variant_carries_the_guidance(self, staged: Path) -> None:
        """Discovered, not hardcoded — so a NEW variant cannot ship without the warning.

        `rules/personas/codex-distilled/` holds slimmed packs for delegated Codex runs
        (today: developer, operations, reviewer — no aidlc, so `personas/aidlc.md` is
        currently the only variant an AIDLC worker loads). If someone later adds a
        distilled aidlc pack, that becomes a second set of runtime instructions for the
        same persona, and a distillation pass is exactly the kind of edit that drops a
        paragraph. Globbing means this test fails at that moment rather than silently
        continuing to check only the one file the author of #4529 happened to know about.

        What every variant must carry is the *fact and its consequence*, not the flag's
        exact name: an author looks the flag up in the skill (pinned separately below),
        but "the default will not gate your waves, and an ungated plan has no human stop"
        has to survive any slimming, because that is the sentence that changes what the
        author writes.
        """
        variants = [p for p in staged.rglob("*aidlc*.md") if "skills" not in p.parts]
        assert variants, f"no aidlc persona reached the staged tree: {sorted(staged.rglob('*.md'))}"
        for variant in variants:
            text = flat(variant.read_text())
            assert "OFF by default" in text, variant
            assert "no human stop" in text, variant


class TestTheOffByDefaultFactIsStated:
    """#1: the every-wave-gate transform is off unless an operator turns it on."""

    def test_the_flag_is_named_so_an_author_can_check_it(self, skill_text: str) -> None:
        # Named exactly, because "a flag controls this" is not actionable — an author
        # who wants to know whether their plan will be gated has to be able to look.
        assert "ORCHESTRATION_AUTONOMY_GATE_EVERY_WAVE" in skill_text

    def test_the_default_is_stated_as_off(self, skill_text: str) -> None:
        window = skill_text[skill_text.index("ORCHESTRATION_AUTONOMY_GATE_EVERY_WAVE") - 600 :][:1600]
        assert "OFF by default" in window or "off by default" in window

    def test_the_consequence_of_relying_on_it_is_spelled_out(self, skill_text: str) -> None:
        """Not just "it is off" — what an ungated plan then does.

        The fact alone is inert; an author needs to know that "no gates declared" plus
        "flag off" equals "no human stop after acceptance", including before a deploy.
        """
        assert "no human stop after acceptance" in skill_text
        assert "Do not rely on it." in skill_text

    def test_the_skill_no_longer_claims_wave_gates_are_inserted_by_default(self, skill_text: str) -> None:
        """The specific false claim #4529 removed, pinned so it cannot return.

        The old rule 6 read: "Registration inserts an acceptance gate in front of the
        whole plan, and (for a proposal that declares no gate of its own) a gate at
        every wave boundary." The parenthetical was false on a default environment.
        """
        assert "and (for a proposal that declares no gate of its own) a gate at" not in skill_text

    def test_the_acceptance_gate_is_distinguished_from_a_wave_gate(self, skill_text: str) -> None:
        """These are separate mechanisms and conflating them is how the gap hid.

        The acceptance gate IS always inserted, so an author who hears "a gate is
        always added" can reasonably conclude their waves are covered. They are not:
        it gates whether the plan runs at all, not anything during the run.
        """
        assert "may this plan run at all" in skill_text


class TestTheAllOrNothingFactIsStated:
    """#2: declaring one gate suppresses the transform entirely."""

    def test_declaring_one_gate_is_not_topped_up(self, skill_text: str) -> None:
        assert "all-or-nothing" in skill_text
        assert "declaring one does not top up the rest" in skill_text

    def test_the_author_is_told_to_declare_all_of_them(self, skill_text: str) -> None:
        assert "declare all of them" in skill_text


class TestTheHeuristics:
    """The conservative defaults, in both directions. Both directions matter: a rule
    that only says "gate more" produces a plan gated at every wave, and gates a human
    learns to click through are worse than none."""

    @pytest.mark.parametrize("trigger", ["deploy", "spend", "irreversible"])
    def test_the_gate_before_triggers_are_present(self, skill_text: str, trigger: str) -> None:
        assert trigger in skill_text.lower()

    def test_code_and_tests_is_explicitly_not_a_gate(self, skill_text: str) -> None:
        assert "Code + tests is not a gate." in skill_text

    def test_the_reason_not_to_over_gate_is_given(self, skill_text: str) -> None:
        """An author told only "do not gate code waves" will gate them anyway when
        unsure. The reason — habituation defeats the gates that matter — is what makes
        the rule stick."""
        assert "click through" in skill_text

    def test_uncertainty_resolves_toward_gating(self, skill_text: str) -> None:
        """The asymmetry is the point: a superfluous gate costs one comment, a missing
        one costs the irreversible thing."""
        assert "gate it and say so in the brief" in skill_text

    def test_the_heuristics_are_marked_as_defaults_not_a_classifier(self, skill_text: str) -> None:
        assert "Judgement beats the list." in skill_text


class TestTheShapeIsAuthorable:
    """A gate an author cannot express correctly is guidance that produces a decoration."""

    def test_the_gate_node_kind_and_address_shape_are_shown(self, skill_raw: str) -> None:
        window = skill_raw[skill_raw.index("Step 7f") :]
        assert '"kind": "gate"' in window
        assert "deploy-gate" in window

    def test_the_gate_must_be_on_the_path_not_beside_it(self, skill_text: str) -> None:
        """The failure mode that produces a gate the engine walks straight past."""
        window = skill_text[skill_text.index("Step 7f") :]
        assert "the gate is the only thing pointing into" in window
        assert "flows around the gate" in window

    def test_a_gate_is_not_confused_with_an_eval(self, skill_text: str) -> None:
        window = skill_text[skill_text.index("Step 7f") :]
        assert "does not replace a wave's `eval` node" in window

    def test_gate_nodes_take_no_issue_ref(self, skill_text: str) -> None:
        window = skill_text[skill_text.index("Step 7f") :]
        assert "no `issue_ref`" in window


class TestHumanVisibilityAndRefinement:
    """Scope item 1 requires the placement be human-visible and refinable in
    conversation — a proposal, not a decision the agent makes silently."""

    def test_the_persona_gate_brief_carries_a_gate_placement_table(self, persona_raw: str) -> None:
        assert "| Wave | Gate proposed? | Why (consequence if wrong) | Gate node address |" in persona_raw

    def test_ungated_waves_appear_in_the_table_too(self, persona_text: str) -> None:
        """A wave omitted from the table is indistinguishable from a wave nobody
        considered, which is exactly the state #4529 is closing."""
        assert "including ungated" in persona_text

    def test_the_persona_states_placement_is_refinable_by_feedback(self, persona_text: str) -> None:
        assert "feedback:" in persona_text
        assert "add or remove gates before acceptance" in persona_text

    def test_the_persona_names_the_post_acceptance_amendment_path(self, persona_text: str) -> None:
        """Before acceptance, `feedback:`. After, the amendment loop — and the persona
        must not imply it can move a gate on an accepted plan itself."""
        assert "@agent-engine replan:" in persona_text
        assert "accept amendment" in persona_text

    def test_the_persona_step_list_reaches_gate_placement(self, persona_text: str) -> None:
        """Pinned because the loop-proposal step list is the procedure actually
        followed; guidance only in the skill is guidance behind one more hop."""
        window = persona_text[persona_text.index("Execute the **loop-proposal** stage") :][:1400]
        assert "Step 7f" in window
        assert "OFF by default" in window

    def test_the_skill_says_acceptance_stays_human(self, skill_text: str) -> None:
        """The one invariant no amount of authoring convenience may erode."""
        window = skill_text[skill_text.index("Step 7f") :]
        assert "no agent-accessible acceptance path" in window


class TestExistingPresentationIsPreserved:
    """Scope item 1 requires the inception Gate 0-3 presentation, the wave-map tables
    and the acceptance formatting survive. A gate-placement addition that quietly
    reformatted the brief would break every downstream reader of these markers."""

    def test_the_gate_marker_convention_survives(self, persona_text: str) -> None:
        assert "<!-- aidlc-gate:<stage-name> -->" in persona_text
        assert "<!-- aidlc-gate:loop-proposal -->" in persona_text

    def test_the_wave_map_table_survives(self, persona_raw: str) -> None:
        assert "| Wave / capability | Story issues | Orchestrator / evaluation drafts | Planned checks | Remaining holds |" in persona_raw

    def test_the_target_environment_table_survives(self, persona_raw: str) -> None:
        assert "| Target environment | AWS account ID | Region | adp-cred label | Selection status |" in persona_raw

    def test_the_five_emission_lint_rows_survive(self, persona_text: str) -> None:
        for row in (
            "1 — CI apply path",
            "2 — Explicit account and credential",
            "3 — Maintained version pins",
            "4 — Hotfix protocol",
            "5 — Live API-contract check",
        ):
            assert row in persona_text, row

    def test_the_gate_brief_heading_and_reply_footer_survive(self, persona_text: str) -> None:
        assert "## 🚦 AI-DLC Gate <N>/<M>" in persona_text
        assert "`@agent-aidlc approve`" in persona_text

    def test_the_one_stage_per_run_rule_survives(self, persona_text: str) -> None:
        assert "**EXIT — run is over**" in persona_text

    def test_the_other_proposal_rules_are_untouched(self, skill_text: str) -> None:
        """Rule 6 was rewritten; rules 1-5, 7 and 8 must be exactly as they were.

        Rule 1 especially: the blank-`org_id` mandate is the tenant-isolation
        instruction, and a rewrite that disturbed it would be the most expensive
        possible collateral damage.
        """
        assert "**`org_id` MUST be the empty string.**" in skill_text
        assert "**Addresses are exactly four segments**" in skill_text
        assert "**Every wave containing stories contains exactly one `eval` node.**" in skill_text
        assert "**The edge set MUST be acyclic**" in skill_text
        assert "**`description` and `design_history` are OPTIONAL" in skill_text
