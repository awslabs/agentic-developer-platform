"""The sandbox's system prompt is rendered by the gateway from preamble + persona (#8113)."""

from pathlib import Path

import pytest

from src.agentauth import chat_persona_prompt
from src.agentauth.chat_persona_prompt import (
    DATA_NOT_INSTRUCTIONS,
    MAX_SYSTEM_PROMPT_CHARS,
    PLATFORM_PREAMBLE,
    persona_text,
    render_system_prompt,
)

RULES = Path(__file__).resolve().parents[4] / "modules" / "agent-factory" / "rules" / "personas"


def test_preamble_grounds_the_assistant_in_adp_and_its_platform_tools():
    assert "Agentic Developer Platform" in PLATFORM_PREAMBLE
    assert "not a payroll" in PLATFORM_PREAMBLE
    assert "`platform_`" in PLATFORM_PREAMBLE
    assert "call the relevant platform tools before answering" in PLATFORM_PREAMBLE
    assert DATA_NOT_INSTRUCTIONS in PLATFORM_PREAMBLE
    # Platform questions win over the intake interview the intent-refinement persona runs.
    assert "do not turn a status question into an intake interview" in PLATFORM_PREAMBLE


def test_intent_refinement_persona_is_rendered_from_the_shared_rules_tree():
    rendered = render_system_prompt("intent-refinement")
    assert rendered.persona == "intent-refinement" and rendered.persona_source == "rules"
    assert rendered.system_prompt.startswith(PLATFORM_PREAMBLE)
    assert "# Persona guidance (intent-refinement)" in rendered.system_prompt
    # The persona's `update_draft` expectation is kept: the sandbox provides that tool.
    assert "update_draft" in rendered.system_prompt
    assert rendered.system_prompt.strip().endswith((RULES / "intent-refinement.md").read_text().strip().splitlines()[-1])
    assert len(rendered.system_prompt) <= MAX_SYSTEM_PROMPT_CHARS
    assert rendered.prompt_revision.startswith("chat-prompt-v1:intent-refinement:")
    assert rendered.prompt_revision == render_system_prompt("intent-refinement").prompt_revision
    assert rendered.prompt_revision != render_system_prompt("developer").prompt_revision
    assert rendered.record() == {"chat_prompt_revision": rendered.prompt_revision, "chat_prompt_persona_source": "rules"}


@pytest.mark.parametrize("persona", [None, "", "no-such-persona", "../../etc/passwd", "Developer", "a" * 65])
def test_unknown_or_unsafe_persona_yields_the_preamble_only_and_says_so(persona):
    rendered = render_system_prompt(persona)
    assert rendered.system_prompt == PLATFORM_PREAMBLE
    assert rendered.persona is None and rendered.persona_source == "preamble_only"
    assert rendered.prompt_revision.startswith("chat-prompt-v1:none:")
    assert persona_text(persona) is None


def test_oversized_persona_is_omitted_rather_than_truncated(tmp_path, monkeypatch):
    (tmp_path / "huge.md").write_text("x" * MAX_SYSTEM_PROMPT_CHARS)
    monkeypatch.setattr(chat_persona_prompt, "persona_directories", lambda: [tmp_path])
    rendered = render_system_prompt("huge")
    assert rendered.system_prompt == PLATFORM_PREAMBLE
    assert rendered.persona_source == "omitted_too_long" and rendered.persona is None


def test_staged_image_copy_is_preferred_over_the_checkout(tmp_path, monkeypatch):
    staged = tmp_path / "chat-personas"
    staged.mkdir()
    (staged / "developer.md").write_text("# staged developer\n")
    monkeypatch.setattr(chat_persona_prompt, "persona_directories", lambda: [staged, RULES])
    assert persona_text("developer") == "# staged developer"
    assert render_system_prompt("developer").system_prompt.endswith("# staged developer")


def test_persona_directories_tolerate_the_shallow_image_layout():
    """In the image the module sits at /app/src/agentauth/...; the repository fallback must not raise (#8113)."""
    from pathlib import Path

    from src.agentauth import chat_persona_prompt

    image_layout = chat_persona_prompt.persona_directories(Path("/app/src/agentauth/chat_persona_prompt.py"))
    assert image_layout == [Path("/app/contracts/chat-personas")]
    repo_layout = chat_persona_prompt.persona_directories(Path("/w/modules/gateway/src/agentauth/chat_persona_prompt.py"))
    assert repo_layout == [Path("/w/modules/gateway/contracts/chat-personas"), Path("/w/modules/agent-factory/rules/personas")]
    assert chat_persona_prompt.render_system_prompt("intent-refinement").system_prompt
