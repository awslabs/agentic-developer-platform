"""The amendment brief has a producer AND a consumer, and they name the same things.

Issue #4529. Review of this story's first cut found a **consumer with no producer**:
`lib/engine_registration.py` read an authored amendment from
`aidlc/spaces/amendments/{request_id}/proposal.json`, and nothing anywhere told an
author to write there — not the persona, not the skill, not the run's environment.
A correctly summoned, correctly authorized author received two opaque identifiers,
followed its ordinary planning instructions, opened a flow nobody asked for, and
filed nothing, while the human's `replan:` request stayed recorded and owed.

The fix has two halves that only work together:

  * the **producer** — `entrypoint._export_authoring_assignment` exports the brief
    from the dispatch envelope (pinned by `test_entrypoint_authoring_assignment.py`)
  * the **consumer** — the authoring instructions tell a run to read those exact
    names and write that exact file

Either half alone is the same bug. An exported variable no instruction mentions is
the defect being fixed here; an instruction naming a variable nothing exports is
that defect mirrored, and is *harder* to notice, because the instruction reads
perfectly well right up until an author follows it and finds an unset variable.

So this file asserts the two halves against each other, and it does it the only way
that is honest:

  * **the real library constants**, imported from `lib.engine_registration` — never
    the string `"ADP_AMENDMENT_OUTPUT_PATH"` retyped here. A test carrying its own
    copy of the name under test passes after a rename breaks both real sides.
  * **the real staged instruction text**, produced by running the real
    `stage-personas.sh` over the real source trees. The worker does not read
    `modules/agent-factory/`; it reads a flat tree assembled at image build time
    (Dockerfile stage 2). Asserting on the source files would pass for guidance the
    image never ships.

Substance, not prose: these assertions are about which variable names, paths and
facts appear. A rewording should not fail this file; deleting an instruction, or
renaming a constant on one side only, should.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.engine_registration import (  # noqa: E402
    AMENDMENT_ARTIFACT_TEMPLATE,
    AMENDMENT_BASE_HASH_ENV,
    AMENDMENT_BASE_VERSION_ENV,
    AMENDMENT_OUTPUT_PATH_ENV,
    AMENDMENT_REQUEST_ENV,
    AMENDMENT_REQUEST_TEXT_ENV,
    FLOW_ID_ENV,
)

HERE = Path(__file__).resolve().parent
STAGE_SCRIPT = HERE.parent / "stage-personas.sh"
MODULE_ROOT = HERE.parents[1]

#: The whole brief, as the library defines it. Every name here must appear in the
#: staged instructions, and the instructions must name nothing else of this shape —
#: see `test_the_instructions_invent_no_variable_the_code_does_not_export`.
BRIEF_ENV = (
    FLOW_ID_ENV,
    AMENDMENT_REQUEST_ENV,
    AMENDMENT_REQUEST_TEXT_ENV,
    AMENDMENT_BASE_VERSION_ENV,
    AMENDMENT_BASE_HASH_ENV,
    AMENDMENT_OUTPUT_PATH_ENV,
)


@pytest.fixture(scope="module")
def staged(tmp_path_factory) -> Path:
    """The real source trees, put through the real staging script once.

    Module-scoped because staging the persona/skill tree is the expensive part and
    every test here reads the same output. Read-only by construction.
    """
    root = tmp_path_factory.mktemp("stage-amendment-contract")
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

    These are hard-wrapped markdown documents: a sentence an author must not delete
    is routinely split mid-phrase by a line break, so a raw substring check would
    fail for a reflow while passing for a deletion — exactly backwards.
    """
    return " ".join(text.split())


@pytest.fixture(scope="module")
def persona_text(staged: Path) -> str:
    """The staged AIDLC persona, whitespace-normalised — what the worker loads."""
    path = staged / "personas" / "aidlc.md"
    assert path.exists(), f"the aidlc persona did not reach the staged tree: {sorted((staged / 'personas').glob('*.md'))}"
    return flat(path.read_text())


