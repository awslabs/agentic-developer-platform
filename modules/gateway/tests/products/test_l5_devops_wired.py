"""Admitted DevOps investigations use durable Product authority at the real broker."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Request
from l5_services import runtime_devops, runtime_support
from l5_services.models.binding import Binding
from l5_services.models.configuration import ConfigurationVersion
from l5_services.models.run import Run
from l5_services.models.work import Work
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.aws_connection_authority import connection_binding
from src.internal import assume_role_routes, l5_credential_routes
from src.internal.assume_role_routes import get_secrets_manager
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.sts_assume_service import AssumeRoleResult
from src.l5_integration import GatewayConfigurationBackend
from src.shared.identity.product_scope import service_product_scope
from src.shared.models.budget import BudgetConfig
from src.shared.models.vault import CredentialServiceDelegation
from tests.products.l5_devops_resource_fixtures import seed_verified_lambda
from tests.products.test_l5_configuration_host import durable_devops_scope as durable_devops_scope
from tests.products.test_l5_configuration_host import l5_owner as l5_owner

__all__ = ["durable_devops_scope", "l5_owner"]


@pytest.mark.asyncio
@pytest.mark.parametrize("service_id", ["l5-devops-worker", "l5-worker"])
async def test_prevention_requires_its_own_durable_delegation(db_session, l5_owner, durable_devops_scope, service_id):
    from fastapi import HTTPException

    fixture = durable_devops_scope
    grant = await db_session.scalar(select(CredentialServiceDelegation).where(CredentialServiceDelegation.credential_id == fixture.credential.id))
    grant.service_id = service_id
    grant.capabilities = ["prevention"]
    await db_session.commit()
    backend = GatewayConfigurationBackend(db_session, l5_owner)
    arguments = (fixture.product_id, fixture.project_id, fixture.selection.binding_id)
    if service_id == "l5-worker":
        with pytest.raises(HTTPException) as denied:
            await backend._prevention_target(*arguments)
        assert denied.value.status_code == 403
    else:
        _version, _receipt, selection, credential = await backend._prevention_target(*arguments)
        assert selection == fixture.selection and credential.id == fixture.credential.id


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["current", "revoked", "consent_removed", "stale_binding", "expired_grant"])
async def test_mounted_investigation_uses_child_grant_and_installed_worker(
    test_client, test_engine, db_session, app, l5_owner, durable_devops_scope, monkeypatch, boundary
):
    fixture = durable_devops_scope
    tenant = l5_owner.org_id
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    reference = fixture.reference
    fixture.credential.agent_consent = True
    fixture.credential.scopes = {**fixture.credential.scopes, "source": "imported_role"}
    fixture.credential.aws_verified_binding = connection_binding(fixture.credential)
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    profile = {**reference, "record_type": "product", "record_id": fixture.product_id, "record_revision": "2"}
    policy = {**reference, "record_type": "policy", "record_id": "investigation-budget"}
    binding = await db_session.get(Binding, (tenant, fixture.selection.binding_id))
    binding.native_resource_ref = {**reference, "record_type": "agent_space", "record_id": "native-space"}
    version = await db_session.get(ConfigurationVersion, (tenant, fixture.config_id, 1))
    refs = await seed_verified_lambda(db_session, binding, version)
    resource = refs["resource_refs"][0]
    version.settings = {
        **version.settings,
        "enabled": True,
        "classification": "internal",
        "profile_ref": profile,
        "limits": {
            "deadline_seconds": 600,
            "max_provider_attempts": 1,
            "max_polls": 10,
            "concurrency": 1,
            "max_cost": "1.00",
            "currency": "USD",
            "policy_ref": policy,
        },
        "devops": {
            **version.settings["devops"],
            "trigger_policies": [
                {
                    "trigger_id": "manual-investigation",
                    "kind": "manual",
                    "enabled": True,
                    "capability": "investigation",
                    "binding_id": binding.binding_id,
                    "resource_refs": [resource],
                    "telemetry_refs": [],
                }
            ],
        },
    }
    parent = await db_session.scalar(select(CredentialServiceDelegation).where(CredentialServiceDelegation.credential_id == fixture.credential.id))
    parent.operations = ["product:read", "credential:session"]
    parent.capabilities = ["investigation"]
    db_session.add(
        BudgetConfig(
            id=policy["record_id"],
            org_id=tenant,
            entity_type="org",
            entity_id=tenant,
            budget_amount_usd=2000,
            period_type="monthly",
            enforcement_mode="hard",
        )
    )
    await db_session.commit()
    messages = []

    async def publish(envelope, delay):
        messages.append(envelope)

    monkeypatch.setattr("src.l5_integration.service_publisher", lambda service: publish if service == "devops" else None)
    path = f"/v1/products/{fixture.product_id}/projects/{fixture.project_id}/services/devops/runs"
    request = {
        "service_id": "devops",
        "configuration_revision": 1,
        "binding_id": binding.binding_id,
        "capability": "investigation",
        "devops_input": {"kind": "investigation", "question": "Why did the service fail?", "resource_refs": [resource], "telemetry_refs": []},
    }
    headers = {"Idempotency-Key": "durable-investigation-key-0001"}
    admitted = await test_client.post(path, json=request, headers=headers)
    assert admitted.status_code == 202, admitted.text
    replay = await test_client.post(path, json=request, headers=headers)
    assert replay.status_code == 202 and replay.json() == admitted.json()
    run_id = admitted.json()["run_id"]
    async with sessions() as independent:
        run = await independent.get(Run, (tenant, run_id))
        child = await independent.get(CredentialServiceDelegation, run.admission["delegation_id"])
        assert child.parent_delegation_id == parent.id and child.id != parent.id
        assert child.operations == ["product:read", "credential:session"]
        assert child.sources == [{**reference, "record_type": "credential"}]
        assert child.capabilities == ["investigation"]
        assert run.admission["source_snapshots"] == []

    worker_actor = l5_owner.model_copy(
        update={
            "user_id": "iam-agent:l5-devops-worker",
            "agent_registry_id": "l5-devops-worker",
            "org_id": "platform-service",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "credential_scopes": ["l5:credential-session"],
        }
    )
    assert await service_product_scope(db_session, worker_actor, child.id, fixture.product_id, fixture.project_id, "credential:session") is not None
    refused = []

    async def capture_refusal(response):
        if response.status_code >= 400:
            await response.aread()
            refused.append(response.json())

    test_client.event_hooks["response"].append(capture_refusal)

    async def verified_worker(request: Request):
        request.state.token_context = worker_actor

    app.dependency_overrides[verify_internal_or_irsa] = verified_worker
    secrets = Mock()
    secrets.current_version_id.return_value = reference["record_revision"]
    secrets.get_secret_at_version.return_value = (
        json.dumps(
            {
                "role_arn": fixture.role,
                "external_id": "offline-external-id",
                "account_id": fixture.selection.account_id,
                "session_duration_seconds": 900,
                "default_region": fixture.selection.region,
            }
        ),
        reference["record_revision"],
    )
    app.dependency_overrides[get_secrets_manager] = lambda: secrets
    assumed = Mock(
        return_value=AssumeRoleResult(
            access_key_id="fixture-key",
            secret_access_key="fixture-secret",
            session_token="fixture-token",
            expiration=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            region=fixture.selection.region,
            profile_name="fixture",
        )
    )
    monkeypatch.setattr(assume_role_routes, "assume_role", assumed)
    monkeypatch.setattr(
        l5_credential_routes,
        "verify_assumed_session",
        AsyncMock(
            return_value={
                "account_id": fixture.selection.account_id,
                "role_arn": fixture.role,
                "region": fixture.selection.region,
                "verified_at": datetime.now(UTC).isoformat(),
            }
        ),
    )
    tasks = []
    native = Mock()
    native.get_agent_space.return_value = {"agentSpace": {"agentSpaceId": "native-space"}}

    def create_task(**payload):
        assert payload["agentSpaceId"] == "native-space" and payload["taskType"] == "INVESTIGATION"
        tasks.append({**payload, "taskId": "native-task", "executionId": "native-execution", "status": "COMPLETED"})
        return {"task": {"taskId": "native-task"}}

    native.create_backlog_task.side_effect = create_task
    native.get_backlog_task.side_effect = lambda **payload: {"task": tasks[0]}
    native.list_executions.return_value = {
        "executions": [
            {
                "agentSpaceId": "native-space",
                "taskId": "native-task",
                "executionId": "native-execution",
            }
        ]
    }
    native.list_journal_records.return_value = {"records": []}
    native.list_recommendations.return_value = {"recommendations": []}
    provider = SimpleNamespace(client=lambda *arguments, **options: native)
    monkeypatch.setenv("L5_SERVICE_ID", "devops")
    monkeypatch.setattr(runtime_support, "sessions", lambda: sessions)
    monkeypatch.setattr(runtime_support, "gateway_transport", lambda: test_client)
    monkeypatch.setattr(runtime_support, "archive_store", lambda service: Mock())
    monkeypatch.setattr(runtime_support, "queue_publisher", lambda service: publish)
    monkeypatch.setattr(runtime_support, "provider_session", lambda credentials, region: provider)
    worker = runtime_devops.factory()
    assert len(messages) == 1
    initial = messages.pop(0)
    assert await worker.execute(initial)
    assert not await worker.execute(initial)
    issued_before_submit = assumed.call_count
    if boundary == "revoked":
        parent.revoked_at = datetime.now(UTC)
    elif boundary == "consent_removed":
        fixture.credential.agent_consent = False
    elif boundary == "stale_binding":
        binding.generation += 1
    elif boundary == "expired_grant":
        selected_grant = await db_session.get(CredentialServiceDelegation, child.id)
        selected_grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    for _step in range(6):
        if not messages:
            break
        envelope = messages.pop(0)
        async with sessions() as independent:
            work = await independent.get(Work, (tenant, envelope["work_id"]))
            work.due_at = datetime.now(UTC) - timedelta(seconds=1)
            await independent.commit()
        assert await worker.execute(envelope)
        assert not await worker.execute(envelope)
    async with sessions() as independent:
        persisted = await independent.get(Run, (tenant, run_id))
        if boundary != "current":
            assert persisted.state == "blocked"
            assert tasks == []
            native.get_agent_space.assert_not_called()
            assert assumed.call_count == issued_before_submit
        else:
            assert persisted.state == "succeeded", (persisted.state, refused)
            assert len(tasks) == native.create_backlog_task.call_count == 1
            assert persisted.native_job_ref["record_id"] == "native-execution"
            assert persisted.native_job_ref["record_revision"] == "native-task"
            for call in assumed.call_args_list:
                policy = json.loads(call.kwargs["session_policy"])
                assert policy["Statement"][0]["Resource"] == "arn:aws:aidevops:us-east-1:123456789012:agentspace/native-space"
            result = await test_client.get(path.removesuffix("/runs") + "/results")
            assert result.status_code == 200, result.text
            assert "native-execution" in result.text
