"""Gateway prevention requests use scoped authority before native effects."""

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator
from l5_services.adapters.devops.spaces import SpaceSelection
from l5_services.api.router import create_router
from l5_services.models.base import Base
from l5_services.models.binding import Binding
from l5_services.models.devops_space import DevopsSpaceIntent
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src import l5_integration


class Goals:
    def __init__(self):
        self.state = "DISABLED"
        self.updates = []
        self.fail_readback = False
        self.fail_after_update = False

    def get_agent_space(self, **kwargs):
        return {"agentSpace": {"agentSpaceId": kwargs["agentSpaceId"]}}

    def list_goals(self, **kwargs):
        if self.fail_readback:
            raise ConnectionError("provider unavailable")
        return {
            "goals": [
                {"goalId": "goal-example", "goalType": "ONCALL_REPORT", "evaluationSchedule": {"state": self.state, "expression": "rate(7 days)"}}
            ]
        }

    def update_goal(self, **kwargs):
        self.updates.append(kwargs)
        self.state = kwargs["evaluationSchedule"]["state"]
        if self.fail_after_update:
            self.fail_readback = True
        return {"goal": {"goalId": kwargs["goalId"]}}


@pytest.fixture
async def prevention_host(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'host-prevention.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    selection = SpaceSelection("tenant-example", "product-example", "project-example", "binding-example", "123456789012", "us-east-1")
    connection_ref = {
        "tenant_id": selection.tenant_id,
        "project_id": selection.project_id,
        "source_connection_id": "connection-example",
        "record_type": "connection",
        "record_id": "connection-example",
        "record_revision": "revision-one",
    }
    policy_ref = {
        "tenant_id": selection.tenant_id,
        "project_id": selection.project_id,
        "source_connection_id": "connection-example",
        "record_revision": "revision-one",
        "record_type": "policy",
        "record_id": "budget-example",
    }
    settings = {
        "visibility": "product_team",
        "devops": {
            "hosting_connection_ref": connection_ref,
            "region": selection.region,
            "prevention_enabled": False,
            "prevention_policy": {"goal_id": "goal-example", "expression": "rate(7 days)"},
        },
        "limits": {
            "policy_ref": policy_ref,
            "max_cost": "5",
            "currency": "USD",
            "deadline_seconds": 3600,
            "max_provider_attempts": 2,
            "max_polls": 12,
            "concurrency": 1,
        },
        "exclusions": [],
    }
    configuration = SimpleNamespace(
        tenant_id=selection.tenant_id,
        product_id=selection.product_id,
        project_id=selection.project_id,
        owner_id="owner-example",
        configuration_id="configuration-example",
        relationship_revision="1",
        handoff_state="linked",
    )
    version = SimpleNamespace(revision=1, settings=settings, hash="f" * 64, state="disabled", inherited_fields=[], gaps=[])
    binding = SimpleNamespace(
        tenant_id=selection.tenant_id,
        product_id=selection.product_id,
        project_id=selection.project_id,
        service_id="devops",
        binding_id=selection.binding_id,
        configuration_id=configuration.configuration_id,
        revision=1,
        generation=1,
        state="ready",
        scope={
            "tenant_id": selection.tenant_id,
            "product_id": selection.product_id,
            "project_id": selection.project_id,
            "relationship_revision": "1",
            "owner_id": "owner-example",
            "visibility": "product_team",
        },
        connection_ref=connection_ref,
        native_resource_ref={"record_id": "space-example", "record_type": "agent_space", "source_connection_id": "connection-example"},
        provider_identity={"account_id": selection.account_id, "region": selection.region, "role_arn": "arn:aws:iam::123456789012:role/example"},
    )
    credential = SimpleNamespace(
        id="connection-example",
        user_id="owner-example",
        org_id=selection.tenant_id,
        service="aws",
        credential_type="aws_role",
        aws_verified_version_id="revision-one",
        aws_external_id="trust-example",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:example",
        label="example",
        scopes={"account_id": selection.account_id, "role_arn": "arn:aws:iam::123456789012:role/example"},
    )
    grant = SimpleNamespace(
        execution_scope=None,
        operations=["credential:session"],
        capabilities=["prevention"],
        binding_generation=1,
        sources=[{**connection_ref, "record_type": "credential"}],
    )
    budget = SimpleNamespace(org_id=selection.tenant_id, entity_type="org", entity_id=selection.tenant_id, enforcement_mode="soft")
    async with sessions() as session:
        session.add(
            DevopsSpaceIntent(
                tenant_id=selection.tenant_id,
                operation_id="operation-example",
                selection=asdict(selection),
                configuration_revision=1,
                intent_hash="a" * 64,
                state="succeeded",
                native_space_id="space-example",
                prevention_policy={"enabled": False},
            )
        )
        await session.commit()

    class Database:
        bind = engine

        async def get(self, model, key):
            if model is Binding:
                return binding if key == (selection.tenant_id, selection.binding_id) else None
            if model is l5_integration.UserCredential:
                return credential if key == credential.id else None
            if model is l5_integration.BudgetConfig:
                return budget if key == "budget-example" else None
            return None

        async def scalar(self, query):
            return grant

        async def scalars(self, query):
            async with sessions() as session:
                return await session.scalars(query)

        async def refresh(self, value):
            pass

    actor = SimpleNamespace(org_id=selection.tenant_id, user_id="owner-example", account_type="human", auth_source="jwt", token_use="access")
    native = Goals()
    backend = l5_integration.GatewayConfigurationBackend(Database(), actor)

    async def current(product_id, project_id, service_id):
        if (product_id, project_id, service_id) != (selection.product_id, selection.project_id, "devops"):
            raise l5_integration.HTTPException(404, detail={"code": "not_found"})
        return configuration, version

    async def provider(selected_credential, selected_space, action_id, *, update=False):
        assert selected_credential is credential and selected_space == selection
        return native

    backend.current = current
    backend.prevention_provider = provider
    monkeypatch.setattr(l5_integration, "verified_connection_evidence", lambda selected: True)
    app = FastAPI()
    app.include_router(create_router(lambda: backend))
    root = f"/v1/products/{selection.product_id}/projects/{selection.project_id}/services/devops/bindings/{selection.binding_id}/prevention"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline.invalid") as client:
        yield client, root, version, grant, budget, native, sessions, binding, credential, selection
    await engine.dispose()


@pytest.mark.asyncio
async def test_scoped_prevention_host_lifecycle(prevention_host):
    client, root, version, grant, budget, native, sessions, binding, credential, selection = prevention_host
    assert (await client.get(root)).json()["status"] == "ready"
    headers = {"Idempotency-Key": "enable_example_0001", "If-Match": f'"{version.hash}"'}
    payload = {"configuration_revision": 1, "action": "enable"}
    assert (await client.post(root, json=payload)).status_code == 428
    assert (await client.post(root, json=payload, headers=headers | {"If-Match": '"stale"'})).status_code == 412
    enabled = await client.post(root, json=payload, headers=headers)
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["desired"]["cost_control"] == "observation_only"
    assert enabled.json()["observed"]["enabled"] is True
    schema = json.loads((Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1/schemas.json").read_text())
    assert not list(Draft202012Validator({**schema, "$ref": "#/$defs/DevopsPreventionStatus"}).iter_errors(enabled.json()))
    assert len(native.updates) == 1
    assert (await client.post(root, json=payload, headers=headers)).status_code == 200
    assert len(native.updates) == 1
    paused = await client.post(root, json={**payload, "action": "pause"}, headers={**headers, "Idempotency-Key": "pause_example_0001"})
    assert paused.status_code == 200 and paused.json()["observed"]["enabled"] is False
    assert len(native.updates) == 2
    budget.enforcement_mode = "hard"
    refused = await client.post(root, json={**payload, "action": "resume"}, headers={**headers, "Idempotency-Key": "resume_example_0001"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "native_budget_unsupported"
    assert len(native.updates) == 2
    grant.capabilities = []
    assert (await client.get(root)).status_code == 403
    assert (await client.get(root.replace("project-example", "foreign-project"))).status_code == 404


@pytest.mark.asyncio
async def test_prevention_host_preserves_pending_readback(prevention_host):
    client, root, version, grant, budget, native, sessions, binding, credential, selection = prevention_host
    native.fail_after_update = True
    headers = {"Idempotency-Key": "enable_fault_0001", "If-Match": f'"{version.hash}"'}
    payload = {"configuration_revision": 1, "action": "enable"}
    response = await client.post(root, json=payload, headers=headers)
    assert response.status_code == 503 and response.json()["detail"]["code"] == "prevention_readback_unavailable"
    blocked = await client.get(root)
    assert blocked.status_code == 200 and blocked.json()["status"] == "blocked"
    assert blocked.json()["reason"] == "native_schedule_readback_unavailable"
    async with sessions() as session:
        receipt = await session.get(DevopsSpaceIntent, ("tenant-example", "operation-example"))
        assert receipt.prevention_policy["state"] == "pending"
        assert receipt.prevention_observation is None
    native.fail_after_update = native.fail_readback = False
    assert (await client.post(root, json=payload, headers=headers)).status_code == 200
    assert len(native.updates) == 1


@pytest.mark.asyncio
async def test_prevention_host_denies_changed_binding_or_grant(prevention_host):
    client, root, version, grant, budget, native, sessions, binding, credential, selection = prevention_host
    binding.provider_identity = {**binding.provider_identity, "account_id": "000000000000"}
    assert (await client.get(root)).status_code == 409
    binding.provider_identity = {**binding.provider_identity, "account_id": selection.account_id}
    grant.sources = []
    assert (await client.get(root)).status_code == 403
    grant.sources = [{**binding.connection_ref, "record_type": "credential"}]
    credential.aws_verified_version_id = "another-revision"
    assert (await client.get(root)).status_code == 404
    assert native.updates == []


@pytest.mark.asyncio
async def test_prevention_refuses_unscoped_fallback(prevention_host):
    client, root, version, grant, budget, native, sessions, binding, credential, selection = prevention_host
    backend = l5_integration.GatewayConfigurationBackend(
        SimpleNamespace(bind=sessions.kw["bind"]), SimpleNamespace(org_id=selection.tenant_id, user_id="owner-example")
    )
    backend.prevention_provider = None
    with pytest.raises(l5_integration.HTTPException) as denied:
        await backend._prevention_client(credential, selection, "action-example")
    assert denied.value.status_code == 503
    assert denied.value.detail["code"] == "prevention_provider_unavailable"
    assert native.updates == []
