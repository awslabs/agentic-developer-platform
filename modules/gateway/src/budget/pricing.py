"""
Bedrock Model Pricing Service.

This module provides centralized cost calculation for Bedrock models.
Pricing is based on AWS Bedrock published rates as of January 2024.
"""

from decimal import Decimal
from typing import Any

from src.shared.logging import get_logger

logger = get_logger(__name__)


# Pricing per 1000 tokens (USD)
# Source: AWS Bedrock pricing page (https://aws.amazon.com/bedrock/pricing/)
# Last updated: January 2024
MODEL_PRICING: dict[str, dict[str, Decimal]] = {
    # Claude 3.5 models (latest)
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
    },
    "anthropic.claude-3-5-haiku-20241022-v1:0": {
        "input": Decimal("0.0008"),
        "output": Decimal("0.004"),
    },
    # Claude 3 models
    "anthropic.claude-3-opus-20240229-v1:0": {
        "input": Decimal("0.015"),
        "output": Decimal("0.075"),
    },
    "anthropic.claude-3-sonnet-20240229-v1:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
    },
    "anthropic.claude-3-haiku-20240307-v1:0": {
        "input": Decimal("0.00025"),
        "output": Decimal("0.00125"),
    },
    # Claude 2.x models (legacy)
    "anthropic.claude-v2:1": {
        "input": Decimal("0.008"),
        "output": Decimal("0.024"),
    },
    "anthropic.claude-v2": {
        "input": Decimal("0.008"),
        "output": Decimal("0.024"),
    },
    "anthropic.claude-instant-v1": {
        "input": Decimal("0.0008"),
        "output": Decimal("0.0024"),
    },
    # Amazon Titan Text models
    "amazon.titan-text-express-v1": {
        "input": Decimal("0.0002"),
        "output": Decimal("0.0006"),
    },
    "amazon.titan-text-lite-v1": {
        "input": Decimal("0.00015"),
        "output": Decimal("0.0002"),
    },
    "amazon.titan-text-premier-v1:0": {
        "input": Decimal("0.0005"),
        "output": Decimal("0.0015"),
    },
    # Amazon Titan Embed models (text)
    "amazon.titan-embed-text-v1": {
        "input": Decimal("0.0001"),
        "output": Decimal("0"),  # Embeddings don't have output tokens
    },
    "amazon.titan-embed-text-v2:0": {
        "input": Decimal("0.00002"),
        "output": Decimal("0"),
    },
    # Cohere models
    "cohere.command-text-v14": {
        "input": Decimal("0.0015"),
        "output": Decimal("0.002"),
    },
    "cohere.command-light-text-v14": {
        "input": Decimal("0.0003"),
        "output": Decimal("0.0006"),
    },
    "cohere.command-r-v1:0": {
        "input": Decimal("0.0005"),
        "output": Decimal("0.0015"),
    },
    "cohere.command-r-plus-v1:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
    },
    # Meta Llama models
    "meta.llama3-8b-instruct-v1:0": {
        "input": Decimal("0.0003"),
        "output": Decimal("0.0006"),
    },
    "meta.llama3-70b-instruct-v1:0": {
        "input": Decimal("0.00265"),
        "output": Decimal("0.0035"),
    },
    "meta.llama3-1-8b-instruct-v1:0": {
        "input": Decimal("0.00022"),
        "output": Decimal("0.00022"),
    },
    "meta.llama3-1-70b-instruct-v1:0": {
        "input": Decimal("0.00099"),
        "output": Decimal("0.00099"),
    },
    "meta.llama3-1-405b-instruct-v1:0": {
        "input": Decimal("0.00532"),
        "output": Decimal("0.016"),
    },
    "meta.llama3-2-1b-instruct-v1:0": {
        "input": Decimal("0.0001"),
        "output": Decimal("0.0001"),
    },
    "meta.llama3-2-3b-instruct-v1:0": {
        "input": Decimal("0.00015"),
        "output": Decimal("0.00015"),
    },
    "meta.llama3-2-11b-instruct-v1:0": {
        "input": Decimal("0.00016"),
        "output": Decimal("0.00016"),
    },
    "meta.llama3-2-90b-instruct-v1:0": {
        "input": Decimal("0.00072"),
        "output": Decimal("0.00072"),
    },
    # Mistral models
    "mistral.mistral-7b-instruct-v0:2": {
        "input": Decimal("0.00015"),
        "output": Decimal("0.0002"),
    },
    "mistral.mixtral-8x7b-instruct-v0:1": {
        "input": Decimal("0.00045"),
        "output": Decimal("0.0007"),
    },
    "mistral.mistral-large-2402-v1:0": {
        "input": Decimal("0.004"),
        "output": Decimal("0.012"),
    },
    "mistral.mistral-small-2402-v1:0": {
        "input": Decimal("0.001"),
        "output": Decimal("0.003"),
    },
    # AI21 Jurassic models
    "ai21.j2-ultra-v1": {
        "input": Decimal("0.0125"),
        "output": Decimal("0.0125"),
    },
    "ai21.j2-mid-v1": {
        "input": Decimal("0.0125"),
        "output": Decimal("0.0125"),
    },
    # OpenAI models served via bedrock-mantle Responses API (Issue #2792, route #2709).
    # Source: AWS Bedrock pricing page (https://aws.amazon.com/bedrock/pricing/),
    # OpenAI section, retrieved 2026-07-03. Published per-1M-token rates converted
    # to per-1000-token (÷1000) to match this table's unit.
    #   GPT-5.5 (US East, in-region parity):  $5.50/1M in, $33.00/1M out
    #   gpt-oss-120b (Standard tier):          $0.1545/1M in, $0.6180/1M out
    "openai.gpt-5.5": {
        "input": Decimal("0.0055"),
        "output": Decimal("0.033"),
    },
    "openai.gpt-oss-120b": {
        "input": Decimal("0.0001545"),
        "output": Decimal("0.000618"),
    },
    # Default fallback pricing (conservative estimate)
    "default": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
    },
}

