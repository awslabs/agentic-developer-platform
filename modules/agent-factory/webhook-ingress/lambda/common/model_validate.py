"""Inline model validation for the webhook-ingress Lambda (issue #2279).

Resolves short aliases to full Bedrock model IDs and validates against
the persona's allowed_models patterns. This is a minimal inline copy of
the alias + fnmatch logic from modules/gateway/src/proxy/model_resolver.py
— the Lambda cannot import from the gateway pod (separate runtime).

The Lambda must answer GitHub in <10s, so we validate locally (no HTTP
call to the gateway).
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

# Short "latest" aliases that humans type in /model directives.
# Kept deliberately minimal — just the names users are likely to type.
# Must stay in sync with model_resolver.py's short aliases.
def _load_catalogue() -> dict:
    try:
        value = json.loads(
            Path(__file__).with_name("persona_model_catalogue.json").read_text(
                encoding="utf-8"
            )
        )
        if (
            value.get("schema_version") != 1
            or value.get("compatibility_class") != "claude-agent-sdk"
            or not isinstance(value.get("aliases"), dict)
            or not isinstance(value.get("canonical_model_ids"), list)
            or not isinstance(value.get("allowed_patterns"), list)
        ):
            raise ValueError("unsupported generated catalogue")
        return value
    except (OSError, ValueError, TypeError):
        # An edge artefact with missing or malformed generated policy data may
        # refuse; it must never invent a model from a local fallback.
        return {
            "aliases": {},
            "canonical_model_ids": [],
            "allowed_patterns": [],
        }


_CATALOGUE = _load_catalogue()
MODEL_ALIASES: dict[str, str] = _CATALOGUE["aliases"]
CANONICAL_MODEL_IDS = frozenset(_CATALOGUE["canonical_model_ids"])

# Default allowed patterns (matches model_resolver.py DEFAULT_ALLOWED_PATTERNS)
DEFAULT_ALLOWED_PATTERNS: list[str] = _CATALOGUE["allowed_patterns"]


def resolve_and_validate(
    alias: str,
    persona_allowed_models: list[str] | None = None,
    tenant_patterns: list[str] | None = None,
) -> str | None:
    """Resolve a model alias and validate access.

    Args:
        alias: The user-typed model name (e.g. "opus", "claude-sonnet-4",
               or a raw Bedrock model ID).
        persona_allowed_models: The persona's allowed_models list from the
                                agent registry (DDB SS attribute). If empty/None,
                                falls back to tenant_patterns.
        tenant_patterns: Tenant-level allowed model patterns. If empty/None,
                         falls back to DEFAULT_ALLOWED_PATTERNS.

    Returns:
        The resolved Bedrock model ID if allowed, or None if rejected
        (unknown alias that doesn't match any pattern, or model not in
        the allowed list).
    """
    # Step 1: Resolve only a published pinned alias or canonical catalogue ID.
    # Pattern-shaped pass-through used to accept models the authority had never
    # published and made the edge a competing selector.
    normalized = alias.strip()
    model_id = MODEL_ALIASES.get(normalized.lower(), normalized)
    if model_id not in CANONICAL_MODEL_IDS:
        return None

    # Step 2: Determine which patterns to validate against
    # Persona-level takes precedence, then tenant, then defaults
    patterns = persona_allowed_models or tenant_patterns or DEFAULT_ALLOWED_PATTERNS

    # Step 3: fnmatch against allowed patterns
    for pattern in patterns:
        if fnmatch.fnmatch(model_id, pattern):
            return model_id

    return None
