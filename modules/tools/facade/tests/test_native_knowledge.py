"""Native Knowledge transports the original binding outside Door arguments."""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tools_facade.native_admission import NativeAdmission, NativeFacadeHandoff, canonical_call
from tools_facade.server import Decision, ToolCall

pytestmark = pytest.mark.asyncio


def fixture(tool="knowledge___search"):
    operation, execution, request = (str(uuid4()) for _ in range(3))
    call = ToolCall(tool=tool, arguments={"query": "private snowman ☃"}, request_id=request)
    decision = Decision.model_validate(
        {
            "schema_version": "1.0",
            "allowed": True,
            "receipt_id": "original-reservation",
            "principal": {"user_id": "owner", "root_human_id": "owner", "tenant_id": "tenant", "team_id": "team"},
            "run_id": str(uuid4()),
            "workload_id": "native-test-" + str(uuid4()),
            "run_expires_at": 1300,
        }
    )
    admission = NativeAdmission(
        operation_id=operation,
        execution_id=execution,
        proof="a" * 43,
        reservation_id=decision.receipt_id,
        quoted_usd="8",
        reserved_usd="8",
        run_cap_usd="20",
        request_sha256=hashlib.sha256(
            canonical_call({"schema_version": "1.0", "action": "call", **call.model_dump(mode="json")})
        ).hexdigest(),
        new_execution=True,
        session_deadline_at=1300,
        cleanup_deadline_at=1600,
        cleanup_attempts=2,
        frozen_grants=[call.tool],
    )
    gateway = SimpleNamespace(
        dispatch_native=AsyncMock(
            return_value={
                "schema_version": "1.0",
                "operation_id": operation,
                "reservation_id": decision.receipt_id,
                "status": "dispatch_started",
                "dispatch_deadline_at": 1299,
            }
        )
    )
    handoff = NativeFacadeHandoff(
        gateway=gateway,
        admission=admission,
        call=call,
        headers={"x-adp-native-test-operation": operation},
        decision=decision,
        clock=lambda: 1000,
    )
    return handoff, call, decision


@pytest.mark.parametrize("change", [
    {"quoted_usd": None}, {"quoted_usd": "0"}, {"quoted_usd": "NaN"},
    {"quoted_usd": "21"}, {"reserved_usd": "7"}, {"run_cap_usd": "100"},
])
async def test_native_handoff_refuses_unpriced_or_unreserved_dispatch(change):
    handoff, call, decision = fixture()
    with pytest.raises(ValueError):
        admission = NativeAdmission.model_validate({**handoff.admission.model_dump(), **change})
        NativeFacadeHandoff(
            gateway=handoff.gateway, admission=admission, call=call,
            headers=handoff.headers, decision=decision, clock=lambda: 1000,
        )
    handoff.gateway.dispatch_native.assert_not_awaited()


async def test_exact_door_arguments_and_separate_signed_native_descriptor():
    handoff, call, decision = fixture()
    await handoff.begin()
    handoff.before_sign("tools/call", call.tool, call.arguments, decision)
    assert handoff.knowledge_binding() == {
        "operation_id": handoff.admission.operation_id,
        "execution_id": handoff.admission.execution_id,
        "request_id": str(call.request_id),
        "request_sha256": handoff.admission.request_sha256,
    }
    handoff.before_send()
    with pytest.raises(ValueError):
        handoff.before_sign("tools/call", call.tool, call.arguments, decision)


@pytest.mark.parametrize("tool", ["atlassian___search_jira", "atlassian___create_jira_issue"])
async def test_atlassian_keeps_original_references_outside_provider_arguments(tool):
    handoff, call, decision = fixture(tool)
    await handoff.begin()
    with pytest.raises(ValueError):
        handoff.before_sign("tools/call", tool, {**call.arguments, "request_id": str(call.request_id)}, decision)
    handoff.before_sign("tools/call", tool, call.arguments, decision)
    assert handoff.atlassian_binding() == {
        "operation_id": handoff.admission.operation_id,
        "execution_id": handoff.admission.execution_id,
        "request_id": str(call.request_id),
        "request_sha256": handoff.admission.request_sha256,
    }
    with pytest.raises(ValueError):
        handoff.knowledge_binding()
    with pytest.raises(ValueError):
        handoff.before_sign("tools/call", tool, call.arguments, decision)


@pytest.mark.parametrize("changed", ["arguments", "injected_ids", "owner", "method", "tool"])
async def test_native_proof_cannot_sign_substituted_knowledge_request(changed):
    handoff, call, decision = fixture()
    await handoff.begin()
    method, tool, arguments = "tools/call", call.tool, dict(call.arguments)
    if changed == "arguments":
        arguments["query"] = "foreign"
    elif changed == "injected_ids":
        arguments.update(request_id=str(call.request_id), execution_id=handoff.admission.execution_id)
    elif changed == "owner":
        decision.principal.user_id = "foreign"
    elif changed == "method":
        method = "tools/list"
    else:
        tool = "knowledge___remember"
    with pytest.raises(ValueError):
        handoff.before_sign(method, tool, arguments, decision)
