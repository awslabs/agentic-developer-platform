"""Independent PMM-08 regressions: retain upstream pricing uncertainty."""

from pricing_policy import RoutingEvidence, normalize_usage
from pricing_policy.storage import V2RateCache
from src.budget.pricing_decisions import decision_from_state
from src.usage.persona_cost import PersonaCostStatus, get_persona_cost_report
from src.usage.service import UsageService
from tests.usage.test_persona_cost import _context, _write


async def test_unknown_model_estimate_is_not_reported_as_fully_known(db_session):
    model = "anthropic.claude-future-unknown-model"
    decision = decision_from_state(
        request_id="independent-estimate",
        org_id="tenant",
        usage=normalize_usage({"input_tokens": 100, "output_tokens": 10}, api_format="anthropic"),
        evidence=RoutingEvidence(
            original_model_id=model,
            billing_model_id=model,
            geography="in_region",
            endpoint_region="us-east-1",
            served_service_tier_raw="standard",
        ),
        state=V2RateCache().state(monotonic=0, now_iso="2026-09-19T12:00:00+00:00"),
    )
    assert decision.confidence == "estimated"
    assert "unknown_model" in decision.estimate_reasons
    await UsageService(db_session).log_request(
        context=_context(persona="developer"),
        model=model,
        input_tokens=100,
        output_tokens=10,
        cost_usd=decision.ledger_cost,
        latency_ms=1,
        status_code=200,
        request_id="independent-estimate",
        agent_run_id="run-developer",
        pricing_decision=decision,
    )
    report = await get_persona_cost_report(db_session, org_id="tenant", principal_kind="service_account", principal_id="owner")
    assert report.status is not PersonaCostStatus.KNOWN, (decision.confidence, decision.estimate_reasons, report)


async def test_partly_unpriced_zero_is_not_reported_as_none_incurred(db_session):
    await _write(db_session, persona="architect", model="known", cost="0", priced=True)
    await _write(db_session, persona="developer", model="unknown", cost="0", priced=False)
    report = await get_persona_cost_report(db_session, org_id="tenant", principal_kind="service_account", principal_id="owner")
    assert report.partial is True
    assert report.status is not PersonaCostStatus.NONE_INCURRED, report
