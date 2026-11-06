"""Native provider dispatch bounds with local Dynamo/provider substitutes."""

import time
from uuid import uuid4

import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi import HTTPException

from agentcore_tools.gateway_interpreter import GatewayInterpreter, GatewayInterpreterReconciler
from test_gateway_interpreter import CLAIMS, EXECUTION_ID, execute_args, interpreter, start_args  # noqa: F401


@pytest.fixture
def native(interpreter):  # noqa: F811 - imported pytest fixture
    now = int(time.time())
    binding = {"version": 1, "kind": "effect", "run_id": str(uuid4()), "operation_id": str(uuid4()),
        "reservation_id": CLAIMS["receipt_id"], "execution_id": EXECUTION_ID,
        "request_id": start_args()["request_id"], "tool": "code_interpreter___start",
        "session_deadline_at": now + 300, "cleanup_deadline_at": now + 600, "cleanup_attempts": 2}
    claims = {**CLAIMS, "run_id": binding["run_id"], "workload_id": "offline-native",
              "run_expires_at": now + 600, "execution_deadline_at": now + 300, "native_interpreter": binding}
    start = interpreter.start(start_args(), claims, lambda: (claims, start_args()))
    assert start["status"] == "confirmed"
    return interpreter, claims, start


def effect(claims, tool, request_id):
    return {**claims, "native_interpreter": {**claims["native_interpreter"], "operation_id": str(uuid4()),
            "tool": tool, "request_id": request_id}}


def cleanup(claims, attempt=1):
    original = claims["native_interpreter"]
    return {**claims, "native_interpreter": {"version": 1, "kind": "cleanup", "run_id": original["run_id"],
        "operation_id": str(uuid4()), "original_operation_id": original["operation_id"],
        "execution_id": EXECUTION_ID, "reservation_id": original["reservation_id"],
        "attempt": attempt, "dispatch_deadline_at": time.time() + 30}}


def test_native_root_never_enters_independent_provider_reconciler(native):
    provider, claims, _ = native
    row = provider._read(claims, EXECUTION_ID, "CODE_SESSION")
    assert "native_interpreter" in row and "cleanup_partition" not in row and "next_cleanup_at" not in row
    # Even a stale/malformed index entry cannot bypass the funded facade path.
    provider._update(provider._key(claims, EXECUTION_ID, "CODE_SESSION"), {
        "cleanup_partition": {"S": "due"}, "next_cleanup_at": {"N": "1"}})
    GatewayInterpreterReconciler(provider, clock=lambda: time.time() + 3600).run_once()
    assert [name for name, _ in provider.provider.calls] == ["start"]


@pytest.mark.parametrize("lost_ack", (False, True))
def test_one_native_task_observation_survives_redelivery_and_lost_claim_ack(native, monkeypatch, lost_ack):
    provider, claims, start = native
    arguments = execute_args(start["session_id"])
    execute_claims = effect(claims, "code_interpreter___execute", arguments["request_id"])
    execute = provider.execute(arguments, execute_claims, lambda: (execute_claims, arguments))
    result_args = {"execution_id": EXECUTION_ID, "request_id": arguments["request_id"],
                   "session_id": start["session_id"], "provider_id": execute["provider_id"]}
    result_claims = effect(claims, "code_interpreter___result", arguments["request_id"])
    provider.provider.outcome = {"taskStatus": "running"}
    original_put = provider.client.put_item

    def put(**kwargs):
        reply = original_put(**kwargs)
        if lost_ack and kwargs["Item"]["arrived_at"]["S"].startswith("CODE_NATIVE_POLL#"):
            raise EndpointConnectionError(endpoint_url="https://offline.invalid")
        return reply

    monkeypatch.setattr(provider.client, "put_item", put)
    for _ in range(3):
        assert provider.result(result_args, result_claims)["status"] == "pending"
    assert [name for name, _ in provider.provider.calls].count("getTask") == (0 if lost_ack else 1)


def test_cleanup_attempt_is_shared_and_never_replayed_or_replaced(native):
    provider, claims, _ = native
    arguments = {"execution_id": EXECUTION_ID}
    provider.provider.read_status = "READY"
    for attempt in (1, 1, 2, 2):
        close_claims = cleanup(claims, attempt)
        found = provider.execution_close(arguments, close_claims, lambda: (close_claims, arguments))
        assert found.get("termination_confirmed") is not True
    names = [name for name, _ in provider.provider.calls]
    assert names.count("get_session") == 4 and names.count("stop") == 2
    with pytest.raises(HTTPException):
        provider.execution_close(arguments, cleanup(claims, 3), lambda: None)
    row = provider._read(claims, EXECUTION_ID, "CODE_SESSION")
    assert "cleanup_partition" not in row and "next_cleanup_at" not in row


