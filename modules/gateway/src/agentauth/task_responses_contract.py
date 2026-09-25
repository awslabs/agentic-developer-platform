"""Explicit, bounded inline Responses contract for Task Codex transport.

Model, destination, service tier, server state and credentials are host-owned.
Encrypted reasoning is inline; executable tool history is not admitted yet. Admission
uses a distinct revision/probe and never borrows Messages or reviewer evidence.
"""

from __future__ import annotations

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
    max_output_tokens: int = Field(strict=True, ge=1, le=4096)

    @model_validator(mode="after")
    def bounded_messages(self):
        if isinstance(self.input, list) and len(self.input) > 64:
            raise ValueError("too many input messages")
        return self


class ResponsesInputDetails(ClosedModel):
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
