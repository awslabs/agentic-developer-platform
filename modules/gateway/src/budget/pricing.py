"""Pre-request budget estimates using the active V2 pricing cache.

OpenAI and Claude quotes select one conservative published variant for the measured/estimated
context and available routing evidence. Startup and background async reads keep
this process cache current; a synchronous inference estimate does no database I/O.
The derived flat tables remain public compatibility views for other providers.
Completed requests use durable decisions, not this estimator.
"""

from dataclasses import replace
from decimal import Decimal
from typing import Any

from pricing_policy import (
    RoutingEvidence,
    canonical_billing_model_id,
    is_anthropic_model,
    is_v2_priced_model,
    legacy_flat_rates,
    legacy_flat_table,
    load_snapshot,
    normalize_usage,
    price_from_rate_row,
    quantize_ledger,
    select_rate_row,
)
from pricing_policy.policy import geography_from_model_prefix
from src.shared.logging import get_logger

logger = get_logger(__name__)


# The flat rate table, DERIVED from the shared snapshot (issue #4969). Built once
# at import: the snapshot file is immutable, so re-deriving per call would only
# repeat work. Includes the conservative "default" row the old literal had.
MODEL_PRICING: dict[str, dict[str, Decimal]] = legacy_flat_table()

# Alias mappings for common model name variations.
#
# Also derived: the curated aliases carry the short forms (`opus5`, `sonnet46`,
# `claude-3-5-sonnet`, ...) that model_resolver.py emits, and the OpenAI models'
# own alias lists carry the profile-prefixed forms. Both are in the snapshot, so
# neither is maintained here any more.
MODEL_ALIASES: dict[str, str] = {
    **load_snapshot().curated_non_openai.get("aliases", {}),
    **load_snapshot().alias_map,
}

# Default output token estimate for pre-request budget check
DEFAULT_OUTPUT_TOKEN_ESTIMATE = 500

# Chars-per-token heuristic for pre-request input estimation.
#
# Deliberately one constant shared by both estimators (body-parsing and
# header-only) so they cannot drift apart and disagree about the same request.
#
# It is an approximation, not a measurement: it counts text only, so requests
# carrying images or large tool-result blocks under-estimate. That is acceptable
# for a pre-charge because the reservation is reconciled to the real token counts
# once the response lands (Issue #4287) — but it is NOT safe to treat the output
# of these estimators as a billable figure.
_CHARS_PER_TOKEN = 4


