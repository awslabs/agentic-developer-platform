"""Report personas reuse the existing Responses contract and registry projection."""

import pytest

from src.admin.persona_models.catalogue import persona_compatibility_class
from src.agentauth.task_responses_contract import TASK_RESPONSES_REVISION
from src.tasks.personas import CODEX_REPORT_PERSONAS, TASK_PERSONAS


@pytest.mark.parametrize("persona", sorted(CODEX_REPORT_PERSONAS))
def test_report_persona_uses_registered_text_responses_contract(persona):
    profile = TASK_PERSONAS[persona]
    assert persona_compatibility_class(persona) == "codex-sdk"
    assert profile.harness_contract_revision == TASK_RESPONSES_REVISION
    assert "tools" not in profile.probe_body
    assert profile.request_shape_sha256 == TASK_PERSONAS["agent-task-gpt-intent-refinement"].request_shape_sha256


def test_report_registration_does_not_rebind_existing_agents():
    assert persona_compatibility_class("developer") == "claude-agent-sdk"
    assert persona_compatibility_class("reviewer") == "claude-agent-sdk"
    assert persona_compatibility_class("agent-codex-developer") == "codex-sdk"
    assert persona_compatibility_class("agent-codex-reviewer") == "codex-sdk"
    assert "agent-task-gpt-operations" not in CODEX_REPORT_PERSONAS
    assert "agent-task-gpt-aidlc" not in CODEX_REPORT_PERSONAS
