"""Guard test: every model ID in config.py must be registered in litellm-config.yaml.

Prevents regressions like #1425 where an invalid model ID (claude-haiku-4-6)
was introduced during config centralization (#1379), causing all Haiku-routed
LLM calls to 400 at runtime.

This test fails fast at CI time instead of silently 400ing per call at runtime.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# Add the ingestion scripts directory to sys.path so 'config' module is importable
_INGESTION_DIR = str(Path(__file__).resolve().parent.parent / "images" / "ingestion")
if _INGESTION_DIR not in sys.path:
    sys.path.insert(0, _INGESTION_DIR)

# Paths relative to the module root
_MODULE_ROOT = Path(__file__).resolve().parent.parent
_LITELLM_CONFIG = _MODULE_ROOT / "manifests" / "litellm-config.yaml"


def _extract_registered_model_names() -> set[str]:
    """Parse litellm-config.yaml and return all registered model_name values.

    The file uses ${VAR} template substitution, so we extract the literal
    model_name strings and also resolve known template patterns.
    """
    content = _LITELLM_CONFIG.read_text()
    # Match model_name entries: lines like `- model_name: "bedrock/..."` or
    # `- model_name: "bedrock/${VAR}"` — we keep both literal and templated.
    pattern = re.compile(r'model_name:\s*"([^"]+)"')
    return set(pattern.findall(content))


def _get_config_model_fields() -> dict[str, str]:
    """Return all model_* field names and their default values from Settings."""
    from config import Settings

    s = Settings()
    return {
        field_name: getattr(s, field_name)
        for field_name in Settings.model_fields
        if field_name.startswith("model_") or field_name in ("wiki_llm_model", "synthesis_model")
    }


class TestModelIdsRegistered:
    """Every model ID in config.py must be registered in litellm-config.yaml."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.registered = _extract_registered_model_names()
        self.config_models = _get_config_model_fields()

    def test_litellm_config_exists(self):
        """litellm-config.yaml must exist."""
        assert _LITELLM_CONFIG.exists(), f"Missing: {_LITELLM_CONFIG}"

    def test_at_least_one_model_registered(self):
        """litellm-config.yaml should have at least one model registered."""
        assert len(self.registered) > 0, "No model_name entries found in litellm-config.yaml"

    def test_all_config_models_are_registered(self):
        """Every model default in config.py must appear in litellm-config.yaml.

        Models using template variables (${VAR}) in litellm-config.yaml are
        excluded from this check since their resolved value depends on deploy-time
        substitution.
        """
        # Only check against literal (non-templated) model names
        literal_registered = {m for m in self.registered if "${" not in m}

        missing = []
        for field_name, model_id in self.config_models.items():
            if not model_id:
                continue  # Skip empty defaults
            if model_id not in literal_registered:
                missing.append(f"  {field_name} = {model_id!r}")

        assert not missing, (
            "Model IDs in config.py are NOT registered in litellm-config.yaml:\n"
            + "\n".join(missing)
            + "\n\nRegistered models:\n  "
            + "\n  ".join(sorted(literal_registered))
            + "\n\nFix: add the missing model to manifests/litellm-config.yaml "
            "or correct the ID in images/ingestion/config.py"
        )

    def test_no_invalid_haiku_4_6_reference(self):
        """Regression guard: the invalid claude-haiku-4-6 must never appear."""
        for field_name, model_id in self.config_models.items():
            assert "claude-haiku-4-6" not in model_id, (
                f"Invalid model ID in {field_name}: {model_id!r} — "
                "claude-haiku-4-6 does not exist. "
                "Use claude-haiku-4-5-20251001-v1:0 instead."
            )
