"""Inline model validation for the webhook-ingress Lambda (issue #2279).

Resolves short aliases to full Bedrock model IDs and validates against
the persona's allowed_models patterns. This is a minimal inline copy of
the alias + fnmatch logic from modules/gateway/src/proxy/model_resolver.py
— the Lambda cannot import from the gateway pod (separate runtime).

The Lambda must answer GitHub in <10s, so we validate locally (no HTTP
call to the gateway).

PMM-07 splits this into two deliberately separate answers:

``resolve_legacy_assignment``
    What the pre-PMM run would have executed: alias expansion plus an
    ``fnmatch`` against the allowed patterns, pass-through included. This is
    the model that actually runs while the posture is ``report_only``, so it
    must keep behaving exactly as it did before PMM-07 and must not depend on
    the generated catalogue at all.

``resolve_canonical_override``
    The strict answer used to build the *proposed* gateway decision: a value
    is accepted only if the authority actually published it. A refusal here is
    recorded as a refusal (the resolver's ``direct_override_unresolved``); it
    is never allowed to become a silent substitution.

Collapsing the two is what regressed: a strict refusal read downstream as "no
directive given", and the worker then substituted its own default, turning a
behaviour-neutral change into a silent model change (the design's §8 report-only
rule and §3 decision 1 both forbid that).
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

# Legacy pattern set, inlined deliberately. ``report_only`` execution must not
# be able to change — or fail — because a *generated* artefact is absent or
# malformed, so the legacy path reads no catalogue file.
DEFAULT_ALLOWED_PATTERNS: list[str] = [
    "anthropic.claude-*",
    "us.anthropic.claude-*",
    "eu.anthropic.claude-*",
    "global.anthropic.claude-*",
]

# Short "latest" aliases that humans type in /model directives.
# Kept deliberately minimal — just the names users are likely to type.
# Must stay in sync with model_resolver.py's short aliases.
LEGACY_MODEL_ALIASES: dict[str, str] = {
    # Version-pinned aliases: <family><major><minor>, compact, no separators.
    # Each maps to an ACTIVE inference-profile ID (global. prefix). The original
    # set was direct-invoke checked (#2300); Sonnet 5 has account availability
    # metadata and local SDK request capture, not a paid invoke receipt.
    # Bare/ambiguous aliases (opus/sonnet/haiku) were removed so a
    # /model choice can't silently drift to a different model over time.
    "opus5": "global.anthropic.claude-opus-5",
    "opus48": "global.anthropic.claude-opus-4-8",
    "opus47": "global.anthropic.claude-opus-4-7",
    "opus46": "global.anthropic.claude-opus-4-6-v1",
    "opus45": "global.anthropic.claude-opus-4-5-20251101-v1:0",
    "sonnet5": "global.anthropic.claude-sonnet-5",
    "sonnet46": "global.anthropic.claude-sonnet-4-6",
    "sonnet45": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "haiku45": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    # NOTE: claude-sonnet-4-20250514 (Legacy, access-denied after 30d unused)
    # and claude-fable-5 (requires non-default data-retention mode) are listed
    # ACTIVE but do NOT invoke for us — deliberately excluded (#2300 lesson:
    # verify by invocation, not just listing).
}

_EMPTY_CATALOGUE: dict = {
    "aliases": {},
    "canonical_model_ids": [],
    "allowed_patterns": [],
}


def _string_map(value: object) -> dict[str, str] | None:
    """Accept a mapping only when every key and value is genuinely text."""
    if not isinstance(value, dict):
        return None
    if not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        return None
    return dict(value)


def _string_list(value: object) -> list[str] | None:
    """Accept a sequence only when every element is genuinely text.

    A JSON list may legally hold objects, numbers or nulls. Those reached
    ``frozenset()`` while module globals were still being initialised, so an
    unhashable element raised during import and took the whole Lambda down
    instead of refusing one directive.
    """
    if not isinstance(value, list):
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return list(value)


def _load_catalogue() -> dict:
    """Read the generated catalogue, containing every malformed shape.

    Validation happens before any module global is derived from the result, so
    a bad artefact degrades to "publishes nothing, therefore refuses every
    canonical override" rather than failing at import time. It must never
    invent a model from a local fallback.
    """
    try:
        raw = Path(__file__).with_name("persona_model_catalogue.json").read_text(
            encoding="utf-8"
        )
        value = json.loads(raw)
    except (OSError, ValueError):
        return dict(_EMPTY_CATALOGUE)

    # A bare array, a null or a string root has no ``.get`` — check the root
    # type first rather than assuming an object.
    if not isinstance(value, dict):
        return dict(_EMPTY_CATALOGUE)

    aliases = _string_map(value.get("aliases"))
    canonical_ids = _string_list(value.get("canonical_model_ids"))
    allowed_patterns = _string_list(value.get("allowed_patterns"))
    if (
        value.get("schema_version") != 1
        or value.get("compatibility_class") != "claude-agent-sdk"
        or aliases is None
        or canonical_ids is None
        or allowed_patterns is None
    ):
        return dict(_EMPTY_CATALOGUE)

    return {
        "aliases": aliases,
        "canonical_model_ids": canonical_ids,
        "allowed_patterns": allowed_patterns,
    }


_CATALOGUE = _load_catalogue()
CANONICAL_MODEL_ALIASES: dict[str, str] = _CATALOGUE["aliases"]
CANONICAL_MODEL_IDS = frozenset(_CATALOGUE["canonical_model_ids"])
CANONICAL_ALLOWED_PATTERNS: list[str] = _CATALOGUE["allowed_patterns"]

# Retained name for existing importers of the legacy alias table.
MODEL_ALIASES: dict[str, str] = LEGACY_MODEL_ALIASES


def resolve_legacy_assignment(
    alias: str,
    persona_allowed_models: list[str] | None = None,
    tenant_patterns: list[str] | None = None,
) -> str | None:
    """Resolve exactly as the edge did before PMM-07 — the model that runs.

    While the posture is ``report_only`` this is the *actual* assignment, so it
    intentionally keeps the historic pass-through: a raw ID that matches an
    allowed pattern resolves even if it is not in the published catalogue. The
    proposed decision judges publication separately, in
    :func:`resolve_canonical_override`.

    Args:
        alias: The user-typed model name (e.g. "sonnet46", or a raw Bedrock
               model ID).
        persona_allowed_models: The persona's allowed_models list from the
                                agent registry (DDB SS attribute). If empty/None,
                                falls back to tenant_patterns.
        tenant_patterns: Tenant-level allowed model patterns. If empty/None,
                         falls back to DEFAULT_ALLOWED_PATTERNS.

    Returns:
        The resolved Bedrock model ID if allowed, else None.
    """
    model_id = LEGACY_MODEL_ALIASES.get(alias.lower(), alias)
    patterns = persona_allowed_models or tenant_patterns or DEFAULT_ALLOWED_PATTERNS
    for pattern in patterns:
        if fnmatch.fnmatch(model_id, pattern):
            return model_id
    return None


def resolve_canonical_override(
    alias: str,
    persona_allowed_models: list[str] | None = None,
    tenant_patterns: list[str] | None = None,
) -> str | None:
    """Resolve only a model the gateway authority actually published.

    Used to build the *proposed* decision, never the executed assignment. The
    edge may refuse obviously invalid input from generated catalogue data
    (design §3 decision 9) but may not select a different model, so the only
    outcomes are "this exact published ID" or None.
    """
    normalized = alias.strip()
    model_id = CANONICAL_MODEL_ALIASES.get(normalized.lower(), normalized)
    if model_id not in CANONICAL_MODEL_IDS:
        return None
    patterns = persona_allowed_models or tenant_patterns or CANONICAL_ALLOWED_PATTERNS
    for pattern in patterns:
        if fnmatch.fnmatch(model_id, pattern):
            return model_id
    return None


def resolve_and_validate(
    alias: str,
    persona_allowed_models: list[str] | None = None,
    tenant_patterns: list[str] | None = None,
) -> str | None:
    """Back-compatible alias for the legacy (executed) resolution.

    Existing callers that ask "what model does this run use?" keep their
    historic answer. Callers that need the strict published check must ask for
    :func:`resolve_canonical_override` explicitly.
    """
    return resolve_legacy_assignment(alias, persona_allowed_models, tenant_patterns)
