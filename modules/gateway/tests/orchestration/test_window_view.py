from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration.shared_window import WindowRenewalRequest, valid_extension, valid_wall_clock
from src.orchestration.window_view import execution_window_view


@pytest.mark.parametrize("state,elapsed,expected", [("ready", 50, "expired"), ("running", 1, "active"), ("passed", 50, "complete")])
async def test_window_visibility_does_not_require_an_execution_row(monkeypatch, state, elapsed, expected):
    now = datetime.now(UTC)
    monkeypatch.setattr("src.orchestration.window_view.flow_started_at", AsyncMock(return_value=now - timedelta(hours=elapsed)))
    monkeypatch.setattr(
        "src.orchestration.window_view.OrchestrationRepository.get_accepted_plan",
        AsyncMock(return_value=SimpleNamespace(version=3, plan_hash="a" * 64)),
    )
    policy = SimpleNamespace(expires_at=now + timedelta(days=2), limits=SimpleNamespace(max_wall_clock_seconds=72000))
    result = await execution_window_view(
        None, flow=SimpleNamespace(org_id="org", id="flow"), nodes=[SimpleNamespace(state=state)], inputs=SimpleNamespace(policy=policy, refusal=None)
    )
    assert result["status"] == expected
    if expected == "expired":
        request = result["renewal_request"]
        assert request["resume_expired"] is True
        assert request["max_wall_clock_seconds"] > 50 * 3600
        assert request["expected_plan_hash"] == "a" * 64


async def test_unverifiable_policy_is_visible(monkeypatch):
    result = await execution_window_view(None, flow=None, nodes=[], inputs=SimpleNamespace(refusal=object()))
    assert result["status"] == "unavailable"


@pytest.mark.parametrize("elapsed_days,renewable", [(8, True), (15, True), (31, False)])
async def test_dashboard_renewal_uses_current_cumulative_limit(monkeypatch, elapsed_days, renewable):
    now = datetime.now(UTC)
    started = now - timedelta(days=elapsed_days)
    monkeypatch.setattr("src.orchestration.window_view.flow_started_at", AsyncMock(return_value=started))
    monkeypatch.setattr(
        "src.orchestration.window_view.OrchestrationRepository.get_accepted_plan",
        AsyncMock(return_value=SimpleNamespace(version=3, plan_hash="a" * 64)),
    )
    prior_seconds = 7 * 86400
    before = started + timedelta(seconds=prior_seconds)
    policy = SimpleNamespace(expires_at=before, limits=SimpleNamespace(max_wall_clock_seconds=prior_seconds))
    result = await execution_window_view(
        None,
        flow=SimpleNamespace(org_id="org", id="flow"),
        nodes=[SimpleNamespace(state="ready")],
        inputs=SimpleNamespace(policy=policy, refusal=None),
    )
    assert result["status"] == "expired"
    if renewable:
        request = WindowRenewalRequest.model_validate(result["renewal_request"])
        observed = datetime.fromisoformat(result["observed_at"])
        assert request.resume_expired is True
        assert valid_wall_clock(prior_seconds, request.max_wall_clock_seconds, observed, started=started, resume_expired=True)
        assert valid_extension(
            before, request.expires_at, observed, prior_seconds, request.max_wall_clock_seconds, started=started, resume_expired=True
        )
    else:
        assert "renewal_request" not in result
        assert "30-day" in result["renewal_unavailable"]
