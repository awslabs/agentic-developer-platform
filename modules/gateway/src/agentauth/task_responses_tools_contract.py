"""Separate wire contract for serial Codex Task tools.

Parsing is not authorization. The model host must compare the namespace against
its frozen reviewed catalogue and verify history through TaskToolReceipts before
using this contract. The existing report-only v2 parser remains unchanged.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

import rfc8785
from pydantic import Field, model_validator

from src.agentauth.task_responses_contract import (
    ClosedModel,
    ResponsesMessage,
    ResponsesOutputMessage,
    ResponsesReasoning,
    ResponsesReasoningInput,
    ResponsesReasoningOutput,
    ResponsesUsage,
)

TASK_RESPONSES_TOOLS_REVISION = "task-codex-sdk-serial-tools-v3"


class TaskFunction(ClosedModel):
    type: Literal["function"]
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=2000)
    parameters: dict
    strict: Literal[False]

    @model_validator(mode="after")
    def bounded_schema(self):
        if self.parameters.get("type") != "object" or self.parameters.get("additionalProperties") is not False:
            raise ValueError("tool requires a closed object schema")
        if len(rfc8785.dumps(self.parameters)) > 16384:
            raise ValueError("tool schema exceeds bound")
        return self


class TaskNamespace(ClosedModel):
    type: Literal["namespace"]
    name: Literal["mcp__adp"]
    description: Literal["Authorized ADP tools."]
    tools: list[TaskFunction] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_names(self):
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("duplicate tool declaration")
        return self


class TaskFunctionCall(ClosedModel):
    type: Literal["function_call"]
    call_id: str = Field(min_length=1, max_length=200)
    namespace: Literal["mcp__adp"]
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    arguments: str = Field(min_length=2, max_length=32768)
    status: Literal["completed"] | None = None

    @model_validator(mode="after")
    def object_arguments(self):
        arguments = json.loads(self.arguments)
        if not isinstance(arguments, dict) or len(self.arguments.encode()) > 32768:
            raise ValueError("tool arguments exceed contract")
        rfc8785.dumps(arguments)  # Reject non-JSON numeric values.
        return self


class TaskFunctionOutput(TaskFunctionCall):
    id: str = Field(min_length=1, max_length=200)


class TaskToolText(ClosedModel):
    type: Literal["input_text"]
    text: str = Field(max_length=32768)


class TaskFunctionResult(ClosedModel):
    type: Literal["function_call_output"]
    call_id: str = Field(min_length=1, max_length=200)
    output: list[TaskToolText] = Field(min_length=2, max_length=2)


class TaskToolsResponsesRequest(ClosedModel):
    input: str | list[ResponsesMessage | ResponsesReasoningInput | TaskFunctionCall | TaskFunctionResult] = Field(min_length=1, max_length=32000)
    instructions: str | None = Field(default=None, max_length=32000)
    reasoning: ResponsesReasoning
    max_output_tokens: int = Field(strict=True, ge=1, le=10000)
    tools: list[TaskNamespace] = Field(min_length=1, max_length=1)
    parallel_tool_calls: Literal[False]

    @model_validator(mode="after")
    def serial_history(self):
        if isinstance(self.input, str):
            return self
        if len(self.input) > 64:
            raise ValueError("too many history items")
        names = {tool.name for tool in self.tools[0].tools}
        seen = set()
        pending = None
        for item in self.input:
            if isinstance(item, TaskFunctionCall):
                if pending or item.call_id in seen or item.name not in names:
                    raise ValueError("unadmitted or overlapping function call")
                seen.add(item.call_id)
                pending = item.call_id
            elif isinstance(item, TaskFunctionResult):
                if not pending or item.call_id != pending:
                    raise ValueError("unpaired function result")
                pending = None
        if pending:
            raise ValueError("missing function result")
        return self


class TaskToolsResponsesResult(ClosedModel):
    id: str = Field(min_length=1, max_length=200)
    status: Literal["completed"]
    output: list[ResponsesOutputMessage | ResponsesReasoningOutput | TaskFunctionOutput] = Field(min_length=1, max_length=16)
    usage: ResponsesUsage

    @model_validator(mode="after")
    def serial_call(self):
        if sum(isinstance(item, TaskFunctionOutput) for item in self.output) > 1:
            raise ValueError("parallel tool calls unavailable")
        return self


# Distinct evidence is required for namespace calls and their continuation. A
# text/reviewer probe cannot certify this wire shape.
TASK_RESPONSES_TOOLS_PROBE_BODY = {
    "input": "Call task_probe with value OK.",
    "reasoning": {"effort": "medium"},
    "max_output_tokens": 128,
    "parallel_tool_calls": False,
    "tools": [
        {
            "type": "namespace",
            "name": "mcp__adp",
            "description": "Authorized ADP tools.",
            "tools": [
                {
                    "type": "function",
                    "name": "task_probe",
                    "description": "Return probe evidence.",
                    "strict": False,
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                }
            ],
        }
    ],
}
TASK_RESPONSES_TOOLS_REQUEST_SHAPE = hashlib.sha256(
    json.dumps(TASK_RESPONSES_TOOLS_PROBE_BODY, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
