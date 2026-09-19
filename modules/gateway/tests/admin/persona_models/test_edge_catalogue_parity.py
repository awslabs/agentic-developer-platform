"""The webhook satellite may refuse early but may not own another model map."""

from __future__ import annotations

import json
from pathlib import Path

from src.admin.persona_models.catalogue import (
    HARNESS_CONTRACT_REVISION,
    PERSONA_ALLOWED_PATTERNS,
    PERSONA_MODEL_ALIASES,
    PLATFORM_MODEL_CATALOGUE,
)


def test_generated_webhook_catalogue_exactly_matches_gateway_authority():
    root = Path(__file__).parents[5]
    generated = json.loads(
        (
            root
            / "modules/agent-factory/webhook-ingress/lambda/common/persona_model_catalogue.json"
        ).read_text(encoding="utf-8")
    )

    assert generated["schema_version"] == 1
    assert generated["compatibility_class"] == "claude-agent-sdk"
    assert generated["harness_contract_revision"] == HARNESS_CONTRACT_REVISION
    assert generated["aliases"] == PERSONA_MODEL_ALIASES
    assert generated["allowed_patterns"] == list(PERSONA_ALLOWED_PATTERNS)
    assert generated["canonical_model_ids"] == [
        row.canonical_model_id for row in PLATFORM_MODEL_CATALOGUE
    ]
