from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agentauth import task_model_binding as module
from src.agentauth.model_policy import ModelPolicyError


@pytest.mark.asyncio
async def test_task_model_requires_explicit_transport_selection(monkeypatch):
    monkeypatch.setattr(module, "_resolve_active_allowlist_policy", AsyncMock(return_value=SimpleNamespace(
        principal_status="active", service_policy_unavailable_reason=None)))
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))
    with pytest.raises(ModelPolicyError, match="task_model_selection_missing"):
        await module.resolve_task_model(db, tenant="tenant", principal="principal",
            deadline=datetime.now(UTC) + timedelta(minutes=30), expected_policy_version="1")


@pytest.mark.asyncio
async def test_task_transport_cannot_reuse_cli_probe(monkeypatch):
    policy = SimpleNamespace(principal_status="active", service_policy_unavailable_reason=None,
        tenant_patterns=["*"], service_restriction_pattern_sets=[], context=object(), routing_user_id="principal")
    monkeypatch.setattr(module, "_resolve_active_allowlist_policy", AsyncMock(return_value=policy))
    monkeypatch.setattr(module.bedrock_routing_resolver, "resolve", AsyncMock(return_value=SimpleNamespace(
        account_id="123456789012", region="us-east-1", is_platform=False)))
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "lookup_evidence", lookup)
    db = SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(
        canonical_model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0", revision=1)))
    with pytest.raises(ModelPolicyError, match="task_model_probe_required"):
        await module.resolve_task_model(db, tenant="tenant", principal="principal",
            deadline=datetime.now(UTC) + timedelta(minutes=30), expected_policy_version="1")
    assert lookup.call_args.kwargs["compatibility_class"] == "anthropic_messages"
    assert lookup.call_args.kwargs["harness_contract_revision"] == "task-messages-v1"
    assert lookup.call_args.kwargs["request_shape_sha256"] == module.TASK_REQUEST_SHAPE