# Alias mappings for common model name variations
MODEL_ALIASES: dict[str, str] = {
    # Claude 3.5 aliases
    "claude-3-5-sonnet": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-sonnet-20241022": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-haiku": "anthropic.claude-3-5-haiku-20241022-v1:0",
    "claude-3-5-haiku-20241022": "anthropic.claude-3-5-haiku-20241022-v1:0",
    # Claude 3 aliases
    "claude-3-opus": "anthropic.claude-3-opus-20240229-v1:0",
    "claude-3-opus-20240229": "anthropic.claude-3-opus-20240229-v1:0",
    "claude-3-sonnet": "anthropic.claude-3-sonnet-20240229-v1:0",
    "claude-3-sonnet-20240229": "anthropic.claude-3-sonnet-20240229-v1:0",
    "claude-3-haiku": "anthropic.claude-3-haiku-20240307-v1:0",
    "claude-3-haiku-20240307": "anthropic.claude-3-haiku-20240307-v1:0",
    # Legacy Claude aliases
    "claude-2.1": "anthropic.claude-v2:1",
    "claude-2": "anthropic.claude-v2",
    "claude-instant-1.2": "anthropic.claude-instant-v1",
}

# Default output token estimate for pre-request budget check
DEFAULT_OUTPUT_TOKEN_ESTIMATE = 500


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
        """
        resolved_id = self.resolve_model_id(model_id)

        if resolved_id in self._pricing:
            return self._pricing[resolved_id]

        # Log and return default pricing
        logger.debug(f"Using default pricing for unknown model: {model_id}")
        return self._pricing["default"]

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
        estimated_tokens = max(1, total_chars // 4)

        logger.debug(f"Estimated input tokens: {estimated_tokens} from {total_chars} chars")

        return estimated_tokens

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
