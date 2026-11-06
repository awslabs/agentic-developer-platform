"""No-header ordinary endpoint cannot borrow a retained operator UUID."""

from uuid import uuid4

import pytest

from src.agentauth.test_run_admission_store import digest
from tests.internal.test_native_test_tools_gate import native_tools  # noqa: F401
from tests.internal.test_tools_authorize import HEADERS, HUMAN, bearer_identity, decision_api  # noqa: F401


@pytest.mark.parametrize("action", ["call", "recover", "close"])
@pytest.mark.parametrize("binding", ["request", "execution"])
def test_omitted_scope_is_refused_before_ordinary_policy_or_budget(native_tools, monkeypatch, action, binding):  # noqa: F811
    f = native_tools
    bearer_identity(monkeypatch)
    request_id, execution_id = str(uuid4()), str(uuid4())
    if binding == "request":
        key = f.store.key("tenant-one", "REQUEST", digest({"user_id": HUMAN, "request_id": request_id}))
        kind = "native_test_interpreter_request_v1"
        document = {
            "run_id": str(uuid4()),
            "request_id": request_id,
            "execution_id": execution_id,
            "user_id": HUMAN,
            "operation_id": str(uuid4()),
            "tool": "code_interpreter___execute",
        }
    else:
        key = f.store.key("tenant-one", "EXECUTION_OWNER", execution_id)
        kind = "native_test_interpreter_execution_v1"
        document = {"run_id": str(uuid4()), "execution_id": execution_id, "user_id": HUMAN, "operation_id": str(uuid4())}
    f.dynamo.put_item(TableName=f.store.table, Item=f.store.item(key, kind, document))
    body = {
        "schema_version": "1.0",
        "action": action,
        "tool": "code_interpreter___execute",
        "request_id": request_id if binding == "request" else None,
        "execution_id": execution_id if binding == "execution" else None,
    }
    reply = f.client.post("/internal/v1/tools/authorize", json=body, headers={"x-adp-user-authorization": "Bearer signed-login"})
    assert reply.status_code == 403, reply.text
    assert reply.json()["detail"] == "native_operator_original_funding_required"
    f.policy_read.assert_not_called()
    f.budget.assert_not_awaited()
    f.rate.assert_not_awaited()
