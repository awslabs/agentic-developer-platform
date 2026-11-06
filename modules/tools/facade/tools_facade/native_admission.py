"""Private, single-use consumer of a gateway native effect admission.

An allow document is insufficient: the gateway must acknowledge its durable
dispatch CAS. Neither a lost acknowledgement nor a provider failure is retried.
"""

import hashlib
import json
import math
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tools_facade.encoding import canonical


def canonical_call(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


class NativeAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    operation_id: str
    proof: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)
    reservation_id: str
    quoted_usd: str = Field(min_length=1, max_length=80)
    reserved_usd: str = Field(min_length=1, max_length=80)
    run_cap_usd: Literal["20"]
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_id: str
    new_execution: bool
    session_deadline_at: int
    cleanup_deadline_at: int
    cleanup_attempts: int = Field(ge=1, le=32)
    frozen_grants: list[str] = Field(max_length=64)


class NativeFacadeHandoff:
    def __init__(self, *, gateway, admission, call, headers, decision, clock=None, action="call"):
        self.gateway, self.admission = gateway, admission
        self.payload = (
            {"schema_version": "1.0", "action": action, **call.model_dump(mode="json")}
            if call is not None
            else {
                "schema_version": "1.0",
                "action": "list",
                "tool": None,
                "arguments": {},
                "request_id": None,
                "execution_id": None,
                "ttl_seconds": None,
            }
        )
        self.headers = dict(headers)
        self.clock = clock or time.time
        self.used, self.submitted, self.deadline = False, False, None
        self.decision_binding = self._binding(decision)
        try:
            quoted = Decimal(admission.quoted_usd)
            reserved = Decimal(admission.reserved_usd)
            if not quoted.is_finite() or quoted <= 0 or quoted != reserved or quoted > Decimal(admission.run_cap_usd):
                raise ValueError("Native tool quote or reservation unavailable")
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("Native tool quote or reservation unavailable") from exc
        if (
            str(UUID(admission.operation_id)) != admission.operation_id
            or str(UUID(admission.execution_id)) != admission.execution_id
            or hashlib.sha256(canonical_call(self.payload)).hexdigest() != admission.request_sha256
            or admission.reservation_id != decision.receipt_id
            or headers.get("x-adp-native-test-operation") != admission.operation_id
            or admission.new_execution != (call is not None and call.execution_id is None)
            or call is not None
            and call.execution_id is not None
            and str(call.execution_id) != admission.execution_id
            or not self.clock() < admission.session_deadline_at <= self.clock() + 300
            or not admission.session_deadline_at <= admission.cleanup_deadline_at <= self.clock() + 600
            or call is not None
            and call.tool not in admission.frozen_grants
        ):
            raise ValueError("Native tool handoff binding changed")

    @staticmethod
    def _binding(decision):
        return canonical(
            {
                "principal": decision.principal.model_dump(),
                "receipt_id": decision.receipt_id,
                "run_id": decision.run_id,
                "workload_id": decision.workload_id,
                "run_expires_at": decision.run_expires_at,
            }
        )

    async def begin(self):
        if self.used:
            raise ValueError("Native tool handoff already consumed")
        self.used = True
        reply = await self.gateway.dispatch_native(self.payload, self.admission.proof, self.headers)
        if (
            not isinstance(reply, dict)
            or set(reply) != {"schema_version", "operation_id", "reservation_id", "status", "dispatch_deadline_at"}
            or reply["schema_version"] != "1.0"
            or reply["status"] != "dispatch_started"
            or reply["operation_id"] != self.admission.operation_id
            or reply["reservation_id"] != self.admission.reservation_id
            or type(reply["dispatch_deadline_at"]) not in (int, float)
            or not math.isfinite(reply["dispatch_deadline_at"])
        ):
            raise ValueError("Native tool dispatch unconfirmed")
        self.deadline = min(reply["dispatch_deadline_at"], self.admission.session_deadline_at)
        self.require_fresh()

    def require_fresh(self):
        if not self.used or self.deadline is None or self.clock() >= self.deadline:
            raise ValueError("Native tool dispatch expired")

    def before_sign(self, method, tool, arguments, decision):
        self.require_fresh()
        original = self.payload
        if original["action"] == "list":
            if (
                self.submitted
                or self._binding(decision) != self.decision_binding
                or method != "tools/list"
                or tool != "tools/list"
                or arguments != {}
            ):
                raise ValueError("Native discovery provider binding changed")
            self.submitted = True
            return
        gateway_tool = original["tool"].replace("code_interpreter___", "code-interpreter___", 1)
        expected = dict(original["arguments"])
        if not original["tool"].startswith(("knowledge___", "atlassian___")):
            expected.update(execution_id=self.admission.execution_id, request_id=original["request_id"])
        if (
            self.submitted
            or self._binding(decision) != self.decision_binding
            or method != "tools/call"
            or tool != gateway_tool
            or canonical(arguments) != canonical(expected)
        ):
            raise ValueError("Native tool provider binding changed")
        self.submitted = True

    def knowledge_binding(self):
        """Signed transport metadata; never inject operation IDs into Door args."""
        self.require_fresh()
        if self.payload["action"] != "call" or not self.payload["tool"].startswith("knowledge___"):
            raise ValueError("Native Knowledge binding unavailable")
        return {
            "operation_id": self.admission.operation_id,
            "execution_id": self.admission.execution_id,
            "request_id": self.payload["request_id"],
            "request_sha256": self.admission.request_sha256,
        }

    def interpreter_binding(self):
        self.require_fresh()
        if self.payload["action"] != "call" or not self.payload["tool"].startswith("code_interpreter___"):
            raise ValueError("Native Interpreter binding unavailable")
        return {
            "version": 1, "kind": "effect", "run_id": json.loads(self.decision_binding)["run_id"],
            "operation_id": self.admission.operation_id, "reservation_id": self.admission.reservation_id,
            "execution_id": self.admission.execution_id, "request_id": self.payload["request_id"],
            "tool": self.payload["tool"], "session_deadline_at": self.admission.session_deadline_at,
            "cleanup_deadline_at": self.admission.cleanup_deadline_at,
            "cleanup_attempts": self.admission.cleanup_attempts,
        }

    def atlassian_binding(self):
        """Carry original funding references outside provider arguments."""
        self.require_fresh()
        if self.payload["action"] != "call" or not self.payload["tool"].startswith("atlassian___"):
            raise ValueError("Native Atlassian binding unavailable")
        return {
            "operation_id": self.admission.operation_id,
            "execution_id": self.admission.execution_id,
            "request_id": self.payload["request_id"],
            "request_sha256": self.admission.request_sha256,
        }

    def before_send(self):
        self.require_fresh()
        if not self.submitted:
            raise ValueError("Native tool dispatch unconfirmed")


