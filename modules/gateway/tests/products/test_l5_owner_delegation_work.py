"""Owner API grants cross real setup, child derivation and work verification."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from l5_services.api.configuration import view
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.binding import Binding
from l5_services.models.run import Run
from l5_services.models.setup import SetupWork
from l5_services.models.work import Work
from l5_services.work_store import TRANSFORM_SUBMIT_OPERATION
from sqlalchemy import select

from src.auth.aws_connection_authority import connection_binding
from src.auth.l5_delegation import issue_l5_work_delegation
from src.internal.assume_role_routes import get_secrets_manager
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.l5_credential_routes import CredentialSessionRequest, get_l5_work_authorizer
from src.internal.sts_assume_service import AssumeRoleResult
from src.l5_integration import GatewayConfigurationBackend
from src.l5_service_identity import WORKER_IDS
from src.l5_wa_provider_selection import ProviderSelection, provider_selection
from src.products.models import ProductSourceBinding
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation
from tests.products import test_l5_owner_delegation as consent_fixtures

consent_fixture = consent_fixtures.consent_fixture
l5_owner = consent_fixtures.l5_owner


async def _wa_resources(db, owner, fixture, monkeypatch):
    """Use real owner/environment validation and reference pins over AWS fakes."""
    account = fixture["credential"].scopes["account_id"]
    role = f"arn:aws:iam::{account}:role/SourceReader"
    bucket = "fictional-source-bucket"
    db.add(
        ProductSourceBinding(
            tenant_id=owner.org_id,
            project_id=fixture["project_id"],
            kind="environment",
            provider="aws",
            credential_id=fixture["credential"].id,
            namespace=account,
            native_id="us-east-1",
            desired={},
            observed={"source_revision": fixture["reference"]["record_revision"]},
            status="healthy",
        )
    )
    await db.flush()
    iam, s3 = Mock(), Mock()
    iam.get_role.return_value = {
        "Role": {
            "Arn": role,
            "RoleId": "AROFIXTURE",
            "AssumeRolePolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "sts:AssumeRole",
                        "Principal": {"Service": "wellarchitected.amazonaws.com"},
                        "Condition": {"StringEquals": {"aws:SourceAccount": account}},
                    }
                ],
            },
        }
    }
    iam.simulate_principal_policy.return_value = {
        "EvaluationResults": [
            {
                "EvalActionName": "s3:GetObjectVersion",
                "EvalResourceName": f"arn:aws:s3:::{bucket}/l5/well_architected/work/*",
                "EvalDecision": "allowed",
                "MissingContextValues": [],
            }
        ]
    }
    s3.get_bucket_location.return_value = {"LocationConstraint": None}
    s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
    s3.get_public_access_block.return_value = {
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        }
    }
    clients = {"iam": iam, "s3": s3}
    provider = SimpleNamespace(client=Mock(side_effect=lambda name, **_: clients[name]))
    identity = {"account_id": account, "region": "us-east-1", "role_arn": fixture["credential"].scopes["role_arn"]}
    monkeypatch.setenv("L5_ARCHIVE_BUCKET", bucket)
    monkeypatch.setattr("src.l5_wa_provider_selection.WellArchitectedService.selected_provider", AsyncMock(return_value=(provider, identity)))
    return await provider_selection(
        GatewayConfigurationBackend(db, owner),
        fixture["project_id"],
        ProviderSelection(connection_ref=fixture["reference"], region="us-east-1", execution_role_arn=role, source_bucket=bucket),
    )


@pytest.fixture
async def work_fixture(consent_fixture, db_session, l5_owner, monkeypatch):
    fixture = consent_fixture
    fixture["credential"].agent_consent = True
    fixture["credential"].scopes = {**fixture["credential"].scopes, "source": "imported_role"}
    fixture["credential"].aws_verified_binding = connection_binding(fixture["credential"])
    mapping = ChannelTenantMap(
        org_id=l5_owner.org_id, provider="github", provider_scope_id="fixture-org", installation_id="17", ownership_disputed=False
    )
    db_session.add(mapping)
    await db_session.flush()
    source = {
        **fixture["reference"],
        "source_connection_id": mapping.id,
        "record_type": "repository_commit",
        "record_id": "27",
        "record_revision": "a" * 40,
    }
    db_session.add(
        ProductSourceBinding(
            tenant_id=l5_owner.org_id,
            project_id=fixture["project_id"],
            kind="repository",
            provider="github",
            installation_id=mapping.id,
            namespace="fixture-org",
            native_id="27",
            desired={"source_revision": "a" * 40},
            observed={"source_revision": "a" * 40},
            status="healthy",
        )
    )
    monkeypatch.setattr("src.products.source_routes.assert_installation_owned_by", AsyncMock())
    monkeypatch.setattr("src.product_onboarding.source_discovery.assert_installation_owned_by", AsyncMock())
    monkeypatch.setattr(
        GatewayConfigurationBackend,
        "source_rights",
        AsyncMock(return_value={(source["source_connection_id"], source["record_id"]), (fixture["credential"].id, "us-east-1")}),
    )
    fixture["source"] = source
    wa_resources = await _wa_resources(db_session, l5_owner, fixture, monkeypatch)
    for service, (configuration, version) in fixture["configurations"].items():
        settings = {**version.settings, "enabled": True, "source_refs": [] if service in {"devops", "finops"} else [source]}
        if service == "transform":
            settings["capabilities"] = ["refactor"]
            settings["transform"] = {
                **settings["transform"],
                "executor_binding_ref": {**fixture["reference"], "record_type": "executor_binding", "record_id": "isolated-executor"},
                "test_policy_ref": {**fixture["reference"], "record_type": "policy", "record_id": "test-policy"},
                "definition_ref": {
                    **fixture["reference"],
                    "record_type": "transform_definition",
                    "record_id": "selected-package",
                    "record_revision": "v1",
                },
                "read_paths": ["src"],
                "write_paths": ["src"],
                "excluded_paths": [],
                "goal_defaults": {"kind": "refactor", "summary": "Improve structure"},
            }
        if service == "well_architected":
            settings[service] = {
                "pillars": ["SECURITY"],
                "execution_role_ref": wa_resources["execution_role_ref"],
                "source_store_ref": wa_resources["source_store_ref"],
            }
        if service == "finops":
            settings[service] = {
                "agent_mode": "adopt",
                "native_agent_ref": "selected-agent",
                "data_role_ref": "arn:aws:iam::123456789012:role/BillingReader",
                "billing_scope": {"view_ref": "arn:aws:billing::123456789012:billingview/selected-view"},
            }
        version.settings, version.state, version.gaps = settings, "ready", []
    await db_session.commit()
    return fixture


def _backend(db, owner):
    qualification = {
        "account_id": "123456789012",
        "region": "us-east-1",
        "broker_role_arn": "arn:aws:iam::123456789012:role/Fixture",
        "execution_role_arn": "arn:aws:iam::123456789012:role/SourceReader",
        "trust": "qualified",
        "source_permissions": "qualified",
        "support": "eligible",
    }
    return GatewayConfigurationBackend(db, owner, wa_qualify=AsyncMock(return_value=qualification))


def _body(grant, operation_id, work_id, connection):
    return CredentialSessionRequest(
        delegation_id=grant.id,
        delegation_revision=grant.revision,
        operation_id=operation_id,
        attempt_id=work_id,
        lease_owner="fixture-worker",
        lease_fence=1,
        connection_ref={**connection, "record_type": "credential"},
    )


async def _grant(test_client, db_session, fixture, service):
    path = fixture["path"] + "/" + service
    response, _, _, _ = await consent_fixtures.request_grant(test_client, path)
    assert response.status_code == 200, response.text
    grant = await db_session.get(CredentialServiceDelegation, response.json()["delegation_id"], populate_existing=True)
    return path, grant


@pytest.mark.asyncio
@pytest.mark.parametrize("service", list(WORKER_IDS))
async def test_owner_api_grant_reaches_setup_authorizer_and_revocation(test_client, db_session, work_fixture, l5_owner, service):
    fixture = work_fixture
    path, grant = await _grant(test_client, db_session, fixture, service)
    backend = _backend(db_session, l5_owner)
    configuration, version = fixture["configurations"][service]
    request = {"action": "create", "configuration_revision": 1}
    if service in {"finops", "transform"}:
        request["action"] = "link"
        request["native_resource_ref"] = (
            version.settings["transform"]["definition_ref"]
            if service == "transform"
            else {**fixture["reference"], "record_type": "finops_agent", "record_id": "selected-agent"}
        )
    operation = await backend.start_binding(fixture["product_id"], fixture["project_id"], service, request, "owner_setup_operation_001")
    assert operation.state == "accepted"
    work = await db_session.scalar(select(SetupWork).where(SetupWork.operation_id == operation.operation_id))
    work.lease_owner, work.fence, work.lease_until = "fixture-worker", 1, datetime.now(UTC) + timedelta(minutes=5)
    await db_session.commit()
    body = _body(grant, operation.operation_id, work.work_id, fixture["reference"])
    permit = await get_l5_work_authorizer(db_session)(grant, body)
    policy = json.loads(permit.session_policy)
    assert policy["Statement"] and permit.binding_generation == grant.binding_generation == 1
    assert permit.setup_verified["authorization"]["delegation_id"] == grant.id
    revoked = await test_client.delete(path + "/delegations/" + grant.id, headers={"If-Match": '"1"'})
    assert revoked.status_code == 200
    with pytest.raises(HTTPException) as denied:
        await get_l5_work_authorizer(db_session)(grant, body)
    assert denied.value.status_code == 409


async def _derived_run(test_client, db, fixture, owner, service):
    path, parent = await _grant(test_client, db, fixture, service)
    configuration, version = fixture["configurations"][service]
    backend = _backend(db, owner)
    _, setup = await backend.binding_authority(fixture["product_id"], fixture["project_id"], service)
    assert setup["authorization"]["delegation_id"] == parent.id
    identity = {"account_id": "123456789012", "role_arn": fixture["credential"].scopes["role_arn"], "region": "us-east-1"}
    native = {
        **fixture["reference"],
        "record_type": {
            "security": "security_space",
            "devops": "agent_space",
            "finops": "finops_agent",
            "well_architected": "agent_profile",
            "transform": "transform_definition",
        }[service],
        "record_id": "selected-resource",
    }
    if service == "well_architected":
        native.update(record_id="arn:aws:wellarchitected:us-east-1:123456789012:agent-profile/Selected", record_revision="a" * 64)
    elif service == "transform":
        native = version.settings["transform"]["definition_ref"]
    binding = Binding(
        tenant_id=owner.org_id,
        binding_id=str(uuid4()),
        configuration_id=configuration.configuration_id,
        product_id=fixture["product_id"],
        project_id=fixture["project_id"],
        service_id=service,
        scope=view(configuration, version)["scope"],
        revision=1,
        generation=1,
        state="ready",
        ownership="customer_linked",
        connection_ref=fixture["reference"],
        connection_key=service,
        native_resource_ref=native,
        provider_identity=identity,
        readiness=[],
        gaps=[],
    )
    admission = {
        "scope": binding.scope,
        "binding_generation": 1,
        "actor_id": owner.user_id,
        "connection_ref": fixture["reference"],
        "provider_identity": identity,
        "source_snapshots": [],
    }
    sources = version.settings["source_refs"]
    if service == "security":
        registration = {"service_role": "arn:aws:iam::123456789012:role/SourceReader", "bucket": "fictional-source-bucket"}
        binding.readiness = [{"kind": "code_review_source_registration", "observed_at": datetime.now(UTC).isoformat(), "registration": registration}]
        admission.update(native_space_ref=native, source_registration=registration)
    elif service == "devops":
        admission["native_space_ref"] = native
    elif service == "well_architected":
        binding.readiness = [{"snapshot_hash": native["record_revision"]}]
        admission.update(native_profile_ref=native, profile_hash=native["record_revision"])
    elif service == "finops":
        billing = {**version.settings["finops"]["billing_scope"], "scope_generation": 1}
        record = {
            "scope": binding.scope,
            "binding_generation": 1,
            "native_resource_ref": native,
            "provider_identity": identity,
            "native_agent_display_name": "Selected agent",
            "billing_scope": billing,
            "configuration_revision": 1,
        }
        binding.readiness = [{"capability": "cost_inquiry", "native_readback": record}]
        admission.update(native_agent_ref=native, native_agent_display_name="Selected agent", billing_scope=billing)
    else:
        from l5_services.adapters.transform.provider_job import NDJSON_BINARY_SHA256, NDJSON_INTERFACE

        admission["transform_settings"] = version.settings["transform"]
        admission["qualified_executor_ref"] = version.settings["transform"]["executor_binding_ref"]
        admission["transform_qualification"] = {
            "schema_version": "1.0",
            "interface": NDJSON_INTERFACE,
            "cli_version": "3.19.0",
            "binary_sha256": NDJSON_BINARY_SHA256,
            "image_digest": "sha256:" + "a" * 64,
            "definition_revision": "v1",
            "region_qualification": "qualified",
            "executor_qualification": "qualified",
            **{
                operation: {"status": "supported", "reason": "Offline fixture", "receipt_sha256": "b" * 64}
                for operation in ("tool_isolation", "submit", "read", "resume", "cancel")
            },
        }
    db.add(binding)
    await db.flush()
    child = await issue_l5_work_delegation(
        db,
        owner,
        parent,
        fixture["credential"],
        binding,
        fixture["reference"],
        sources,
        version.settings["capabilities"][0],
        600,
        "submit",
        "owner_api_derived_work_001",
    )
    assert child.parent_delegation_id == parent.id
    admission.update(
        admitted_at=datetime.now(UTC).isoformat(),
        delegation_id=child.id,
        delegation_version=child.revision,
        source_snapshots=[{"source_ref": source, "snapshot_id": str(uuid4())} for source in sources],
    )
    run = Run(
        tenant_id=owner.org_id,
        run_id=str(uuid4()),
        product_id=fixture["product_id"],
        project_id=fixture["project_id"],
        service_id=service,
        configuration_id=configuration.configuration_id,
        configuration_revision=1,
        binding_id=binding.binding_id,
        trigger_id="manual",
        binding_generation=1,
        input={"capability": version.settings["capabilities"][0], "sources": sources},
        admission=admission,
        deadline_at=datetime.now(UTC) + timedelta(minutes=10),
        state="queued",
    )
    work = Work(
        tenant_id=owner.org_id,
        work_id=str(uuid4()),
        run_id=run.run_id,
        kind="submit",
        generation=1,
        step=1,
        due_at=datetime.now(UTC),
        lease_owner="fixture-worker",
        lease_until=datetime.now(UTC) + timedelta(minutes=5),
        fence=1,
    )
    attempt = WorkerAttempt(
        tenant_id=owner.org_id,
        attempt_id=str(uuid4()),
        run_id=run.run_id,
        operation=TRANSFORM_SUBMIT_OPERATION if service == "transform" else "submit",
        first_work_id=work.work_id,
        first_fence=1,
        idempotency_token="a" * 64,
        input_hash="b" * 64,
        started_at=datetime.now(UTC),
        status="prepared",
    )
    db.add_all((run, work, attempt))
    await db.commit()
    return SimpleNamespace(
        path=path,
        parent=parent,
        child=child,
        run=run,
        work=work,
        binding=binding,
        attempt=attempt,
        body=_body(child, run.run_id, work.work_id, fixture["reference"]),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("service", list(WORKER_IDS))
async def test_owner_api_child_grant_reaches_real_work_policy_and_revocation(test_client, db_session, work_fixture, l5_owner, service):
    selected = await _derived_run(test_client, db_session, work_fixture, l5_owner, service)
    permit = await get_l5_work_authorizer(db_session)(selected.child, selected.body)
    assert json.loads(permit.session_policy)["Statement"]
    assert (permit.operation_id, permit.attempt_id, permit.lease_fence) == (selected.run.run_id, selected.work.work_id, 1)
    revoked = await test_client.delete(selected.path + "/delegations/" + selected.parent.id, headers={"If-Match": '"1"'})
    assert revoked.status_code == 200
    with pytest.raises(HTTPException) as denied:
        await get_l5_work_authorizer(db_session)(selected.child, selected.body)
    assert denied.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "stale_at_admission", "binding_changed", "missing_admission"])
async def test_security_continues_past_setup_freshness_only_with_original_pins(
    test_client,
    db_session,
    work_fixture,
    l5_owner,
    failure,
):
    selected = await _derived_run(test_client, db_session, work_fixture, l5_owner, "security")
    now = datetime.now(UTC)
    selected.binding.readiness = [{**selected.binding.readiness[0], "observed_at": (now - timedelta(minutes=25)).isoformat()}]
    selected.run.admission = {**selected.run.admission, "admitted_at": (now - timedelta(minutes=20)).isoformat()}
    if failure == "stale_at_admission":
        selected.run.admission = {**selected.run.admission, "admitted_at": now.isoformat()}
    elif failure == "binding_changed":
        selected.binding.generation += 1
    elif failure == "missing_admission":
        selected.run.admission = {key: value for key, value in selected.run.admission.items() if key != "admitted_at"}
    await db_session.commit()
    if failure:
        with pytest.raises(HTTPException) as denied:
            await get_l5_work_authorizer(db_session)(selected.child, selected.body)
        assert denied.value.status_code == 409
    else:
        permit = await get_l5_work_authorizer(db_session)(selected.child, selected.body)
        assert permit.operation_id == selected.run.run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("service", list(WORKER_IDS))
async def test_owner_grant_crosses_mounted_broker_with_real_work_verifier(test_client, db_session, work_fixture, l5_owner, app, monkeypatch, service):
    selected = await _derived_run(test_client, db_session, work_fixture, l5_owner, service)
    credential = work_fixture["credential"]
    worker = l5_owner.model_copy(
        update={
            "user_id": "iam-agent:" + WORKER_IDS[service],
            "agent_registry_id": WORKER_IDS[service],
            "org_id": "platform-service",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "credential_scopes": ["l5:credential-session"],
        }
    )

    async def verified(request: Request):
        request.state.token_context = worker

    app.dependency_overrides[verify_internal_or_irsa] = verified
    manager = Mock()
    manager.current_version_id.return_value = credential.aws_verified_version_id
    manager.get_secret_at_version.return_value = (
        json.dumps(
            {
                "role_arn": credential.scopes["role_arn"],
                "external_id": credential.aws_external_id,
                "account_id": "123456789012",
                "session_duration_seconds": 900,
                "default_region": "us-east-1",
            }
        ),
        credential.aws_verified_version_id,
    )
    app.dependency_overrides[get_secrets_manager] = lambda: manager
    assumed = Mock(
        return_value=AssumeRoleResult(
            access_key_id="fixture-key",
            secret_access_key="fixture-secret",
            session_token="fixture-token",
            expiration=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            region="us-east-1",
            profile_name="fixture",
        )
    )
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    monkeypatch.setattr("src.internal.assume_role_routes.assume_role", assumed)
    monkeypatch.setattr("src.internal.l5_credential_routes.verify_assumed_session", AsyncMock(return_value=selected.binding.provider_identity))
    response = await test_client.post("/internal/v1/l5/credential-session", json=selected.body.model_dump())
    assert response.status_code == 200, response.text
    assert response.json()["provider_identity"] == selected.binding.provider_identity
    policy = json.loads(assumed.call_args.kwargs["session_policy"])
    assert policy["Statement"] and assumed.call_count == 1
    if service == "transform":
        assert policy["Statement"][0]["Action"] == ["transform-custom:GetTransformationPackageUrl", "transform-custom:ExecuteTransformation"]
        assert policy["Statement"][0]["Resource"] == "arn:aws:transform-custom:us-east-1:123456789012:package/selected-package"
        assert policy["Statement"][1]["Action"] == ["transform-custom:ConverseStream"]
        assert policy["Statement"][1]["Resource"] == "*"
        for statement in policy["Statement"]:
            assert statement["Condition"]["StringEquals"] == {"aws:RequestedRegion": "us-east-1"}
            assert statement["Condition"]["DateLessThan"]["aws:CurrentTime"] == response.json()["expires_at"].replace("Z", "+00:00")
    worker = worker.model_copy(update={"agent_registry_id": WORKER_IDS["security" if service != "security" else "devops"]})
    foreign = await test_client.post("/internal/v1/l5/credential-session", json=selected.body.model_dump())
    assert foreign.status_code == 403 and assumed.call_count == 1
    worker = worker.model_copy(update={"agent_registry_id": WORKER_IDS[service]})
    revoked = await test_client.delete(selected.path + "/delegations/" + selected.parent.id, headers={"If-Match": '"1"'})
    assert revoked.status_code == 200
    denied = await test_client.post("/internal/v1/l5/credential-session", json=selected.body.model_dump())
    assert denied.status_code == 403 and assumed.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source_revoked", "source_snapshot", "definition", "region", "qualification", "attempt"])
async def test_transform_owner_grant_cannot_widen_durable_work(test_client, db_session, work_fixture, l5_owner, change):
    selected = await _derived_run(test_client, db_session, work_fixture, l5_owner, "transform")
    if change == "source_revoked":
        source = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.project_id == selected.run.project_id))
        source.status = "revoked"
    elif change == "source_snapshot":
        selected.run.admission = {**selected.run.admission, "source_snapshots": []}
    elif change in {"definition", "region"}:
        transform = dict(selected.run.admission["transform_settings"])
        if change == "definition":
            transform["definition_ref"] = {**transform["definition_ref"], "record_id": "foreign-package"}
        else:
            transform["region"] = "us-west-2"
        selected.run.admission = {**selected.run.admission, "transform_settings": transform}
    elif change == "qualification":
        qualification = {**selected.run.admission["transform_qualification"], "region_qualification": "unqualified"}
        selected.run.admission = {**selected.run.admission, "transform_qualification": qualification}
    else:
        selected.attempt.operation = "submit"
    await db_session.commit()
    with pytest.raises(HTTPException) as denied:
        await get_l5_work_authorizer(db_session)(selected.child, selected.body)
    assert denied.value.status_code == 409
