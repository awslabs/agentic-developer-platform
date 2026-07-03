"""Single source of truth for proxy paths under budget + rate-limit enforcement.

Both the budget middleware (``src/budget/enforcement_middleware.py``) and the
rate-limit middleware (``src/ratelimit/enforcement_middleware.py``) gate the same
set of proxy paths. These lists used to be duplicated, which let new routes slip
through enforcement (Issue #2792: the ``/openai/v1/responses`` passthrough was
metered but neither budget- nor rate-limit-enforced). Keeping the list here — and
importing it from both middlewares — means a new proxy route is registered for
enforcement in exactly one place.

Matching semantics: a request path is enforced when it ``startswith`` any entry,
so trailing-path routes like ``/model/`` cover ``/model/{id}/invoke``.
"""

from __future__ import annotations

# Paths that require budget + rate-limit enforcement.
ENFORCED_PATHS: tuple[str, ...] = (
    "/v1/chat/completions",
    "/v1/messages",
    "/bedrock/invoke",
    "/bedrock/invoke-with-response-stream",
    "/model/",
    # Issue #2792: OpenAI Responses-API passthrough (bedrock-mantle, #2709).
    # Path-based gate only — the mantle route forwards the body byte-for-byte,
    # so enforcement must NOT read the request body here.
    "/openai/v1/responses",
)
