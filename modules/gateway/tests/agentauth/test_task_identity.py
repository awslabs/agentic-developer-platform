from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agentauth import task_identity as module
from src.agentauth.model_policy import ModelPolicyError
from src.budget.enforcement_service import BudgetEnforcementService
from src.shared.models.organization import Department, Team
from src.shared.models.persona_models import ServicePrincipal, ServicePrincipalAlias
from src.shared.schemas.auth import TokenContext


@pytest.fixture
async def identity_db(test_engine):
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
        db.add_all(
            [
                Department(id="dept", org_id="tenant", name="Engineering"),
                Department(id="other-dept", org_id="tenant", name="Research"),
                Team(id="team", org_id="tenant", department_id="dept", name="Cyber"),
                Team(id="other-team", org_id="tenant", department_id="other-dept", name="Other"),
                Team(id="foreign-team", org_id="foreign", department_id="dept", name="Foreign"),
                ServicePrincipal(canonical_service_principal_id="principal", org_id="tenant", display_name="Test", approved_by="operator"),
                ServicePrincipalAlias(
                    id="alias",
                    org_id="tenant",
                    canonical_service_principal_id="principal",
                    alias_source="cognito_m2m",
                    alias_id="cognito_m2m:client",
                    registered_by="operator",
                ),
            ]
        )
        await db.commit()
        yield db


def context(kind="service", team=""):
    return TokenContext(
        user_id="principal",
        org_id="tenant",
        team_id=team,
        department_id="",
        account_type=kind,
        canonical_service_principal_id="principal",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_service_resolves_full_budget_hierarchy(identity_db, monkeypatch):
    monkeypatch.setattr(module, "_cognito_assignment", AsyncMock(return_value=("team", "dept")))
    resolved = await module.resolve_task_identity_context(identity_db, context())
    assert resolved.user_id == "principal"
    assert resolved.canonical_service_principal_id == "principal"
    assert BudgetEnforcementService._get_entity_hierarchy(None, resolved) == [
        ("service_account", "principal"),
        ("team", "team"),
        ("department", "dept"),
        ("org", "tenant"),
    ]


@pytest.mark.asyncio
async def test_human_department_is_derived_from_current_team(identity_db):
    resolved = await module.resolve_task_identity_context(identity_db, context("human", "team"))
    assert (resolved.team_id, resolved.department_id) == ("team", "dept")
    assert BudgetEnforcementService._get_entity_hierarchy(None, resolved)[0] == ("user", "principal")


@pytest.mark.asyncio
@pytest.mark.parametrize("assignment", [(None, None), ("missing", "dept"), ("foreign-team", "dept"), ("team", "other-dept")])
async def test_invalid_assignment_refuses_instead_of_tenant_fallback(identity_db, monkeypatch, assignment):
    monkeypatch.setattr(module, "_cognito_assignment", AsyncMock(return_value=assignment))
    with pytest.raises(ModelPolicyError, match="task_identity_hierarchy_unavailable"):
        await module.resolve_task_identity_context(identity_db, context())


@pytest.mark.asyncio
async def test_live_assignment_is_reread_without_cache(identity_db, monkeypatch):
    lookup = AsyncMock(side_effect=[("team", "dept"), ("other-team", "other-dept"), RuntimeError("unavailable")])
    monkeypatch.setattr(module, "_cognito_assignment", lookup)
    first = await module.resolve_task_identity_context(identity_db, context())
    second = await module.resolve_task_identity_context(identity_db, context())
    assert first.team_id == "team" and second.team_id == "other-team"
    with pytest.raises(ModelPolicyError, match="task_identity_hierarchy_unavailable"):
        await module.resolve_task_identity_context(identity_db, context())


@pytest.mark.asyncio
@pytest.mark.parametrize("conflicting", [False, True])
async def test_every_active_alias_must_agree(identity_db, monkeypatch, conflicting):
    identity_db.add(
        ServicePrincipalAlias(
            id="alias2",
            org_id="tenant",
            canonical_service_principal_id="principal",
            alias_source="cognito_m2m",
            alias_id="cognito_m2m:client2",
            registered_by="operator",
        )
    )
    await identity_db.commit()

    async def lookup(alias, tenant):
        return ("other-team", "other-dept") if conflicting and alias.id == "alias2" else ("team", "dept")

    monkeypatch.setattr(module, "_cognito_assignment", lookup)
    if conflicting:
        with pytest.raises(ModelPolicyError, match="task_identity_hierarchy_unavailable"):
            await module.resolve_task_identity_context(identity_db, context())
    else:
        assert (await module.resolve_task_identity_context(identity_db, context())).team_id == "team"


@pytest.mark.asyncio
async def test_revoked_alias_cannot_supply_assignment(identity_db, monkeypatch):
    from sqlalchemy import update

    await identity_db.execute(update(ServicePrincipalAlias).values(is_active=False, revoked_at=datetime.now(UTC), revoked_by="operator"))
    await identity_db.commit()
    lookup = AsyncMock()
    monkeypatch.setattr(module, "_cognito_assignment", lookup)
    with pytest.raises(ModelPolicyError, match="task_identity_hierarchy_unavailable"):
        await module.resolve_task_identity_context(identity_db, context())
    lookup.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{}, {"org_id": "foreign"}, {"status": "disabled"}, {"team_id": ""}, {"department_id": ""}, {"client_id": "other"}]
)
async def test_cognito_reads_authoritative_metadata_consistently(monkeypatch, change):
    from src.admin import agent_service

    item = {"client_id": "client", "org_id": "tenant", "status": "active", "team_id": "team", "department_id": "dept", **change}
    table = Mock()
    table.get_item.return_value = {"Item": item}
    service = SimpleNamespace(table_name="agents", dynamodb=SimpleNamespace(Table=lambda name: table))
    monkeypatch.setattr(agent_service, "AgentService", lambda: service)
    alias = SimpleNamespace(alias_id="cognito_m2m:client")
    if change:
        with pytest.raises(ModelPolicyError, match="task_identity_hierarchy_unavailable"):
            await module._cognito_assignment(alias, "tenant")
    else:
        assert await module._cognito_assignment(alias, "tenant") == ("team", "dept")
    table.get_item.assert_called_once_with(Key={"client_id": "client"}, ConsistentRead=True)


