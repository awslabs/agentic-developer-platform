from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agentauth import task_model_binding as module
from src.agentauth.model_policy import ModelPolicyError


@pytest.mark.asyncio
async def test_task_model_requires_explicit_transport_selection(monkeypatch):
    monkeypatch.setattr(
        module,
        "_resolve_active_allowlist_policy",
        AsyncMock(return_value=SimpleNamespace(principal_status="active", service_policy_unavailable_reason=None)),
    )
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))
    with pytest.raises(ModelPolicyError, match="task_model_selection_missing"):
        await module.resolve_task_model(
            db, tenant="tenant", principal="principal", deadline=datetime.now(UTC) + timedelta(minutes=30), expected_policy_version="1"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "persona,revision,shape",
    [
        (module.TASK_PERSONA, module.TASK_CONTRACT_REVISION, module.TASK_REQUEST_SHAPE),
        (module.TASK_CYBER_PERSONA, module.TASK_CYBER_CONTRACT_REVISION, module.TASK_CYBER_REQUEST_SHAPE),
        *(
            (name, profile.harness_contract_revision, profile.request_shape_sha256)
            for name, profile in module.TASK_PERSONAS.items()
            if name.endswith("-developer")
        ),
    ],
)
async def test_task_transport_cannot_reuse_cli_probe(monkeypatch, persona, revision, shape):
    policy = SimpleNamespace(
        principal_status="active",
        service_policy_unavailable_reason=None,
        tenant_patterns=["*"],
        service_restriction_pattern_sets=[],
        context=object(),
        routing_user_id="principal",
    )
    monkeypatch.setattr(module, "_resolve_active_allowlist_policy", AsyncMock(return_value=policy))
    monkeypatch.setattr(
        module.bedrock_routing_resolver,
        "resolve",
        AsyncMock(return_value=SimpleNamespace(account_id="123456789012", region="us-east-1", is_platform=False)),
    )
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "lookup_evidence", lookup)
    native = module.TASK_PERSONAS[persona].compatibility_class == "codex-sdk"
    model_id = "openai.gpt-6-astra" if native else "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    db = SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(canonical_model_id=model_id, revision=1)))
    with pytest.raises(ModelPolicyError, match="task_model_probe_required"):
        await module.resolve_task_model(
            db,
            tenant="tenant",
            principal="principal",
            deadline=datetime.now(UTC) + timedelta(minutes=30),
            expected_policy_version="1",
            persona=persona,
            responses_tools=native,
        )
    assert lookup.call_args.kwargs["compatibility_class"] == ("codex-sdk" if native else "anthropic_messages")
    assert lookup.call_args.kwargs["harness_contract_revision"] == revision
    assert lookup.call_args.kwargs["request_shape_sha256"] == shape


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [False, True])
async def test_task_responses_requires_distinct_task_probe_and_codex_class(monkeypatch, tools):
    from src.agentauth.task_responses_contract import TASK_RESPONSES_REVISION

    persona = "agent-task-gpt-fixture"
    # The transport implementation does not register or enable this persona.
    with pytest.raises(ModelPolicyError, match="task_model_transport_unsupported"):
        await module.resolve_task_model(
            object(),
            tenant="tenant",
            principal="principal",
            deadline=datetime.now(UTC) + timedelta(minutes=30),
            expected_policy_version="1",
            persona=persona,
            responses_tools=tools,
        )
    monkeypatch.setattr(module, "persona_compatibility_class", lambda key: "codex-sdk" if key == persona else None)
    policy = SimpleNamespace(
        principal_status="active",
        service_policy_unavailable_reason=None,
        tenant_patterns=["*"],
        service_restriction_pattern_sets=[],
        context=object(),
        routing_user_id="principal",
    )
    monkeypatch.setattr(module, "_resolve_active_allowlist_policy", AsyncMock(return_value=policy))
    monkeypatch.setattr(
        module.bedrock_routing_resolver,
        "resolve",
        AsyncMock(return_value=SimpleNamespace(account_id="123456789012", region="us-east-1", is_platform=False)),
    )
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "lookup_evidence", lookup)
    db = SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(canonical_model_id="openai.gpt-6-astra", revision=1)))
    with pytest.raises(ModelPolicyError, match="task_model_probe_required"):
        await module.resolve_task_model(
            db,
            tenant="tenant",
            principal="principal",
            deadline=datetime.now(UTC) + timedelta(minutes=30),
            expected_policy_version="1",
            persona=persona,
            responses_tools=tools,
        )
    assert lookup.call_args.kwargs["compatibility_class"] == "codex-sdk"
    from src.agentauth.task_responses_tools_contract import TASK_RESPONSES_TOOLS_REVISION

    assert lookup.call_args.kwargs["harness_contract_revision"] == (TASK_RESPONSES_TOOLS_REVISION if tools else TASK_RESPONSES_REVISION)
    assert lookup.call_args.kwargs["request_shape_sha256"] == (
        module.TASK_RESPONSES_TOOLS_REQUEST_SHAPE if tools else module.TASK_RESPONSES_REQUEST_SHAPE
    )


@pytest.mark.asyncio
async def test_task_codex_cannot_fall_back_to_anthropic(monkeypatch):
    monkeypatch.setattr(module, "persona_compatibility_class", lambda _: "codex-sdk")
    monkeypatch.setattr(
        module,
        "_resolve_active_allowlist_policy",
        AsyncMock(return_value=SimpleNamespace(principal_status="active", service_policy_unavailable_reason=None)),
    )
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(canonical_model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0", revision=1))
    )
    with pytest.raises(ModelPolicyError, match="task_model_transport_unsupported"):
        await module.resolve_task_model(
            db,
            tenant="tenant",
            principal="principal",
            deadline=datetime.now(UTC) + timedelta(minutes=30),
            expected_policy_version="1",
            persona="agent-task-gpt-fixture",
        )
