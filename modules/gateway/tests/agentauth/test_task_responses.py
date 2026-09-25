"""Responses preserves Task receipts/authority and uses gateway-owned routing."""

# ruff: noqa: F811
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from src.agentauth import task_responses as module
from src.agentauth.task_responses_contract import TaskResponsesRequest, TaskResponsesResult
from src.agentauth.task_runtime_routes import ModelBody
from src.tasks.records import payload_digest
from src.tasks.store import TaskStoreError
from tests.agentauth.test_task_model import _make_model_fixture, execute, model  # noqa: F401
from tests.agentauth.test_task_runtime import runtime  # noqa: F401
from tests.tasks.test_store import client, store  # noqa: F401


def request():
    return {
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "fixture"}]}],
        "reasoning": {"effort": "medium"},
        "max_output_tokens": 64,
    }


def result():
    return {
        "id": "resp_fixture",
        "status": "completed",
        "output": [
            {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "fixture", "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 20,
            "output_tokens": 10,
            "input_tokens_details": {"cached_tokens": 4},
            "output_tokens_details": {"reasoning_tokens": 2},
        },
    }


def test_route_roundtrip_preserves_exact_digest_without_messages_fields():
    uid = "00000000-0000-4000-8000-000000000001"
    body = {
        "schema_version": "1.0",
        "attempt": {"run": {"task_id": "tsk_" + uid, "invocation_id": uid, "generation": 1}, "runtime_attempt_id": uid},
        "turn_id": uid,
        "request_digest": payload_digest(request()),
        "responses_request": request(),
    }
    parsed = ModelBody.model_validate(body)
    assert parsed.invocation() == request()
    assert payload_digest(parsed.invocation()) == body["request_digest"]
    for extra in ({"max_tokens": 64}, {"system": "override"}, {"messages": [{"role": "user", "content": [{"type": "text", "text": "ambiguous"}]}]}):
        with pytest.raises(ValidationError):
            ModelBody.model_validate({**body, **extra})


@pytest.mark.parametrize(
    "override",
    [
        {"model": "override"},
        {"endpoint": "https://example.invalid"},
        {"store": True},
        {"previous_response_id": "foreign"},
        {"tools": []},
        {"input": [{"type": "function_call", "name": "shell"}]},
        {"max_output_tokens": True},
    ],
)
def test_responses_contract_rejects_unsupported_authority_and_history(override):
    with pytest.raises(ValidationError):
        TaskResponsesRequest.model_validate({**request(), **override})


def test_responses_usage_is_inclusive_and_reasoning_is_bounded():
    assert TaskResponsesResult.model_validate(result()).usage.input_tokens == 20
    for usage in (
        {"input_tokens": 20, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 21}},
        {"input_tokens": 20, "output_tokens": 10, "output_tokens_details": {"reasoning_tokens": 11}},
    ):
        with pytest.raises(ValidationError):
            TaskResponsesResult.model_validate({**result(), "usage": usage})


def responses_fixture(model, monkeypatch):
    binding = {**model.service.readiness.return_value[0], "transport": "openai_responses", "model_id": "openai.gpt-6-astra"}
    # No production persona is enabled by this fixture. Real admission still
    # requires the authoritative registry and distinct invocability evidence.
    _make_model_fixture(model, "agent-task-gpt-fixture", binding)
    monkeypatch.setattr(
        "src.admin.persona_models.catalogue.persona_compatibility_class", lambda persona: "codex-sdk" if persona == "agent-task-gpt-fixture" else None
    )
    model.service.readiness.return_value = (binding, *model.service.readiness.return_value[1:])
    model.request = request()
    model.provider.return_value.update(content=[], stop_reason="completed", responses_response=result(), usage=result()["usage"])


@pytest.mark.asyncio
async def test_messages_grant_cannot_dispatch_responses(model):
    model.request = request()
    with pytest.raises(TaskStoreError, match="transport does not match grant"):
        await execute(model, responses_request=True)
    model.provider.assert_not_awaited()
    model.enforcement.check_budget_hierarchy.assert_not_awaited()


@pytest.mark.asyncio
async def test_responses_receipt_settles_once_and_replays_without_provider(model, monkeypatch):
    from src.agentauth import task_model

    responses_fixture(model, monkeypatch)
    receipt = await execute(model, responses_request=True)
    assert receipt["operation_status"] == "confirmed" and receipt["reservation_status"] == "settled"
    assert receipt["responses_response"] == result()
    assert task_model.quote_request.call_args.args[1] == "/openai/v1/responses"
    assert await execute(model, responses_request=True) == receipt
    model.provider.assert_awaited_once()
    model.enforcement.reconcile_reservation.assert_awaited_once()


@pytest.mark.asyncio
async def test_responses_unknown_handoff_keeps_hold_and_forbids_replay(model, monkeypatch):
    responses_fixture(model, monkeypatch)
    model.provider.side_effect = TimeoutError()
    receipt = await execute(model, responses_request=True)
    assert receipt["operation_status"] == "unknown"
    assert await execute(model, responses_request=True) == receipt
    model.provider.assert_awaited_once()
    model.enforcement.reconcile_reservation.assert_not_awaited()


