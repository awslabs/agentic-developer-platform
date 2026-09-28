"""Simple single-account pool service for same-account Bedrock access.

Uses the pod's own credentials (IRSA) to call Bedrock directly.
No cross-account role assumption needed.
"""

import asyncio
import logging
from typing import Any

import boto3

from src.shared.config import get_settings
from src.shared.interfaces.pool import IPoolService

logger = logging.getLogger(__name__)


class AsyncBedrockClient:
    """Wraps sync boto3 bedrock-runtime client with async methods.

    Uses two separate boto3 clients with different timeout configurations:

    - _invoke_client: read_timeout=3600 (1 hour) for non-streaming calls.
      AWS documentation recommends 3600s for large models like Claude Opus/Sonnet
      that can take 60+ seconds for large contexts. The read_timeout applies to
      the entire response body, so it must be large enough for the full response.
      Ref: https://repost.aws/knowledge-center/bedrock-large-model-read-timeouts

    - _streaming_client: read_timeout=None for streaming calls.
      For InvokeModelWithResponseStream, tokens arrive continuously so the
      read_timeout applies between individual chunks — not the full response.
      Setting it to None avoids spurious timeouts between chunks on slow models.
      The streaming connection stays alive as long as Bedrock is sending data.
    """

    def __init__(self, region: str):
        from botocore.config import Config

        # Non-streaming: 1 hour timeout per AWS recommendation for large models
        invoke_config = Config(
            read_timeout=3600,
            connect_timeout=10,
            retries={"max_attempts": 2, "mode": "adaptive"},
        )

        # Streaming: generous read_timeout between chunks.
        # read_timeout=None would never timeout but TCP idle timeouts on
        # load balancers/NAT gateways can reset connections after ~5 minutes
        # of silence between chunks. Set to 300s to survive long gaps.
        streaming_config = Config(
            read_timeout=300,
            connect_timeout=10,
            retries={"max_attempts": 1, "mode": "standard"},
        )

        self._invoke_client = boto3.client("bedrock-runtime", region_name=region, config=invoke_config)
        self._streaming_client = boto3.client("bedrock-runtime", region_name=region, config=streaming_config)

    async def invoke_model(self, **kwargs) -> dict:
        return await asyncio.to_thread(self._invoke_client.invoke_model, **kwargs)

    async def invoke_model_with_response_stream(self, **kwargs) -> dict:
        return await asyncio.to_thread(self._streaming_client.invoke_model_with_response_stream, **kwargs)


class SimplePoolService(IPoolService):
    """Single-account Bedrock pool using default credentials (IRSA)."""

    def __init__(self, region: str | None = None):
        settings = get_settings()
        self._region = region or settings.aws_region
        self._client = None

    async def get_client(self) -> Any:
        if self._client is None:
            self._client = AsyncBedrockClient(self._region)
            logger.info("Bedrock runtime client initialized", extra={"region": self._region})
        return self._client

    async def report_error(self, account_id: str) -> None:
        logger.warning("Bedrock error reported", extra={"account_id": account_id})

    async def get_pool_status(self) -> list[dict[str, Any]]:
        return [{"account_id": "self", "region": self._region, "is_healthy": True}]
