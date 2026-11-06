"""Offline scoped native updates with independent SQL and provider readback."""

from dataclasses import asdict

import pytest
from l5_services.adapters.devops.spaces import SpaceAdapter, SpaceSelection
from l5_services.devops.lifecycle import SpaceLifecycle
from l5_services.models import DevopsSpaceIntent
from l5_services.models.base import Base
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_devops_retirement import LifecycleSpaces


@pytest.fixture
async def update_scenario(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'updates.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    selection = SpaceSelection("tenant-example", "product-example", "project-example", "binding-example",
                               "123456789012", "example-region-1")
    provider = LifecycleSpaces()
    lifecycle = SpaceLifecycle(sessions, SpaceAdapter(provider, account_id=selection.account_id,
                                                      region=selection.region))
    await lifecycle.prepare_create(selection, "operation-example", 1)
    await lifecycle.execute_create(selection.tenant_id, "operation-example")
    try:
        yield sessions, selection, provider, lifecycle
    finally:
        await engine.dispose()


@pytest.fixture
def native_update(monkeypatch):
    def install(provider):
        updates = []
        faults = {"lose_response": False}

        def update_agent_space(*, agentSpaceId, description):
            updates.append((agentSpaceId, description))
            provider.spaces[agentSpaceId]["agentSpace"]["description"] = description
            if faults["lose_response"]:
                raise ConnectionError("provider update response lost")
            return provider.spaces[agentSpaceId]

        monkeypatch.setattr(provider, "update_agent_space", update_agent_space, raising=False)
        return updates, faults

    return install


@pytest.mark.asyncio
async def test_scoped_update_preserves_identity_and_automation(update_scenario, native_update):
    sessions, selection, provider, lifecycle = update_scenario
    updates, _ = native_update(provider)
    provider.spaces["space-example"]["agentSpace"]["preferences"] = {"review_enabled": False}
    await lifecycle.prepare_update(selection, "operation-example", "update-example", 2,
                                   "updated for this project")
    assert updates == []
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
        assert receipt.state == "update_pending" and receipt.configuration_revision == 1
        assert receipt.update_configuration_revision == 2 and receipt.native_space_id == "space-example"
    result = await lifecycle.execute_update(selection, "operation-example", "update-example")
    assert result.ownership == "adp_created" and result.agent_space_id == "space-example"
    assert updates == [("space-example", "updated for this project")]
    assert provider.spaces["space-example"]["agentSpace"]["preferences"] == {"review_enabled": False}
    assert (await lifecycle.execute_update(selection, "operation-example", "update-example")).agent_space_id == result.agent_space_id
    assert len(updates) == 1
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
        assert (receipt.state, receipt.effective_description, receipt.ownership) == (
            "ready", "updated for this project", "adp_created")


@pytest.mark.asyncio
async def test_lost_update_response_reconciles_without_second_effect(update_scenario, native_update):
    sessions, selection, provider, lifecycle = update_scenario
    updates, faults = native_update(provider)
    await lifecycle.prepare_update(selection, "operation-example", "update-example", 2, "revised description")
    with pytest.raises(ValueError, match="changed intent"):
        await lifecycle.prepare_update(selection, "operation-example", "update-example", 2, "different description")
    with pytest.raises(ValueError, match="previous space operation"):
        await lifecycle.prepare_update(selection, "operation-example", "next-update", 3, "different description")
    faults["lose_response"] = True
    with pytest.raises(ConnectionError, match="response lost"):
        await lifecycle.execute_update(selection, "operation-example", "update-example")
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
        assert receipt.state == "update_outcome_unknown" and receipt.effective_description is None
    faults["lose_response"] = False
    restarted = SpaceLifecycle(sessions, lifecycle.adapter)
    assert (await restarted.execute_update(selection, "operation-example", "update-example")).agent_space_id == "space-example"
    assert updates == [("space-example", "revised description")]


@pytest.mark.asyncio
async def test_update_refuses_foreign_adopted_or_unobserved_space(update_scenario, native_update):
    sessions, selection, provider, lifecycle = update_scenario
    updates, _ = native_update(provider)
    adopted = SpaceSelection(selection.tenant_id, "adopted-product", "adopted-project", "adopted-binding",
                             selection.account_id, selection.region)
    async with sessions() as session:
        session.add(DevopsSpaceIntent(tenant_id=adopted.tenant_id, operation_id="adopted-operation",
                                     selection=asdict(adopted), configuration_revision=1, intent_hash="c" * 64,
                                     state="ready", native_space_id="space-example", ownership="customer_linked",
                                     owned_trigger_ids=[], resume_trigger_ids=[], active_executions=[]))
        await session.commit()
    with pytest.raises(ValueError, match="original creation ownership"):
        await lifecycle.prepare_update(adopted, "adopted-operation", "update-example", 2, "cannot change adopted")
    with pytest.raises(ValueError, match="scope does not match"):
        await lifecycle.prepare_update(adopted, "operation-example", "update-example", 2, "foreign change")
    assert updates == []
    await lifecycle.prepare_update(selection, "operation-example", "update-example", 2, "not yet observed")
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
        receipt.state = "update_effect_started"
        await session.commit()
    assert await lifecycle.reconcile_update(selection, "operation-example", "update-example") is None
    assert await lifecycle.execute_update(selection, "operation-example", "update-example") is None
    assert updates == []


@pytest.mark.asyncio
async def test_update_rejects_implicit_automation_change(update_scenario, native_update, monkeypatch):
    sessions, selection, provider, lifecycle = update_scenario
    updates, _ = native_update(provider)
    with pytest.raises(ValueError, match="valid description"):
        await lifecycle.prepare_update(selection, "operation-example", "update-invalid", 2, "  ")
    assert updates == []
    await lifecycle.prepare_update(selection, "operation-example", "update-example", 2, "safe description")
    original_update = provider.update_agent_space

    def changed_preferences(**kwargs):
        result = original_update(**kwargs)
        provider.spaces[kwargs["agentSpaceId"]]["agentSpace"]["preferences"] = {"review_enabled": True}
        return result

    monkeypatch.setattr(provider, "update_agent_space", changed_preferences)
    with pytest.raises(ValueError, match="automation preferences not verified"):
        await lifecycle.execute_update(selection, "operation-example", "update-example")
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
        assert receipt.state == "update_outcome_unknown" and receipt.effective_description is None
