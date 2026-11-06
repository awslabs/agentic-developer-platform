"""L5 selected-role sessions require independently current work and a verified role."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from fastapi import HTTPException, Request
from l5_services.adapters.transform.authority import TransformWorkAuthorizer
from l5_services.models.base import Base as L5Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.run import Run
from l5_services.models.work import Work
from sqlalchemy import update

from src.auth.aws_connection_authority import connection_binding
from src.auth.l5_delegation import issue_l5_work_delegation
from src.internal import assume_role_routes, l5_credential_routes
from src.internal.assume_role_routes import get_secrets_manager
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.l5_credential_routes import (
    L5WorkPermit,
    _security_session_policy,
    _security_setup_policy,
    _threat_model_session_policy,
    active_grant,
    get_l5_work_authorizer,
    verify_assumed_session,
)
from src.internal.sts_assume_service import AssumeRoleResult
from src.l5_service_identity import WORKER_IDS, worker_id
from src.shared.identity.product_scope import service_product_scope
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from tests.products.test_i01 import HEADERS, REQUEST


@pytest.mark.asyncio
async def test_work_delegation_ids_and_product_rights_are_service_owned(test_client, db_session, app, source_member, monkeypatch):
    grant, credential, body, _, _, _, _ = await setup_broker(test_client, db_session, app, source_member, monkeypatch)
    grant.operations = ["product:read", "credential:session"]
    grant.capabilities = ["cost_inquiry"]
    grant.operation_id = grant.attempt_id = grant.lease_fence = None
    grant.expires_at = datetime.now(UTC) + timedelta(hours=1)
    connection = {**body["connection_ref"], "record_type": "connection"}
    binding = SimpleNamespace(service_id="finops", project_id=grant.project_id, generation=grant.binding_generation)
    grant.service_id = worker_id("finops")
    await db_session.commit()

    finops = await issue_l5_work_delegation(
        db_session,
        source_member,
        grant,
        credential,
        binding,
        connection,
        [],
        "cost_inquiry",
        60,
        "submit",
        "shared-idempotency-key",
    )
    await db_session.commit()
    assert finops.service_id == worker_id("finops")
    assert finops.parent_delegation_id == grant.id
    assert finops.operations == ["product:read", "credential:session"]
    sibling_actor = source_member.model_copy(
        update={
            "user_id": "iam-agent:l5-finops-worker",
            "agent_registry_id": worker_id("finops"),
            "org_id": "platform-service",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "credential_scopes": ["l5:credential-session"],
        }
    )
    assert await service_product_scope(db_session, sibling_actor, finops.id, grant.product_id, grant.project_id, "product:read") is not None
    security_actor = sibling_actor.model_copy(update={"agent_registry_id": worker_id("security")})
    assert await service_product_scope(db_session, security_actor, finops.id, grant.product_id, grant.project_id, "product:read") is None

    security_template = CredentialServiceDelegation(
        tenant_id=grant.tenant_id,
        credential_id=credential.id,
        service_id=worker_id("security"),
        product_id=grant.product_id,
        project_id=grant.project_id,
        created_by=source_member.user_id,
        operations=["product:read", "source:pin", "source:read", "credential:session"],
        sources=grant.sources,
        capabilities=["code_review"],
        visibility="restricted",
        revision=3,
        binding_generation=grant.binding_generation,
        expires_at=grant.expires_at,
    )
    db_session.add(security_template)
    await db_session.commit()
    security = await issue_l5_work_delegation(
        db_session,
        source_member,
        security_template,
        credential,
        SimpleNamespace(service_id="security", project_id=grant.project_id, generation=grant.binding_generation),
        connection,
        [],
        "code_review",
        60,
        "submit",
        "shared-idempotency-key",
    )
    assert security.id != finops.id
    assert security.service_id == worker_id("security")


@pytest.mark.asyncio
async def test_sibling_run_work_permit_requires_its_registered_grant_and_owned_sql_rows(
    test_client, db_session, test_engine, app, source_member, monkeypatch
):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    grant, credential, body, _, _, assumed, identity_assertion = await setup_broker(test_client, db_session, app, source_member, monkeypatch)
    assert WORKER_IDS == {
        "security": "l5-worker",
        "devops": "l5-devops-worker",
        "finops": "l5-finops-worker",
        "transform": "l5-transform-worker",
        "well_architected": "l5-well-architected-worker",
    }
    grant.service_id = worker_id("finops")
    grant.capabilities = ["cost_inquiry"]
    tenant_id, product_id, project_id = grant.tenant_id, grant.product_id, grant.project_id
    connection_ref = {**body["connection_ref"], "record_type": "connection"}
    configuration = Configuration(
        tenant_id=tenant_id,
        configuration_id="finops-configuration",
        product_id=product_id,
        project_id=project_id,
        service_id="finops",
        owner_id=source_member.user_id,
        relationship_revision="1",
        current_revision=1,
        handoff_state="linked",
    )
    version = ConfigurationVersion(
        tenant_id=tenant_id,
        configuration_id=configuration.configuration_id,
        revision=1,
        settings={"enabled": True, "connection_ref": connection_ref},
        inherited_fields=[],
        gaps=[],
        state="ready",
        hash="a" * 64,
    )
    binding = Binding(
        tenant_id=tenant_id,
        binding_id="finops-binding",
        configuration_id=configuration.configuration_id,
        product_id=product_id,
        project_id=project_id,
        service_id="finops",
        scope={"tenant_id": tenant_id, "product_id": product_id, "project_id": project_id},
        revision=1,
        generation=grant.binding_generation,
        state="ready",
        ownership="adopted",
        connection_ref=connection_ref,
        connection_key="a" * 64,
        readiness=[],
        gaps=[],
        provider_identity={"account_id": "123456789012", "role_arn": credential.scopes["role_arn"], "region": "us-east-1"},
    )
    run = Run(
        tenant_id=tenant_id,
        run_id=body["operation_id"],
        product_id=product_id,
        project_id=project_id,
        service_id="finops",
        configuration_id=configuration.configuration_id,
        configuration_revision=1,
        binding_id=binding.binding_id,
        trigger_id="cost-trigger",
        binding_generation=binding.generation,
        input={"capability": "cost_inquiry"},
        admission={
            "delegation_id": grant.id,
            "delegation_version": grant.revision,
            "binding_generation": binding.generation,
            "actor_id": source_member.user_id,
            "connection_ref": connection_ref,
            "scope": binding.scope,
            "source_snapshots": [],
            "provider_identity": binding.provider_identity,
        },
        deadline_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    work = Work(
        tenant_id=tenant_id,
        work_id=body["attempt_id"],
        run_id=run.run_id,
        kind="submit",
        generation=1,
        step=1,
        due_at=datetime.now(UTC),
        lease_owner=body["lease_owner"],
        lease_until=datetime.now(UTC) + timedelta(minutes=10),
        fence=body["lease_fence"],
    )
    db_session.add_all([configuration, version, binding, run, work])
    await db_session.commit()
    service = source_member.model_copy(
        update={
            "user_id": "iam-agent:l5-finops-worker",
            "agent_registry_id": worker_id("finops"),
            "org_id": "platform-service",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "credential_scopes": ["l5:credential-session"],
        }
    )
    assert await service_product_scope(db_session, service, grant.id, product_id, project_id, "credential:session") is not None
    with pytest.raises(HTTPException) as unqualified:
        await get_l5_work_authorizer(db_session)(grant, l5_credential_routes.CredentialSessionRequest.model_validate(body))
    assert unqualified.value.status_code == 409
    assumed.assert_not_called()

    async def verified(request: Request):
        request.state.token_context = service

    app.dependency_overrides[verify_internal_or_irsa] = verified
    profile = await test_client.get(
        f"/internal/v1/products/{product_id}/projects/{project_id}",
        headers={"X-L5-Delegation-ID": grant.id},
    )
    assert profile.status_code == 200, profile.text
    denied = await test_client.post("/internal/v1/l5/credential-session", json=body)
    assert denied.status_code == 409
    assumed.assert_not_called()
    native_space = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "source_connection_id": credential.id,
        "record_type": "agent_space",
        "record_id": "space-one",
    }
    grant.service_id = worker_id("devops")
    grant.capabilities = ["investigation"]
    configuration.service_id = binding.service_id = run.service_id = "devops"
    version.settings = {"enabled": True, "devops": {"hosting_connection_ref": connection_ref, "region": "us-east-1"}}
    binding.native_resource_ref = native_space
    run.input = {"capability": "investigation", "sources": []}
    run.admission = {**run.admission, "native_space_ref": native_space}
    await db_session.commit()
    devops_permit = await get_l5_work_authorizer(db_session)(grant, l5_credential_routes.CredentialSessionRequest.model_validate(body))
    assert (devops_permit.operation_id, devops_permit.attempt_id, devops_permit.lease_fence) == (run.run_id, work.work_id, work.fence)
    policy = json.loads(devops_permit.session_policy)
    assert policy["Statement"][0]["Resource"] == "arn:aws:aidevops:us-east-1:123456789012:agentspace/space-one"
    assert "aidevops:CreateBacklogTask" in policy["Statement"][0]["Action"]
    assert "aidevops:DeleteAgentSpace" not in policy["Statement"][0]["Action"]
    assumed.assert_not_called()
    service = service.model_copy(update={"user_id": "iam-agent:l5-devops-worker", "agent_registry_id": worker_id("devops")})
    assumed.return_value = replace(assumed.return_value, region="us-east-1")
    identity_assertion.return_value = {**identity_assertion.return_value, "region": "us-east-1"}
    issued = await test_client.post("/internal/v1/l5/credential-session", json=body)
    assert issued.status_code == 200, issued.text
    assert issued.json()["provider_identity"]["region"] == "us-east-1"
    assert json.loads(assumed.call_args.kwargs["session_policy"]) == policy
    assert assumed.call_count == 1
    run.admission = {**run.admission, "native_space_ref": {**native_space, "record_id": "foreign-space"}}
    await db_session.commit()
    with pytest.raises(HTTPException) as changed_space:
        await get_l5_work_authorizer(db_session)(grant, l5_credential_routes.CredentialSessionRequest.model_validate(body))
    assert changed_space.value.status_code == 409
    denied_space = await test_client.post("/internal/v1/l5/credential-session", json=body)
    assert denied_space.status_code == 409
    assert assumed.call_count == 1
    run.admission = {**run.admission, "native_space_ref": native_space, "source_snapshots": [{"snapshot_id": "foreign"}]}
    await db_session.commit()
    with pytest.raises(HTTPException) as forged_sources:
        await get_l5_work_authorizer(db_session)(grant, l5_credential_routes.CredentialSessionRequest.model_validate(body))
    assert forged_sources.value.status_code == 409
    run.admission = {**run.admission, "native_space_ref": native_space, "source_snapshots": []}
    grant.service_id = worker_id("finops")
    grant.capabilities = ["cost_inquiry"]
    configuration.service_id = binding.service_id = run.service_id = "finops"
    version.settings = {"enabled": True, "connection_ref": connection_ref}
    run.input = {"capability": "cost_inquiry"}
    await db_session.commit()
    run.service_id = "security"
    await db_session.commit()
    with pytest.raises(HTTPException) as foreign:
        await get_l5_work_authorizer(db_session)(grant, l5_credential_routes.CredentialSessionRequest.model_validate(body))
    assert foreign.value.status_code == 409
    run.service_id = "finops"
    binding.provider_identity = {**binding.provider_identity, "role_arn": "arn:aws:iam::123456789012:role/Other"}
    run.admission = {**run.admission, "provider_identity": binding.provider_identity}
    await db_session.commit()
    with pytest.raises(HTTPException) as wrong_role:
        await get_l5_work_authorizer(db_session)(grant, l5_credential_routes.CredentialSessionRequest.model_validate(body))
    assert wrong_role.value.status_code == 409
    assert assumed.call_count == 1
    assumed.reset_mock()

    profile_arn = "arn:aws:wellarchitected:us-east-1:123456789012:agent-profile/OwnedProfile"
    profile_ref = {
        **connection_ref,
        "record_type": "agent_profile",
        "record_id": profile_arn,
        "record_revision": "f" * 64,
    }
    identity = {"account_id": "123456789012", "role_arn": credential.scopes["role_arn"], "region": "us-east-1"}
    grant.service_id = worker_id("well_architected")
    grant.capabilities = ["architecture_review"]
    configuration.service_id = binding.service_id = run.service_id = "well_architected"
    binding.provider_identity = identity
    binding.native_resource_ref = profile_ref
    binding.readiness = [{"snapshot_hash": profile_ref["record_revision"]}]
    assumed.return_value = replace(assumed.return_value, region="us-east-1")
    identity_assertion.return_value = {**identity_assertion.return_value, "region": "us-east-1"}
    run.input = {"capability": "architecture_review"}
    run.admission = {
        **run.admission,
        "provider_identity": identity,
        "native_profile_ref": profile_ref,
        "profile_hash": profile_ref["record_revision"],
    }
    await db_session.commit()

    wa_actor = service.model_copy(
        update={
            "user_id": "iam-agent:l5-well-architected-worker",
            "agent_registry_id": worker_id("well_architected"),
        }
    )

    async def verified_wa(request: Request):
        request.state.token_context = wa_actor

    app.dependency_overrides[verify_internal_or_irsa] = verified_wa
    selected = l5_credential_routes.CredentialSessionRequest.model_validate(body)
    permit = await get_l5_work_authorizer(db_session)(grant, selected)
    policy = json.loads(permit.session_policy)
    assert policy["Statement"][0]["Resource"] == [profile_arn, "arn:aws:wellarchitected:us-east-1:123456789012:agent-recommendation/*"]
    assert all(action.startswith("wellarchitected:") for action in policy["Statement"][0]["Action"])
    delivered = await test_client.post("/internal/v1/l5/credential-session", json=body)
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["provider_identity"]["region"] == "us-east-1"
    assert json.loads(assumed.call_args.kwargs["session_policy"]) == policy
    assert assumed.call_count == 1

    async def verified_security(request: Request):
        request.state.token_context = wa_actor.model_copy(
            update={
                "user_id": "iam-agent:l5-worker",
                "agent_registry_id": worker_id("security"),
            }
        )

    app.dependency_overrides[verify_internal_or_irsa] = verified_security
    foreign_actor = await test_client.post("/internal/v1/l5/credential-session", json=body)
    assert foreign_actor.status_code == 403
    assert assumed.call_count == 1
    app.dependency_overrides[verify_internal_or_irsa] = verified_wa

    identity_assertion.return_value = {**identity_assertion.return_value, "region": "us-west-2"}
    wrong_region = await test_client.post("/internal/v1/l5/credential-session", json=body)
    assert wrong_region.status_code == 409
    assert assumed.call_count == 2
    identity_assertion.return_value = {**identity_assertion.return_value, "region": "us-east-1"}

    binding.native_resource_ref = {**profile_ref, "record_revision": "0" * 64}
    await db_session.commit()
    stale_profile = await test_client.post("/internal/v1/l5/credential-session", json=body)
    assert stale_profile.status_code == 409
    assert assumed.call_count == 2

    binding.native_resource_ref = profile_ref
    await db_session.commit()
    wrong_project = await test_client.post(
        "/internal/v1/l5/credential-session",
        json={
            **body,
            "connection_ref": {**body["connection_ref"], "project_id": "other-project"},
        },
    )
    assert wrong_project.status_code == 403
    stale_revision = await test_client.post(
        "/internal/v1/l5/credential-session",
        json={
            **body,
            "delegation_revision": body["delegation_revision"] + 1,
        },
    )
    assert stale_revision.status_code == 409

    work.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    expired_lease = await test_client.post("/internal/v1/l5/credential-session", json=body)
    assert expired_lease.status_code == 409
    work.lease_until = datetime.now(UTC) + timedelta(minutes=10)
    grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    expired_grant = await test_client.post("/internal/v1/l5/credential-session", json=body)
    assert expired_grant.status_code == 403
    grant.expires_at = datetime.now(UTC) + timedelta(hours=1)
    grant.revoked_at = datetime.now(UTC)
    await db_session.commit()
    revoked_grant = await test_client.post("/internal/v1/l5/credential-session", json=body)
    assert revoked_grant.status_code == 403
    assert assumed.call_count == 2


@pytest.mark.parametrize("remaining", [-1, 0, 45, 901])
@pytest.mark.parametrize("short_lease", [False, True])
def test_grant_expiry_preserves_bounded_short_leases_and_default_session_minimum(monkeypatch, remaining, short_lease):
    now = datetime(2026, 10, 28, tzinfo=UTC)
    monkeypatch.setattr(l5_credential_routes, "utcnow", lambda: now)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    deadline = now + timedelta(seconds=remaining)
    grant = SimpleNamespace(
        **scope,
        credential_id="connection-example",
        installation_id=None,
        operation_id="operation-example",
        attempt_id="attempt-example",
        lease_fence=2,
        binding_generation=1,
        revision=1,
        revoked_at=None,
        expires_at=deadline,
    )
    body = SimpleNamespace(
        delegation_revision=1,
        operation_id=grant.operation_id,
        attempt_id=grant.attempt_id,
        lease_owner="worker-example",
        lease_fence=2,
    )
    permit = L5WorkPermit(
        **scope,
        source_connection_id=grant.credential_id,
        operation_id=grant.operation_id,
        attempt_id=grant.attempt_id,
        lease_fence=2,
        binding_generation=1,
        expires_at=now + timedelta(hours=1),
        lease_owner=body.lease_owner,
    )
    options = {"minimum_remaining": timedelta(0)} if short_lease else {}
    if remaining <= (0 if short_lease else 900):
        with pytest.raises(HTTPException) as failure:
            active_grant(grant, body, permit, **options)
        assert failure.value.status_code == 403
        assert failure.value.detail == {"code": "delegation_expiring"}
    else:
        assert active_grant(grant, body, permit, **options) == deadline


async def setup_broker(test_client, db_session, app, source_member, monkeypatch):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    credential = UserCredential(
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        agent_consent=True,
        service="aws",
        credential_type="aws_role",
        label="synthetic-selected-role",
        secret_arn="test-secret",
        aws_external_id="test-external-id",
        aws_verified_at=datetime.now(UTC),
        aws_verification_attempt="verification-a",
        aws_verified_version_id="version-1",
        scopes={
            "account_id": "123456789012",
            "role_arn": "arn:aws:iam::123456789012:role/MyTeamAgentRole",
            "source": "imported_role",
            "status": "verified",
        },
    )
    db_session.add(credential)
    await db_session.flush()
    credential.aws_verified_binding = connection_binding(credential)
    grant = CredentialServiceDelegation(
        tenant_id=source_member.org_id,
        credential_id=credential.id,
        service_id="l5-worker",
        product_id=product_id,
        project_id=project_id,
        created_by=source_member.user_id,
        operations=["product:read", "credential:session"],
        sources=[
            {
                "tenant_id": source_member.org_id,
                "project_id": project_id,
                "source_connection_id": credential.id,
                "record_type": "credential",
                "record_id": credential.id,
                "record_revision": "version-1",
            }
        ],
        capabilities=[],
        visibility="restricted",
        revision=3,
        operation_id="operation-a",
        attempt_id="attempt-a",
        lease_fence=2,
        binding_generation=4,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(grant)
    await db_session.commit()
    service = source_member.model_copy(
        update={
            "user_id": "iam-agent:l5-worker",
            "agent_registry_id": "l5-worker",
            "org_id": "platform-service",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "credential_scopes": ["l5:credential-session"],
        }
    )

    async def verified(request: Request):
        request.state.token_context = service

    app.dependency_overrides[verify_internal_or_irsa] = verified
    request_body = {
        "delegation_id": grant.id,
        "delegation_revision": 3,
        "operation_id": "operation-a",
        "attempt_id": "attempt-a",
        "lease_owner": "offline-worker",
        "lease_fence": 2,
        "connection_ref": {
            "tenant_id": source_member.org_id,
            "project_id": project_id,
            "source_connection_id": credential.id,
            "record_type": "credential",
            "record_id": credential.id,
            "record_revision": "version-1",
        },
    }
    permit = L5WorkPermit(
        tenant_id=source_member.org_id,
        product_id=product_id,
        project_id=project_id,
        source_connection_id=credential.id,
        operation_id="operation-a",
        attempt_id="attempt-a",
        lease_fence=2,
        binding_generation=4,
        expires_at=datetime.now(UTC) + timedelta(seconds=45),
        session_policy=_security_session_policy(
            SimpleNamespace(
                provider_identity={"account_id": "123456789012", "region": "us-east-1"},
                native_resource_ref={"record_type": "security_space", "record_id": "space-one"},
            ),
            {"bucket": "example-source-staging", "service_role": "arn:aws:iam::123456789012:role/example-security-execution"},
            datetime.now(UTC) + timedelta(seconds=45),
        ),
        lease_owner="offline-worker",
    )

    async def authorize(grant, body):
        return permit

    manager = MagicMock()
    manager.current_version_id.return_value = "version-1"
    manager.get_secret_at_version.return_value = (
        json.dumps(
            {
                "role_arn": credential.scopes["role_arn"],
                "external_id": "test-external-id",
                "account_id": "123456789012",
                "session_duration_seconds": 900,
                "default_region": "us-west-2",
            }
        ),
        "version-1",
    )
    app.dependency_overrides[get_secrets_manager] = lambda: manager
    assumed = Mock(
        return_value=AssumeRoleResult(
            access_key_id="test-access",
            secret_access_key="test-secret",
            session_token="test-session",
            expiration=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            region="us-west-2",
            profile_name="test-profile",
        )
    )
    monkeypatch.setattr(assume_role_routes, "assume_role", assumed)
    identity = AsyncMock(
        return_value={
            "account_id": "123456789012",
            "role_arn": credential.scopes["role_arn"],
            "region": "us-west-2",
            "verified_at": datetime.now(UTC).isoformat(),
        }
    )
    monkeypatch.setattr("src.internal.l5_credential_routes.verify_assumed_session", identity)
    return grant, credential, request_body, permit, authorize, assumed, identity


def test_security_agent_session_policies_pin_known_space_and_limit_unknown_creation():
    expires_at = datetime.now(UTC) + timedelta(minutes=1)
    identity = {"account_id": "123456789012", "region": "us-east-1"}
    space = {"record_type": "security_space", "record_id": "space-one"}
    binding = SimpleNamespace(provider_identity=identity, native_resource_ref=space)
    exact = "arn:aws:securityagent:us-east-1:123456789012:agent-space/space-one"
    for policy in (
        _security_session_policy(
            binding, {"bucket": "example-source-staging", "service_role": "arn:aws:iam::123456789012:role/example-security-execution"}, expires_at
        ),
        _threat_model_session_policy(
            binding, {"bucket": "example-source-staging", "service_role": "arn:aws:iam::123456789012:role/example-security-execution"}, expires_at
        ),
    ):
        assert json.loads(policy)["Statement"][0]["Resource"] == exact
    assert (
        "securityagent:StartThreatModelJob"
        in json.loads(
            _threat_model_session_policy(
                binding, {"bucket": "example-source-staging", "service_role": "arn:aws:iam::123456789012:role/example-security-execution"}, expires_at
            )
        )["Statement"][0]["Action"]
    )
    for invalid in ("space*", "../other", "space/other"):
        binding.native_resource_ref = {**space, "record_id": invalid}
        with pytest.raises(HTTPException) as denied:
            _security_session_policy(
                binding, {"bucket": "example-source-staging", "service_role": "arn:aws:iam::123456789012:role/example-security-execution"}, expires_at
            )
        assert denied.value.status_code == 409
        with pytest.raises(HTTPException):
            _threat_model_session_policy(
                binding, {"bucket": "example-source-staging", "service_role": "arn:aws:iam::123456789012:role/example-security-execution"}, expires_at
            )

    intent = SimpleNamespace(
        operation_id="setup-one", tenant_id="tenant-one", product_id="product-one", project_id="project-one", configuration_id="config-one"
    )
    action = SimpleNamespace(action="create")
    create = json.loads(_security_setup_policy(identity["account_id"], identity["region"], expires_at, intent, action, None, False))["Statement"]
    assert create[0]["Resource"] == "*"
    assert set(create[0]["Action"]) == {"securityagent:CreateAgentSpace", "securityagent:ListAgentSpaces"}
    assert create[0]["Condition"]["StringEquals"] == {"aws:RequestedRegion": "us-east-1"}
    assert create[1]["Resource"] == "arn:aws:securityagent:us-east-1:123456789012:agent-space/*"
    assert create[1]["Action"] == ["securityagent:BatchGetAgentSpaces"]
    assert create[1]["Condition"]["StringEquals"] == {"aws:ResourceTag/adp-setup-operation": "setup-one"}
    assert all(item["Condition"]["DateLessThan"]["aws:CurrentTime"] == expires_at.isoformat() for item in create)
    reconciled = json.loads(_security_setup_policy(identity["account_id"], identity["region"], expires_at, intent, action, space, False))["Statement"]
    assert reconciled[0]["Action"] == ["securityagent:ListAgentSpaces"]
    assert reconciled[1]["Resource"] == exact and "StringEquals" not in reconciled[1]["Condition"]
    action.action = "check"
    check = json.loads(_security_setup_policy(identity["account_id"], identity["region"], expires_at, intent, action, space, True))["Statement"]
    assert len(check) == 2 and check[0]["Resource"] == exact
    assert check[1]["Action"] == ["iam:PassRole"]
    assert check[1]["Resource"].startswith("arn:aws:iam::123456789012:role/adp/security/ADP-Security-")
    assert "*" not in check[1]["Resource"]
    assert check[1]["Condition"]["StringEquals"] == {"iam:PassedToService": "securityagent.amazonaws.com"}
    registered = {"service_role": "arn:aws:iam::123456789012:role/ExistingSourceRole", "bucket": "example-source-staging"}
    selected_policy = json.loads(
        _security_setup_policy(identity["account_id"], identity["region"], expires_at, intent, action, space, True, registered)
    )
    assert selected_policy["Statement"][1]["Resource"] == registered["service_role"]
    with pytest.raises(HTTPException):
        _security_setup_policy(
            identity["account_id"],
            identity["region"],
            expires_at,
            intent,
            action,
            space,
            True,
            {**registered, "service_role": "arn:aws:iam::210987654321:role/Foreign"},
        )
    assert "securityagent:CreateThreatModel" in check[0]["Action"]
    assert "securityagent:ListThreatModels" in check[0]["Action"]
    assert "securityagent:BatchGetThreatModels" in check[0]["Action"]
    assert "securityagent:CreateAgentSpace" not in check[0]["Action"]
    action.action = "cleanup"
    cleanup = json.loads(_security_setup_policy(identity["account_id"], identity["region"], expires_at, intent, action, space, True))["Statement"]
    assert cleanup[0]["Action"] == ["securityagent:ListAgentSpaces"]
    assert cleanup[1]["Resource"] == exact and cleanup[1]["Action"] == ["securityagent:BatchGetAgentSpaces", "securityagent:DeleteAgentSpace"]
    action.action = "link"
    with pytest.raises(HTTPException) as missing:
        _security_setup_policy(identity["account_id"], identity["region"], expires_at, intent, action, None, False)
    assert missing.value.status_code == 409
    with pytest.raises(HTTPException) as wildcard:
        _security_setup_policy(identity["account_id"], identity["region"], expires_at, intent, action, {**space, "record_id": "space*"}, False)
    assert wildcard.value.status_code == 409


@pytest.mark.asyncio
async def test_transform_authorizer_consumes_actual_run_session_response(
    test_client,
    db_session,
    app,
    source_member,
    monkeypatch,
):
    grant, credential, body, permit, authorize, assumed, identity = await setup_broker(test_client, db_session, app, source_member, monkeypatch)
    app.dependency_overrides[get_l5_work_authorizer] = lambda: authorize
    connection = {**body["connection_ref"], "record_type": "connection"}
    scope = {"tenant_id": grant.tenant_id, "product_id": grant.product_id, "project_id": grant.project_id}
    admission = {
        "scope": scope,
        "connection_ref": connection,
        "transform_settings": {"connection_ref": connection, "region": "us-west-2"},
        "provider_identity": identity.return_value,
        "delegation_id": grant.id,
        "delegation_version": grant.revision,
        "binding_generation": grant.binding_generation,
    }
    run = SimpleNamespace(
        **scope,
        service_id="transform",
        run_id=body["operation_id"],
        state="queued",
        revision=1,
        binding_generation=grant.binding_generation,
        input={"capability": "refactor"},
        admission=admission,
        deadline_at=grant.expires_at,
        capture_limits={},
    )
    claim = SimpleNamespace(
        tenant_id=grant.tenant_id,
        run_id=run.run_id,
        work_id=body["attempt_id"],
        owner=body["lease_owner"],
        fence=body["lease_fence"],
        generation=1,
        kind="submit",
        run_revision=run.revision,
        run_input=run.input,
        admission=admission,
    )
    authority = await TransformWorkAuthorizer(test_client)(run, claim, "submit")
    assert authority.source_request == {key: value for key, value in body.items() if key != "connection_ref"}
    assert authority.provider_identity == identity.return_value
    assert authority.provider_credentials()["session_token"] == "test-session"
    assert authority.expires_at == permit.expires_at
    assumed.assert_called_once()


@pytest.mark.asyncio
async def test_broker_requires_current_work_and_selected_verified_role(test_client, db_session, test_engine, app, source_member, monkeypatch):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    grant, credential, body, permit, authorize, assumed, identity = await setup_broker(test_client, db_session, app, source_member, monkeypatch)
    path = "/internal/v1/l5/credential-session"
    unavailable = await test_client.post(path, json=body)
    assert unavailable.status_code == 409, unavailable.text
    assert unavailable.json()["detail"]["code"] == "stale_delegation_or_attempt"
    assumed.assert_not_called()

    app.dependency_overrides[get_l5_work_authorizer] = lambda: authorize
    delivered = await test_client.post(path, json=body)
    assert delivered.status_code == 200, delivered.text
    assert delivered.headers["Cache-Control"] == "no-store"
    result = delivered.json()
    assert set(result) == {
        "access_key_id",
        "secret_access_key",
        "session_token",
        "expires_at",
        "provider_identity",
        "delegation_revision",
        "operation_id",
        "attempt_id",
    }
    assert result["delegation_revision"] == grant.revision
    assert (result["operation_id"], result["attempt_id"]) == (permit.operation_id, permit.attempt_id)
    assert result["provider_identity"]["role_arn"] == credential.scopes["role_arn"]
    identity.assert_awaited_once()
    assert assumed.call_count == 1
    assert assumed.call_args.kwargs["role_arn"] == credential.scopes["role_arn"]
    assert assumed.call_args.kwargs["session_duration_seconds"] == 900
    policy = json.loads(assumed.call_args.kwargs["session_policy"])
    assert all(statement["Condition"]["DateLessThan"]["aws:CurrentTime"] for statement in policy["Statement"])
    assert policy["Statement"][1]["Resource"] == "arn:aws:s3:::example-source-staging/security-agent/source/*"
    assert "securityagent:CreatePentest" not in policy["Statement"][0]["Action"]
    assert datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00")) <= permit.expires_at


@pytest.mark.asyncio
async def test_sibling_worker_cannot_claim_security_broker_session(test_client, db_session, app, source_member, monkeypatch):
    _, _, body, _, authorize, assumed, identity = await setup_broker(test_client, db_session, app, source_member, monkeypatch)
    app.dependency_overrides[get_l5_work_authorizer] = lambda: authorize
    sibling = source_member.model_copy(
        update={
            "user_id": "iam-agent:l5-devops-worker",
            "agent_registry_id": worker_id("devops"),
            "org_id": "platform-service",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "credential_scopes": ["l5:credential-session"],
        }
    )

    async def verified(request: Request):
        request.state.token_context = sibling

    app.dependency_overrides[verify_internal_or_irsa] = verified
    denied = await test_client.post("/internal/v1/l5/credential-session", json=body)
    assert denied.status_code == 403, denied.text
    assumed.assert_not_called()
    identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_broker_denies_forged_connection_stale_attempt_and_revocation(test_client, db_session, app, source_member, monkeypatch):
    grant, credential, body, permit, authorize, assumed, identity = await setup_broker(test_client, db_session, app, source_member, monkeypatch)
    path = "/internal/v1/l5/credential-session"
    app.dependency_overrides[get_l5_work_authorizer] = lambda: authorize
    forged = {**body, "connection_ref": {**body["connection_ref"], "record_id": "foreign-role"}}
    rejected = await test_client.post(path, json=forged)
    assert rejected.status_code == 403
    assert "foreign-role" not in rejected.text

    async def stale(grant, body):
        return L5WorkPermit(**{**permit.__dict__, "attempt_id": "other-attempt"})

    app.dependency_overrides[get_l5_work_authorizer] = lambda: stale
    refused = await test_client.post(path, json=body)
    assert refused.status_code == 409, refused.text
    assumed.assert_not_called()

    for field in ("lease_fence", "binding_generation"):

        async def wrong_fence(grant, body):
            return L5WorkPermit(**{**permit.__dict__, field: 99})

        app.dependency_overrides[get_l5_work_authorizer] = lambda: wrong_fence
        mismatch = await test_client.post(path, json=body)
        assert mismatch.status_code == 409, mismatch.text
        assumed.assert_not_called()

    app.dependency_overrides[get_l5_work_authorizer] = lambda: authorize
    forged_source = {**body, "connection_ref": {**body["connection_ref"], "record_revision": "forged"}}
    refused_source = await test_client.post(path, json=forged_source)
    assert refused_source.status_code == 403
    assert "forged" not in refused_source.text
    assumed.assert_not_called()

    state = {"calls": 0}

    async def revoked(grant, body):
        state["calls"] += 1
        if state["calls"] == 2:
            await db_session.execute(
                update(CredentialServiceDelegation).where(CredentialServiceDelegation.id == grant.id).values(revoked_at=datetime.now(UTC))
            )
            await db_session.commit()
        return permit

    app.dependency_overrides[get_l5_work_authorizer] = lambda: revoked
    lost = await test_client.post(path, json=body)
    assert lost.status_code in {403, 409}, lost.text
    assert assumed.call_count == 1
    identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_broker_does_not_return_session_revoked_after_assume(test_client, db_session, app, source_member, monkeypatch):
    grant, _, body, permit, _, assumed, identity = await setup_broker(test_client, db_session, app, source_member, monkeypatch)
    calls = 0

    async def revoke_after_assume(grant, body):
        nonlocal calls
        calls += 1
        if calls == 4:
            await db_session.execute(
                update(CredentialServiceDelegation).where(CredentialServiceDelegation.id == grant.id).values(revoked_at=datetime.now(UTC))
            )
            await db_session.commit()
        return permit

    app.dependency_overrides[get_l5_work_authorizer] = lambda: revoke_after_assume
    response = await test_client.post("/internal/v1/l5/credential-session", json=body)
    assert response.status_code == 409, response.text
    assert "test-secret" not in response.text
    assert assumed.call_count == 1
    identity.assert_awaited_once()


@pytest.mark.asyncio
async def test_selected_session_checks_exact_account_and_role(monkeypatch):
    session = SimpleNamespace(access_key_id="test-access", secret_access_key="test-secret", session_token="test-session", region="us-west-2")
    observed = []

    def get_caller_identity():
        return observed[-1]

    client = SimpleNamespace(get_caller_identity=get_caller_identity)
    monkeypatch.setattr(
        "src.internal.l5_credential_routes.boto3.Session",
        lambda **kwargs: SimpleNamespace(client=lambda service: client),
    )
    role_arn = "arn:aws:iam::123456789012:role/ADPDeployAgent"
    observed.append({"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/ADPDeployAgent/run-a"})
    verified = await verify_assumed_session(session, role_arn, "123456789012")
    assert verified["role_arn"] == role_arn
    observed.append({"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/OtherRole/run-a"})
    with pytest.raises(HTTPException) as mismatch:
        await verify_assumed_session(session, role_arn, "123456789012")
    assert mismatch.value.status_code == 403
    observed.append({"Account": "000000000000", "Arn": "arn:aws:sts::000000000000:assumed-role/ADPDeployAgent/run-a"})
    with pytest.raises(HTTPException) as wrong_account:
        await verify_assumed_session(session, role_arn, "123456789012")
    assert wrong_account.value.status_code == 403
