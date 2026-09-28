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

# Short "latest" aliases that humans type in /model directives.
# Kept deliberately minimal — just the names users are likely to type.
# Must stay in sync with model_resolver.py's short aliases.
MODEL_ALIASES: dict[str, str] = {
    # Version-pinned aliases: <family><major><minor>, compact, no separators.
    # Each maps to an invocable inference-profile ID (global. prefix) — verified
    # ACTIVE via `aws bedrock list-inference-profiles` AND verified to invoke
    # via bedrock-runtime invoke-model (issue #2300). Bare/ambiguous aliases
    # (opus/sonnet/haiku) were removed in favour of explicit versions so a
    # /model choice can't silently drift to a different model over time.
    "opus5": "global.anthropic.claude-opus-5",
    "opus48": "global.anthropic.claude-opus-4-8",
    "opus47": "global.anthropic.claude-opus-4-7",
    "opus46": "global.anthropic.claude-opus-4-6-v1",
    "opus45": "global.anthropic.claude-opus-4-5-20251101-v1:0",
    "sonnet46": "global.anthropic.claude-sonnet-4-6",
    "sonnet45": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "haiku45": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    # NOTE: claude-sonnet-4-20250514 (Legacy, access-denied after 30d unused)
    # and claude-fable-5 (requires non-default data-retention mode) are listed
    # ACTIVE but do NOT invoke for us — deliberately excluded (#2300 lesson:
    # verify by invocation, not just listing).
}

# Default allowed patterns (matches model_resolver.py DEFAULT_ALLOWED_PATTERNS)
DEFAULT_ALLOWED_PATTERNS: list[str] = [
    "anthropic.claude-*",
    "us.anthropic.claude-*",
    "eu.anthropic.claude-*",
    "global.anthropic.claude-*",
]


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
    # Step 1: Resolve alias → model_id (pass-through if not a known alias)
    model_id = MODEL_ALIASES.get(alias.lower(), alias)

    # Step 2: Determine which patterns to validate against
    # Persona-level takes precedence, then tenant, then defaults
    patterns = persona_allowed_models or tenant_patterns or DEFAULT_ALLOWED_PATTERNS

    # Step 3: fnmatch against allowed patterns
    for pattern in patterns:
        if fnmatch.fnmatch(model_id, pattern):
            return model_id

    return None