class PricingService:
    """
    Service for calculating costs for Bedrock model usage.

    Provides:
    - Model-specific cost calculation
    - Input token estimation from request body
    - Output token estimation for pre-request checks
    - Support for model aliases
    """

    def __init__(self, pricing_table: dict[str, dict[str, Decimal]] | None = None):
        """
        Initialize the pricing service.

        Args:
            pricing_table: Optional custom pricing table (for testing)
        """
        self._pricing = pricing_table or MODEL_PRICING
        # A caller-supplied table is authoritative and gets no snapshot fallback:
        # a test that injects three models means three models, and silently
        # widening it to the full snapshot would make such a test unable to fail.
        self._use_shared_resolver = pricing_table is None

    def resolve_model_id(self, model_id: str) -> str:
        """
        Resolve model ID from alias if needed.

        Args:
            model_id: Model ID or alias

        Returns:
            Resolved Bedrock model ID
        """
        # Check if it's an alias first
        if model_id in MODEL_ALIASES:
            return MODEL_ALIASES[model_id]

        # Check if it's a direct model ID
        if model_id in self._pricing:
            return model_id

        # Try case-insensitive matching
        model_lower = model_id.lower()
        for key in self._pricing:
            if key.lower() == model_lower:
                return key

        # Return as-is (will use default pricing)
        return model_id

    def get_model_pricing(self, model_id: str) -> dict[str, Decimal]:
        """
        Get pricing for a specific model.

        Args:
            model_id: Bedrock model ID or alias

        Returns:
            Dict with 'input' and 'output' prices per 1000 tokens
            (may also include 'cache_read_input' and 'cache_creation_input')
        """
        if self._use_shared_resolver and is_v2_priced_model(canonical_billing_model_id(model_id)):
            from src.budget.pricing_v2_reader import cached_rate_state

            snapshot = load_snapshot()
            billing_id = canonical_billing_model_id(model_id, snapshot=snapshot)
            rows = tuple(row for row in cached_rate_state().rows if row.model_id == billing_id)
            snapshot = replace(snapshot, rates=rows or tuple(row for row in snapshot.rates if row.model_id == billing_id))
            if is_anthropic_model(billing_id):
                if not snapshot.rates:
                    # A retired/excluded Claude model still has its historical
                    # model-specific bundled quote until AWS publishes a row.
                    return legacy_flat_rates(model_id, snapshot=snapshot)[0]
                row, _ = select_rate_row(
                    rows=snapshot.rates,
                    usage=normalize_usage({"input_tokens": 1000, "output_tokens": 1000}, api_format="anthropic"),
                    evidence=RoutingEvidence(
                        original_model_id=model_id, billing_model_id=billing_id, geography=geography_from_model_prefix(model_id)
                    ),
                    short_threshold=snapshot.short_context_max_input_tokens,
                )
                return {
                    "input": row.input_price_per_1k_tokens,
                    "output": row.output_price_per_1k_tokens,
                    **({"cache_read_input": row.cache_read_price_per_1k_tokens} if row.cache_read_price_per_1k_tokens is not None else {}),
                    **({"cache_creation_input": row.cache_write_price_per_1k_tokens} if row.cache_write_price_per_1k_tokens is not None else {}),
                    **(
                        {"cache_creation_1h_input": row.cache_write_1h_price_per_1k_tokens}
                        if row.cache_write_1h_price_per_1k_tokens is not None
                        else {}
                    ),
                }
            return legacy_flat_rates(model_id, snapshot=snapshot)[0]
        resolved_id = self.resolve_model_id(model_id)

        if resolved_id in self._pricing:
            return self._pricing[resolved_id]

        if self._use_shared_resolver:
            # Issue #4969: defer to the shared resolver rather than dropping
            # straight to "default". It applies the cross-region prefix and
            # version-suffix normalization the two tables used to implement
            # separately (#4592), so `us.anthropic.claude-opus-4-8` finds its
            # published rate here exactly as it does in settlement.
            rates, known = legacy_flat_rates(model_id)
            if known:
                return rates
            logger.debug(f"Using default pricing for unknown model: {model_id}")
            return rates

        # Log and return default pricing
        logger.debug(f"Using default pricing for unknown model: {model_id}")
        return self._pricing["default"]

    def quote_cost(self, model_id: str, input_tokens: int, output_tokens: int, *, state=None) -> tuple[Decimal, Decimal, Decimal]:
        """Quote supported models from one cached generation, without request-path I/O."""
        if self._use_shared_resolver and is_v2_priced_model(canonical_billing_model_id(model_id)):
            from src.budget.pricing_v2_reader import cached_rate_state

            state = state or cached_rate_state()
            billing_id = canonical_billing_model_id(model_id)
            rows = tuple(row for row in state.rows if row.model_id == billing_id)
            if not rows:
                rows = tuple(row for row in load_snapshot().rates if row.model_id == billing_id)
            if rows:
                usage = normalize_usage(
                    {"input_tokens": input_tokens, "output_tokens": output_tokens},
                    api_format="anthropic" if is_anthropic_model(billing_id) else "openai",
                )
                row, _ = select_rate_row(
                    rows=rows,
                    usage=usage,
                    evidence=RoutingEvidence(
                        original_model_id=model_id, billing_model_id=billing_id, geography=geography_from_model_prefix(model_id)
                    ),
                    short_threshold=load_snapshot().short_context_max_input_tokens,
                )
                exact, _ = price_from_rate_row(row, usage)
                return quantize_ledger(exact), row.input_price_per_1k_tokens, row.output_price_per_1k_tokens
            # Missing models use the explicit generic estimate, never an old
            # OpenAI flat literal or a fabricated variant from another model.
            if is_anthropic_model(billing_id):
                pricing = legacy_flat_rates(model_id)[0]
            else:
                pricing = load_snapshot().curated_non_openai["rates"]["default"]
            input_rate, output_rate = Decimal(pricing["input"]), Decimal(pricing["output"])
        else:
            pricing = self.get_model_pricing(model_id)
            input_rate, output_rate = pricing["input"], pricing["output"]
        exact = (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate) / Decimal("1000")
        return quantize_ledger(exact), input_rate, output_rate

    def calculate_cost(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> Decimal:
        """
        Calculate total cost for a request.

        Args:
            model_id: Bedrock model ID or alias
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens

        Returns:
            Total cost in USD (Decimal)
        """
        if self._use_shared_resolver and is_v2_priced_model(canonical_billing_model_id(model_id)):
            return self.quote_cost(model_id, input_tokens, output_tokens)[0]
        pricing = self.get_model_pricing(model_id)

        input_cost = (Decimal(input_tokens) / Decimal("1000")) * pricing["input"]
        output_cost = (Decimal(output_tokens) / Decimal("1000")) * pricing["output"]

        total_cost = input_cost + output_cost

        # Round to 6 decimal places for precision
        return round(total_cost, 6)

    def estimate_input_tokens(self, request_body: dict[str, Any]) -> int:
        """
        Estimate input tokens from request body.

        Uses a simple heuristic of ~4 characters per token for English text.
        This is conservative to avoid underestimating costs.

        Args:
            request_body: Request body dictionary

        Returns:
            Estimated input token count
        """
        total_chars = 0

        # Extract messages content
        messages = request_body.get("messages", [])
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                # Handle content blocks (e.g., text, images)
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text", "")
                        if text:
                            total_chars += len(text)

        # Include system message if present
        system = request_body.get("system", "")
        if isinstance(system, str):
            total_chars += len(system)
        elif isinstance(system, list):
            for block in system:
                if isinstance(block, dict):
                    text = block.get("text", "")
                    if text:
                        total_chars += len(text)

        # Estimate tokens (~4 chars per token, conservative)
        estimated_tokens = max(1, total_chars // _CHARS_PER_TOKEN)

        logger.debug(f"Estimated input tokens: {estimated_tokens} from {total_chars} chars")

        return estimated_tokens

    def estimate_input_tokens_from_payload_size(self, content_length: int) -> int:
        """
        Estimate input tokens from the serialized request size alone.

        Issue #4287: the budget middleware is pure ASGI and must NOT read the
        request body — ``receive()`` is a one-shot stream and the mantle route
        (``/openai/v1/responses``) forwards the body byte-for-byte, so consuming
        it in middleware would starve the downstream handler. See
        ``src/shared/enforced_paths.py``. The ``content-length`` header is
        therefore the only size signal available pre-request.

        This over-counts relative to ``estimate_input_tokens`` because it counts
        JSON structure (keys, quotes, braces) as prompt text. That direction is
        the safe one for a pre-charge: over-estimating reserves too much
        headroom, which is corrected downward on reconciliation, whereas
        under-estimating lets a request overshoot the cap it just passed.

        Args:
            content_length: Value of the ``content-length`` request header.

        Returns:
            Estimated input token count (at least 1).
        """
        return max(1, content_length // _CHARS_PER_TOKEN)

    def estimate_cost_from_payload_size(self, model_id: str, content_length: int) -> Decimal:
        """
        Estimate request cost from the model id and serialized request size.

        Issue #4287: the pre-request estimate used to be a flat ``$0.05``
        regardless of model or size, so one large request against an expensive
        model could overshoot a cap the check had just passed. This makes the
        pre-charge both model-aware and size-aware without reading the body.

        Output tokens fall back to ``DEFAULT_OUTPUT_TOKEN_ESTIMATE``: ``max_tokens``
        lives in the body, which is unreadable here.

        Args:
            model_id: Bedrock model ID or alias (from the request URL path).
            content_length: Value of the ``content-length`` request header.

        Returns:
            Estimated cost in USD.
        """
        input_tokens = self.estimate_input_tokens_from_payload_size(content_length)
        output_tokens = self.estimate_output_tokens(None)

        estimated_cost = self.calculate_cost(model_id, input_tokens, output_tokens)

        logger.debug(f"Estimated request cost from payload size: ${estimated_cost:.6f} (model={model_id}, bytes={content_length})")

        return estimated_cost

    def estimate_output_tokens(self, max_tokens: int | None) -> int:
        """
        Estimate output tokens for pre-request budget check.

        Uses max_tokens if provided, otherwise uses a default estimate.

        Args:
            max_tokens: Max tokens from request (if specified)

        Returns:
            Estimated output token count
        """
        if max_tokens is not None and max_tokens > 0:
            # Use half of max_tokens as a reasonable estimate
            return max(1, max_tokens // 2)

        return DEFAULT_OUTPUT_TOKEN_ESTIMATE

    def estimate_request_cost(
        self,
        model_id: str,
        request_body: dict[str, Any],
    ) -> Decimal:
        """
        Estimate the cost of a request before execution.

        This is used for pre-request budget checks.

        Args:
            model_id: Bedrock model ID or alias
            request_body: Request body dictionary

        Returns:
            Estimated cost in USD
        """
        input_tokens = self.estimate_input_tokens(request_body)
        max_tokens = request_body.get("max_tokens")
        output_tokens = self.estimate_output_tokens(max_tokens)

        estimated_cost = self.calculate_cost(model_id, input_tokens, output_tokens)

        logger.debug(f"Estimated request cost: ${estimated_cost:.6f} (input: {input_tokens}, output: {output_tokens})")

        return estimated_cost


# Global pricing service instance
pricing_service = PricingService()