@pytest.mark.asyncio
async def test_task_policy_restores_team_routing(identity_db, monkeypatch):
    from src.agentauth.model_policy import _resolve_active_allowlist_policy
    from src.proxy import model_resolver
    from src.proxy.bedrock_routing import BedrockRoutingResolver
    from tests.proxy.test_bedrock_routing_resolver import _destination, _mapping

    monkeypatch.setattr(module, "_cognito_assignment", AsyncMock(return_value=("team", "dept")))
    monkeypatch.setattr(
        model_resolver, "production_model_resolver", lambda settings: SimpleNamespace(get_configured_allowed_models=lambda ctx: (["*"], "test"))
    )
    identity_db.add_all(
        [
            _destination("team-destination", "222222222222", owner_org_id="tenant"),
            _destination("org-destination", "333333333333", owner_org_id="tenant"),
            _mapping("team-mapping", "team-destination", scope_type="team", scope_id_org="tenant", scope_id_team="team"),
            _mapping("org-mapping", "org-destination", scope_type="org", scope_id_org="tenant"),
        ]
    )
    await identity_db.commit()
    policy = await _resolve_active_allowlist_policy(
        identity_db,
        tenant_id="tenant",
        principal_kind="service_account",
        principal_id="principal",
        expires_at=context().expires_at,
        require_hierarchy=True,
    )
    target = await BedrockRoutingResolver().resolve(identity_db, policy.context, user_id=policy.routing_user_id)
    assert target.account_id == "222222222222"
    assert (policy.context.team_id, policy.context.department_id) == ("team", "dept")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["human", "service"])
