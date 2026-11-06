"""FinOps result reads recheck the current Product connection and binding."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from l5_services.models.base import Base
from l5_services.models.binding import Binding
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src import l5_integration
from src.l5_service_identity import worker_id
from src.shared.models.base import Base as GatewayBase
from src.shared.models.vault import CredentialServiceDelegation


@pytest.mark.asyncio
async def test_finops_result_source_rights_fail_closed_after_revocation(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(GatewayBase.metadata.create_all)
    connection_ref = {
        "tenant_id": "tenant-example",
        "project_id": "project-example",
        "source_connection_id": "connection-example",
        "record_type": "connection",
        "record_id": "connection-example",
        "record_revision": "version-one",
    }
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example", "owner_id": "owner-example"}
    run = SimpleNamespace(
        service_id="finops",
        tenant_id="tenant-example",
        product_id="product-example",
        project_id="project-example",
        binding_id="binding-example",
        binding_generation=1,
        input={"sources": []},
        admission={"scope": scope, "connection_ref": connection_ref, "delegation_id": "delegation-example"},
    )
    visible_connections = {"connection-example"}

    async def permitted_sources(db, actor, project_id):
        assert project_id == "project-example"
        return set()

    async def connections(db, actor):
        return [({"provider": "aws", "connection_ref": {"id": connection_id}}, object()) for connection_id in visible_connections]

    monkeypatch.setattr(l5_integration, "permitted_source_refs", permitted_sources)
    monkeypatch.setattr(l5_integration, "authorized_connections", connections)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            binding = Binding(
                tenant_id="tenant-example",
                binding_id="binding-example",
                configuration_id="config-example",
                product_id="product-example",
                project_id="project-example",
                service_id="finops",
                scope=scope,
                revision=1,
                generation=1,
                state="ready",
                ownership="customer_linked",
                connection_ref=connection_ref,
                connection_key="a" * 64,
                readiness=[],
                gaps=[],
            )
            session.add(binding)
            grant = CredentialServiceDelegation(
                id="delegation-example",
                tenant_id="tenant-example",
                credential_id="connection-example",
                service_id=worker_id("finops"),
                product_id="product-example",
                project_id="project-example",
                created_by="owner-example",
                operations=["credential:session"],
                sources=[{**connection_ref, "record_type": "credential"}],
                capabilities=["cost_inquiry"],
                visibility="restricted",
                binding_generation=1,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
            session.add(grant)
            await session.commit()
            actor = SimpleNamespace(org_id="tenant-example", user_id="owner-example", account_type="human")
            backend = l5_integration.GatewayConfigurationBackend(session, actor)
            assert await backend.run_source_rights("project-example", [run]) == [run]
            for service_id in ("l5-worker", "l5-devops-worker", "l5-transform-worker"):
                grant.service_id = service_id
                await session.flush()
                assert await backend.run_source_rights("project-example", [run]) == []
            grant.service_id = "l5-finops-worker"
            await session.flush()
            assert await backend.run_source_rights("project-example", [run]) == [run]
            visible_connections.clear()
            assert await backend.run_source_rights("project-example", [run]) == []
            visible_connections.add("connection-example")
            grant.revoked_at = datetime.now(UTC)
            await session.flush()
            assert await backend.run_source_rights("project-example", [run]) == []
            grant.revoked_at = None
            await session.flush()
            binding.state = "unlinked"
            await session.flush()
            assert await backend.run_source_rights("project-example", [run]) == []
            binding.state = "ready"
            binding.generation = 2
            await session.flush()
            assert await backend.run_source_rights("project-example", [run]) == []
            binding.generation = 1
            await session.flush()
            actor.account_type = "service"
            assert await backend.run_source_rights("project-example", [run]) == []
    finally:
        await engine.dispose()
