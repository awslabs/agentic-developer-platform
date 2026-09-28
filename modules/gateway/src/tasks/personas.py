"""Task-only executable/model profiles, separate from webhook dispatch personas."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType

from src.agentauth.task_responses_contract import TASK_RESPONSES_PROBE_BODY, TASK_RESPONSES_REVISION
from src.agentauth.task_responses_tools_contract import TASK_RESPONSES_TOOLS_PROBE_BODY, TASK_RESPONSES_TOOLS_REVISION


@dataclass(frozen=True)
class TaskPersonaProfile:
    compatibility_class: str
    harness_contract_revision: str
    probe_json: str

    @property
    def probe_body(self):
        return json.loads(self.probe_json)

    @property
    def request_shape_sha256(self):
        return hashlib.sha256(self.probe_json.encode()).hexdigest()


_TEXT_PROBE = {
    "anthropic_version": "bedrock-2023-05-31",
    "max_tokens": 16,
    "system": "Reply briefly.",
    "messages": [{"role": "user", "content": [{"type": "text", "text": "Reply OK."}]}],
}
_TOOL_PROBE = {
    "anthropic_version": "bedrock-2023-05-31",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": [{"type": "text", "text": "Call task_probe with value OK."}]}],
    "tools": [
        {
            "name": "task_probe",
            "description": "Return probe evidence.",
            "input_schema": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False},
        }
    ],
    "tool_choice": {"type": "tool", "name": "task_probe"},
}


def _profile(revision, body):
    return TaskPersonaProfile("anthropic_messages", revision, json.dumps(body, sort_keys=True, separators=(",", ":")))


TASK_PERSONAS = MappingProxyType(
    {
        "agent-task-gpt-intent-refinement": TaskPersonaProfile(
            "codex-sdk", TASK_RESPONSES_REVISION, json.dumps(TASK_RESPONSES_PROBE_BODY, sort_keys=True, separators=(",", ":"))
        ),
        "agent-task-gpt-developer": TaskPersonaProfile(
            "codex-sdk", TASK_RESPONSES_TOOLS_REVISION, json.dumps(TASK_RESPONSES_TOOLS_PROBE_BODY, sort_keys=True, separators=(",", ":"))
        ),
        "agent-task-investigator": _profile("task-messages-v1", _TEXT_PROBE),
        "agent-task-cyber": _profile("task-cyber-sdk-messages-v1", _TOOL_PROBE),
        "agent-task-claude-developer": _profile("task-coding-sdk-messages-v1", _TOOL_PROBE),
        "agent-task-codex-developer": _profile("task-codex-responses-compat-v1", _TOOL_PROBE),
    }
)
