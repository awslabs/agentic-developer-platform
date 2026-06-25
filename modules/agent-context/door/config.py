"""Configuration for the Context MCP Server.

Reads from environment variables (injected via K8s ConfigMap envFrom).
Follows the same pattern as images/ingestion/config.py.
"""

from __future__ import annotations

import os


class ServerConfig:
    """Context MCP Server configuration from environment variables."""

    def __init__(self) -> None:
        # Zoekt (exact code search)
        self.zoekt_url: str = os.environ.get(
            "ZOEKT_URL", "http://zoekt.agent-context.svc.cluster.local:6070"
        )
        self.zoekt_timeout: float = float(os.environ.get("ZOEKT_TIMEOUT", "10.0"))

        # S3 (content store — code-indexes, wikis, repos)
        self.s3_bucket: str = os.environ.get("S3_BUCKET_NAME", "")
        self.s3_content_prefix: str = os.environ.get("S3_CONTENT_PREFIX", "content")
        self.s3_region: str = os.environ.get("AWS_REGION", "us-east-1")
        self.code_index_s3_prefix: str = os.environ.get(
            "CODE_INDEX_S3_PREFIX", "content/code-indexes"
        )

        # S3 Vectors (semantic search — optional)
        self.s3_vectors_bucket: str = os.environ.get("S3_VECTORS_BUCKET_NAME", "")
        self.s3_vectors_region: str = os.environ.get("S3_VECTORS_REGION", "")
        self.semantic_enabled: bool = os.environ.get(
            "SEMANTIC_SEARCH_ENABLED", "false"
        ).lower() in ("true", "1", "yes")

        # LiteLLM proxy (for embeddings in semantic/experience)
        self.litellm_url: str = os.environ.get(
            "LLM_BASE_URL", "http://litellm-proxy.agent-context.svc.cluster.local:4000/v1"
        )

        # Postgres (catalog for browse + ACL store)
        self.database_url: str = os.environ.get("DATABASE_URL", "")

        # Neptune (graph database for structural queries)
        self.neptune_endpoint: str = os.environ.get("NEPTUNE_ENDPOINT", "")
        self.neptune_enabled: bool = os.environ.get("NEPTUNE_ENABLED", "false").lower() in (
            "true",
            "1",
            "yes",
        )

        # Tenant scoping (E8 multi-tenancy — kill switch)
        self.tenant_scope_enabled: bool = os.environ.get(
            "TENANT_SCOPE_ENABLED", "false"
        ).lower() in ("true", "1", "yes")

        # Project scoping (E9 — kill switch)
        self.project_filter_enabled: bool = os.environ.get(
            "PROJECT_FILTER_ENABLED", "false"
        ).lower() in ("true", "1", "yes")

        # Server
        self.host: str = os.environ.get("MCP_HOST", "0.0.0.0")
        self.port: int = int(os.environ.get("MCP_PORT", "5100"))


# Singleton — import this in server modules
config = ServerConfig()