@pytest.mark.parametrize("mutation", ("missing", "owner_run", "root_operation", "reservation", "expired"))
def test_native_cleanup_cannot_downgrade_or_change_original_binding(native, mutation):
    provider, claims, _ = native
    close_claims = cleanup(claims)
    if mutation == "missing":
        del close_claims["native_interpreter"]
    else:
        field = {"owner_run": "run_id", "root_operation": "original_operation_id",
                 "reservation": "reservation_id", "expired": "dispatch_deadline_at"}[mutation]
        close_claims["native_interpreter"][field] = time.time() - 1 if mutation == "expired" else "other"
    with pytest.raises(HTTPException):
        provider.execution_close({"execution_id": EXECUTION_ID}, close_claims, lambda: None)
    assert [name for name, _ in provider.provider.calls] == ["start"]


def test_lost_cleanup_claim_ack_cannot_send_provider_observation(native, monkeypatch):
    provider, claims, _ = native
    original_put = provider.client.put_item

    def lost(**kwargs):
        original_put(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://offline.invalid")

    monkeypatch.setattr(provider.client, "put_item", lost)
    arguments = {"execution_id": EXECUTION_ID}
    for _ in range(2):
        close_claims = cleanup(claims)
        response = provider.execution_close(arguments, close_claims, lambda: (close_claims, arguments))
        assert response["status"] == "pending"
    assert [name for name, _ in provider.provider.calls] == ["start"]


def test_confirmed_native_cleanup_returns_original_proof_without_new_provider_read(native):
    provider, claims, _ = native
    arguments = {"execution_id": EXECUTION_ID}
    first = cleanup(claims)
    response = provider.execution_close(arguments, first, lambda: (first, arguments))
    assert response["status"] == "confirmed" and response["termination_confirmed"] is True
    names = [name for name, _ in provider.provider.calls]
    second = cleanup(claims, 2)
    assert provider.execution_close(arguments, second, lambda: (second, arguments)) == response
    assert [name for name, _ in provider.provider.calls] == names


def test_gateway_interpreter_pins_dynamo_sdk_attempts(native, monkeypatch):
    provider, _, _ = native
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    configured = GatewayInterpreter(provider=provider.provider)
    assert configured.client.meta.config.retries["total_max_attempts"] == 1
    assert configured.client.meta.config.connect_timeout == 2
    assert configured.client.meta.config.read_timeout == 3
def test_native_stream_limits_tiny_event_count_and_closes():
    from types import SimpleNamespace
    from agentcore_tools.code_interpreter import CodeInterpreter

    class Events:
        closed, count = False, 0
        def __iter__(self):
            for _ in range(100):
                self.count += 1
                yield {"result": {"structuredContent": {}}}
        def close(self):
            self.closed = True

    events = Events()
    provider = SimpleNamespace(invoke_code_interpreter=lambda **kwargs: {"stream": events})
    with pytest.raises(HTTPException, match="stream exceeds"):
        CodeInterpreter(None, None, "offline", provider=provider).invoke("offline", "getTask", {}, native=True)
    assert events.closed and events.count == 65


def test_native_raw_stream_stops_consuming_after_bound_and_deadline(monkeypatch):
    from agentcore_tools.code_interpreter import NativeInterpreterStream
    from agentcore_tools import code_interpreter

    class Raw:
        count, closed = 0, False
        def stream(self, *args, **kwargs):
            for _ in range(100):
                self.count += 1
                yield b"x" * 1024
        def close(self):
            self.closed = True

    monkeypatch.setattr(code_interpreter.time, "monotonic", lambda: 1000)
    raw = Raw()
    stream = NativeInterpreterStream(raw, 1020)
    with pytest.raises(HTTPException, match="stream exceeds"):
        list(stream.stream())
    assert raw.count == 65
    stream.close()
    assert raw.closed
    late = Raw()
    with pytest.raises(HTTPException, match="stream exceeds"):
        list(NativeInterpreterStream(late, 1000).stream())
    assert late.count == 1
