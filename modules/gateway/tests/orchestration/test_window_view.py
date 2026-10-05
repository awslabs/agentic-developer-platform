from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
