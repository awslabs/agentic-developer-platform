"""Exercise MCP metadata at every private mediator's actual dispatch boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.internal import tools_github, tools_jira, tools_knowledge, tools_slack


@pytest.fixture(
    params=[
        (tools_knowledge, "search", {"query": "fixture"}),
        (tools_github, "get_user", {}),
        (tools_jira, "get_issue", {"cloudId": "00000000-0000-4000-8000-000000000001", "issueIdOrKey": "TEST-1"}),
        (tools_slack, "get_user", {"user": "U0123456789"}),
    ]
)
def mediator(request, monkeypatch):
    module, operation, arguments = request.param
    monkeypatch.setattr(module, "current_binding", lambda **kwargs: {})
    verify = AsyncMock(side_effect=HTTPException(403, "fixture authority refused"))
    monkeypatch.setattr(module, "verify_facade", verify)
    runtime = SimpleNamespace(env={"ADP_DOOR_SERVICE_URL": "http://context-mcp.agent-context.svc.cluster.local:5100"})
    packet = {
        "jsonrpc": "2.0",
        "id": "request-one",
        "method": "tools/call",
        "params": {
            "name": operation,
            "arguments": {**arguments, "adp_assertion": "fixture-assertion"},
        },
    }
    serve = module._serve if module is tools_knowledge else module.serve
    return SimpleNamespace(module=module, serve=serve, verify=verify, runtime=runtime, packet=packet, arguments=arguments)


async def dispatch(mediator, packet):
    if mediator.module is tools_knowledge:
        return await mediator.serve(None, packet, mediator.runtime)
    return await mediator.serve(packet, mediator.runtime)


@pytest.mark.parametrize("metadata", [None, {}, {"progressToken": "progress-one", "principal": "foreign"}])
async def test_transport_metadata_reaches_existing_authority_checks_without_becoming_arguments(mediator, metadata):
    mediator.packet["params"]["_meta"] = metadata
    with pytest.raises(HTTPException) as failure:
        await dispatch(mediator, mediator.packet)
    assert failure.value.status_code == 403 and failure.value.detail == "fixture authority refused"
    mediator.verify.assert_awaited_once()
    assert mediator.verify.call_args.args[2] == mediator.arguments
    assert mediator.packet["params"]["_meta"] == metadata


@pytest.mark.parametrize("metadata", [False, 0, "authority", ["progress"]])
async def test_malformed_transport_metadata_is_refused_before_authority_or_provider_use(mediator, metadata):
    mediator.packet["params"]["_meta"] = metadata
    with pytest.raises(HTTPException) as failure:
        await dispatch(mediator, mediator.packet)
    assert failure.value.status_code == 400
    mediator.verify.assert_not_awaited()


@pytest.mark.parametrize("method", ["initialize", "notifications/initialized", "ping"])
async def test_protocol_control_messages_accept_transport_metadata_without_user_authority(mediator, method):
    packet = {"jsonrpc": "2.0", "method": method, "params": {"_meta": {"progressToken": "progress-one"}}}
    if method != "notifications/initialized":
        packet["id"] = "control-one"
    if method == "initialize":
        packet["params"]["protocolVersion"] = "2025-06-18"
    response = await dispatch(mediator, packet)
    assert response.status_code == (202 if method == "notifications/initialized" else 200)
    mediator.verify.assert_not_awaited()
