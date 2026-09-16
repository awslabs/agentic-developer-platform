"""A DB-independent pricing boundary for tests of attribution fields."""

from pricing_policy import normalize_usage
from pricing_policy.storage import V2RateCache
from src.budget.pricing_decisions import decision_from_state


async def price_fixture_usage(*, request_id, org_id, raw_usage, evidence, session_factory=None):
    state = V2RateCache().state(monotonic=0, now_iso="2026-09-12T00:00:00+00:00")
    return decision_from_state(
        request_id=request_id,
        org_id=org_id,
        usage=normalize_usage(raw_usage, api_format="openai"),
        evidence=evidence,
        state=state,
    )
