"""One request's raw Bedrock evidence, shared explicitly through its lifetime."""

import asyncio
import json
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from pricing_policy import RoutingEvidence, canonical_billing_model_id, is_anthropic_model
from pricing_policy.policy import ServiceTier, geography_from_model_prefix
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
    _served_tier_conflict: bool = field(default=False, repr=False)
    provider_request_id: str | None = None

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

    def _capture_served_tier(self, *values: Any) -> None:
        if self.routing is None or self._served_tier_conflict:
            return
        recognized = {value.strip().lower() for value in values if isinstance(value, str) and value.strip().lower() in ServiceTier.ALL}
        if self.routing.served_service_tier is not None:
            recognized.add(self.routing.served_service_tier)
        if len(recognized) > 1:
            # Conflicting upstream facts cannot certify either tier. Keep the
            # conflict sticky so a later event cannot silently restore certainty.
            self._served_tier_conflict = True
            self.routing = replace(self.routing, served_service_tier_raw="conflicting_upstream_tiers")
        elif recognized:
            # Compare normalized values, but keep the actual upstream spelling
            # as provenance. A consistent later event need not replace it.
            raw = (
                self.routing.served_service_tier_raw
                if self.routing.served_service_tier is not None
                else next(value for value in values if isinstance(value, str) and value.strip().lower() in recognized)
            )
            self.routing = replace(self.routing, served_service_tier_raw=raw)
        else:
            raw = next((value for value in values if isinstance(value, str) and value.strip()), None)
            if raw is not None:
                self.routing = replace(self.routing, served_service_tier_raw=raw)

    def response(self, body: dict[str, Any], metadata: dict[str, Any]) -> None:
        response_metadata = metadata.get("ResponseMetadata") or {}
        request_id = response_metadata.get("RequestId")
        if isinstance(request_id, str) and request_id:
            self.provider_request_id = request_id
        self.response_body.update(body)
        self.raw_usage.update(body.get("usage") or {})
        usage = body.get("usage") or {}
        if self.is_claude:
            self._capture_served_tier(usage.get("service_tier"), metadata.get("serviceTier"), body.get("service_tier"))
        else:
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
        start_usage = message.get("usage") or {}
        delta_usage = event.get("usage") or {}
        if self.is_claude:
            self._capture_served_tier(
                start_usage.get("service_tier"), delta_usage.get("service_tier"), event.get("service_tier"), message.get("service_tier")
            )
        else:
            served = event.get("service_tier") or message.get("service_tier")
            if self.routing and isinstance(served, str):
                self.routing = replace(self.routing, served_service_tier_raw=served)
