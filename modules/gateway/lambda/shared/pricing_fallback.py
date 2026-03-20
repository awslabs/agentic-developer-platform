"""
Hardcoded Model Pricing Fallback for Lambda Functions.

This module provides a fallback pricing table when the AWS Pricing API is
unavailable or the model_pricing database table is empty. Prices are based
on AWS Bedrock published rates.

Issue #234: Budget Usage Tracking Lambda

Source: AWS Bedrock pricing page (https://aws.amazon.com/bedrock/pricing/)
"""

from decimal import Decimal
from typing import Any

# Pricing per 1000 tokens (USD)
# Source: AWS Bedrock pricing page
# Last updated: February 2026
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
    # Claude 4 models (2025)
    "anthropic.claude-opus-4-20250514-v1:0": {
        "input": Decimal("0.015"),
        "output": Decimal("0.075"),
    },
    "anthropic.claude-sonnet-4-20250514-v1:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
    },
    "anthropic.claude-haiku-4-20250514-v1:0": {
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
    # Default fallback pricing (conservative estimate)
    "default": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
    },
}


def resolve_model_id(model_id: str) -> str:
    """
    Resolve cross-region inference profile model IDs to base model IDs.

    Cross-region inference uses prefixes like:
    - us.anthropic.claude-3-5-sonnet-20241022-v2:0
    - global.anthropic.claude-sonnet-4-20250514-v1:0

    This strips the region prefix to get the base model ID for pricing lookup.

    Args:
        model_id: Model ID potentially with cross-region prefix

    Returns:
        Base model ID without cross-region prefix
    """
    # Strip cross-region inference profile prefixes
    if model_id.startswith("us."):
        return model_id[3:]  # Remove "us."
    elif model_id.startswith("global."):
        return model_id[7:]  # Remove "global."
    elif model_id.startswith("eu."):
        return model_id[3:]  # Remove "eu."
    elif model_id.startswith("apac."):
        return model_id[5:]  # Remove "apac."

    return model_id


def get_model_pricing(model_id: str) -> dict[str, Decimal]:
    """
    Get pricing for a specific model from the fallback table.

    Args:
        model_id: Bedrock model ID (may include cross-region prefix)

    Returns:
        Dict with 'input' and 'output' prices per 1000 tokens
    """
    resolved_id = resolve_model_id(model_id)

    if resolved_id in MODEL_PRICING:
        return MODEL_PRICING[resolved_id]

    # Try case-insensitive matching
    model_lower = resolved_id.lower()
    for key in MODEL_PRICING:
        if key.lower() == model_lower:
            return MODEL_PRICING[key]

    # Return default pricing for unknown models
    return MODEL_PRICING["default"]


def calculate_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    pricing_table: dict[str, Any] | None = None,
) -> Decimal:
    """
    Calculate total cost for a request.

    Args:
        model_id: Bedrock model ID
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        pricing_table: Optional custom pricing table (for database-sourced pricing)

    Returns:
        Total cost in USD (Decimal)
    """
    if pricing_table and model_id in pricing_table:
        pricing = pricing_table[model_id]
        input_price = Decimal(str(pricing.get("input", "0.003")))
        output_price = Decimal(str(pricing.get("output", "0.015")))
    else:
        pricing = get_model_pricing(model_id)
        input_price = pricing["input"]
        output_price = pricing["output"]

    input_cost = (Decimal(input_tokens) / Decimal("1000")) * input_price
    output_cost = (Decimal(output_tokens) / Decimal("1000")) * output_price

    total_cost = input_cost + output_cost

    # Round to 6 decimal places for precision
    return round(total_cost, 6)
