"""Conservative client-cell cost estimates; not an admission or settlement authority."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation


COSTS = ("input_token_usd", "output_token_usd", "startup_usd", "network_storage_usd",
         "cleanup_usd", "retry_usd")
LIMITS = ("max_model_requests", "max_input_tokens", "max_output_tokens", "max_request_bytes",
          "max_response_bytes", "max_retries")


def amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("all-in pricing amount is invalid")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("all-in pricing amount is invalid") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("all-in pricing amount is invalid")
    return result


def reserve_all_in(fixture, *, execution_kind="model", now=None):
    # The selected driver owns this mode; fixture contents cannot select it.
    if execution_kind not in {"model", "stdio"}:
        raise ValueError("unknown client execution kind")
    quote = fixture.get("all_in_quote") if isinstance(fixture, dict) else None
    if (not isinstance(quote, dict) or set(quote) != {"version", "source_revision", "effective_at",
                                                     *COSTS, *LIMITS} or quote["version"] != 1 or
            not isinstance(quote["source_revision"], str) or len(quote["source_revision"]) != 40 or
            any(character not in "0123456789abcdef" for character in quote["source_revision"])):
        raise ValueError("fresh model, provider and overhead price provenance required")
    try:
        effective = datetime.fromisoformat(quote["effective_at"])
    except (ValueError, TypeError) as exc:
        raise ValueError("pricing provenance timestamp invalid") from exc
    current = now or datetime.now(UTC)
    if (effective.tzinfo is None or effective > current or
            current - effective > timedelta(hours=24)):
        raise ValueError("pricing provenance is stale or future dated")
    if any(type(quote[key]) is not int or not 0 <= quote[key] <= 1000000 for key in LIMITS):
        raise ValueError("model request, token, byte and retry bounds required")
    if (quote["max_request_bytes"] < 1 or
            quote["max_response_bytes"] < 1 or quote["max_retries"] != 0):
        raise ValueError("unbounded or replaying client work is not admitted")
    costs = {key: amount(quote[key]) for key in COSTS}
    model_limits = ("max_model_requests", "max_input_tokens", "max_output_tokens")
    if execution_kind == "stdio":
        if (any(quote[key] != 0 for key in model_limits) or
                costs["input_token_usd"] != 0 or costs["output_token_usd"] != 0):
            raise ValueError("stdio driver requires zero model quantities and tariffs")
    elif (any(quote[key] < 1 for key in model_limits) or
          costs["input_token_usd"] == 0 or costs["output_token_usd"] == 0):
        raise ValueError("bounded and priced native model work is required")
    provider = amount(fixture["unit_cost_usd"]) * fixture["expected_provider_calls"]
    model = quote["max_model_requests"] * (
        quote["max_input_tokens"] * costs["input_token_usd"] +
        quote["max_output_tokens"] * costs["output_token_usd"])
    reserved = provider + model + sum(costs[key] for key in COSTS[2:])
    if reserved > amount(fixture["max_cost_usd"]) or reserved > Decimal("20"):
        raise ValueError("all-in cell quote exceeds the $20 run envelope")
    return {"reserved_usd": reserved, "limits": {key: quote[key] for key in LIMITS},
            "pricing_source_revision": quote["source_revision"], "effective_at": effective.isoformat()}
