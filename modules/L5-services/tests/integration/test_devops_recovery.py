"""SQLite-backed create recovery without native AWS calls."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.adapters.devops.spaces import SpaceAdapter, SpaceSelection
from l5_services.devops.lifecycle import SpaceLifecycle
from l5_services.models import DevopsSpaceIntent
from l5_services.models.base import Base


class NativeSpaces:
    def __init__(self):
        self.spaces = {}
        self.creates = 0
        self.lose_readback = False

    def list_agent_spaces(self, **kwargs):
        return {"agentSpaces": [space["agentSpace"] for space in self.spaces.values()]}

    def create_agent_space(self, **kwargs):
        self.creates += 1
        agent_space_id = "space-example"
        self.spaces[agent_space_id] = {"agentSpace": {"agentSpaceId": agent_space_id,
                                                    "name": kwargs["name"]}, "tags": kwargs["tags"]}
        return {"agentSpace": {"agentSpaceId": agent_space_id}}

    def get_agent_space(self, *, agentSpaceId):
        if self.lose_readback:
            raise ConnectionError("native response lost")
        return self.spaces[agentSpaceId]


@pytest.mark.asyncio
async def test_persisted_intent_recovers_after_lost_response(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'spaces.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    selection = SpaceSelection("tenant-example", "product-example", "project-example", "binding-example",
                               "123456789012", "example-region-1")
    provider = NativeSpaces()
    adapter = SpaceAdapter(provider, account_id=selection.account_id, region=selection.region)
    lifecycle = SpaceLifecycle(sessions, adapter)
    try:
        await lifecycle.prepare_create(selection, "operation-example", 1)
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
            assert receipt.state == "accepted" and receipt.native_space_id is None
        assert provider.creates == 0
        with pytest.raises(ValueError, match="changed creation intent"):
            await lifecycle.prepare_create(selection, "operation-example", 2)
        provider.lose_readback = True
        with pytest.raises(ConnectionError, match="response lost"):
            await lifecycle.execute_create(selection.tenant_id, "operation-example")
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
            assert receipt.state == "outcome_unknown" and receipt.native_space_id is None
        provider.lose_readback = False
        restarted = SpaceLifecycle(sessions, adapter)
        observed = await restarted.reconcile_create(selection.tenant_id, "operation-example")
        assert observed.agent_space_id == "space-example" and observed.ownership == "adp_created"
        assert (await restarted.execute_create(selection.tenant_id, "operation-example")).agent_space_id == observed.agent_space_id
        assert provider.creates == 1
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
            assert (receipt.state, receipt.native_space_id, receipt.scope_fingerprint) == (
                "succeeded", "space-example", selection.fingerprint())
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_uncertain_create_never_repeats_unobserved_effect(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'spaces.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine)
    selection = SpaceSelection("tenant-example", "product-example", "project-example", "binding-example",
                               "123456789012", "example-region-1")
    provider = NativeSpaces()
    lifecycle = SpaceLifecycle(sessions, SpaceAdapter(provider, account_id=selection.account_id,
                                                      region=selection.region))
    try:
        await lifecycle.prepare_create(selection, "operation-example", 1)
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
            receipt.state = "effect_started"
            await session.commit()
        assert await lifecycle.reconcile_create(selection.tenant_id, "operation-example") is None
        assert await lifecycle.execute_create(selection.tenant_id, "operation-example") is None
        assert provider.creates == 0
    finally:
        await engine.dispose()
