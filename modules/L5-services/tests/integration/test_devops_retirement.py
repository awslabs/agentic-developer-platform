"""Ownership-aware native retirement against isolated SQL and provider fixtures."""

from dataclasses import asdict

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.adapters.devops.spaces import SpaceAdapter, SpaceSelection
from l5_services.devops.lifecycle import SpaceLifecycle
from l5_services.models import DevopsSpaceIntent
from l5_services.models.base import Base

from test_devops_recovery import NativeSpaces


class LifecycleSpaces(NativeSpaces):
    def __init__(self):
        super().__init__()
        self.triggers = {"trigger-owned": "ENABLED", "trigger-shared": "DISABLED"}
        self.executions = [{"executionId": "execution-active", "executionStatus": "RUNNING"}]
        self.deleted = []
        self.associations = []
        self.assets = []

    def list_triggers(self, *, agentSpaceId, **kwargs):
        return {"items": [{"triggerId": trigger_id, "agentSpaceId": agentSpaceId, "status": status}
                          for trigger_id, status in self.triggers.items()]}

    def update_trigger(self, *, agentSpaceId, triggerId, status):
        self.triggers[triggerId] = status

    def list_executions(self, *, agentSpaceId, **kwargs):
        return {"executions": [execution | {"agentSpaceId": agentSpaceId} for execution in self.executions]}

    def list_associations(self, *, agentSpaceId, **kwargs):
        return {"associations": self.associations}

    def list_assets(self, *, agentSpaceId, **kwargs):
        return {"items": self.assets}

    def delete_agent_space(self, *, agentSpaceId):
        self.deleted.append(agentSpaceId)


class ManagedScheduler:
    def __init__(self):
        self.paused = False
        self.verify = True

    def pause(self, selection, agent_space_id):
        self.paused = True

    def resume(self, selection, agent_space_id):
        self.paused = False

    def is_paused(self, selection, agent_space_id):
        return self.paused if self.verify else False


@pytest.fixture
async def lifecycle_fixture(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}")
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
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
        receipt.owned_trigger_ids = ["trigger-owned"]
        await session.commit()
    try:
        yield sessions, selection, provider, lifecycle, ManagedScheduler()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_owned_suspend_and_resume_preserve_external_automation(lifecycle_fixture):
    sessions, selection, provider, lifecycle, scheduler = lifecycle_fixture
    active = await lifecycle.suspend(selection, "operation-example", scheduler)
    assert active == ["execution-active"]
    assert provider.triggers == {"trigger-owned": "DISABLED", "trigger-shared": "DISABLED"}
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
        assert (receipt.state, receipt.active_executions, receipt.resume_trigger_ids) == (
            "suspended", active, ["trigger-owned"])
    await lifecycle.resume(selection, "operation-example", scheduler)
    assert provider.triggers == {"trigger-owned": "ENABLED", "trigger-shared": "DISABLED"}
    assert provider.deleted == []


@pytest.mark.asyncio
async def test_retirement_waits_for_work_and_original_ownership(lifecycle_fixture):
    sessions, selection, provider, lifecycle, scheduler = lifecycle_fixture
    await lifecycle.suspend(selection, "operation-example", scheduler)
    assert await lifecycle.retire(selection, "operation-example", scheduler,
                                  cleanup_policy_accepted=True, owned_cleanup_confirmed=True) == "draining"
    assert provider.deleted == []
    provider.executions = []
    assert await lifecycle.retire(selection, "operation-example", scheduler,
                                  cleanup_policy_accepted=False, owned_cleanup_confirmed=True) == "retained"
    assert provider.deleted == []
    foreign = SpaceSelection(selection.tenant_id, "different-product", selection.project_id,
                             selection.binding_id, selection.account_id, selection.region)
    with pytest.raises(ValueError, match="scope does not match"):
        await lifecycle.retire(foreign, "operation-example", scheduler,
                               cleanup_policy_accepted=True, owned_cleanup_confirmed=True)
    assert await lifecycle.retire(selection, "operation-example", scheduler,
                                  cleanup_policy_accepted=True, owned_cleanup_confirmed=True) == "cleanup_pending"
    assert provider.deleted == ["space-example"]
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
        assert receipt.state == "cleanup_pending" and receipt.active_executions == []


