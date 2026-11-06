"""Real owner/source/delegation boundaries; only external providers are replaced."""

import base64
import io
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from l5_services.api.configuration import view
from l5_services.models.binding import Binding
from l5_services.models.preparation import AdmissionPreparation
from sqlalchemy import delete, func, select

from src import l5_backend_devops as owned
from src.l5_devops_verification import VerificationReader, canonical
from src.l5_integration import GatewayConfigurationBackend
from src.products.models import ProductSourceBinding, ProductSourceSnapshot
from src.products.source_routes import source_ref_authorized
from src.products.source_snapshots import CapturedSource
from src.shared.models.organization import TeamMembership
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation
from tests.products.test_l5_owner_delegation import consent_fixture, l5_owner, request_grant

__all__ = ["consent_fixture", "l5_owner"]


@pytest.fixture
async def devops_backend(test_client, db_session, consent_fixture, l5_owner, monkeypatch):
    fixture = consent_fixture
    config, version = fixture["configurations"]["devops"]
    connection = fixture["reference"]
    mapping = ChannelTenantMap(
        id=str(uuid4()),
        provider="github",
        provider_scope_id="42",
        org_id=l5_owner.org_id,
        installation_id="123",
        installed_by_user_id=l5_owner.user_id,
        ownership_disputed=False,
    )
    head = {**connection, "source_connection_id": mapping.id, "record_type": "repository_commit", "record_id": "456", "record_revision": "b" * 40}
    base = {**head, "record_revision": "a" * 40}
    binding_id = str(uuid4())
    trigger = {
        "trigger_id": "manual-release",
        "kind": "manual",
        "enabled": True,
        "capability": "release_readiness",
        "binding_id": binding_id,
        "source_refs": [head],
    }
    version.settings = {
        **version.settings,
        "capabilities": ["release_readiness"],
        "source_refs": [base, head],
        "classification": "internal",
        "profile_ref": {**connection, "record_type": "profile"},
        "limits": {"max_cost": "5", "currency": "USD", "deadline_seconds": 300, "policy_ref": {**connection, "record_type": "policy"}},
        "devops": {**version.settings["devops"], "trigger_policies": [trigger]},
    }
    binding = Binding(
        tenant_id=l5_owner.org_id,
        product_id=fixture["product_id"],
        project_id=fixture["project_id"],
        service_id="devops",
        binding_id=binding_id,
        configuration_id=config.configuration_id,
        state="ready",
        gaps=[],
        generation=1,
        revision=1,
        scope=view(config, version)["scope"],
        connection_ref=connection,
        native_resource_ref={**connection, "record_type": "agent_space", "record_id": "space-one"},
        provider_identity={"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Fixture", "region": "us-east-1"},
        ownership="customer_linked",
        connection_key="fixture-connection",
        native_key="fixture-native",
        readiness=[],
    )
    source = ProductSourceBinding(
        tenant_id=l5_owner.org_id,
        project_id=fixture["project_id"],
        kind="repository",
        provider="github",
        installation_id=mapping.id,
        namespace="42",
        native_id="456",
        status="healthy",
        generation=1,
        revision=1,
        desired={"kind": "repository", "provider": "github", "native_id": "456", "connection_ref": {"id": mapping.id, "kind": "github_installation"}},
        observed={"source_revision": head["record_revision"]},
    )
    db_session.add_all([mapping, binding, source])
    await db_session.commit()
    response, _, _, _ = await request_grant(test_client, fixture["path"] + "/devops")
    assert response.status_code == 200, response.text
    parent = await db_session.get(CredentialServiceDelegation, response.json()["delegation_id"])
    provider = Mock()
    native = Mock()
    repository = {
        "id": 456,
        "name": "repo",
        "full_name": "example/repo",
        "html_url": "https://github.com/example/repo",
        "owner": {"id": 42, "login": "example", "type": "Organization"},
    }
    service = {
        "serviceId": "service-one",
        "serviceType": "github",
        "additionalServiceDetails": {"github": {"owner": "example", "ownerType": "organization"}},
    }
    association = {
        "agentSpaceId": "space-one",
        "associationId": "association-one",
        "serviceId": "service-one",
        "status": "valid",
        "configuration": {
            "github": {"repoName": "repo", "repoId": "456", "owner": "example", "ownerType": "organization", "instanceIdentifier": "github.com"}
        },
        "capabilities": {"RELEASE_READINESS_REVIEW": {"enabled": True}, "RELEASE_READINESS_REVIEW_AUTOMATED_TESTING": {"enabled": False}},
    }
    native.get_agent_space.return_value = {"agentSpace": {"agentSpaceId": "space-one"}}
    native.list_services.return_value = {"services": [service]}
    native.get_service.return_value = {"service": service}
    native.list_associations.return_value = {"associations": [association]}
    native.get_association.return_value = {"association": association}
    iam = Mock()
    iam.simulate_principal_policy.side_effect = lambda **kwargs: {
        "EvaluationResults": [
            {"EvalActionName": "aidevops:CreateBacklogTask", "EvalResourceName": kwargs["ResourceArns"][0], "EvalDecision": "allowed"}
        ]
    }
    sts = Mock()
    assumed_arn = "arn:aws:sts::123456789012:assumed-role/Fixture/readiness"
    sts.get_caller_identity.return_value = {"Account": "123456789012", "Arn": assumed_arn}
    provider.client.side_effect = lambda name, **kwargs: {"devops-agent": native, "iam": iam, "sts": sts}[name]
    monkeypatch.setattr(owned.boto3, "Session", lambda **kwargs: provider)
    secrets = Mock()
    secrets.current_version_id.return_value = connection["record_revision"]
    secrets.get_secret_at_version.return_value = (
        json.dumps(
            {"role_arn": binding.provider_identity["role_arn"], "account_id": "123456789012", "external_id": fixture["credential"].aws_external_id}
        ),
        connection["record_revision"],
    )
    monkeypatch.setattr(owned, "SecretsManagerHelper", lambda **kwargs: secrets)
    assumed = Mock(
        return_value=SimpleNamespace(assumed_role_arn=assumed_arn, access_key_id="offline", secret_access_key="offline", session_token="offline")
    )
    monkeypatch.setattr(owned, "assume_role", assumed)
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    requests = []

    class GitHub:
        def __init__(self, *args):
            pass

        async def get_installation_repository(self, installation, repository_id):
            assert (installation, repository_id) == (123, 456)
            return repository

        async def installation_request(self, installation, method, path):
            assert installation == 123 and method == "GET"
            requests.append(path)
            if "/commits/" in path:
                return {"sha": path.rsplit("/", 1)[1]}
            if "/statuses?" in path:
                return [{"state": "success"}]
            if "/deployments?" in path:
                return [{"id": 789, "sha": head["record_revision"], "environment": "staging"}]
            raise AssertionError(path)

        async def aclose(self):
            pass

    monkeypatch.setattr("src.l5_devops_reads.GitHubAppClient", GitHub)
    monkeypatch.setattr("src.l5_devops_reads._get_github_app_credentials", lambda: ("offline-app", "offline-key"))
    backend = GatewayConfigurationBackend(
        db_session,
        l5_owner,
        publish=lambda *args: None,
        release_authority=owned.Callback("release"),
        devops_integration_observer=owned.Callback("integrations"),
    )
    backend.admission_key, backend.admission_operation = "release-request-00001", "submit"
    run_input = {
        "service_id": "devops",
        "configuration_revision": 1,
        "binding_id": binding_id,
        "capability": "release_readiness",
        "devops_input": {
            "kind": "release_readiness",
            "repository_ref": head,
            "base_revision": base["record_revision"],
            "head_revision": head["record_revision"],
        },
    }
    return SimpleNamespace(
        **fixture,
        config=config,
        version=version,
        source=source,
        binding=binding,
        parent=parent,
        backend=backend,
        head=head,
        base=base,
        mapping=mapping,
        run_input=run_input,
        native=native,
        iam=iam,
        assumed=assumed,
        association=association,
        secrets=secrets,
        provider=provider,
        requests=requests,
    )


