"""The protected evaluation path authorizes with the canonical member id, not the raw approver sub (#7419)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration import evaluation_authority
from src.shared.identity.resolver import UnresolvableUserEntityError


def _inputs():
    policy = SimpleNamespace(
        principal_id="cognito-sub-of-approver",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        limits=SimpleNamespace(max_wall_clock_seconds=3600),
    )
    return SimpleNamespace(refusal=None, policy=policy, plan_version=3)


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setattr(evaluation_authority, "is_shared_continuation", AsyncMock(return_value=False), raising=False)
    monkeypatch.setattr("src.orchestration.shared_policy.is_shared_continuation", AsyncMock(return_value=False))
    monkeypatch.setattr(evaluation_authority, "load_in_force_policy", AsyncMock(return_value=_inputs()))
    monkeypatch.setattr(evaluation_authority, "flow_started_at", AsyncMock(return_value=None))


async def test_protected_evaluation_marker_carries_the_canonical_principal(protected, monkeypatch):
    resolver = AsyncMock(return_value="users-id-of-approver")
    monkeypatch.setattr("src.shared.identity.resolver.resolve_root_user_entity_id", resolver)
    inputs, marker = await evaluation_authority.evaluation_inputs(object(), org_id="org", flow_id="flow")
    assert marker["principal_user_id"] == "users-id-of-approver"
    assert marker["prior_spend_usd"] == "0"
    resolver.assert_awaited_once()
    assert resolver.await_args.args[1:] == ("org", "cognito-sub-of-approver")
    assert inputs.policy.principal_id == "cognito-sub-of-approver"


async def test_unresolvable_principal_keeps_the_recorded_principal(protected, monkeypatch):
    monkeypatch.setattr(
        "src.shared.identity.resolver.resolve_root_user_entity_id",
        AsyncMock(side_effect=UnresolvableUserEntityError("cognito-sub-of-approver", "no member")),
    )
    _, marker = await evaluation_authority.evaluation_inputs(object(), org_id="org", flow_id="flow")
    assert marker["principal_user_id"] == "cognito-sub-of-approver"
