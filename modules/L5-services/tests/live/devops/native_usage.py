"""Check task usage without inventing cost attribution AWS does not expose."""

from decimal import Decimal, InvalidOperation


TASK_API = "aws-devops-agent:GetBacklogTask"
UNAVAILABLE_REASON = "provider_does_not_expose_task_cost"


def _amount(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("native task cost must be a finite nonnegative amount") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("native task cost must be a finite nonnegative amount")
    return amount


def verify_native_usage(usage, limit, *, minimum_requests=0):
    """Return coverage and a known/estimated amount; None means unknown, never zero.

    A reader still has to bind this observation to the independently read task.
    Request/duration bounds apply even when AWS omits per-task billing. Monthly
    account usage cannot be attributed to one task by this verifier.
    """
    if (not isinstance(usage, dict)
            or type(usage.get("requests")) is not int
            or not minimum_requests <= usage["requests"] <= limit["max_requests"]
            or type(usage.get("elapsed_seconds")) is not int
            or not 0 <= usage["elapsed_seconds"] <= limit["deadline_seconds"]
            or usage.get("cost_scope", "native_task") != "native_task"):
        raise ValueError("native task lacks scoped usage within its request/time budget")
    coverage = usage.get("cost_coverage", "measured")
    if coverage == "unavailable":
        if (usage.get("cost_source") != TASK_API
                or usage.get("cost_reason") != UNAVAILABLE_REASON
                or any(key in usage for key in ("spend_usd", "estimated_spend_usd", "estimate_ref"))):
            raise ValueError("unavailable task cost must identify the API limitation without an amount")
        return coverage, None
    if coverage == "estimated":
        if ("spend_usd" in usage or not isinstance(usage.get("estimate_ref"), str)
                or not usage["estimate_ref"].strip()):
            raise ValueError("native task estimates need provenance and cannot claim measured spend")
        amount = _amount(usage.get("estimated_spend_usd"))
    elif coverage == "measured":
        if any(key in usage for key in ("estimated_spend_usd", "estimate_ref")):
            raise ValueError("native task measured usage cannot contain an estimate")
        amount = _amount(usage.get("spend_usd"))
    else:
        raise ValueError("unsupported native task cost coverage")
    if amount > _amount(limit["max_spend_usd"]):
        raise ValueError("native task exceeded its authorized cost bound")
    return coverage, amount