@pytest.mark.asyncio
async def test_real_release_issues_exact_child_and_pins_both_sources(devops_backend, db_session, monkeypatch):
    selected = devops_backend
    verified, client = await selected.backend.admission_authority(selected.product_id, selected.project_id, "devops", selected.run_input)
    child = await db_session.get(CredentialServiceDelegation, verified["delegation_id"])
    assert child.parent_delegation_id == selected.parent.id and child.execution_scope == selected.parent.execution_scope
    assert child.sources[1:] == [selected.base, selected.head]
    assert client.verified == verified and client.grant_id == child.id
    assert verified["release_eligibility"]["task_permission"] == "allowed"
    assert source_ref_authorized(child, selected.source, selected.base)
    assert not source_ref_authorized(child, selected.source, {**selected.base, "record_revision": "c" * 40})
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    db_session.add(
        AdmissionPreparation(
            tenant_id=child.tenant_id,
            product_id=child.product_id,
            project_id=child.project_id,
            service_id="devops",
            actor_id=selected.backend.actor.user_id,
            operation="submit",
            idempotency_key=selected.backend.admission_key,
            request_hash="a" * 64,
            verified=verified,
            deadline_at=deadline,
        )
    )
    await db_session.flush()

    async def capture(binding, mapping, request, objects):
        assert binding.id == selected.source.id and mapping.id == selected.mapping.id
        artifact = {
            "artifact_id": "products/source-snapshots/offline",
            "version": "v1",
            "sha256": "c" * 64,
            "media_type": "application/zip",
            "bytes": 1,
        }
        return CapturedSource(
            source_ref=request.source_ref,
            content=artifact,
            file_manifest=artifact,
            file_count=1,
            exclusions=request.exclusions,
            expires_at=request.retain_until,
        )

    monkeypatch.setattr("src.products.l5_admission_capture.capture_source", capture)
    monkeypatch.setattr("src.products.l5_admission_capture.get_snapshot_objects", lambda: object())
    for i, source in enumerate((selected.base, selected.head)):
        receipt = await client.pin_source(
            selected.product_id,
            selected.project_id,
            {
                "delegation_id": child.id,
                "delegation_revision": child.revision,
                "source_ref": source,
                "max_bytes": verified["max_bytes"],
                "max_files": verified["max_files"],
                "exclusions": verified["exclusions"],
                "retain_until": deadline.isoformat(),
            },
            idempotency_key=f"release-source-{i}",
        )
        assert receipt["source_ref"] == source
    assert await db_session.scalar(select(func.count()).select_from(ProductSourceSnapshot)) == 2
    selected.parent.revoked_at = datetime.now(UTC)
    await db_session.commit()
    with pytest.raises(HTTPException):
        await selected.backend.admission_authority(selected.product_id, selected.project_id, "devops", selected.run_input)
    assert await db_session.scalar(select(func.count()).select_from(CredentialServiceDelegation)) == 2
    # No provider mutation is part of admission or readiness.
    selected.native.create_backlog_task.assert_not_called()
    selected.native.associate_service.assert_not_called()
    selected.native.update_goal.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "membership", "source", "source_connection", "parent_scope", "permission", "association", "base"])