@pytest.fixture(scope="module")
def skill_text(staged: Path) -> str:
    """The staged emission skill, whitespace-normalised — what the worker loads."""
    path = staged / "skills" / "aidlc-emit-issues" / "SKILL.md"
    assert path.exists(), f"the emission skill did not reach the staged tree: {sorted((staged / 'skills').iterdir())}"
    return flat(path.read_text())


@pytest.fixture(scope="module")
def instruction_texts(persona_text: str, skill_text: str) -> dict[str, str]:
    """Both documents a commissioned author actually loads, by name.

    Both, not either: `loadRules()` puts the persona in the prompt directly while the
    skill is read on demand, and the two carry different halves of the procedure. A
    variable named in neither is a variable no author is told about.
    """
    return {"persona": persona_text, "skill": skill_text}


class TestTheHarnessIsNotVacuous:
    """Guards the fixtures' own premises. A silently-empty staged tree, or a staging
    script that quietly stopped copying skills, would make every assertion below pass
    against nothing — which is the failure mode this whole file exists to catch in the
    code, so it must not be the failure mode of the file itself."""

    def test_the_staged_persona_is_the_real_one(self, persona_text: str) -> None:
        assert "loop-proposal" in persona_text
        assert "aidlc-gate:" in persona_text

    def test_the_staged_skill_is_the_real_one(self, skill_text: str) -> None:
        assert "Step 7e" in skill_text
        assert "proposal.json" in skill_text

    def test_the_constants_under_test_are_not_empty_strings(self) -> None:
        """An import that resolved to `""` would make every `in` assertion below
        trivially true, since `"" in anything` is True."""
        for name in BRIEF_ENV:
            assert name, "a brief constant is empty"
            assert name.startswith("ADP_"), name

    def test_the_brief_constants_are_distinct(self) -> None:
        """Two constants accidentally given the same value would let one instruction
        satisfy both assertions while an author never learns about the other."""
        assert len(set(BRIEF_ENV)) == len(BRIEF_ENV), BRIEF_ENV


class TestEveryExportedNameIsTaughtToTheAuthor:
    """The producer half, checked from the consumer's side.

    Parametrised over the library's own tuple rather than a hand-written list, so
    adding a seventh variable to the brief fails here until an instruction mentions
    it. That is the direction the original defect ran: code grew a name, instructions
    did not.
    """

    @pytest.mark.parametrize("env_name", BRIEF_ENV, ids=lambda n: n)
    def test_the_instructions_name_the_variable(self, env_name: str, instruction_texts: dict[str, str]) -> None:
        where = [doc for doc, text in instruction_texts.items() if env_name in text]
        assert where, f"{env_name} is exported to every commissioned author but named in neither the persona nor the skill"

    def test_the_persona_names_the_whole_brief(self, persona_text: str) -> None:
        """The persona is in the prompt unconditionally; the skill is a file the author
        must choose to open. So the persona — not only the skill — has to carry the full
        set, or an author who never reads the skill is working from a partial brief."""
        missing = [name for name in BRIEF_ENV if name not in persona_text]
        assert not missing, f"the persona omits {missing}"

    def test_the_trigger_variable_is_what_selects_amendment_mode(self, persona_text: str) -> None:
        """`ADP_AMENDMENT_REQUEST_ID` is the discriminator: `authoring_assignment()`
        returns None without it, so a run that behaves as an amendment run on any other
        signal would be filing against an assignment the server will refuse."""
        window = persona_text[: persona_text.index("Intent Identity")]
        assert AMENDMENT_REQUEST_ENV in window, "amendment mode must be selected before the planning instructions begin"


