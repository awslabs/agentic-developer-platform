"""Private executor transport never treats a forged response as native evidence."""

import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from l5_services.adapters.finops.private_bridge import PrivateFinOpsBridge
from l5_services.workers.execution import AuthorityRevoked


class ExecutorLambda:
    def __init__(self):
        self.calls = []
        self.result = None

    def invoke(self, *, FunctionName, InvocationType, Payload):
        request = json.loads(Payload)
        self.calls.append((FunctionName, InvocationType, request))
        answer = self.result or {
            "action": request["action"], "operation_id": request["authority"]["operation_id"],
            "attempt_id": request["authority"]["attempt_id"], "result": None,
        }
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(answer).encode())}


async def test_private_executor_is_pinned_to_selected_work_and_read_only_recovery():
    client = ExecutorLambda()
    function_arn = "arn:aws:lambda:us-east-1:000000000000:function:example-l5-finops-executor"
    executor = PrivateFinOpsBridge(client, function_arn, "us-east-1")
    authority = {"operation_id": "run-example", "attempt_id": "work-example", "lease_fence": 2,
                 "provider_identity": {"account_id": "000000000000"},
                 "access_key_id": "test-key", "secret_access_key": "test-secret", "session_token": "test-session",
                 "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}
    intent = {"run_id": "run-example", "attempt_token": "sealed-token"}
    assert await executor.submit(intent=intent, authority=authority) is None
    assert await executor.read_submit(intent=intent, authority=authority) is None
    assert [call[2]["action"] for call in client.calls] == ["submit", "read_submit"]
    assert all(call[0] == function_arn and call[1] == "RequestResponse" for call in client.calls)
    assert client.calls[0][2]["authority"]["lease_fence"] == 2
    assert not {"access_key_id", "secret_access_key", "session_token"} & set(client.calls[0][2]["authority"])
    assert "test-secret" not in json.dumps(client.calls)
    with pytest.raises(PermissionError, match="selected work"):
        await executor.submit(intent={**intent, "run_id": "foreign-run"}, authority=authority)
    with pytest.raises(PermissionError, match="expired"):
        await executor.read_submit(intent=intent, authority={
            **authority, "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()})
    assert len(client.calls) == 2
    client.result = {"action": "observe", "operation_id": "foreign-run",
                     "attempt_id": "work-example", "result": {"outcome": "completed"}}
    with pytest.raises(ConnectionError, match="another work"):
        await executor.observe(native_job_ref={"turn_id": "turn-example"}, authority=authority)
    client.result = {"action": "read_submit", "operation_id": "run-example",
                     "attempt_id": "work-example", "error": "authority_revoked"}
    with pytest.raises(AuthorityRevoked, match="revoked"):
        await executor.read_submit(intent=intent, authority=authority)
    client.result["error"] = "fabricated_success"
    with pytest.raises(ConnectionError, match="not qualified"):
        await executor.read_submit(intent=intent, authority=authority)
    client.result = None
    client.invoke = lambda **_: {"StatusCode": 500}
    with pytest.raises(ConnectionError, match="uncertain"):
        await executor.read_submit(intent=intent, authority=authority)
    with pytest.raises(ValueError, match="private FinOps executor"):
        PrivateFinOpsBridge(client, "arn:aws:lambda:us-east-1:000000000000:function:foreign", "us-east-1")
