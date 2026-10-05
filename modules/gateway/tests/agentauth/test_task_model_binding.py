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
    assert module._resolve_active_allowlist_policy.call_args.kwargs["require_hierarchy"] is True
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


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("case", ["fresh", "stale", "missing", "other_context_stale", "global", "reader_failure"])
async def test_task_pricing_uses_selected_profile_and_endpoint(monkeypatch, case, native):
    now = datetime.now(UTC)
    model = "us.anthropic.claude-opus-5"
    if case == "global" and not native:
        model = "global.anthropic.claude-opus-5"
    if native:
        model = "openai.gpt-6-astra"
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
    monkeypatch.setattr(
        module, "lookup_evidence", AsyncMock(return_value=SimpleNamespace(is_stale=False, is_proven=True, provider_request_id="real-probe"))
    )

    def rate(geo=None, region="us-east-1", age=0, tier="standard", context="flat"):
        geo = geo or ("in_region" if native else "geo_cris")
        return SimpleNamespace(
            model_id="openai.gpt-6-astra" if native else "anthropic.claude-opus-5",
            geography=geo,
            region=region,
            service_tier=tier,
            context_tier=context,
            verified_at=(now - timedelta(days=age)).isoformat(),
        )

    rows = [rate("global_cris", age=10), rate(region="eu-west-1", age=10), rate(tier="batch", age=10)]
    if case != "missing" and not (native and case == "global"):
        rows.append(rate(age=10 if case == "stale" else 0))
    if case == "other_context_stale":
        rows.append(rate(age=10, context="long"))
    monkeypatch.setattr(
        module,
        "get_rate_state",
        AsyncMock(
            return_value=SimpleNamespace(
                rows=tuple(rows),
                from_database=True,
                reasons=("read_failure",) if case == "reader_failure" else (),
                generation_id=160,
                pointer_revision=160,
            )
        ),
    )
    db = SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(canonical_model_id=model, revision=2)))
    kwargs = dict(
        tenant="tenant",
        principal="principal",
        deadline=now + timedelta(hours=1),
        expected_policy_version="2",
        persona="agent-task-gpt-developer" if native else module.TASK_CYBER_PERSONA,
    )
    if case == "fresh":
        binding = await module.resolve_task_model(db, **kwargs)
        assert binding["model_id"] == model
        assert binding["model_policy_version"] == "2"
    else:
        with pytest.raises(ModelPolicyError, match="task_model_pricing_unavailable"):
            await module.resolve_task_model(db, **kwargs)
