"""WA admission retains the pinned Profile identity before any native work is available."""

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from l5_services.adapters.well_architected.binding import ProfileAdapter, ProfileSelection
from l5_services.api.bindings import BindingStore
from l5_services.api.router import create_router
from l5_services.models.base import Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration
from l5_services.models.run import Run
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.l5_integration import GatewayConfigurationBackend


@pytest.mark.asyncio
async def test_wa_admission_fences_drift_and_keeps_pinned_readback():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    tenant, product, project = "tenant-example", "product-example", "project-example"
    account, region = "123456789012", "us-east-1"
    broker_role = f"arn:aws:iam::{account}:role/broker-example"
    execution_role = f"arn:aws:iam::{account}:role/execution-example"
    profile_arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/example"
    source = {
        "tenant_id": tenant,
        "project_id": project,
        "source_connection_id": "connection-example",
        "record_type": "document",
        "record_id": "architecture-example",
        "record_revision": "rev-1",
    }
    connection_ref = {**source, "record_type": "connection", "record_id": "connection-example"}
    scope = {"tenant_id": tenant, "product_id": product, "project_id": project}
    qualification = {
        "account_id": account,
        "region": region,
        "broker_role_arn": broker_role,
        "execution_role_arn": execution_role,
        "trust": "qualified",
        "source_permissions": "qualified",
        "support": "eligible",
    }
    selection = ProfileSelection(tenant, product, project, account, region, broker_role, execution_role, ("SECURITY",), qualification)
    native = {
        "arn": profile_arn,
        "pillars": ["SECURITY"],
        "executionRoleArn": execution_role,
        "aggregationConfiguration": [],
        "eligibleForArchitectureGeneration": True,
        "fieldErrors": [],
    }
    client = Mock()
    client.get_agent_profile.return_value = native
    client.list_agent_contexts.return_value = {"items": []}
    pinned = ProfileAdapter(client, account_id=account, region=region).adopt(selection, profile_arn).snapshot_hash
    client.reset_mock()
    settings = {
        "enabled": True,
        "classification": "internal",
        "limits": {
            "deadline_seconds": 3600,
            "max_provider_attempts": 1,
            "max_polls": 30,
            "concurrency": 1,
            "max_cost": "1.00",
            "currency": "USD",
            "policy_ref": {**source, "record_type": "policy", "record_id": "example-budget"},
        },
        "source_refs": [source],
        "triggers": [{"kind": "manual", "enabled": True, "capability": "architecture_review", "binding_id": "binding-example"}],
        "well_architected": {"pillars": ["SECURITY"], "execution_role_ref": {"record_id": execution_role}},
    }
    version = SimpleNamespace(revision=1, state="ready", gaps=[], settings=settings)
    verified = {
        "configuration_id": "configuration-example",
        "configuration_revision": 1,
        "scope": scope,
        "connection_ref": connection_ref,
        "account_id": account,
        "region": region,
        "role_arn": broker_role,
        "source_role_qualification": qualification,
    }
    configuration = Configuration(
        tenant_id=tenant,
        configuration_id="configuration-example",
        product_id=product,
        project_id=project,
        service_id="well_architected",
        relationship_revision="1",
        owner_id="owner-example",
        current_revision=1,
        handoff_state="linked",
    )
    reference = {**source, "record_type": "agent_profile", "record_id": profile_arn, "record_revision": pinned}
    binding = Binding(
        tenant_id=tenant,
        binding_id="binding-example",
        configuration_id=configuration.configuration_id,
        product_id=product,
        project_id=project,
        service_id="well_architected",
        scope=scope,
        revision=1,
        generation=1,
        state="ready",
        ownership="customer_linked",
        connection_ref=connection_ref,
        connection_key="connection-key",
        native_resource_ref=reference,
        native_key="native-key",
        provider_identity={"account_id": account, "region": region, "role_arn": broker_role},
        readiness=[{"snapshot_hash": pinned}],
        gaps=[],
    )
    request = {
        "service_id": "well_architected",
        "configuration_revision": 1,
        "binding_id": "binding-example",
        "capability": "architecture_review",
        "sources": [source],
    }
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add_all((configuration, binding))
            await session.commit()
            actor = SimpleNamespace(org_id=tenant, user_id="owner-example", account_type="human")
            backend = GatewayConfigurationBackend(session, actor)

            async def current(*args):
                return configuration, version

            async def authority(*args):
                return BindingStore(session, tenant, product, project, "well_architected"), verified

            async def rights(*args):
                return {(source["source_connection_id"], source["record_id"])}

            async def provider(*args):
                return client

            backend.current = current
            backend.binding_authority = authority
            backend.source_rights = rights
            app = FastAPI()
            app.include_router(create_router(lambda: backend))
            url = f"/v1/products/{product}/projects/{project}/services/well_architected/runs"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:

                async def submit(payload=request):
                    return await http.post(url, json=payload, headers={"Idempotency-Key": "admission-example-0001"})

                missing = await submit()
                assert missing.status_code == 503 and missing.json()["detail"]["code"] == "native_readback_unavailable"
                assert client.get_agent_profile.call_count == 0
                backend.wa_profile_client = provider
                stale = await submit({**request, "configuration_revision": 2})
                assert stale.status_code == 409 and stale.json()["detail"]["code"] == "stale_revision"
                foreign = await submit({**request, "sources": [{**source, "record_id": "foreign-example"}]})
                assert foreign.status_code == 404 and client.get_agent_profile.call_count == 0
                intact = await submit()
                assert intact.status_code == 503 and intact.json()["detail"]["code"] == "work_authority_unavailable"
                assert client.get_agent_profile.call_count == 1
                from botocore.exceptions import ClientError

                client.get_agent_profile.side_effect = ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "Unavailable"}}, "GetAgentProfile"
                )
                unavailable = await submit()
                assert unavailable.status_code == 503 and unavailable.json()["detail"]["code"] == "native_readback_unavailable"
                await session.refresh(binding)
                assert binding.state == "ready" and binding.readiness == [{"snapshot_hash": pinned}]
                client.get_agent_profile.side_effect = None
                native["pillars"] = ["RESILIENCE"]
                drifted = await submit()
                assert drifted.status_code == 409 and drifted.json()["detail"]["code"] == "native_profile_drift"
                assert client.get_agent_profile.call_count == 3
                await session.refresh(binding)
                observed = ProfileAdapter(client, account_id=account, region=region).adopt(selection, profile_arn)
                assert binding.state == "blocked" and binding.gaps == ["pillars_mismatch", "native_profile_drift"]
                assert binding.readiness[0]["snapshot_hash"] == pinned
                assert binding.readiness[1]["snapshot_hash"] == observed.snapshot_hash != pinned
                assert binding.native_resource_ref == reference and binding.ownership == "customer_linked"
                native_calls = client.get_agent_profile.call_count
                again = await submit()
                assert again.status_code == 409 and client.get_agent_profile.call_count == native_calls
                assert (await session.scalars(select(Run))).all() == []
                assert client.create_agent_profile.call_count == client.delete_agent_profile.call_count == 0
    finally:
        await engine.dispose()
