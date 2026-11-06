"""Exact fixed tool payloads with bounded, owner-checked operation references.

This policy is selected by a protected contract and a registered server adapter.
Only operation UUIDs and the existing opaque session/view/result references may
vary. The quote still hashes the entire actual request; replay changes conflict.
The facade/provider source must enforce original-run ownership of those handles.
"""

from __future__ import annotations

import json
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.agentauth.native_test_effect_contract import EffectCostContract, ProtectedEffectQuoteAdapter
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical, digest

MAX_CALL_BYTES = 65536
MAX_RESPONSE_BYTES = 262144
TOOL_PATTERN = r"^[a-z][a-z0-9_]{0,63}___[a-z][a-z0-9_]{0,63}$"
STATEFUL_STARTS = frozenset({"browser___start", "code_interpreter___start"})
OPAQUE_FIELDS = {
    "browser___step": {"session_id", "view_id"},
    "browser___inspect": {"session_id", "view_id"},
    "code_interpreter___execute": {"session_id"},
    "code_interpreter___result": {"session_id", "provider_id"},
    "code_interpreter___file": {"session_id"},
}
SUPPORTED_CALLS = STATEFUL_STARTS | OPAQUE_FIELDS.keys() | {"websearch___search"}
KNOWLEDGE_READS = frozenset(
    {
        "knowledge___search",
        "knowledge___understand",
        "knowledge___impact",
        "knowledge___browse",
        "knowledge___secure",
    }
)
ATLASSIAN_CALLS = frozenset(
    "atlassian___" + name
    for name in (
        "search_jira",
        "get_jira_issue",
        "search_confluence",
        "get_confluence_page",
        "create_jira_issue",
        "update_jira_issue",
        "comment_jira_issue",
        "transition_jira_issue",
    )
)
AUTHORITY_ARGUMENTS = frozenset(
    {
        "adp_assertion",
        "request_id",
        "execution_id",
        "principal",
        "user_id",
        "tenant_id",
        "owner_sub",
        "run_id",
        "workload_id",
        "receipt_id",
    }
)


def atlassian_call_permitted(tool, arguments):
    """The fixed operation contract still binds every provider argument."""
    return (
        tool in ATLASSIAN_CALLS
        and isinstance(arguments, dict)
        and not AUTHORITY_ARGUMENTS.intersection(arguments)
        and isinstance(arguments.get("cloudId"), str)
        and 0 < len(arguments["cloudId"]) <= 128
    )


def knowledge_read_permitted(tool, arguments):
    """Only the existing private read paths; a quote never enables writes."""
    if tool not in KNOWLEDGE_READS or not isinstance(arguments, dict):
        return False
    if {
        "adp_assertion",
        "request_id",
        "execution_id",
        "principal",
        "user_id",
        "tenant_id",
        "owner_sub",
        "run_id",
        "workload_id",
        "receipt_id",
    }.intersection(arguments):
        return False
    if tool == "knowledge___search":
        return arguments.get("scope", "code") == "code" and not arguments.get("product_id")
    if tool == "knowledge___browse":
        return arguments.get("action") in {"ls", "tree", "read", "info"}
    if tool == "knowledge___secure":
        return arguments.get("action") in {"identify", "plan", "verify"}
    return True


class NativeFacadeCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["1.0"]
    action: Literal["call", "list", "recover"]
    tool: str | None = Field(pattern=TOOL_PATTERN)
    arguments: dict
    request_id: str | None
    execution_id: str | None
    ttl_seconds: int | None = Field(ge=1, le=300)

    @classmethod
    def parse(cls, body):
        try:
            if type(body) is not bytes or not 0 < len(body) <= MAX_CALL_BYTES:
                raise ValueError
            value = json.loads(body)
            call = cls.model_validate(value)
            if call.action == "list":
                if (
                    canonical(call.model_dump()).encode() != body
                    or call.tool is not None
                    or call.arguments
                    or call.request_id is not None
                    or call.execution_id is not None
                    or call.ttl_seconds is not None
                ):
                    raise ValueError
                return call
            if canonical(call.model_dump()).encode() != body or str(UUID(call.request_id, version=4)) != call.request_id:
                raise ValueError
            if call.execution_id is not None and str(UUID(call.execution_id, version=4)) != call.execution_id:
                raise ValueError
            if call.action == "recover" and (
                call.tool != "code_interpreter___execute"
                or call.execution_id is None
                or call.ttl_seconds is not None
                or set(call.arguments) != {"session_id", "code", "language"}
            ):
                raise ValueError
            if call.ttl_seconds is not None and call.tool not in STATEFUL_STARTS:
                raise ValueError
            if (
                call.tool not in SUPPORTED_CALLS
                and not knowledge_read_permitted(call.tool, call.arguments)
                and not atlassian_call_permitted(call.tool, call.arguments)
            ):
                # Closing uses the original bounded cleanup allowance, never a
                # fresh ordinary effect authorization or fresh session identity.
                raise ValueError
            if call.tool in KNOWLEDGE_READS | ATLASSIAN_CALLS and call.execution_id is not None:
                # Each bounded read owns its original execution. A caller may
                # recover/close that execution, never extend it with new work.
                raise ValueError
            return call
        except (ValueError, TypeError, AttributeError):
            raise AdmissionRefusedError("native_test_facade_payload_refused") from None

    def approved_digest(self):
        if self.action == "list":
            return digest({"request_binding": "facade_list_v1", "max_pages": 1})
        arguments = dict(self.arguments)
        for name in OPAQUE_FIELDS.get(self.tool, ()):
            value = arguments.get(name)
            pattern = r"[A-Za-z0-9_.:-]{1,128}" if name == "provider_id" else r"[a-f0-9]{64}"
            if self.execution_id is None or not isinstance(value, str) or re.fullmatch(pattern, value) is None:
                raise AdmissionRefusedError("native_test_facade_payload_refused")
            arguments[name] = {"original_owned_reference": name}
        return digest(
            {
                "request_binding": "facade_recover_v1" if self.action == "recover" else "facade_call_v1",
                "tool": self.tool,
                "arguments": arguments,
                "execution_binding": "new" if self.execution_id is None else "owned",
                "ttl_seconds": (self.ttl_seconds or 300) if self.tool in STATEFUL_STARTS else None,
            }
        )


class FacadeEffectCostContract(EffectCostContract):
    request_binding: Literal["facade_call_v1"]
    max_call_bytes: Literal[65536]
    max_response_bytes: Literal[262144]
    transport_attempts: Literal[1]
    claim_attempts: Literal[1]


class InterpreterStepCostContract(FacadeEffectCostContract):
    """A protected one-use slot for a single original Interpreter journey."""

    interpreter_step: Literal["list", "start", "execute", "execute_status", "result", "file"]
    max_step_requests: Literal[1]


class FacadeEffectQuoteAdapter(ProtectedEffectQuoteAdapter):
    def _request_permitted(self, body, contract):
        call = NativeFacadeCall.parse(body)
        if type(contract) is InterpreterStepCostContract:
            step = contract.interpreter_step
            if (
                call.action != ("list" if step == "list" else "recover" if step == "execute_status" else "call")
                or step != "list"
                and call.tool != "code_interpreter___" + ("execute" if step == "execute_status" else step)
                or (call.execution_id is None) != (step in {"list", "start"})
            ):
                return False
        return (
            type(contract) in {FacadeEffectCostContract, InterpreterStepCostContract}
            and call.approved_digest() == contract.request_sha256
            and (call.tool not in STATEFUL_STARTS or (call.ttl_seconds or 300) <= contract.session_seconds)
        )

    def _quote_request_permitted(self, quote, contract):
        # The quote binds full actual bytes. Revalidation checks their normalized
        # protected payload; operation/owner/source bindings remain in the journal.
        return type(contract) in {FacadeEffectCostContract, InterpreterStepCostContract}