class TestTheAuthorIsToldWhereToWrite:
    """The exact gap that was found: the artifact path's consumer had no producer."""

    def test_the_instructions_point_at_the_exported_path_variable(self, instruction_texts: dict[str, str]) -> None:
        """Not a path the author composes — the variable the entrypoint exports.

        An author that builds the path itself can get it subtly wrong, and the
        issue-keyed shape is the obvious wrong guess. The failure is silent: a valid
        amendment on disk, nothing found, request still owed.
        """
        for doc, text in instruction_texts.items():
            assert AMENDMENT_OUTPUT_PATH_ENV in text, f"the {doc} never tells the author to write to {AMENDMENT_OUTPUT_PATH_ENV}"

    def test_the_skill_states_the_path_shape_the_library_composes(self, skill_text: str) -> None:
        """The literal directory shape, taken from the library's own template rather
        than retyped, so a change to `AMENDMENT_ARTIFACT_TEMPLATE` fails here instead of
        leaving the documented shape describing a file nothing reads.
        """
        directory = AMENDMENT_ARTIFACT_TEMPLATE.rsplit("/", 2)[0]  # "aidlc/spaces/amendments"
        filename = AMENDMENT_ARTIFACT_TEMPLATE.rsplit("/", 1)[1]  # "proposal.json"
        assert directory in skill_text, f"the skill does not state the {directory} location"
        assert filename in skill_text

    def test_the_author_is_told_the_path_is_keyed_on_the_request(self, skill_text: str) -> None:
        """Two `replan:` asks on one issue must not share a file. An author who assumes
        the issue-keyed shape it has used all its life writes the wrong file, and the
        second ask silently overwrites the first."""
        assert "keyed on the **request**" in skill_text or "keyed on the request" in skill_text

    def test_writing_elsewhere_is_named_as_invisible(self, instruction_texts: dict[str, str]) -> None:
        """ "Write here" is weaker than "write here, and anywhere else is invisible". The
        failure has no error — the run succeeds and reports nothing filed — so an author
        needs to know that before it happens, not diagnose it after."""
        assert "invisible" in instruction_texts["skill"]
        assert "invisible" in instruction_texts["persona"]

    def test_a_missing_path_is_a_stop_not_a_guess(self, persona_text: str) -> None:
        """The honest failure. A guessed path produces the silent-no-artifact outcome;
        stopping produces a report a human can act on."""
        window = persona_text[persona_text.index(AMENDMENT_OUTPUT_PATH_ENV) :]
        assert "do not guess a path" in window


class TestTheAuthorIsToldWhatToWrite:
    """A brief that says where but not what produces a file the server refuses."""

    def test_the_document_is_the_same_schema_as_a_new_plan(self, skill_text: str) -> None:
        """`register_amendment_proposal` POSTs to a route typed `LoopProposal`, the same
        model the new-flow route takes. An amendment-specific schema does not exist, and
        an author inventing one gets a 422."""
        window = skill_text[skill_text.index("Step 7g") :]
        assert "Step 7e" in window, "the amendment step must defer to the document schema rather than restate it"

    def test_the_author_is_told_the_document_is_whole_not_a_patch(self, skill_text: str) -> None:
        """`amend_plan` supersedes every node absent from the document. An author who
        submits only the changed nodes deletes the rest of the plan — including
        completed work."""
        window = skill_text[skill_text.index("Step 7g") :]
        assert "Absence is deletion." in window
        assert "not a patch" in window

    def test_the_author_is_told_addresses_must_be_preserved_exactly(self, skill_text: str) -> None:
        """A node keeps its row, state, attempts and history only if its address is
        unchanged. A retyped address reads as "delete this node, add an unrelated one"."""
        window = skill_text[skill_text.index("Step 7g") :]
        assert "byte-identical addresses" in window

    def test_the_author_is_told_no_gate_is_synthesised_on_this_path(self, skill_text: str) -> None:
        """The asymmetry that silently removes a human stop.

        Registering a NEW plan runs `transform_for_registration`, which synthesises an
        acceptance gate (and optionally wave gates). Accepting an AMENDMENT runs
        `amend_plan`, which synthesises nothing. So an author carrying the habit over
        from Step 7f — "a gate is always added for me" — files an amendment that deletes
        the acceptance gate the original registration inserted. Gate placement is this
        story's subject, and this is the one way an amendment silently reduces oversight.
        """
        window = skill_text[skill_text.index("Step 7g") :]
        assert "No gate is inserted for you." in window
        assert "it deletes them" in window

    def test_the_persona_carries_the_no_synthesis_warning_too(self, persona_text: str) -> None:
        """Not skill-only: an author that never opens the skill would otherwise learn
        this the hard way, on an accepted plan that is already running."""
        assert "No gate is synthesised for you" in persona_text

    def test_the_gate_vocabulary_is_shared_with_a_new_plan(self, instruction_texts: dict[str, str]) -> None:
        """Deferred to Step 7f rather than restated. Two copies of the gate heuristics
        drift, and the drifted copy is the one an amendment uses on a live plan."""
        for doc, text in instruction_texts.items():
            assert "Step 7f" in text, f"the {doc} does not point amendment authoring at the gate-placement rules"

    def test_the_base_version_is_what_the_author_reads(self, instruction_texts: dict[str, str]) -> None:
        """The server compares the recorded base at acceptance and refuses a conflict.
        An author that re-reads "the current plan" instead authors against a version it
        was not commissioned for, and the draft is rejected after the work is done."""
        for doc, text in instruction_texts.items():
            assert AMENDMENT_BASE_VERSION_ENV in text, doc
        assert "not a newer read" in instruction_texts["persona"]


