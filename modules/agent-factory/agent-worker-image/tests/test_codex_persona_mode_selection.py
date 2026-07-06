"""Shape guard for the codex supervisor persona's mode-selection section (#2901).

The @agent-codex supervisor picks WHICH distilled pack its Codex delegations use
by parsing a mode directive from the triggering comment and passing it to the
wrapper via `AGENT_TYPE=<mode>`. That convention lives only in the persona
markdown (`rules/personas/codex.md`) — this test pins it so an edit can't
silently drop the instruction and regress the pack-selection to today's
un-instructed no-op fallback.

No PyYAML/parsing needed: the persona is a plain-markdown instruction document,
so substring assertions on the rendered text are the right shape guard (same
pattern as test_codex_bridge_skill.py).
"""

from __future__ import annotations

from pathlib import Path

# rules/personas/codex.md lives at the repo module root, two levels up from
# agent-worker-image/tests/.
CODEX_MD = (
    Path(__file__).resolve().parents[2] / "rules" / "personas" / "codex.md"
)

SECTION_HEADING = "## Selecting the distilled pack for delegations"

# The distilled fileset — exactly these three modes are valid (issue Non-goals).
VALID_MODES = ["developer", "reviewer", "operations"]


def test_codex_persona_exists() -> None:
    assert CODEX_MD.is_file(), f"missing {CODEX_MD}"


def test_mode_selection_section_present() -> None:
    text = CODEX_MD.read_text()
    assert SECTION_HEADING in text, (
        f"codex.md is missing the '{SECTION_HEADING}' section"
    )


def test_mode_selection_section_between_gate_and_guidelines() -> None:
    """The section must sit between the codex-bridge gate and Behavioral Guidelines."""
    text = CODEX_MD.read_text()
    gate = text.index("## The codex-bridge gate")
    section = text.index(SECTION_HEADING)
    guidelines = text.index("## Behavioral Guidelines")
    assert gate < section < guidelines, (
        "mode-selection section must sit between the codex-bridge gate and "
        "Behavioral Guidelines"
    )


def test_section_names_all_three_valid_modes() -> None:
    text = CODEX_MD.read_text()
    for mode in VALID_MODES:
        assert mode in text, f"codex.md must name the '{mode}' distilled mode"


def test_section_documents_agent_type_override_invocation() -> None:
    """Delegations apply the mode via the AGENT_TYPE env override on the wrapper."""
    text = CODEX_MD.read_text()
    assert "AGENT_TYPE=<mode>" in text, (
        "codex.md must document the AGENT_TYPE=<mode> env-override invocation form"
    )
    assert "run-codex.sh" in text, (
        "codex.md must reference the run-codex.sh wrapper for delegations"
    )


def test_section_documents_pipe_suffix_directive_form() -> None:
    text = CODEX_MD.read_text()
    assert "@agent-codex|" in text, (
        "codex.md must document the '@agent-codex|<mode>' pipe-suffix directive form"
    )


def test_invalid_mode_does_not_fail_the_run() -> None:
    """An unknown mode must fall through to the default, never fail the run."""
    low = CODEX_MD.read_text().lower()
    assert "do not fail the run" in low, (
        "codex.md must state that an invalid mode does NOT fail the run"
    )
