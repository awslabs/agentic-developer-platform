"""One request's raw Bedrock evidence, shared explicitly through its lifetime."""

import asyncio
import json
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from pricing_policy import RoutingEvidence, canonical_billing_model_id, is_anthropic_model
from pricing_policy.policy import geography_from_model_prefix
from src.chat_logging.service import StreamingResponseBuffer


@dataclass
class PricingCapture:
    request_id: str
    original_model: str
    raw_usage: dict[str, Any] = field(default_factory=dict)
    response_body: dict[str, Any] = field(default_factory=dict)
    routing: RoutingEvidence | None = None
    decision: dict[str, Any] | None = None
    finalization_task: asyncio.Task[None] | None = field(default=None, repr=False)
    buffer: StreamingResponseBuffer = field(default_factory=StreamingResponseBuffer)

    @property
    def is_claude(self) -> bool:
        return self.routing is not None and is_anthropic_model(self.routing.billing_model_id)

    def forwarded(self, client: Any, model_id: str, *, stream: bool = False) -> None:
        # SimplePool wraps distinct invoke/stream clients. Read metadata from the
        # exact signer used, never the global AWS region or a shadow account.
        actual_client = getattr(client, "_streaming_client" if stream else "_invoke_client", client)
        metadata = getattr(actual_client, "meta", None)
        region = getattr(metadata, "region_name", None)
        endpoint = getattr(metadata, "endpoint_url", None)
        region = region if isinstance(region, str) else None
        endpoint = urlsplit(endpoint).hostname if isinstance(endpoint, str) else None
        geography = geography_from_model_prefix(model_id)
        if model_id.startswith("anthropic."):
            geography = "in_region"
        if region and region.startswith("us-gov-"):
            geography = "govcloud"
        self.routing = RoutingEvidence(
            original_model_id=self.original_model,
            billing_model_id=canonical_billing_model_id(model_id),
            forwarded_model_id=model_id,
            endpoint_region=region,
            endpoint_host=endpoint,
            geography=geography,
        )

    def response(self, body: dict[str, Any], metadata: dict[str, Any]) -> None:
        self.response_body.update(body)
        self.raw_usage.update(body.get("usage") or {})
        served = metadata.get("serviceTier") or body.get("service_tier")
        if self.routing and isinstance(served, str):
            self.routing = replace(self.routing, served_service_tier_raw=served)

    def chunk(self, chunk: bytes) -> None:
        try:
            event = json.loads(chunk)
        except (ValueError, TypeError):
            return
        if not isinstance(event, dict):
            return
        self.buffer.add_chunk(event)
        self.raw_usage.update(self.buffer.usage)
        message = event.get("message") or {}
        served = event.get("service_tier") or message.get("service_tier")
        if self.routing and isinstance(served, str):
            self.routing = replace(self.routing, served_service_tier_raw=served)
