"""Explicit, bounded inline Responses contract for Task Codex transport.

Model, destination, service tier, server state and credentials are host-owned.
Encrypted reasoning is inline; executable tool history is not admitted yet. Admission
uses a distinct revision/probe and never borrows Messages or reviewer evidence.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TASK_RESPONSES_TRANSPORT = "openai_responses"
TASK_RESPONSES_REVISION = "task-codex-sdk-inline-responses-v2"


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResponsesText(ClosedModel):
    type: Literal["input_text", "output_text"]
    text: str = Field(max_length=32000)
    annotations: list = Field(default_factory=list, max_length=0)


class ResponsesMessage(ClosedModel):
    type: Literal["message"] = "message"
    role: Literal["system", "developer", "user", "assistant"]
    content: str | list[ResponsesText] = Field(max_length=32000)
    status: Literal["completed"] | None = None
    phase: Literal["commentary", "final_answer"] | None = None

    @model_validator(mode="after")
    def bounded_parts(self):
        if isinstance(self.content, list) and len(self.content) > 64:
            raise ValueError("too many text parts")
        return self


class ResponsesReasoning(ClosedModel):
    effort: Literal["minimal", "low", "medium", "high", "xhigh"]


class ResponsesSummary(ClosedModel):
    type: Literal["summary_text"]
    text: str = Field(max_length=32000)


class ResponsesReasoningInput(ClosedModel):
    type: Literal["reasoning"]
    encrypted_content: str = Field(min_length=1, max_length=32768)
    summary: list[ResponsesSummary] = Field(max_length=16)
    status: Literal["completed"] | None = None


class ResponsesReasoningOutput(ResponsesReasoningInput):
    id: str = Field(min_length=1, max_length=200)


class TaskResponsesRequest(ClosedModel):
    input: str | list[ResponsesMessage | ResponsesReasoningInput] = Field(min_length=1, max_length=32000)
    instructions: str | None = Field(default=None, max_length=32000)
    reasoning: ResponsesReasoning
    max_output_tokens: int = Field(strict=True, ge=1, le=10000)

    @model_validator(mode="after")
    def bounded_messages(self):
        if isinstance(self.input, list) and len(self.input) > 64:
            raise ValueError("too many input messages")
        return self


class ResponsesInputDetails(ClosedModel):
    cache_write_tokens: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)
    cached_tokens: int = Field(strict=True, ge=0, le=2**53 - 1)


class ResponsesOutputDetails(ClosedModel):
    reasoning_tokens: int = Field(strict=True, ge=0, le=2**53 - 1)


class ResponsesUsage(ClosedModel):
    input_tokens: int = Field(strict=True, ge=0, le=2**53 - 1)
    output_tokens: int = Field(strict=True, ge=0, le=2**53 - 1)
    input_tokens_details: ResponsesInputDetails | None = None
    output_tokens_details: ResponsesOutputDetails | None = None

    @model_validator(mode="after")
    def consistent_counts(self):
        if self.input_tokens_details and self.input_tokens_details.cached_tokens > self.input_tokens:
            raise ValueError("cache exceeds inclusive input")
        if (
            self.input_tokens_details
            and self.input_tokens_details.cached_tokens + (self.input_tokens_details.cache_write_tokens or 0) > self.input_tokens
        ):
            raise ValueError("cache read/write exceeds inclusive input")
        if self.output_tokens_details and self.output_tokens_details.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning exceeds inclusive output")
        if self.input_tokens + self.output_tokens > 2**53 - 1:
            raise ValueError("unsafe total tokens")
        return self


class ResponsesOutputText(ClosedModel):
    type: Literal["output_text"]
    text: str = Field(max_length=32000)
    annotations: list = Field(default_factory=list, max_length=0)


class ResponsesOutputMessage(ClosedModel):
    id: str = Field(min_length=1, max_length=200)
    type: Literal["message"]
    role: Literal["assistant"]
    status: Literal["completed"]
    phase: Literal["commentary", "final_answer"] | None = None
    content: list[ResponsesOutputText] = Field(min_length=1, max_length=64)


class TaskResponsesResult(ClosedModel):
    id: str = Field(min_length=1, max_length=200)
    status: Literal["completed"]
    output: list[ResponsesOutputMessage | ResponsesReasoningOutput] = Field(min_length=1, max_length=16)
    usage: ResponsesUsage


def normalize_provider_result(document):
    """Normalize documented empty provider metadata at the trusted boundary.

    Keep the closed execution contract: nonempty reasoning content/logprobs and
    unknown fields still fail validation. Preserve inclusive cache-write usage.
    """
    import copy

    result = {key: copy.deepcopy(document.get(key)) for key in ("id", "status", "output", "usage")}
    usage = result["usage"]
    if isinstance(usage, dict) and "total_tokens" in usage:
        total = usage.pop("total_tokens")
        if type(total) is not int or total != usage.get("input_tokens", -1) + usage.get("output_tokens", -1):
            raise ValueError("Provider total usage differs")
    if isinstance(result["output"], list):
        for item in result["output"]:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "reasoning" and item.get("content") == []:
                item.pop("content")
            if item.get("type") == "message" and isinstance(item.get("content"), list):
                for part in item["content"]:
                    if isinstance(part, dict) and part.get("logprobs") == []:
                        part.pop("logprobs")
    return result


# Probe the gateway-normalized text transport, not the legacy reviewer's direct
# SDK/proxy grant. Full tool/reasoning history will require a new contract probe.
TASK_RESPONSES_PROBE_BODY = {
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply OK."}]}],
    "reasoning": {"effort": "medium"},
    "max_output_tokens": 64,
}
TASK_RESPONSES_REQUEST_SHAPE = hashlib.sha256(json.dumps(TASK_RESPONSES_PROBE_BODY, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