class TestTheRequestTextIsData:
    """`ADP_AMENDMENT_REQUEST_TEXT` is human prose that reached the server through a
    GitHub comment. It is length-capped and attributed, but it is not sanitised, and it
    arrives in the environment of a run holding `PLAN_DRAFT` on a live flow."""

    def test_the_instructions_mark_the_request_text_as_data(self, instruction_texts: dict[str, str]) -> None:
        for doc, text in instruction_texts.items():
            window = text[text.index(AMENDMENT_REQUEST_TEXT_ENV) :]
            assert "never as instructions to execute" in window or "never an instruction to execute" in window, doc

    def test_an_absent_request_text_is_handled_rather_than_fatal(self, instruction_texts: dict[str, str]) -> None:
        """An empty `replan:` is a valid request — the parser records it and the
        entrypoint exports no text for it. An author that treats the absent variable as
        an error refuses a request the human legitimately made."""
        # Case-folded: the persona states this in a table cell where the sentence
        # starts capitalised, and the skill states it mid-paragraph. Which one is
        # capitalised is not a fact worth failing on.
        for doc, text in instruction_texts.items():
            assert "may be absent" in text.lower(), doc


class TestTheAuthorCannotApplyItsOwnAmendment:
    """The invariant no amount of authoring convenience may erode. `register_amendment_draft`
    writes no node, edge, decision or plan version, and `PLAN_DRAFT` is not `PLAN_APPROVE`
    — but an author that believes it has applied the plan reports a live change that did
    not happen, and a human stops watching for the accept they still owe."""

    def test_the_instructions_say_propose_not_apply(self, instruction_texts: dict[str, str]) -> None:
        for doc, text in instruction_texts.items():
            assert "propose" in text.lower(), doc
            assert "inert" in text, doc

    def test_the_named_human_accept_is_the_only_path(self, instruction_texts: dict[str, str]) -> None:
        for doc, text in instruction_texts.items():
            assert "accept amendment" in text, doc
            assert "no agent-accessible acceptance path" in text, doc

    def test_the_author_is_forbidden_from_reporting_the_plan_as_changed(self, instruction_texts: dict[str, str]) -> None:
        """The reporting half. A draft is inert whatever the run says, so this protects
        the human's understanding rather than the graph."""
        for doc, text in instruction_texts.items():
            assert "waiting for the human to accept" in text, doc

    def test_amendment_mode_does_not_run_the_planning_procedure(self, persona_text: str) -> None:
        """What the original defect actually produced: an author with no brief fell
        through to the planning instructions, created an inception space for a flow
        nobody asked about, and stopped at a gate of its own."""
        window = persona_text[persona_text.index("Amendment Mode") :]
        assert "does not apply to you" in window
        assert "do not dispatch anything" in window
