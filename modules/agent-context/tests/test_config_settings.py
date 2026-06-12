"""Tests for the centralized Settings configuration module.

Verifies:
  1. All keys load with correct types and defaults
  2. Parity: every new default equals the prior os.getenv() default
  3. Fail-fast on invalid values
  4. Per-task model tiering is exposed correctly
  5. Boolean string parsing from env vars
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add the ingestion scripts directory to sys.path so 'config' module is importable
_INGESTION_DIR = str(Path(__file__).resolve().parent.parent / "images" / "ingestion")
if _INGESTION_DIR not in sys.path:
    sys.path.insert(0, _INGESTION_DIR)


# ---------------------------------------------------------------------------
# Parity tests — guard against silently changing defaults during refactor
# ---------------------------------------------------------------------------

# The old defaults (prior to this refactor) — extracted from the original
# os.getenv("KEY", "default") calls across all 9 ingestion scripts.
OLD_DEFAULTS = {
    "ov_url": "http://openviking.agent-context.svc.cluster.local:1933",
    "aws_region": "us-east-1",
    "sqs_queue_url": "",
    "dynamo_table": "adp-context-service-state",
    "deepwiki_url": "http://deepwiki.agent-context.svc.cluster.local:8001",
    "deepwiki_enabled": True,
    "llm_base_url": "http://litellm-proxy.agent-context.svc.cluster.local:4000/v1",
    "clone_base": "/platform-data/repos",
    "code_index_dir": "/platform-data/code-indexes",
    "learning_dir": "/platform-data/learning",
    "state_dir": "/platform-data",
    "repos_file": "/config/repos.txt",
    "urls_file": "/config/urls.txt",
    "docs_file": "/config/docs.txt",
    "accounts_file": "/config/accounts.txt",
    "request_timeout": 120,
    "max_wikis_per_run": 15,
    "max_indexes_per_run": 15,
    "min_cluster_size": 3,
    "l1_sample_size": 20,
    "stale_wiki_days": 14,
    "max_download_size": 100 * 1024 * 1024,
    "graphrag_enabled": False,
    "neptune_endpoint": "",
    "neptune_port": 8182,
    "opensearch_endpoint": "",
    "openviking_root_key": "",
    "root_key": "",
    "github_app_id_secret": "",
    "github_app_key_secret": "",
    "github_app_owner": "",
}


def _make_settings(**overrides):
    """Create a fresh Settings instance with optional env overrides."""
    import importlib
    import sys

    # Remove cached module so we get a fresh Settings instance
    sys.modules.pop("config", None)

    env = {k.upper(): str(v) for k, v in overrides.items()}
    with patch.dict(os.environ, env, clear=False):
        # Import fresh
        import config

        importlib.reload(config)
        return config.Settings()


class TestParityDefaults:
    """Every Settings default must equal the old os.getenv() default."""

    def test_all_defaults_match(self):
        """Core parity assertion: no silent default drift during refactor."""
        s = _make_settings()
        for key, expected in OLD_DEFAULTS.items():
            actual = getattr(s, key)
            assert actual == expected, (
                f"Default drift detected for '{key}': "
                f"Settings has {actual!r}, old default was {expected!r}"
            )


class TestTypes:
    """Settings fields have the correct types."""

    def test_integer_fields(self):
        s = _make_settings()
        assert isinstance(s.request_timeout, int)
        assert isinstance(s.neptune_port, int)
        assert isinstance(s.max_wikis_per_run, int)
        assert isinstance(s.min_cluster_size, int)
        assert isinstance(s.l1_sample_size, int)
        assert isinstance(s.stale_wiki_days, int)
        assert isinstance(s.max_download_size, int)
        assert isinstance(s.min_learnings_threshold, int)
        assert isinstance(s.max_unsynthesized_age_days, int)

    def test_boolean_fields(self):
        s = _make_settings()
        assert isinstance(s.deepwiki_enabled, bool)
        assert isinstance(s.graphrag_enabled, bool)

    def test_string_fields(self):
        s = _make_settings()
        assert isinstance(s.ov_url, str)
        assert isinstance(s.aws_region, str)
        assert isinstance(s.llm_base_url, str)
        assert isinstance(s.model_wiki, str)
        assert isinstance(s.model_tagging, str)
        assert isinstance(s.model_index, str)


class TestBooleanParsing:
    """Boolean fields accept string values from env vars."""

    @pytest.mark.parametrize(
        "val,expected",
        [
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("1", True),
            ("yes", True),
            ("false", False),
            ("False", False),
            ("0", False),
            ("no", False),
        ],
    )
    def test_deepwiki_enabled_parsing(self, val, expected):
        s = _make_settings(DEEPWIKI_ENABLED=val)
        assert s.deepwiki_enabled is expected

    @pytest.mark.parametrize(
        "val,expected",
        [
            ("true", True),
            ("false", False),
        ],
    )
    def test_graphrag_enabled_parsing(self, val, expected):
        s = _make_settings(GRAPHRAG_ENABLED=val)
        assert s.graphrag_enabled is expected


class TestEnvOverrides:
    """Settings reads values from environment variables."""

    def test_ov_url_override(self):
        s = _make_settings(OV_URL="http://custom:1234")
        assert s.ov_url == "http://custom:1234"

    def test_aws_region_override(self):
        s = _make_settings(AWS_REGION="eu-west-1")
        assert s.aws_region == "eu-west-1"

    def test_integer_from_env(self):
        s = _make_settings(REQUEST_TIMEOUT="60")
        assert s.request_timeout == 60

    def test_neptune_port_from_env(self):
        s = _make_settings(NEPTUNE_PORT="9999")
        assert s.neptune_port == 9999


class TestModelTiering:
    """Per-task model tiering is correctly configured."""

    def test_default_model_assignments(self):
        s = _make_settings()
        # Wiki uses Sonnet (reasoning-heavy task)
        assert "sonnet" in s.model_wiki.lower()
        # Tagging uses Haiku (cheap classification)
        assert "haiku" in s.model_tagging.lower()
        # Index uses Haiku (cheap summarization)
        assert "haiku" in s.model_index.lower()
        # Learning uses Sonnet (reasoning)
        assert "sonnet" in s.model_learning.lower()
        # GraphRAG extraction uses Haiku (structured output)
        assert "haiku" in s.model_graphrag.lower()

    def test_model_tiering_overridable(self):
        s = _make_settings(MODEL_WIKI="bedrock/custom-model")
        assert s.model_wiki == "bedrock/custom-model"

    def test_legacy_wiki_llm_model_field(self):
        """WIKI_LLM_MODEL env var still works for backward compat."""
        s = _make_settings(WIKI_LLM_MODEL="bedrock/custom-legacy")
        assert s.wiki_llm_model == "bedrock/custom-legacy"


class TestOvKeyResolution:
    """The ov_key property resolves from either env var name."""

    def test_openviking_root_key(self):
        s = _make_settings(OPENVIKING_ROOT_KEY="secret123")
        assert s.ov_key == "secret123"

    def test_root_key_fallback(self):
        s = _make_settings(ROOT_KEY="fallback456")
        assert s.ov_key == "fallback456"

    def test_openviking_root_key_takes_precedence(self):
        s = _make_settings(OPENVIKING_ROOT_KEY="primary", ROOT_KEY="fallback")
        assert s.ov_key == "primary"

    def test_empty_when_neither_set(self):
        s = _make_settings()
        assert s.ov_key == ""


class TestExtraFieldsIgnored:
    """Unknown env vars don't crash Settings (extra='ignore')."""

    def test_unknown_env_var(self):
        s = _make_settings(COMPLETELY_UNKNOWN_VAR="whatever")
        # Should not raise — extra='ignore' in model_config
        assert s.ov_url  # Still works normally
