"""Lost initial CLI replies remain recoverable after short-TTL provider cleanup."""

import asyncio
import base64
import hashlib
import json
import time
from types import SimpleNamespace

import boto3
import pytest

from tools_facade import executions
from tools_facade.reconciler import run_once
from tools_facade.server import Decision
from tests.e2e.tools.test_cleanup_ac04 import CLI, Driver


@pytest.fixture(params=["interpreter-text", "interpreter-structured", "browser"])
def stateful_gateway(request, monkeypatch):
    for path in ("modules/tools", "modules/tools/agentcore"):
        monkeypatch.syspath_prepend(str(CLI.parents[2] / path))
    if request.param.startswith("interpreter"):
        from modules.tools.agentcore.tests import test_gateway_interpreter_facade as integration

        fixture = integration.gateway.__wrapped__(monkeypatch, SimpleNamespace(param=request.param.split("-")[1]))
        authority, provider = next(fixture)
        calls = provider.calls
        arguments = {}
    else:
        from modules.tools.agentcore.tests import test_gateway_browser_facade as integration

        fixture = integration.gateway.__wrapped__(monkeypatch)
        authority, browser, queue, consumer, calls = next(fixture)

        class ImmediateStart(integration.Provider):
            async def invoke(self, tool, arguments, decision):
                result = await super().invoke(tool, arguments, decision)
                if tool == integration.START:
                    integration.consume(queue, consumer)
                return result

        provider = ImmediateStart()
        arguments = {"url": "https://example.com"}
    try:
        yield authority, provider, calls, integration.START, integration.HEADERS, arguments
    finally:
        fixture.close()


@pytest.mark.parametrize("scheduled_close", [False, True])
def test_original_short_ttl_start_survives_lost_reply_revocation_and_restart(
        stateful_gateway, monkeypatch, tmp_path, scheduled_close):
    authority, provider, calls, tool, headers, arguments = stateful_gateway
    monkeypatch.setenv("BG_CONFIG_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(CLI))
    now = [int(time.time())]
    original_store = executions.ExecutionStore

    class ClockedStore(original_store):
        def __init__(self):
            super().__init__(clock=lambda: now[0])

    monkeypatch.setattr(executions, "ExecutionStore", ClockedStore)
    driver = Driver(provider, gateway=authority, headers=headers)
    response, initial = driver.call(tool, {**arguments, "ttl_seconds": 2})
    assert response["isError"] and initial["status"] == "unknown"
    reference, identifier = initial["operation_ref"], driver.accepted["execution_id"]
    saved = driver.shim.load_operation(reference)
    assert saved["ttl_seconds"] == 2 and not saved.get("execution_id")
    assert calls.count("start") == 1
    db = boto3.client("dynamodb", region_name="us-east-1")
    key = {"execution_id": {"S": identifier}}
    before = db.get_item(TableName="facade-executions", Key=key, ConsistentRead=True)["Item"]
    assert int(before["deadline_at"]["N"]) == now[0] + 2
    browser_session = None
    if tool == "browser___start":
        sessions = boto3.resource("dynamodb", region_name="us-east-1").Table("browser-operations")
        browser_session, = [row for row in sessions.scan(ConsistentRead=True)["Items"]
                            if row["arrived_at"] == "SESSION"]
        assert browser_session["execution_id"] == identifier
        assert browser_session["provider_id"] == "private-provider"
    now[0] += 2
    authority.grants.clear()
    if scheduled_close:
        run_once(ClockedStore())
        now[0] += 60

        def close_provider(item):
            found = asyncio.run(provider.invoke(tool.replace("___start", "___execution_close"),
                {"execution_id": identifier}, Decision.model_validate(authority.decision(tool))))
            return found["result"]["result"]

        run_once(ClockedStore(), stop_provider=close_provider)
        root = db.get_item(TableName="facade-executions", Key=key, ConsistentRead=True)["Item"]
        assert root["cleanup_evidence"]["S"] == "provider_terminated"
    driver.restart()
    # The original TTL is part of the immutable payload, including when no
    # execution ID survived locally. Wrong TTL must not disclose the receipt.
    wrong = {**saved, "ttl_seconds": 3}
    assert json.loads(driver.shim.submit_operation(wrong, recover=True)["content"][0]["text"])["status"] == "unknown"
    driver.shim.store_operation(saved)
    result = driver.call("adp___operation_retry", {"operation_ref": reference})[1]
    assert result["status"] == "confirmed" and result["execution_id"] == identifier
    assert result["receipt_id"] == driver.accepted["receipt_id"] and calls.count("start") == 1
    packet = driver.call("adp___artifact_read", {"operation_ref": reference})[1]
    content = base64.b64decode(packet["data_base64"])
    assert packet["length"] == packet["total_length"] == len(content)
    assert hashlib.sha256(content).hexdigest() == packet["sha256"]
    if browser_session is None:
        assert json.loads(content) == result["result"]
    else:
        # Browser artifacts contain the original provider view, while the
        # public receipt contains an owned handle and its artifact descriptor.
        assert json.loads(content) == {
            "manifest": {"session_id": "private-provider"},
            "observations": [], "view_id": "view-one",
        }
        view = result["result"]["result"]["result"]["result"]
        assert view["session_id"] == browser_session["session_id"] != browser_session["provider_id"]
        assert view["view_id"] == "view-one" and view["observations"] == []
        assert result["artifact"] == view["artifact"] == {
            "artifact_id": saved["request_id"], "content_type": "application/json",
            "byte_length": len(content), "content_sha256": hashlib.sha256(content).hexdigest(),
        }
    closed = driver.call("adp___execution_close", {"operation_ref": reference})[1]
    assert closed["cleanup_status"] == "confirmed"
    after = db.get_item(TableName="facade-executions", Key=key, ConsistentRead=True)["Item"]
    for field in ("generation", "deadline_at", "retention_ttl", "owner_id", "tenant_id", "team_id", "binding"):
        assert after[field] == before[field]
    if browser_session is not None:
        current = sessions.get_item(Key={name: browser_session[name] for name in ("event_id", "arrived_at")},
                                    ConsistentRead=True)["Item"]
        for field in ("execution_id", "start_id", "provider_id", "provider_token", "session_id"):
            assert current[field] == browser_session[field]
    driver.restart()
    assert driver.call("adp___operation_status", {"operation_ref": reference})[1]["result"] == result["result"]
    assert calls.count("start") == 1
