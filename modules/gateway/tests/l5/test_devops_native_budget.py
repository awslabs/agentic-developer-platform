"""Native observations cannot reserve budget or conceal malformed ADP admission."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from l5_services.adapters.devops.observations import import_native_task
from l5_services.models.base import Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.run import Run
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.l5_integration import GatewayConfigurationBackend
from src.shared.models.budget import BudgetConfig, BudgetUsage


@pytest.mark.parametrize("native_project", ["project-one", "other-project"])
async def test_native_import_does_not_reserve_admission_budget(tmp_path, native_project):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'budget.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 26, 9, tzinfo=UTC)
    scope = {
        "tenant_id": "tenant-one",
        "product_id": "product-one",
        "project_id": "project-one",
        "relationship_revision": "1",
        "owner_id": "owner-one",
        "visibility": "product_team",
    }
    native_scope = {**scope, "project_id": native_project}
    reference = {
        "tenant_id": "tenant-one",
        "project_id": native_project,
        "source_connection_id": "connection-one",
        "record_type": "agent_space",
        "record_id": "space-one",
        "record_revision": "1",
    }
    limits = {
        "max_cost": "1",
        "policy_ref": {"tenant_id": "tenant-one", "project_id": "project-one", "record_type": "policy", "record_id": "budget-one"},
    }
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(BudgetConfig.__table__.create)
            await connection.run_sync(BudgetUsage.__table__.create)
        async with sessions() as session:
            session.add(
                BudgetConfig(
                    id="budget-one",
                    org_id="tenant-one",
                    entity_type="org",
                    entity_id="tenant-one",
                    period_type="daily",
                    budget_amount_usd=Decimal("100"),
                    enforcement_mode="hard",
                )
            )
            session.add(
                Configuration(
                    tenant_id="tenant-one",
                    configuration_id="config-one",
                    product_id="product-one",
                    project_id=native_project,
                    service_id="devops",
                    relationship_revision="1",
                    owner_id="owner-one",
                    current_revision=1,
                    handoff_state="linked",
                )
            )
            session.add(
                ConfigurationVersion(
                    tenant_id="tenant-one",
                    configuration_id="config-one",
                    revision=1,
                    settings={},
                    inherited_fields=[],
                    gaps=[],
                    state="ready",
                    hash="a" * 64,
                )
            )
            binding = Binding(
                tenant_id="tenant-one",
                binding_id="binding-one",
                configuration_id="config-one",
                product_id="product-one",
                project_id=native_project,
                service_id="devops",
                scope=native_scope,
                revision=1,
                generation=1,
                state="ready",
                ownership="adopted",
                connection_ref=reference,
                connection_key="a" * 64,
                native_resource_ref=reference,
                native_key="b" * 64,
                readiness=[],
                gaps=[],
            )
            session.add(binding)
            await session.commit()
            backend = GatewayConfigurationBackend(session, SimpleNamespace(org_id="tenant-one", user_id="owner-one"))
            replay = AsyncMock(return_value=None)
            assert await backend._require_devops_budget(session, limits, scope, now, replay) is None
            native = await import_native_task(
                session,
                binding,
                native_scope,
                {"agentSpaceId": "space-one", "taskType": "INVESTIGATION", "executionId": "execution-one", "taskId": "task-one"},
                now=now,
            )
            assert native.origin == "native" and native.admission is None
            assert await backend._require_devops_budget(session, limits, scope, now, replay) is None
            admission = {"scope": native_scope, "admitted_at": "2026-10-26T09:00:00Z", "limits": {**limits, "max_cost": "99"}}
            admitted = Run(
                tenant_id="tenant-one",
                run_id="admitted-run",
                product_id="product-one",
                project_id=native_project,
                service_id="devops",
                configuration_id="config-one",
                configuration_revision=1,
                binding_id="binding-one",
                binding_generation=1,
                trigger_id="manual",
                input={"service_id": "devops"},
                admission=admission,
                state="running",
                provider_state="running",
                deadline_at=now + timedelta(hours=1),
            )
            session.add(admitted)
            await session.commit()
            assert await backend._require_devops_budget(session, limits, scope, now, replay) is None
            with pytest.raises(HTTPException) as exhausted:
                await backend._require_devops_budget(session, {**limits, "max_cost": "2"}, scope, now, replay)
            assert exhausted.value.status_code == 409 and exhausted.value.detail["code"] == "budget_exhausted"
            # A budget-free manual investigation cannot poison reservations for automated work.
            admitted.input = {"service_id": "devops", "capability": "investigation"}
            admitted.admission = {**admission, "causal_origin": "manual", "limits": {"max_cost": "300"}}
            await session.commit()
            assert await backend._require_devops_budget(session, {**limits, "max_cost": "2"}, scope, now, replay) is None
            admitted.input = {"service_id": "devops"}
            for malformed in (None, {}, {**admission, "limits": {}}):
                admitted.admission = malformed
                await session.commit()
                with pytest.raises(HTTPException) as unavailable:
                    await backend._require_devops_budget(session, limits, scope, now, replay)
                assert unavailable.value.status_code == 409
                assert unavailable.value.detail["code"] == "execution_policy_unavailable"
    finally:
        await engine.dispose()