class NativeInterpreterRecoveryHandoff(NativeFacadeHandoff):
    """One original operation read, then at most one correlated paid task poll."""

    def __init__(self, *, operation, **kwargs):
        super().__init__(action="recover", **kwargs)
        call = self.payload
        if (call["tool"] != "code_interpreter___execute" or call["execution_id"] is None
                or call["ttl_seconds"] is not None or set(call["arguments"]) != {"session_id", "code", "language"}
                or operation["request_id"]["S"] != call["request_id"]
                or operation["operation_execution_id"]["S"] != call["execution_id"]
                or operation["request_digest"]["S"] != hashlib.sha256(canonical(call["arguments"])).hexdigest()):
            raise ValueError("Original native execute required")
        self.original_receipt = operation["receipt_id"]["S"]
        self.phase, self.poll_arguments, self.signed_tool = 0, None, None

    def before_sign(self, method, tool, arguments, decision):
        self.require_fresh()
        expected = {"execution_id": self.admission.execution_id, "request_id": self.payload["request_id"]}
        expected_tool = "code-interpreter___operation_get"
        if self.phase == 1:
            expected, expected_tool = self.poll_arguments, "code-interpreter___result"
        if (self.phase > 1 or expected is None or method != "tools/call" or tool != expected_tool
                or self._binding(decision) != self.decision_binding or canonical(arguments) != canonical(expected)):
            raise ValueError("Native recovery provider binding changed")
        self.phase += 1
        self.submitted = True
        self.signed_tool = tool.replace("code-interpreter___", "code_interpreter___", 1)

    def observe_original(self, response):
        self.require_fresh()
        wrapper = response.get("result", {})
        receipt = wrapper.get("result", {})
        principal = json.loads(self.decision_binding)["principal"]
        if (self.phase != 1 or self.poll_arguments is not None
                or wrapper.get("user_id") != principal["user_id"]
                or wrapper.get("tenant_id") != principal["tenant_id"]
                or receipt.get("execution_id") != self.admission.execution_id
                or receipt.get("request_id") != self.payload["request_id"]
                or receipt.get("receipt_id") != self.original_receipt
                or receipt.get("session_id") != self.payload["arguments"]["session_id"]):
            raise ValueError("Original native execute receipt changed")
        if receipt.get("status") == "pending":
            task = receipt.get("provider_id")
            if not isinstance(task, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", task) is None:
                raise ValueError("Original native execute task unavailable")
            self.poll_arguments = {
                "execution_id": self.admission.execution_id, "request_id": self.payload["request_id"],
                "session_id": receipt["session_id"], "provider_id": task,
            }

    def interpreter_binding(self):
        self.require_fresh()
        if self.signed_tool is None:
            raise ValueError("Native recovery dispatch unconfirmed")
        return {
            "version": 1, "kind": "effect", "run_id": json.loads(self.decision_binding)["run_id"],
            "operation_id": self.admission.operation_id, "reservation_id": self.admission.reservation_id,
            "execution_id": self.admission.execution_id, "request_id": self.payload["request_id"],
            "tool": self.signed_tool, "session_deadline_at": self.admission.session_deadline_at,
            "cleanup_deadline_at": self.admission.cleanup_deadline_at,
            "cleanup_attempts": self.admission.cleanup_attempts,
        }


class NativeCleanupAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    run_id: str
    operation_id: str
    original_operation_id: str
    execution_id: str
    reservation_id: str
    attempt: int = Field(ge=1, le=32)
    status: str = Field(pattern=r"^dispatch_started$")
    started_at: float
    dispatch_deadline_at: float
    replay: bool


class NativeCleanupHandoff:
    def __init__(self, *, admission, decision, execution_id, tool, headers, clock=None):
        self.admission, self.clock = admission, clock or time.time
        self.decision = decision
        self.decision_binding = NativeFacadeHandoff._binding(decision)
        self.tool, self.submitted = tool, False
        if (
            admission.replay
            or admission.execution_id != str(execution_id)
            or admission.operation_id != headers.get("x-adp-native-test-operation")
            or admission.reservation_id != decision.receipt_id
            or admission.run_id != decision.run_id
            or not math.isfinite(admission.dispatch_deadline_at)
        ):
            raise ValueError("Native cleanup binding changed")
        self.require_fresh()

    def require_fresh(self):
        if self.clock() >= self.admission.dispatch_deadline_at:
            raise ValueError("Native cleanup horizon expired")

    def before_sign(self, method, tool, arguments, decision):
        self.require_fresh()
        expected = self.tool.replace("___start", "___execution_close").replace(
            "code_interpreter___", "code-interpreter___", 1
        )
        if (
            self.submitted
            or self.tool not in {"browser___start", "code_interpreter___start"}
            or NativeFacadeHandoff._binding(decision) != self.decision_binding
            or method != "tools/call"
            or tool != expected
            or arguments != {"execution_id": self.admission.execution_id}
        ):
            raise ValueError("Native cleanup provider binding changed")
        self.submitted = True

    def before_send(self):
        self.require_fresh()
        if not self.submitted:
            raise ValueError("Native cleanup dispatch unconfirmed")

    def interpreter_binding(self):
        self.require_fresh()
        if self.tool != "code_interpreter___start":
            raise ValueError("Native Interpreter cleanup unavailable")
        return {
            "version": 1, "kind": "cleanup", "run_id": self.admission.run_id,
            "operation_id": self.admission.operation_id, "original_operation_id": self.admission.original_operation_id,
            "reservation_id": self.admission.reservation_id, "execution_id": self.admission.execution_id,
            "attempt": self.admission.attempt, "dispatch_deadline_at": self.admission.dispatch_deadline_at,
        }
