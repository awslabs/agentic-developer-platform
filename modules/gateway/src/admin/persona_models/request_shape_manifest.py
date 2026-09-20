"""Read the SDK-generated probe request-shape manifest without reconstructing it."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from src.admin.persona_models.catalogue import (
    COMPATIBILITY_CLASS_CLAUDE,
    HARNESS_CONTRACT_REVISION,
    PLATFORM_MODEL_CATALOGUE,
)

_PATH = Path(__file__).with_name("request-shape-manifest.json")


@lru_cache(maxsize=1)
def request_shape_manifest() -> dict[str, str]:
    raw = json.loads(_PATH.read_text(encoding="utf-8"))
    prompt_digest = raw.get("probe_prompt_sha256")
    if (
        raw.get("schema_version") != 2
        or raw.get("request_shape_normalization") != "claude-code-probe-date-device-v2"
        or raw.get("compatibility_class") != COMPATIBILITY_CLASS_CLAUDE
        or raw.get("harness_contract_revision") != HARNESS_CONTRACT_REVISION
        or not isinstance(prompt_digest, str)
        or len(prompt_digest) != 64
        or any(character not in "0123456789abcdef" for character in prompt_digest)
    ):
        raise RuntimeError("probe request-shape manifest does not match the Gateway harness contract")
    models = raw.get("models")
    if not isinstance(models, dict) or not models:
        raise RuntimeError("probe request-shape manifest has no models")
    if any(
        not isinstance(model, str)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        for model, digest in models.items()
    ):
        raise RuntimeError("probe request-shape manifest contains an invalid digest")
    catalogue_models = {model.canonical_model_id for model in PLATFORM_MODEL_CATALOGUE}
    if set(models) != catalogue_models:
        raise RuntimeError("probe request-shape manifest and platform catalogue model sets differ")
    return models


def expected_request_shape(model_id: str) -> str | None:
    return request_shape_manifest().get(model_id)