async def test_release_denials_do_not_issue_child(devops_backend, db_session, change):
    selected = devops_backend
    if change == "owner":
        selected.config.owner_id = "another-owner"
    elif change == "membership":
        selected.backend.actor = selected.backend.actor.model_copy(update={"is_platform_admin": True})
        await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == selected.backend.actor.user_id))
    elif change == "source":
        selected.source.status = "revoked"
    elif change == "source_connection":
        selected.mapping.ownership_disputed = True
    elif change == "parent_scope":
        selected.parent.execution_scope = None
    elif change == "permission":
        selected.iam.simulate_principal_policy.side_effect = lambda **kwargs: {"EvaluationResults": []}
    elif change == "association":
        selected.association["capabilities"]["RELEASE_READINESS_REVIEW"]["enabled"] = False
    else:
        selected.run_input["devops_input"]["base_revision"] = "c" * 40
    await db_session.commit()
    with pytest.raises(HTTPException):
        await selected.backend.admission_authority(selected.product_id, selected.project_id, "devops", selected.run_input)
    assert await db_session.scalar(select(func.count()).select_from(CredentialServiceDelegation)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["membership", "configuration"])
async def test_owner_and_configuration_rechecked_after_provider_reads(devops_backend, db_session, monkeypatch, change):
    selected = devops_backend
    selected.backend.actor = selected.backend.actor.model_copy(update={"is_platform_admin": True})
    from src import l5_devops_reads

    original = l5_devops_reads.GitHubAppClient.installation_request
    changed = False

    async def request(self, installation, method, path):
        nonlocal changed
        result = await original(self, installation, method, path)
        if not changed and "/statuses?" in path:
            changed = True
            if change == "membership":
                await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == selected.backend.actor.user_id))
            else:
                selected.version.hash = "d" * 64
            await db_session.commit()
        return result

    monkeypatch.setattr(l5_devops_reads.GitHubAppClient, "installation_request", request)
    with pytest.raises(HTTPException):
        await selected.backend.admission_authority(selected.product_id, selected.project_id, "devops", selected.run_input)
    assert changed
    assert await db_session.scalar(select(func.count()).select_from(CredentialServiceDelegation)) == 1


