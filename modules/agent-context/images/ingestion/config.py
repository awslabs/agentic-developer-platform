"""Centralized configuration for the Agent Context ingestion pipeline.

Single source of truth for all environment-variable-based config.
Every script imports `from config import settings` instead of
scattered `os.getenv()` calls.

Uses pydantic-settings (BaseSettings) for:
  - Typed fields with validation
  - Automatic env-var loading
  - Fail-fast on missing required values
  - Per-task model tiering

12-Factor: all values come from env vars (injected via a single ConfigMap
+ Secret in K8s). Defaults match the previous inline defaults so behavior
is identical for existing deployments.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Agent Context ingestion pipeline configuration.

    All fields have defaults matching the pre-refactor inline os.getenv() defaults.
    Environment variables override these at runtime (via K8s ConfigMap envFrom).
    """

    # --- S3 Vectors (semantic search) --------------------------------------------
    s3_vectors_bucket_name: str = ""
    s3_vectors_shard_count: int = 4
    s3_vectors_region: str = ""  # Falls back to aws_region if empty

    # --- S3 (content store + personal context) --------------------------------
    s3_bucket_name: str = ""  # Platform data bucket (content store + personal-context)
    s3_content_prefix: str = "content"  # Key prefix for ingestion content objects

    # --- AWS ------------------------------------------------------------------
    aws_region: str = "us-east-1"

    # --- SQS + DynamoDB -------------------------------------------------------
    sqs_queue_url: str = ""
    dynamo_table: str = "adp-context-service-state"

    # --- Zoekt (exact/regex code search) ----------------------------------------
    zoekt_url: str = "http://zoekt.agent-context.svc.cluster.local:6070"
    zoekt_timeout: float = 10.0

    # --- DeepWiki -------------------------------------------------------------
    deepwiki_url: str = "http://deepwiki.agent-context.svc.cluster.local:8001"
    deepwiki_enabled: bool = True

    # --- Wiki store (S3 + S3 Vectors) -------------------------------------------
    wiki_sink: str = "s3"
    wiki_s3_prefix: str = "content/wikis"
    code_index_s3_prefix: str = "content/code-indexes"
    wiki_s3_bucket: str = ""  # Set via env: WIKI_S3_BUCKET
    s3_vectors_bucket: str = ""  # Set via env: S3_VECTORS_BUCKET

    # --- LLM (via LiteLLM proxy) ----------------------------------------------
    llm_base_url: str = "http://litellm-proxy.agent-context.svc.cluster.local:4000/v1"

    # --- Per-task model tiering -----------------------------------------------
    # Wiki generation/updates: needs strong reasoning (Sonnet)
    # NOTE: model_wiki is sent as a parameter to DeepWiki's /chat/completions/stream
    # endpoint but DeepWiki's actual model is governed by its own container config
    # (images/deepwiki/config/generator.json). These should stay in sync manually.
    model_wiki: str = "bedrock/global.anthropic.claude-sonnet-4-6"
    # Topic tagging: classification task (Haiku — cheap + fast)
    model_tagging: str = "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0"
    # Index page generation: summarization (Haiku — cheap + fast)
    model_index: str = "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0"
    # Curricula + learning artifacts: needs reasoning (Sonnet)
    model_learning: str = "bedrock/global.anthropic.claude-sonnet-4-6"
    # GraphRAG entity extraction from wiki: structured output (Haiku)
    model_graphrag: str = "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0"
    # Legacy: WIKI_LLM_MODEL env var (kept for backward compat with manifests)
    wiki_llm_model: str = "bedrock/global.anthropic.claude-sonnet-4-6"

    # --- File paths -----------------------------------------------------------
    clone_base: str = "/platform-data/repos"
    code_index_dir: str = "/platform-data/code-indexes"
    learning_dir: str = "/platform-data/learning"
    state_dir: str = "/platform-data"
    repos_file: str = "/config/repos.txt"
    urls_file: str = "/config/urls.txt"
    docs_file: str = "/config/docs.txt"
    accounts_file: str = "/config/accounts.txt"

    # --- Timeouts and limits --------------------------------------------------
    request_timeout: int = 120
    max_wikis_per_run: int = 15
    max_indexes_per_run: int = 15
    min_cluster_size: int = 3
    l1_sample_size: int = 20
    stale_wiki_days: int = 14
    max_download_size: int = 100 * 1024 * 1024  # 100 MB

    # --- GraphRAG (Neptune + OpenSearch) --------------------------------------
    graphrag_enabled: bool = False
    neptune_endpoint: str = ""
    neptune_port: int = 8182
    opensearch_endpoint: str = ""

    # --- GitHub App -----------------------------------------------------------
    github_app_id_secret: str = ""
    github_app_key_secret: str = ""
    github_app_owner: str = ""

    # --- SBOM generation (dual-rail, #1358) ------------------------------------
    sbom_enabled: bool = True
    sbom_s3_prefix: str = "sbom"  # S3 key prefix within platform-data bucket
    syft_timeout: int = 120  # seconds for syft CLI execution
    sbom_db_enabled: bool = True  # Write dependency rows to Postgres (best-effort)

    # --- Gateway DB (registry reader, Issue #2082 Phase 2) -------------------
    gateway_db_name: str = ""  # Gateway DB name (e.g. "bedrockgateway"); empty = registry disabled
    gateway_db_host: str = ""  # Defaults to DB_HOST if empty (same RDS instance)

    # --- Telemetry (Knowledge Layer observability, #1746) ----------------------
    knowledge_layer_telemetry_enabled: bool = True
    knowledge_layer_traces_enabled: bool = True
    otel_exporter_otlp_endpoint: str = "http://adot-collector.adp-agents.svc.cluster.local:4317"
    log_format: str = "json"  # "json" or "text" (text for local dev)

    # --- Status Callback (worker → gateway, Issue #2049) ---------------------
    gateway_callback_url: str = ""  # Base URL for gateway callback (e.g. http://gateway-svc:8080)
    gateway_internal_api_key: str = ""  # Shared secret for X-Internal-Api-Key header

    # --- Personal Context Synthesis -------------------------------------------
    synthesis_model: str = "bedrock/global.anthropic.claude-sonnet-4-6"
    min_learnings_threshold: int = 5
    max_unsynthesized_age_days: int = 7

    @field_validator("deepwiki_enabled", "graphrag_enabled", mode="before")
    @classmethod
    def _parse_bool_string(cls, v: object) -> object:
        """Accept 'true'/'false' strings from env vars."""
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes")
        return v

    @property
    def effective_s3_vectors_region(self) -> str:
        """Resolve the S3 Vectors region (falls back to aws_region)."""
        return self.s3_vectors_region or self.aws_region

    model_config = {
        "env_prefix": "",
        "case_sensitive": False,
        "extra": "ignore",
    }


# Singleton instance — import this in scripts
settings = Settings()
