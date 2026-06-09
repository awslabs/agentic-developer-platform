"""Thin embedding client for personal-context via LiteLLM proxy.

Generates text embeddings using the LiteLLM proxy endpoint configured for
``bedrock/amazon.titan-embed-text-v2:0`` (1024-dimensional vectors).

In production, the proxy runs at ``http://litellm-proxy.agent-context.svc.cluster.local:4000``.
The endpoint can be overridden via the ``LITELLM_PROXY_URL`` environment variable.
"""

from __future__ import annotations

import os
from typing import Protocol

import httpx

# Defaults matching config.env
DEFAULT_PROXY_URL = "http://litellm-proxy.agent-context.svc.cluster.local:4000"
DEFAULT_MODEL = "bedrock/amazon.titan-embed-text-v2:0"
EMBEDDING_DIMENSION = 1024


class EmbeddingClient(Protocol):
    """Protocol for embedding generation (allows test mocking)."""

    def embed(self, text: str) -> list[float]: ...


class LiteLLMEmbeddingClient:
    """Generate embeddings via the LiteLLM proxy's OpenAI-compatible endpoint.

    Uses ``POST /embeddings`` with the configured model. The proxy routes
    this to AWS Bedrock Titan Embed v2.
    """

    def __init__(
        self,
        proxy_url: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
    ):
        self.proxy_url = (
            proxy_url or os.environ.get("LITELLM_PROXY_URL", DEFAULT_PROXY_URL)
        ).rstrip("/")
        self.model = model or os.environ.get("LITELLM_EMBEDDING_MODEL", DEFAULT_MODEL)
        self.timeout = timeout

    def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for the given text.

        Parameters
        ----------
        text:
            The text to embed. Must be non-empty.

        Returns
        -------
        A list of floats (1024-dimensional vector).

        Raises
        ------
        ValueError
            If text is empty.
        httpx.HTTPStatusError
            If the proxy returns a non-2xx response.
        """
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")

        response = httpx.post(
            f"{self.proxy_url}/embeddings",
            json={
                "model": self.model,
                "input": text,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()

        data = response.json()
        embedding = data["data"][0]["embedding"]
        return embedding