@pytest.mark.asyncio
async def test_sessions_are_finite_and_read_checks_never_update(devops_backend):
    selected = devops_backend
    service = owned.DevopsService(selected.backend)
    await service.selected_provider(selected.binding, integration=True, permission=True)
    policy = json.loads(selected.assumed.call_args.kwargs["session_policy"])
    assert all(statement.get("Condition", {}).get("DateLessThan") for statement in policy["Statement"])
    actions = [action for row in policy["Statement"] for action in (row["Action"] if isinstance(row["Action"], list) else [row["Action"]])]
    assert not any(action in actions for action in ("aidevops:CreateBacklogTask", "aidevops:UpdateGoal", "aidevops:AssociateService"))
    read = owned.PreventionClient(selected.native, "space-one")
    with pytest.raises(ValueError):
        read.update_goal(agentSpaceId="space-one", goalId="goal-one", evaluationSchedule={"state": "ENABLED"}, clientToken="test")
    write = owned.PreventionClient(selected.native, "space-one", "goal-one")
    with pytest.raises(ValueError):
        write.update_goal(agentSpaceId="space-one", goalId="another", evaluationSchedule={"state": "ENABLED"}, clientToken="test")
    selected.native.update_goal.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "hash", "signature", "configuration", "admission", "producer", "target"])
async def test_signed_verification_evidence_is_admission_bound(devops_backend, monkeypatch, fault):
    selected = devops_backend
    now = datetime.now(UTC)
    deployment = {**selected.head, "record_type": "deployment", "record_id": "deployment-one"}
    target = {**selected.head, "record_type": "verification_target", "record_id": "target-one"}
    observation = {"deployment_ref": deployment, "target_ref": target, "scenario_specs": []}
    payload = {
        "schema_version": 1,
        "producer": "independent-fixture-producer",
        "scope": {"tenant_id": selected.head["tenant_id"], "product_id": selected.product_id, "project_id": selected.project_id},
        "configuration": {"id": selected.config.configuration_id, "revision": 1, "hash": selected.version.hash},
        "admission": {"operation": "submit", "idempotency_key": selected.backend.admission_key},
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "observation": observation,
    }
    if fault == "configuration":
        payload["configuration"]["hash"] = "different"
    if fault == "admission":
        payload["admission"]["idempotency_key"] = "different"
    if fault == "producer":
        payload["producer"] = "different"
    if fault == "target":
        payload["observation"]["target_ref"] = {**target, "record_id": "different"}
    content = canonical({"payload": payload, "signature": base64.b64encode(b"offline signature").decode()})
    reference = {
        "bucket": "offline-evidence",
        "key": "evidence.json",
        "version": "v1",
        "sha256": sha256(content).hexdigest(),
        "owner_account_id": "123456789012",
        "signing_key_arn": "arn:aws:kms:us-east-1:123456789012:key/offline-key",
        "producer": "independent-fixture-producer",
    }
    if fault == "hash":
        reference["sha256"] = "0" * 64
    s3, kms = Mock(), Mock()
    s3.get_object.return_value = {"VersionId": "v1", "Body": io.BytesIO(content)}
    kms.verify.return_value = {"SignatureValid": fault != "signature", "KeyId": reference["signing_key_arn"]}
    session = SimpleNamespace(client=lambda name, **kwargs: {"s3": s3, "kms": kms}[name])
    reader = VerificationReader(selected.backend, session=session, reference=reference)
    if fault:
        with pytest.raises(HTTPException) as denied:
            await reader.read_deployment(deployment, target)
        assert denied.value.detail["code"] == "deployment_evidence_unverified"
    else:
        assert await reader.read_deployment(deployment, target) == {"deployment_ref": deployment, "target_ref": target}
        assert await reader.read_scenarios([]) == []
        assert kms.verify.call_args.kwargs["Message"] == sha256(canonical(payload)).digest()
        assert s3.get_object.call_args.kwargs["ExpectedBucketOwner"] == "123456789012"