@pytest.mark.asyncio
async def test_adopted_and_unverified_automation_are_not_deleted(lifecycle_fixture):
    sessions, selection, provider, lifecycle, scheduler = lifecycle_fixture
    provider.triggers["trigger-shared"] = "ENABLED"
    with pytest.raises(ValueError, match="unowned native automation"):
        await lifecycle.suspend(selection, "operation-example", scheduler)
    assert provider.triggers["trigger-owned"] == "ENABLED" and not scheduler.paused
    provider.triggers["trigger-shared"] = "DISABLED"
    scheduler.verify = False
    with pytest.raises(ValueError, match="pause is not verified"):
        await lifecycle.suspend(selection, "operation-example", scheduler)
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
        assert receipt.state == "suspending"
        adopted = SpaceSelection(selection.tenant_id, "adopted-product", "adopted-project", "adopted-binding",
                                 selection.account_id, selection.region)
        session.add(DevopsSpaceIntent(tenant_id=adopted.tenant_id, operation_id="adopted-operation",
                                     selection=asdict(adopted), configuration_revision=1,
                                     intent_hash="a" * 64, state="ready", native_space_id="space-example",
                                     ownership="customer_linked", owned_trigger_ids=[], resume_trigger_ids=[],
                                     active_executions=[]))
        await session.commit()
    assert await lifecycle.retire(adopted, "adopted-operation", scheduler,
                                  cleanup_policy_accepted=True, owned_cleanup_confirmed=True) == "retained"
    assert provider.deleted == []


@pytest.mark.asyncio
async def test_shared_space_and_assets_block_owned_cleanup(lifecycle_fixture):
    sessions, selection, provider, lifecycle, scheduler = lifecycle_fixture
    await lifecycle.suspend(selection, "operation-example", scheduler)
    provider.executions = []
    adopted = SpaceSelection(selection.tenant_id, "adopted-product", "adopted-project", "adopted-binding",
                             selection.account_id, selection.region)
    async with sessions() as session:
        session.add(DevopsSpaceIntent(tenant_id=adopted.tenant_id, operation_id="adopted-operation",
                                     selection=asdict(adopted), configuration_revision=1, intent_hash="b" * 64,
                                     state="retained", native_space_id="space-example", ownership="customer_linked",
                                     owned_trigger_ids=[], resume_trigger_ids=[], active_executions=[]))
        await session.commit()
    with pytest.raises(ValueError, match="linked to another binding"):
        await lifecycle.retire(selection, "operation-example", scheduler,
                               cleanup_policy_accepted=True, owned_cleanup_confirmed=True)
    async with sessions() as session:
        adopted_receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "adopted-operation"))
        await session.delete(adopted_receipt)
        await session.commit()
    provider.associations = [{"associationId": "shared-association"}]
    with pytest.raises(ValueError, match="associations must be retained"):
        await lifecycle.retire(selection, "operation-example", scheduler,
                               cleanup_policy_accepted=True, owned_cleanup_confirmed=True)
    assert provider.deleted == []


@pytest.mark.asyncio
async def test_unknown_execution_status_cannot_be_declared_drained(lifecycle_fixture):
    sessions, selection, provider, lifecycle, scheduler = lifecycle_fixture
    await lifecycle.suspend(selection, "operation-example", scheduler)
    provider.executions = [{"executionId": "execution-new", "executionStatus": "UNRECOGNIZED"}]
    with pytest.raises(ValueError, match="unknown native execution status"):
        await lifecycle.retire(selection, "operation-example", scheduler,
                               cleanup_policy_accepted=True, owned_cleanup_confirmed=True)
    assert provider.deleted == []
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
        assert receipt.state == "suspended"