@pytest.mark.parametrize("transport", ["anthropic_messages", "openai_responses"])
async def test_task_usage_event_retains_hierarchy_and_caller_kind(monkeypatch, kind, transport):
    from src.agentauth import task_model
    from src.chat_logging.service import ChatLoggingService

    writer = SimpleNamespace(write_log=AsyncMock(return_value=True))
    logger = SimpleNamespace(enabled=True, _get_s3_writer=lambda: writer)
    logger._build_chat_log = lambda **kwargs: ChatLoggingService._build_chat_log(None, **kwargs)
    monkeypatch.setattr(task_model, "ChatLoggingService", lambda: logger)
    ctx = context(kind, "team").model_copy(update={"department_id": "dept"})
    decision = SimpleNamespace(request_id="request", to_dict=lambda: {})
    await task_model.write_task_usage_event(
        context=ctx,
        identity=SimpleNamespace(tenant="tenant"),
        binding={
            "model_id": "openai.gpt-6-astra" if transport == "openai_responses" else "global.anthropic.claude-opus-5",
            "transport": transport,
        },
        decision=decision,
        usage={"input_tokens": 2, "output_tokens": 3},
        latency_ms=10,
    )
    document = writer.write_log.call_args.kwargs["log_data"]
    assert document["api_format"] == ("openai" if transport == "openai_responses" else "anthropic")
    assert {key: document[key] for key in ("org_id", "department_id", "team_id", "account_type")} == {
        "org_id": "tenant",
        "department_id": "dept",
        "team_id": "team",
        "account_type": kind,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_registry_assignment_checks_base_row_after_index_lookup(monkeypatch, disabled):
    from src.admin import agent_registry_service

    row = SimpleNamespace(agent_name="registry-agent", agent_id="id", org_id="tenant", status="active", team_id="stale-index-team")
    item = {
        k: {"S": v}
        for k, v in {"agent_name": "registry-agent", "org_id": "tenant", "team_id": "team", "status": "disabled" if disabled else "active"}.items()
    }
    service = SimpleNamespace(
        table_name="registry",
        list_agents=AsyncMock(return_value=SimpleNamespace(items=[row], last_key=None)),
        dynamodb=SimpleNamespace(get_item=Mock(return_value={"Item": item})),
    )
    monkeypatch.setattr(agent_registry_service, "AgentRegistryService", lambda: service)
    aliases = [SimpleNamespace(alias_id="registry-agent")]
    if disabled:
        with pytest.raises(ModelPolicyError, match="task_identity_hierarchy_unavailable"):
            await module._registry_assignments(aliases, "tenant")
    else:
        assert await module._registry_assignments(aliases, "tenant") == [("team", None)]
    service.dynamodb.get_item.assert_called_once_with(TableName="registry", Key={"agent_id": {"S": "id"}}, ConsistentRead=True)


@pytest.mark.asyncio
async def test_legacy_service_assignment_uses_tenant_scoped_sql_row(identity_db):
    from sqlalchemy import update

    from src.shared.models.organization import ServiceAccount

    identity_db.add(
        ServiceAccount(
            id="legacy", org_id="tenant", department_id="dept", team_id="team", name="legacy", iam_role_arn="arn:aws:iam::123456789012:role/legacy"
        )
    )
    await identity_db.execute(update(ServicePrincipalAlias).values(alias_source="sa_registration", alias_id="legacy"))
    await identity_db.commit()
    assert (await module.resolve_task_identity_context(identity_db, context())).team_id == "team"
    await identity_db.execute(update(ServiceAccount).values(org_id="foreign"))
    await identity_db.commit()
    with pytest.raises(ModelPolicyError, match="task_identity_hierarchy_unavailable"):
        await module.resolve_task_identity_context(identity_db, context())


@pytest.mark.asyncio
async def test_unresolvable_alias_cannot_silently_drop_hierarchy(identity_db):
    from sqlalchemy import update

    await identity_db.execute(update(ServicePrincipalAlias).values(alias_source="eventbridge", alias_id="event-rule"))
    await identity_db.commit()
    with pytest.raises(ModelPolicyError, match="task_identity_hierarchy_unavailable"):
        await module.resolve_task_identity_context(identity_db, context())
