"""One gateway-owned Responses call; TaskModel owns reservations and receipts."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import httpx
from starlette.concurrency import run_in_threadpool

from src.agentauth.task_responses_contract import TaskResponsesRequest, TaskResponsesResult
from src.budget.pricing_decisions import price_completed_usage
from src.orchestration.responses_quotes import OpenAIResponsesQuoteAdapter
from src.proxy.bedrock_enforcement import RoutingDecision
from src.proxy.bedrock_signing import bedrock_destination_signer
from src.shared.config import get_settings
from src.tasks.store import TaskStoreError


async def invoke_task_responses(db, *, identity, binding, target, request, operation_id):
    # Do not use create_response: it independently settles proxy requests. This
    # caller must settle exactly once through the durable TaskModel operation.
    from src.proxy.routes import get_mantle_service

    TaskResponsesRequest.model_validate(request)
    service = get_mantle_service()
    credentials = None
    if not target.is_platform:
        credentials = await bedrock_destination_signer.get_credentials(db, target, user_id=identity.canonical_principal)
    routed = service._routed_request(RoutingDecision(target=target, credentials=credentials))
    expected_region = target.region or get_settings().aws_region
    endpoint = urlsplit(routed.upstream_url)
    if (
        endpoint.scheme != "https"
        or not re.fullmatch(
            r"bedrock-(?:runtime|mantle)\." + re.escape(expected_region) + r"\.(?:amazonaws\.com(?:\.cn)?|api\.aws)", endpoint.hostname or ""
        )
        or endpoint.port not in {None, 443}
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or endpoint.path != "/openai/v1/responses"
    ):
        raise TaskStoreError("task Responses destination differs from admitted evidence")
    body = json.dumps({**request, "model": binding["model_id"], "stream": False, "store": False}, separators=(",", ":")).encode()
    body = service._apply_inference_profile(body, prefix=routed.inference_profile_prefix)
    headers = await run_in_threadpool(service._headers, body, routed)
    # No retries or redirects: unknown provider outcomes retain the Task hold.
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5), follow_redirects=False, trust_env=False) as client:
        async with client.stream("POST", routed.upstream_url, content=body, headers=headers) as response:
            if response.status_code != 200:
                raise TaskStoreError("task Responses provider did not confirm completion")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 65536:
                    raise TaskStoreError("task Responses result exceeds frame bound")
            document = json.loads(raw)
            provider_id = response.headers.get("x-amzn-requestid") or response.headers.get("x-request-id")
    if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 255 or not isinstance(document, dict):
        raise TaskStoreError("task Responses provider receipt unavailable")
    usage = document.get("usage")
    trusted = await OpenAIResponsesQuoteAdapter().reconcile(document)
    if not trusted.known or trusted.output_tokens > request["max_output_tokens"]:
        raise TaskStoreError("task Responses usage unavailable or exceeded bound")
    if "total_tokens" in usage and (type(usage["total_tokens"]) is not int or usage["total_tokens"] != trusted.input_tokens + trusted.output_tokens):
        raise TaskStoreError("task Responses total usage inconsistent")
    # Keep only the output contract. Provider metadata is consumed for pricing;
    # it must not become a new instruction, credential or tool in the SDK child.
    result = TaskResponsesResult.model_validate(
        {
            "id": document.get("id"),
            "status": document.get("status"),
            "output": document.get("output"),
            "usage": {key: usage[key] for key in ("input_tokens", "output_tokens", "input_tokens_details", "output_tokens_details") if key in usage},
        }
    ).model_dump(exclude_none=True)
    evidence = service._capture_usage(
        usage,
        body,
        binding["model_id"],
        {"provider_request_id": provider_id, "service_tier": document.get("service_tier")},
        base_url=routed.base_url,
    )
    decision = await price_completed_usage(
        request_id=operation_id, org_id=identity.tenant, raw_usage=usage, evidence=evidence.routing, api_format="openai"
    )
    return {
        "content": [],
        "stop_reason": "completed",
        "responses_response": result,
        "usage": result["usage"],
        "price": decision,
        "provider_request_id": provider_id,
    }
