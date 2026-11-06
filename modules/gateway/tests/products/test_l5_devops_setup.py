"""DevOps setup crosses the mounted Gateway, current broker and installed worker."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Request
from l5_services import runtime_devops, runtime_support
from l5_services.models.binding import Binding
from l5_services.models.configuration import ConfigurationVersion
from l5_services.models.devops_space import DevopsSpaceIntent
from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.aws_connection_authority import connection_binding
from src.internal import assume_role_routes, l5_credential_routes
from src.internal.assume_role_routes import get_secrets_manager
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.sts_assume_service import AssumeRoleResult
from src.shared.models.vault import CredentialServiceDelegation
from tests.products.test_l5_configuration_host import durable_devops_scope as durable_devops_scope
from tests.products.test_l5_configuration_host import l5_owner as l5_owner

__all__ = ["durable_devops_scope", "l5_owner"]


@pytest.fixture
async def installed_setup(test_client, test_engine, db_session, app, l5_owner, durable_devops_scope, monkeypatch):
    fixture = durable_devops_scope
    tenant = l5_owner.org_id
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    fixture.credential.agent_consent = True
    fixture.credential.scopes = {**fixture.credential.scopes, "source": "imported_role"}
    fixture.credential.aws_verified_binding = connection_binding(fixture.credential)
    version = await db_session.get(ConfigurationVersion, (tenant, fixture.config_id, 1))
    version.settings = {**version.settings, "enabled": True, "capabilities": ["investigation"]}
    parent = await db_session.scalar(select(CredentialServiceDelegation).where(CredentialServiceDelegation.credential_id == fixture.credential.id))
    parent.capabilities = ["investigation"]
    parent.operations = ["product:read", "credential:session"]
    await db_session.commit()
    messages = []

    async def publish(envelope, delay):
        messages.append(envelope)

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

    async def verified_worker(request: Request):
        request.state.token_context = worker_actor

    app.dependency_overrides[verify_internal_or_irsa] = verified_worker
    secrets = Mock()
    secrets.current_version_id.return_value = fixture.reference["record_revision"]
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
        fixture.reference["record_revision"],
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
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
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
    state = {"space": {"agentSpaceId": "new-space", "name": "adopted-space"}, "tags": {}, "timeout": False, "goal_state": "DISABLED"}
    native = Mock()

    def create_space(**payload):
        state["space"] = {"agentSpaceId": "new-space", "name": payload["name"]}
        state["tags"] = payload["tags"]
        if state["timeout"]:
            raise TimeoutError("lost creation response")
        return {"agentSpace": state["space"], "tags": state["tags"]}

    native.create_agent_space.side_effect = create_space
    native.get_agent_space.side_effect = lambda **payload: {"agentSpace": state["space"], "tags": state["tags"]}
    native.list_agent_spaces.side_effect = lambda **payload: {"agentSpaces": [state["space"]]}
    native.list_goals.side_effect = lambda **payload: {
        "goals": [
            {
                "goalId": "goal-one",
                "goalType": "ONCALL_REPORT",
                "evaluationSchedule": {"state": state["goal_state"], "expression": "rate(1 day)"},
            }
        ]
    }
    native.update_goal.side_effect = lambda **payload: state.update(goal_state=payload["evaluationSchedule"]["state"])
    provider = SimpleNamespace(client=lambda *args, **kwargs: native)
    monkeypatch.setenv("L5_SERVICE_ID", "devops")
    monkeypatch.setattr("src.l5_integration.service_publisher", lambda service: publish if service == "devops" else None)
    monkeypatch.setattr(runtime_support, "sessions", lambda: sessions)
    monkeypatch.setattr(runtime_support, "gateway_transport", lambda: test_client)
    monkeypatch.setattr(runtime_support, "archive_store", lambda service: Mock())
    monkeypatch.setattr(runtime_support, "queue_publisher", lambda service: publish)
    monkeypatch.setattr(runtime_support, "provider_session", lambda credentials, region: provider)
    return SimpleNamespace(
        fixture=fixture,
        tenant=tenant,
        sessions=sessions,
        parent=parent,
        version=version,
        messages=messages,
        worker=runtime_devops.factory(),
        native=native,
        state=state,
        assumed=assumed,
        path=f"/v1/products/{fixture.product_id}/projects/{fixture.project_id}/services/devops/binding-operations",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["investigation", "release_readiness", "prevention"])
@pytest.mark.parametrize(
    "boundary",
    [
        "create",
        "link",
        "uncertain",
        "enabled_goals",
        "revoked",
        "consent_removed",
        "stale_configuration",
        "browser_only",
        "empty_capabilities",
        "configuration_capability_changed",
    ],
)
async def test_installed_setup_preserves_authority_and_creation_identity(test_client, db_session, installed_setup, boundary, capability):
    setup = installed_setup
    setup.parent.capabilities = [capability]
    setup.version.settings = {**setup.version.settings, "capabilities": [capability]}
    await db_session.commit()
    request = {"action": "link" if boundary == "link" else "create", "configuration_revision": 1}
    if boundary == "link":
        request["native_resource_ref"] = {**setup.fixture.reference, "record_type": "agent_space", "record_id": "new-space"}
    headers = {"Idempotency-Key": "devops_setup_operation_00001"}
    accepted = await test_client.post(setup.path, json=request, headers=headers)
    assert accepted.status_code == 202, accepted.text
    replay = await test_client.post(setup.path, json=request, headers=headers)
    assert replay.status_code == 202 and replay.json() == accepted.json()
    operation_id = accepted.json()["operation_id"]
    assert len(setup.messages) == 1
    queued = await test_client.get(setup.path + "/" + operation_id)
    assert queued.status_code == 200 and queued.json()["state"] == "accepted"
    setup.assumed.assert_not_called()
    assert setup.native.mock_calls == []
    if boundary == "revoked":
        setup.parent.revoked_at = datetime.now(UTC)
    elif boundary == "consent_removed":
        setup.fixture.credential.agent_consent = False
    elif boundary == "stale_configuration":
        setup.version.settings = {**setup.version.settings, "enabled": False}
    elif boundary == "browser_only":
        setup.parent.capabilities = ["native_ui"]
    elif boundary == "empty_capabilities":
        setup.parent.capabilities = []
    elif boundary == "configuration_capability_changed":
        other_capability = "prevention" if capability == "investigation" else "investigation"
        setup.version.settings = {**setup.version.settings, "capabilities": [other_capability]}
    elif boundary == "uncertain":
        setup.state["timeout"] = True
    elif boundary == "enabled_goals":
        setup.state["goal_state"] = "ENABLED"
    await db_session.commit()
    envelope = setup.messages.pop()
    result = await setup.worker.execute(envelope)
    assert not await setup.worker.execute(envelope)
    denied = boundary in {
        "revoked",
        "consent_removed",
        "stale_configuration",
        "browser_only",
        "empty_capabilities",
        "configuration_capability_changed",
    }
    if denied:
        assert not result
        setup.assumed.assert_not_called()
        assert setup.native.mock_calls == []
        async with setup.sessions() as reader:
            work = await reader.get(SetupWork, (setup.tenant, envelope["work_id"]))
            assert work.blocked_at is not None and work.completed_at is None
        return
    if boundary == "uncertain":
        async with setup.sessions() as reader:
            operation = await reader.get(BindingOperation, (setup.tenant, operation_id))
            assert operation.state == "outcome_unknown"
            work = await reader.get(SetupWork, (setup.tenant, envelope["work_id"]))
            work.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            await reader.commit()
        assert await setup.worker.execute(envelope)
    async with setup.sessions() as reader:
        operation = await reader.get(BindingOperation, (setup.tenant, operation_id))
        assert operation.state == "succeeded", operation.gaps
        binding = await reader.get(Binding, (setup.tenant, operation.binding_id))
        intent = await reader.get(SetupIntent, (setup.tenant, operation_id))
        receipt = await reader.get(DevopsSpaceIntent, (setup.tenant, operation_id))
        work = await reader.get(SetupWork, (setup.tenant, envelope["work_id"]))
        assert work.completed_at is not None and binding.state == "ready"
        assert receipt.selection["binding_id"] == binding.binding_id
        assert receipt.native_space_id == binding.native_resource_ref["record_id"] == "new-space"
        assert receipt.intent_hash == intent.intent_hash
        assert receipt.prevention_observation["native_schedule"]["enabled"] is False
    assert setup.native.create_agent_space.call_count == (0 if boundary == "link" else 1)
    assert setup.native.update_goal.call_count == (1 if boundary == "enabled_goals" else 0)
    assert not await setup.worker.execute(envelope)
    if boundary in {"create", "link"}:
        followup = {"action": "check", "configuration_revision": 1, "binding_id": binding.binding_id} if boundary == "create" else request
        accepted = await test_client.post(
            setup.path,
            json=followup,
            headers={
                "Idempotency-Key": "devops_setup_followup_00001",
                "If-Match": '"1"',
            },
        )
        assert accepted.status_code == 202, accepted.text
        assert await setup.worker.execute(setup.messages.pop())
        async with setup.sessions() as reader:
            operation = await reader.get(BindingOperation, (setup.tenant, accepted.json()["operation_id"]))
            assert operation.state == "succeeded", operation.gaps
            assert operation.binding_id == binding.binding_id
            receipts = (
                await reader.scalars(
                    select(DevopsSpaceIntent).where(
                        DevopsSpaceIntent.tenant_id == setup.tenant,
                        DevopsSpaceIntent.selection["binding_id"].as_string() == binding.binding_id,
                    )
                )
            ).all()
            assert len(receipts) == 1
        assert setup.native.create_agent_space.call_count == (1 if boundary == "create" else 0)
