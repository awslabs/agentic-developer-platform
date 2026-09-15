"""Simple single-account pool service for same-account Bedrock access.

Uses the pod's own credentials (IRSA) to call Bedrock directly.
No cross-account role assumption needed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import boto3

from src.shared.config import get_settings
from src.shared.interfaces.pool import IPoolService

if TYPE_CHECKING:
    # Issue #4744: type-only, so the pool carries NO runtime dependency on the
    # routing/signing modules. The pool is a client factory that happens to accept
    # credentials; it must not be able to reach the resolver even transitively, or
    # a future edit here could pick an account. Duck typing covers the runtime need
    # (`.region`, `.as_boto3_kwargs()`), and tests inject plain stand-ins.
    from src.proxy.bedrock_signing import DestinationCredentials

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

    def __init__(self, region: str, credentials: dict[str, str] | None = None, *, single_attempt: bool = False):
        """Build the two clients.

        Args:
            region: Region for both clients.
            credentials: Issue #4744 (#4692 · R3) — explicit AWS credentials
                (``aws_access_key_id`` / ``aws_secret_access_key`` /
                ``aws_session_token``) for a cross-account routed call. ``None`` means
                the pod's ambient IRSA credentials, which is today's behaviour and
                every unmapped call's behaviour.

                They are applied to **both** clients below, and that is a correctness
                requirement rather than tidiness (design note §2.4): the dead
                ``PoolService`` built its cross-account client with **no ``Config`` at
                all**, so it silently inherited botocore's 60s default read timeout.
                Routing a principal through a client like that would time out exactly
                the long Opus/Sonnet generations the 3600s setting below exists to
                survive — presenting as random failures *only for routed principals*,
                which is close to the hardest latency bug to attribute. Passing
                credentials into the existing construction path, rather than building a
                separate client for routed calls, is what makes that regression
                impossible instead of merely unlikely.
        """
        from botocore.config import Config

        # Non-streaming: 1 hour timeout per AWS recommendation for large models
        invoke_config = Config(
            read_timeout=3600,
            connect_timeout=10,
            retries={"total_max_attempts": 1, "mode": "standard"} if single_attempt else {"max_attempts": 2, "mode": "adaptive"},
        )

        # Streaming: generous read_timeout between chunks.
        # read_timeout=None would never timeout but TCP idle timeouts on
        # load balancers/NAT gateways can reset connections after ~5 minutes
        # of silence between chunks. Set to 300s to survive long gaps.
        streaming_config = Config(
            read_timeout=300,
            connect_timeout=10,
            retries={"total_max_attempts": 1, "mode": "standard"} if single_attempt else {"max_attempts": 1, "mode": "standard"},
        )

        creds = credentials or {}
        self._invoke_client = boto3.client("bedrock-runtime", region_name=region, config=invoke_config, **creds)
        self._streaming_client = boto3.client("bedrock-runtime", region_name=region, config=streaming_config, **creds)

    async def invoke_model(self, **kwargs) -> dict:
        return await asyncio.to_thread(self._invoke_client.invoke_model, **kwargs)

    async def invoke_model_with_response_stream(self, **kwargs) -> dict:
        return await asyncio.to_thread(self._streaming_client.invoke_model_with_response_stream, **kwargs)


class SimplePoolService(IPoolService):
    """Single-account Bedrock pool using default credentials (IRSA).

    Issue #4744 (#4692 · R3): ``get_client`` now takes an optional resolved routing
    target. Without one — or with the platform rung — it returns the single ambient
    IRSA client exactly as before, which is the path every unmapped call takes and is
    byte-identical to main.
    """

    def __init__(self, region: str | None = None):
        settings = get_settings()
        self._region = region or settings.aws_region
        self._client = None
        self._single_attempt_client = None

    async def get_client(self, credentials: DestinationCredentials | None = None, *, single_attempt: bool = False) -> Any:
        """Return a Bedrock client, ambient by default or destination-signed.

        Args:
            credentials: Issue #4744 — credentials for a routed cross-account call,
                already assumed and cached by
                :class:`~src.proxy.bedrock_signing.BedrockDestinationSigner`. ``None``
                means the ambient IRSA client.

                **Credentials, not a target.** The pool receives the *result* of the
                routing decision, never the inputs — it cannot see a mapping, an org
                id, or a principal, so no future edit inside this module can consult
                the ladder or pick an account. The pool is a client factory; routing
                policy stays in ``src/proxy/``. This also keeps the argument an
                explicit parameter rather than ambient state, per design note §2.1:
                the contextvar alternative was rejected because a credential decision
                read from ambient state is precisely how one principal's call gets
                signed with another's credentials.

        Note the asymmetry in caching, which is deliberate: the ambient client is a
        long-lived singleton, while a routed client is built per call. The routed
        client is cheap (``boto3.client`` construction is local; no network I/O) and
        the expensive part — the STS AssumeRole — *is* cached, by the signer, keyed on
        the full identity tuple. Caching client objects per principal here would mean
        holding a second, differently-keyed cache of credential-bearing objects
        alongside that one, which is the cross-principal reuse hazard §2.3 is about.
        """
        if credentials is not None:
            # No memoization: see the note above. The client is disposable; the
            # credentials behind it are what is cached, one layer up.
            if single_attempt:
                return AsyncBedrockClient(credentials.region or self._region, credentials.as_boto3_kwargs(), single_attempt=True)
            return AsyncBedrockClient(credentials.region or self._region, credentials.as_boto3_kwargs())

        if single_attempt:
            if self._single_attempt_client is None:
                self._single_attempt_client = AsyncBedrockClient(self._region, single_attempt=True)
            return self._single_attempt_client

        if self._client is None:
            self._client = AsyncBedrockClient(self._region)
            logger.info("Bedrock runtime client initialized", extra={"region": self._region})
        return self._client

    async def report_error(self, account_id: str) -> None:
        logger.warning("Bedrock error reported", extra={"account_id": account_id})

    async def get_pool_status(self) -> list[dict[str, Any]]:
        return [{"account_id": "self", "region": self._region, "is_healthy": True}]