@pytest.mark.asyncio
async def test_responses_budget_denial_has_no_provider_effect(model, monkeypatch):
    responses_fixture(model, monkeypatch)
    model.enforcement.check_budget_hierarchy.return_value.allowed = False
    receipt = await execute(model, responses_request=True)
    assert receipt["error_code"] == "budget_exceeded"
    model.provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_responses_requires_codex_class_before_reserving(model, monkeypatch):
    responses_fixture(model, monkeypatch)
    monkeypatch.setattr("src.admin.persona_models.catalogue.persona_compatibility_class", lambda _: "claude-agent-sdk")
    with pytest.raises(TaskStoreError, match="admitted Codex persona"):
        await execute(model, responses_request=True)
    model.provider.assert_not_awaited()
    model.enforcement.check_budget_hierarchy.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "usage", "status", "tool", "request_id", "oversize"])
async def test_actual_responses_transport_signs_bound_model_and_validates_provider(monkeypatch, bad):
    from src.proxy.mantle_service import MantlePassthroughService

    signed = []

    class Auth:
        def sign(self, method, url, body):
            signed.append((method, url, body))
            return {"Authorization": "fixture-signature"}

    service = MantlePassthroughService(Auth(), "https://bedrock-runtime.us-east-1.amazonaws.com", inference_profile_prefix="us")
    monkeypatch.setattr("src.proxy.routes.get_mantle_service", lambda: service)
    calls = []

    def provider(incoming):
        calls.append(incoming)
        assert incoming.content == signed[-1][2]
        assert json.loads(incoming.content)["model"] == "us.openai.gpt-6-astra"
        assert json.loads(incoming.content)["store"] is False
        response = result()
        if bad == "usage":
            response["usage"]["output_tokens"] = 65
        if bad == "status":
            response["status"] = "incomplete"
        if bad == "tool":
            response["output"] = [{"type": "function_call", "name": "view_image"}]
        if bad == "oversize":
            response["output"][0]["content"][0]["text"] = "x" * 65536
        return httpx.Response(200, json=response, headers={} if bad == "request_id" else {"x-amzn-requestid": "provider-fixture"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: client)
    price = AsyncMock(return_value=object())
    monkeypatch.setattr(module, "price_completed_usage", price)
    invocation = dict(
        db=object(),
        identity=SimpleNamespace(tenant="fixture-tenant", canonical_principal="fixture-principal"),
        binding={"model_id": "openai.gpt-6-astra"},
        target=SimpleNamespace(is_platform=True, region="us-east-1"),
        request=request(),
        operation_id="fixture-op",
    )
    if bad:
        with pytest.raises((TaskStoreError, ValidationError)):
            await module.invoke_task_responses(**invocation)
        price.assert_not_awaited()
    else:
        actual = await module.invoke_task_responses(**invocation)
        assert actual["responses_response"] == result()
        assert actual["provider_request_id"] == "provider-fixture"
        assert price.call_args.kwargs["api_format"] == "openai"
        assert price.call_args.kwargs["evidence"].forwarded_model_id == "us.openai.gpt-6-astra"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_destination_failure_never_falls_back_to_platform(monkeypatch):
    monkeypatch.setattr("src.proxy.routes.get_mantle_service", lambda: object())
    assume = AsyncMock(side_effect=RuntimeError("fixture destination unavailable"))
    monkeypatch.setattr(module.bedrock_destination_signer, "get_credentials", assume)
    sent = []
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kw: sent.append(kw))
    with pytest.raises(RuntimeError, match="fixture destination unavailable"):
        await module.invoke_task_responses(
            object(),
            identity=SimpleNamespace(tenant="tenant", canonical_principal="service-principal"),
            binding={"model_id": "openai.gpt-6-astra"},
            target=SimpleNamespace(is_platform=False),
            request=request(),
            operation_id="fixture-op",
        )
    assert sent == []
    assert assume.call_args.kwargs["user_id"] == "service-principal"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    [
        "https://bedrock-runtime.eu-west-1.amazonaws.com/openai/v1/responses",
        "https://example.invalid/openai/v1/responses",
        "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/responses?override=1",
    ],
)
async def test_destination_must_match_admitted_region_and_aws_endpoint(monkeypatch, endpoint):
    service = SimpleNamespace(_routed_request=lambda _: SimpleNamespace(upstream_url=endpoint))
    monkeypatch.setattr("src.proxy.routes.get_mantle_service", lambda: service)
    with pytest.raises(TaskStoreError, match="destination differs from admitted evidence"):
        await module.invoke_task_responses(
            object(),
            identity=SimpleNamespace(tenant="tenant", canonical_principal="service-principal"),
            binding={"model_id": "openai.gpt-6-astra"},
            target=SimpleNamespace(is_platform=True, region="us-east-1"),
            request=request(),
            operation_id="fixture-op",
        )
