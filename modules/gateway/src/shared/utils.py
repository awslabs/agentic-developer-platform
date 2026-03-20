import hashlib
import secrets
from decimal import Decimal


def generate_token() -> str:
    """Generate a bg- prefixed secure random token."""
    return f"bg-{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    """SHA-256 hash for token storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def calculate_cost(model_id: str, input_tokens: int, output_tokens: int, pricing: dict[str, dict[str, Decimal]]) -> Decimal:
    """Calculate cost from model pricing table. Pricing keys: input_price_per_1k, output_price_per_1k."""
    prices = pricing.get(model_id)
    if not prices:
        return Decimal("0")
    input_cost = (Decimal(input_tokens) / 1000) * prices["input_price_per_1k"]
    output_cost = (Decimal(output_tokens) / 1000) * prices["output_price_per_1k"]
    return input_cost + output_cost
