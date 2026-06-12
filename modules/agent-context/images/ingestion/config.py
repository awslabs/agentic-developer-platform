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

    # --- Core services --------------------------------------------------------
    ov_url: str = "http://openviking.agent-context.svc.cluster.local:1933"
    openviking_root_key: str = ""
    # Legacy alias — some scripts used ROOT_KEY
    root_key: str = ""

    # --- AWS ------------------------------------------------------------------
    aws_region: str = "us-east-1"

    # --- SQS + DynamoDB -------------------------------------------------------
    sqs_queue_url: str = ""
    dynamo_table: str = "adp-context-service-state"

    # --- DeepWiki -------------------------------------------------------------
    deepwiki_url: str = "http://deepwiki.agent-context.svc.cluster.local:8001"
    deepwiki_enabled: bool = True

    # --- LLM (via LiteLLM proxy) ----------------------------------------------
    llm_base_url: str = "http://litellm-proxy.agent-context.svc.cluster.local:4000/v1"

    # --- Per-task model tiering -----------------------------------------------
    # Wiki generation/updates: needs strong reasoning (Sonnet)
    model_wiki: str = "bedrock/global.anthropic.claude-sonnet-4-6"
    # Topic tagging: classification task (Haiku — cheap + fast)
    model_tagging: str = "bedrock/global.anthropic.claude-haiku-4-6"
    # Index page generation: summarization (Haiku — cheap + fast)
    model_index: str = "bedrock/global.anthropic.claude-haiku-4-6"
    # Curricula + learning artifacts: needs reasoning (Sonnet)
    model_learning: str = "bedrock/global.anthropic.claude-sonnet-4-6"
    # GraphRAG entity extraction from wiki: structured output (Haiku)
    model_graphrag: str = "bedrock/global.anthropic.claude-haiku-4-6"
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
    def ov_key(self) -> str:
        """Resolve the OpenViking API key (supports both env var names)."""
        return self.openviking_root_key or self.root_key

    model_config = {
        "env_prefix": "",
        "case_sensitive": False,
        "extra": "ignore",
    }


# Singleton instance — import this in scripts
settings = Settings()
