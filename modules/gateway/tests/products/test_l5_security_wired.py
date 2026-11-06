"""Security admission uses Product, setup, run and delegation rows without allow callbacks."""

import asyncio
import io
import json
import subprocess
import sys
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest
from fastapi import HTTPException, Request
from l5_services import runtime
from l5_services.adapters.security.binding import SecurityBindingAdapter
from l5_services.api.bindings import _intent_hash
from l5_services.api.configuration import view
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.base import Base as L5Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.evidence import AssessmentResult
from l5_services.models.preparation import AdmissionPreparation
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run
from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork
from l5_services.models.work import Work
from l5_services.product_client import ProductClient
from l5_services.security_registration import current_model, current_registration
from l5_services.source_manifest import materialize_inputs
from l5_services.workers.execution import AuthorityRevoked, load_service
from l5_services.workers.relay import repair_once
from sqlalchemy import delete, select

from src.auth.aws_connection_authority import connection_binding
from src.internal import assume_role_routes
from src.internal.assume_role_routes import get_secrets_manager
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.l5_credential_routes import ConnectionRef, CredentialSessionRequest, get_l5_work_authorizer
from src.internal.sts_assume_service import AssumeRoleResult
from src.l5_integration import GatewayConfigurationBackend
from src.products.models import ProductOperation, ProductProjectBinding, ProductSourceBinding, ProductSourceSnapshot
from src.products.source_capture import SnapshotObjects, get_snapshot_objects
from src.products.source_capture_jobs import claim_capture, run_capture
from src.products.source_snapshots import ArtifactRef, CapturedSource
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation, UserCredential
from tests.products.test_i01 import HEADERS, REQUEST


def _archive_runtime(tmp_path):
    module_root = Path(__file__).resolve().parents[4] / "modules/L5-services"
    wheel_output = subprocess.run(
        [str(module_root / "scripts/build-package.sh"), str(tmp_path / "package")], check=True, capture_output=True, text=True
    )
    wheel = Path(wheel_output.stdout.strip().splitlines()[-1])
    workers_zip = tmp_path / "workers.zip"
    subprocess.run([str(module_root / "scripts/build-workers.sh"), str(wheel), str(workers_zip)], check=True, capture_output=True, text=True)
    with zipfile.ZipFile(workers_zip) as installed:
        factory_source = installed.read("l5_services/runtime.py")
        assert {
            "l5_services/adapters/security/code_review.py",
            "l5_services/adapters/security/threat_model.py",
            "l5_services/workers/execution.py",
        } <= set(installed.namelist())
    archive_runtime = ModuleType("l5_services.archive_runtime")
    exec(compile(factory_source, str(workers_zip) + "/l5_services/runtime.py", "exec"), archive_runtime.__dict__)
    return archive_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_boundary",
    [
        "accepted",
        "draft_selection",
        "revoked",
        "foreign_product",
        "foreign_connection",
        "expired_grant",
        "expired_lease",
        "stale_fence",
        "revoked_after_session",
        "expired_after_session",
        "stale_after_session",
        "binding_revoked_after_session",
        "credential_stale",
        "threat_model",
        "threat_model_binding_revoked",
    ],
)
async def test_mounted_security_code_review_admission(
    test_client, test_engine, db_session, app, source_member, monkeypatch, provider_boundary, tmp_path
):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    tenant_id, owner_id = source_member.org_id, source_member.user_id
    now = datetime.now(UTC)
    source_sha = "a" * 40
    is_threat = provider_boundary.startswith("threat_model")
    mapping = ChannelTenantMap(org_id=tenant_id, provider="github", provider_scope_id="example-team", installation_id="17", ownership_disputed=False)
    credential = UserCredential(
        org_id=tenant_id,
        user_id=owner_id,
        agent_consent=True,
        service="aws",
        credential_type="aws_role",
        label="offline-role",
        secret_arn="offline-secret",
        aws_external_id="offline-external-id",
        aws_verified_at=now,
        aws_verification_attempt="verification-one",
        aws_verified_version_id="version-one",
        scopes={"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Selected", "source": "imported_role", "status": "verified"},
    )
    db_session.add_all([mapping, credential])
    await db_session.flush()
    credential.aws_verified_binding = connection_binding(credential)
    source = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "source_connection_id": mapping.id,
        "record_type": "document" if is_threat else "repository_commit",
        "record_id": "repo-one:ARCHITECTURE.md" if is_threat else "repo-one",
        "record_revision": source_sha,
    }
    draft_source = {**source, "record_type": "repository", "record_revision": "discovery-v1"} if provider_boundary == "draft_selection" else source
    connection_ref = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "source_connection_id": credential.id,
        "record_type": "connection",
        "record_id": credential.id,
        "record_revision": "version-one",
    }
    db_session.add(
        ProductSourceBinding(
            tenant_id=tenant_id,
            project_id=project_id,
            kind="document" if is_threat else "repository",
            provider="github",
            installation_id=mapping.id,
            namespace="example-team",
            native_id=source["record_id"],
            desired={"source_revision": source_sha},
            observed={"source_revision": source_sha},
            status="healthy",
        )
    )
    db_session.add(
        ProductSourceBinding(
            tenant_id=tenant_id,
            project_id=project_id,
            credential_id=credential.id,
            kind="environment",
            provider="aws",
            namespace="selected-account",
            native_id="us-east-1",
            desired={"region": "us-east-1"},
            observed={"source_revision": "version-one"},
            status="healthy",
        )
    )
    relationship = await db_session.scalar(
        select(ProductProjectBinding).where(
            ProductProjectBinding.product_id == product_id, ProductProjectBinding.project_id == project_id, ProductProjectBinding.valid_to.is_(None)
        )
    )
    configuration = Configuration(
        tenant_id=tenant_id,
        configuration_id="security-config",
        product_id=product_id,
        project_id=project_id,
        service_id="security",
        owner_id=owner_id,
        relationship_revision=str(relationship.relationship_revision),
        current_revision=1,
        handoff_state="linked",
    )
    trigger = {
        "trigger_id": "manual-review",
        "kind": "manual",
        "enabled": True,
        "capability": "threat_model" if is_threat else "code_review",
        "binding_id": "security-space",
        "source_refs": [draft_source],
    }
    settings = {
        "service_id": "security",
        "enabled": True,
        "capabilities": [trigger["capability"]],
        "connection_ref": connection_ref,
        "source_refs": [draft_source],
        "profile_ref": source,
        "triggers": [trigger],
        "region": "us-east-1",
        "purpose": "Review",
        "classification": "internal",
        "visibility": "only_me",
        "limits": {
            "deadline_seconds": 3600,
            "max_provider_attempts": 1,
            "max_polls": 30,
            "concurrency": 1,
            "max_cost": "1.00",
            "currency": "USD",
            "policy_ref": {**connection_ref, "record_type": "policy", "record_id": "example-budget"},
        },
    }
    version = ConfigurationVersion(
        tenant_id=tenant_id,
        configuration_id="security-config",
        revision=1,
        settings=settings,
        inherited_fields=[],
        gaps=["execution_delegation"],
        state="needs_input",
        hash="b" * 64,
    )
    scope = view(configuration, version)["scope"]
    native_ref = {**connection_ref, "record_type": "security_space", "record_id": "space-one", "record_revision": now.isoformat()}
    identity = {"account_id": "123456789012", "role_arn": credential.scopes["role_arn"], "region": "us-east-1", "verified_at": now.isoformat()}

    class Native:
        def batch_get_agent_spaces(self, **kwargs):
            assert kwargs["agentSpaceIds"] == ["space-one"]
            return {
                "agentSpaces": [
                    {
                        "agentSpaceId": "space-one",
                        "name": "adp-setup-one",
                        "description": "ADP setup " + "f" * 64,
                        "createdAt": now,
                        "awsResources": {
                            "iamRoles": ["arn:aws:iam::123456789012:role/SourceReader"],
                            "s3Buckets": ["arn:aws:s3:::example-source-staging"],
                        },
                    }
                ]
            }

        def list_threat_models(self, **kwargs):
            return {"threatModelSummaries": [{"title": model["title"], "threatModelId": model_id} for model_id, model in self.models.items()]}

        def create_threat_model(self, **kwargs):
            self.models["model-one"] = {
                "title": kwargs["title"],
                "description": kwargs["description"],
                "agentSpaceId": kwargs["agentSpaceId"],
                "serviceRole": kwargs["serviceRole"],
                "scopeDocs": kwargs["scopeDocs"],
                "threatModelId": "model-one",
                "createdAt": now,
            }
            return {"threatModelId": "model-one"}

        def batch_get_threat_models(self, **kwargs):
            return {"threatModels": [self.models[model_id].copy() for model_id in kwargs["threatModelIds"]]}

        def add_artifact(self, **kwargs):
            assert kwargs["agentSpaceId"] == "space-one" and kwargs["artifactType"] == "MD"
            self.artifact_adds += 1
            self.document_bytes = kwargs["artifactContent"]
            return {"artifactId": "artifact-one"}

        def update_threat_model(self, **kwargs):
            assert kwargs["threatModelId"] == "model-one"
            assert kwargs["serviceRole"] == "arn:aws:iam::123456789012:role/SourceReader"
            self.models["model-one"]["scopeDocs"] = kwargs["scopeDocs"]

        def start_threat_model_job(self, **kwargs):
            assert kwargs == {"agentSpaceId": "space-one", "threatModelId": "model-one"}
            self.starts += 1
            return {"threatModelJobId": "job-one"}

        def batch_get_threat_model_jobs(self, **kwargs):
            assert kwargs["threatModelJobIds"] == ["job-one"]
            return {
                "threatModelJobs": [
                    {
                        "agentSpaceId": "space-one",
                        "threatModelId": "model-one",
                        "threatModelJobId": "job-one",
                        "scopeDocs": self.models["model-one"]["scopeDocs"],
                        "status": "COMPLETED",
                        "createdAt": now,
                    }
                ]
            }

        def list_threat_model_job_tasks(self, **kwargs):
            return {
                "threatModelJobTaskSummaries": [
                    {
                        "taskId": "task-one",
                        "agentSpaceId": "space-one",
                        "threatModelId": "model-one",
                        "threatModelJobId": "job-one",
                        "executionStatus": "COMPLETED",
                        "updatedAt": now,
                    }
                ]
            }

        def list_threats(self, **kwargs):
            return {
                "threats": [
                    {
                        "threatId": "threat-one",
                        "threatJobId": "job-one",
                        "title": "Check access",
                        "statement": "Document access",
                        "severity": "HIGH",
                        "recommendation": "Restrict readers",
                        "evidence": [{"path": "ARCHITECTURE.md"}],
                    }
                ]
            }

        def batch_get_threats(self, **kwargs):
            return {"threats": self.list_threats()["threats"]}

    native = Native()
    native.models = {}
    native.artifact_adds = 0
    native.starts = 0
    readback = await SecurityBindingAdapter(native, identity).read(native_ref, identity)
    binding = Binding(
        tenant_id=tenant_id,
        binding_id="security-space",
        configuration_id=configuration.configuration_id,
        product_id=product_id,
        project_id=project_id,
        service_id="security",
        scope=scope,
        revision=1,
        generation=2,
        state=readback["state"],
        ownership="adp_created" if is_threat else "managed",
        creation_receipt={"operation_id": "setup-one", "intent_hash": "f" * 64, "native_resource_ref": native_ref, "provider_identity": identity}
        if is_threat
        else None,
        connection_ref=connection_ref,
        connection_key="c" * 64,
        native_resource_ref=native_ref,
        native_key="d" * 64,
        provider_identity=identity,
        readiness=readback["readiness"],
        gaps=readback["gaps"],
    )
    if is_threat:
        scope_document = AsyncMock(
            return_value={
                "snapshot": {"source_ref": source, "content": {"media_type": "text/markdown"}},
                "content": b"# Architecture\n\nPrivate system overview.\n",
            }
        )
        readback = await SecurityBindingAdapter(native, identity, scope_document=scope_document).read(native_ref, identity, binding=binding)
        scope_document.assert_awaited_once()
        assert native.models["model-one"]["scopeDocs"] == [{"artifactId": "artifact-one"}]
        binding.readiness = readback["readiness"]
        assert current_model(binding)["record_id"] == "model-one"
    template = CredentialServiceDelegation(
        tenant_id=tenant_id,
        credential_id=credential.id,
        service_id="l5-worker",
        product_id=product_id,
        project_id=project_id,
        created_by=owner_id,
        operations=["product:read", "source:pin", "source:read", "credential:session"],
        sources=[{**connection_ref, "record_type": "credential"}, source],
        capabilities=[trigger["capability"]],
        visibility="only_me",
        revision=1,
        binding_generation=2,
        expires_at=now + timedelta(hours=3),
    )
    db_session.add_all([configuration, version, binding, template])
    await db_session.commit()

    async def owned(*args, **kwargs):
        return None

    monkeypatch.setattr("src.products.source_routes.assert_installation_owned_by", owned)
    monkeypatch.setattr("src.product_onboarding.source_discovery.assert_installation_owned_by", owned)
    source_file = b"# Architecture\n\nPrivate system overview.\n" if is_threat else b"def checked(): return True\n"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("src/main.py", source_file)
    source_bytes = source_file if is_threat else archive.getvalue()
    listing = json.dumps(
        {
            "source_ref": source,
            "exclusions": [],
            "files": [
                {
                    "source_ref": source,
                    "path": "ARCHITECTURE.md" if is_threat else "src/main.py",
                    "sha256": sha256(source_file).hexdigest(),
                    "bytes": len(source_file),
                }
            ],
        }
    ).encode()
    content = ArtifactRef(
        artifact_id="products/source-snapshots/offline-content",
        version="v1",
        media_type="text/markdown" if is_threat else "application/zip",
        bytes=len(source_bytes),
        sha256=sha256(source_bytes).hexdigest(),
    )
    manifest = ArtifactRef(
        artifact_id="products/source-snapshots/offline-manifest",
        version="v1",
        media_type="application/json",
        bytes=len(listing),
        sha256=sha256(listing).hexdigest(),
    )
    objects = SnapshotObjects(MagicMock(), "fictional-snapshot-bucket")

    def snapshot_object(**kwargs):
        artifact, payload = {
            content.artifact_id: (content, source_bytes),
            manifest.artifact_id: (manifest, listing),
        }[kwargs["Key"]]
        assert kwargs["Bucket"] == objects.bucket and kwargs["VersionId"] == artifact.version
        response = {"VersionId": artifact.version, "Metadata": {"sha256": artifact.sha256}}
        if "Range" in kwargs:
            assert kwargs["Range"].startswith("bytes=")
            start, end = map(int, kwargs["Range"].removeprefix("bytes=").split("-"))
            assert 0 <= start <= end < len(payload) and end - start + 1 <= 1024 * 1024
            response["ContentRange"] = f"bytes {start}-{end}/{len(payload)}"
            payload = payload[start : end + 1]
        return {**response, "Body": io.BytesIO(payload), "ContentLength": len(payload)}

    objects.s3.get_object.side_effect = snapshot_object
    app.dependency_overrides[get_snapshot_objects] = lambda: objects
    capture = AsyncMock(
        side_effect=lambda selected, mapping, body, objects: CapturedSource(
            source_ref=body.source_ref,
            exclusions=body.exclusions,
            file_count=1,
            expires_at=body.retain_until + timedelta(minutes=1),
            content=content,
            file_manifest=manifest,
        )
    )
    monkeypatch.setattr("src.products.l5_admission_capture.capture_source", capture)
    monkeypatch.setattr("src.products.l5_admission_capture.get_snapshot_objects", lambda: objects)
    admission_publish = AsyncMock()
    monkeypatch.setattr("src.l5_queue_registry.publisher", lambda service_id: admission_publish)
    backend = GatewayConfigurationBackend(db_session, source_member, publish=admission_publish)
    run_input = {
        "service_id": "security",
        "configuration_revision": 1,
        "binding_id": "security-space",
        "capability": trigger["capability"],
        "sources": [source],
    }
    if not is_threat:
        with pytest.raises(HTTPException) as preparing:
            await backend.submit(product_id, project_id, "security", run_input, "review-once")
        assert preparing.value.status_code == 503
        assert preparing.value.detail["code"] == "source_capture_preparing"
        preparation_id = preparing.value.detail["preparation_id"]
        async with backend.sessions() as independent:
            queued = await independent.get(ProductOperation, (tenant_id, preparation_id))
            assert queued.state == "pending" and queued.receipt == {"stage": "queued"}
            assert queued.request["admission_key"] == "review-once" and queued.request["run_input"] == run_input
            assert await independent.scalar(select(Run).where(Run.product_id == product_id)) is None
            assert await independent.scalar(select(Work)) is None
        capture.assert_not_awaited()
        admission_publish.assert_not_awaited()
        capture_identity = await claim_capture(backend.sessions)
        assert capture_identity[:2] == (tenant_id, preparation_id)
        await run_capture(backend.sessions, capture_identity)
        async with backend.sessions() as independent:
            finished = await independent.get(ProductOperation, (tenant_id, preparation_id))
            assert finished.state == "succeeded", finished.receipt
            assert finished.attempt_count == 1 and finished.lease_owner is None and finished.lease_until is None
            continued = await independent.get(RunReceipt, (tenant_id, product_id, project_id, "security", owner_id, "submit", "review-once"))
            assert continued is not None
        admission_publish.assert_awaited_once()
    run = await backend.submit(product_id, project_id, "security", run_input, "review-once")
    capture.assert_awaited_once()
    admission_publish.assert_awaited_once()
    async with backend.sessions() as independent:
        durable = await independent.get(Run, (tenant_id, run.run_id))
        work = await independent.scalar(select(Work).where(Work.run_id == run.run_id))
        snapshot = await independent.scalar(
            select(ProductSourceSnapshot).where(ProductSourceSnapshot.delegation_id == durable.admission["delegation_id"])
        )
        receipt = await independent.get(RunReceipt, (tenant_id, product_id, project_id, "security", owner_id, "submit", "review-once"))
        preparation = await independent.get(AdmissionPreparation, (tenant_id, product_id, project_id, "security", owner_id, "submit", "review-once"))
        assert work is not None and snapshot is not None and receipt is not None and preparation is not None
        if is_threat:
            assert durable.admission["native_model_ref"] == current_model(binding)
        else:
            assert durable.admission["source_registration"]["bucket"] == "example-source-staging"
        assert durable.admission["source_snapshots"][0]["snapshot_id"] == snapshot.id
        child = await independent.get(CredentialServiceDelegation, durable.admission["delegation_id"])
        assert child.parent_delegation_id == template.id and child.operation_id is None
    work = await db_session.get(Work, (tenant_id, work.work_id))
    work.lease_owner = "offline-worker"
    work.lease_until = datetime.now(UTC) + timedelta(seconds=45)
    work.fence = 1
    await db_session.commit()
    child = await db_session.get(CredentialServiceDelegation, run.admission["delegation_id"])
    request = CredentialSessionRequest(
        delegation_id=child.id,
        delegation_revision=child.revision,
        operation_id=run.run_id,
        attempt_id=work.work_id,
        connection_ref=ConnectionRef.model_validate(source),
        lease_owner=work.lease_owner,
        lease_fence=work.fence,
    )
    permit = await get_l5_work_authorizer(db_session)(child, request)
    assert permit.admitted_source_snapshot_ids == frozenset({snapshot.id})
    assert (child.operation_id, child.attempt_id, child.lease_fence) == (run.run_id, work.work_id, 1)
    db_session.add(
        WorkerAttempt(
            tenant_id=tenant_id,
            attempt_id="offline-attempt",
            run_id=run.run_id,
            operation="submit",
            first_work_id=work.work_id,
            first_fence=work.fence,
            idempotency_token="c" * 64,
            input_hash="d" * 64,
            started_at=now,
            status="prepared",
        )
    )
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

    async def authenticated(request: Request):
        request.state.token_context = service

    app.dependency_overrides[verify_internal_or_irsa] = authenticated
    manager = MagicMock()
    manager.current_version_id.return_value = "version-one"
    manager.get_secret_at_version.return_value = (
        json.dumps(
            {
                "role_arn": credential.scopes["role_arn"],
                "external_id": "offline-external-id",
                "account_id": "123456789012",
                "default_region": "us-east-1",
            }
        ),
        "version-one",
    )
    app.dependency_overrides[get_secrets_manager] = lambda: manager
    assumed = Mock(
        return_value=AssumeRoleResult(
            access_key_id="offline-access",
            secret_access_key="offline-secret",
            session_token="offline-token",
            expiration=(datetime.now(UTC) + timedelta(seconds=900)).isoformat(),
            region="us-east-1",
            profile_name="offline-profile",
        )
    )
    monkeypatch.setattr(assume_role_routes, "assume_role", assumed)
    monkeypatch.setattr("src.internal.l5_credential_routes.verify_assumed_session", AsyncMock(return_value=identity))
    credential_request = request.model_copy(
        update={
            "connection_ref": ConnectionRef.model_validate({**connection_ref, "record_type": "credential"}),
            "lease_owner": work.lease_owner,
            "lease_fence": work.fence,
        }
    )
    foreign_owner = credential_request.model_copy(update={"lease_owner": "foreign-worker"})
    denied = await test_client.post("/internal/v1/l5/credential-session", json=foreign_owner.model_dump())
    assert denied.status_code == 409 and assumed.call_count == 0
    delivered = await test_client.post("/internal/v1/l5/credential-session", json=credential_request.model_dump())
    assert delivered.status_code == 200, delivered.text
    lease_end = work.lease_until.replace(tzinfo=UTC)
    assert datetime.fromisoformat(delivered.json()["expires_at"].replace("Z", "+00:00")) <= lease_end
    assert assumed.call_args.kwargs["session_duration_seconds"] == 900
    policy = json.loads(assumed.call_args.kwargs["session_policy"])
    if is_threat:
        assert len(policy["Statement"]) == 2
        assert policy["Statement"][1]["Action"] == ["iam:PassRole"]
        assert policy["Statement"][1]["Resource"] == "arn:aws:iam::123456789012:role/SourceReader"
        assert policy["Statement"][1]["Condition"]["StringEquals"] == {"iam:PassedToService": "securityagent.amazonaws.com"}
        assert "securityagent:StartThreatModelJob" in policy["Statement"][0]["Action"]
        assert "securityagent:CreateCodeReview" not in policy["Statement"][0]["Action"]
        assert "securityagent:CreateThreatModel" not in policy["Statement"][0]["Action"]
        assert policy["Statement"][0]["Resource"] == "arn:aws:securityagent:us-east-1:123456789012:agent-space/space-one"
        source_client = ProductClient(test_client, child.id)
        source_request = {
            "delegation_id": child.id,
            "delegation_revision": child.revision,
            "operation_id": run.run_id,
            "attempt_id": work.work_id,
            "lease_fence": work.fence,
            "lease_owner": work.lease_owner,
        }
        assert await source_client.read_source(product_id, project_id, snapshot.id, source_request) == run.admission["source_snapshots"][0]
        assert await source_client.read_source_manifest(product_id, project_id, snapshot.id, source_request) == listing
        assert b"".join([chunk async for chunk in source_client.stream_source(product_id, project_id, snapshot.id, source_request)]) == source_bytes
        binding.readiness = binding.readiness[:1]
        await db_session.commit()
        revoked = await test_client.post("/internal/v1/l5/credential-session", json=credential_request.model_dump())
        assert revoked.status_code == 409 and assumed.call_count == 1
        binding.readiness = readback["readiness"]
        work.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.execute(delete(WorkerAttempt).where(WorkerAttempt.attempt_id == "offline-attempt"))
        await db_session.commit()
        delivery = [{"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}]

        async def publish(envelope, delay):
            delivery.append(envelope)

        archive_runtime = _archive_runtime(tmp_path)
        monkeypatch.setitem(sys.modules, archive_runtime.__name__, archive_runtime)
        monkeypatch.setenv("L5_WORKER_FACTORY", archive_runtime.__name__ + ":factory")
        monkeypatch.setenv("L5_SERVICE_ID", "security")
        monkeypatch.setattr(archive_runtime, "_gateway_transport", lambda: test_client)
        monkeypatch.setattr(archive_runtime, "_sessions", lambda: backend.sessions)
        monkeypatch.setattr(
            archive_runtime, "_archive_store", lambda: SimpleNamespace(put_archive=lambda *args: None, get_archive=lambda *args: None)
        )
        monkeypatch.setattr(archive_runtime, "_queue_publisher", lambda: publish)
        monkeypatch.setattr(
            archive_runtime,
            "_provider_session",
            lambda receipt, region: SimpleNamespace(client=lambda service, region_name: native if service == "securityagent" else None),
        )
        worker = load_service()
        clock = {"now": datetime.now(UTC)}
        worker.clock = lambda: clock["now"]
        worker.security_threat_adapter.clock = lambda: clock["now"]
        if provider_boundary == "threat_model_binding_revoked":
            current_loop = asyncio.get_running_loop()
            original_model_read = native.batch_get_threat_models

            async def revoke_binding():
                async with backend.sessions() as independent:
                    current = await independent.get(Binding, (tenant_id, binding.binding_id))
                    current.readiness = []
                    await independent.commit()

            def revoke_after_read(**kwargs):
                result = original_model_read(**kwargs)
                asyncio.run_coroutine_threadsafe(revoke_binding(), current_loop).result(timeout=10)
                return result

            native.batch_get_threat_models = revoke_after_read
            assert await worker.execute(delivery[0])
            assert await worker.execute(delivery[1])
            async with backend.sessions() as independent:
                retained = await independent.get(Run, (tenant_id, run.run_id))
                results = (await independent.scalars(select(AssessmentResult).where(AssessmentResult.run_id == run.run_id))).all()
                attempts = (await independent.scalars(select(WorkerAttempt).where(WorkerAttempt.run_id == run.run_id))).all()
            assert retained.state == "blocked" and retained.native_job_ref is None
            assert len(attempts) == 1 and attempts[0].status == "prepared"
            assert not results and native.artifact_adds == 1 and native.starts == 0
            assert not await worker.execute(delivery[1])
            return
        for step in range(4):
            assert await worker.execute(delivery[step])
            if step == 1:
                clock["now"] += timedelta(minutes=10)
        async with backend.sessions() as independent:
            completed = await independent.get(Run, (tenant_id, run.run_id))
            results = (await independent.scalars(select(AssessmentResult).where(AssessmentResult.run_id == run.run_id))).all()
        assert completed.state == "succeeded" and native.starts == 1 and native.artifact_adds == 2
        assert native.document_bytes == source_bytes
        assert results[0].payload["findings"][0]["source_refs"] == [source]
        assert not await worker.execute(delivery[1]) and native.starts == 1
        model_setup = SetupIntent(
            tenant_id=tenant_id,
            operation_id="model-check-operation",
            product_id=product_id,
            project_id=project_id,
            service_id="security",
            actor_id=owner_id,
            idempotency_key="model-check-key",
            configuration_id=configuration.configuration_id,
            scope=scope,
            request={"action": "check", "configuration_revision": 1, "binding_id": binding.binding_id},
            binding_generation=binding.generation,
            authorization={
                "actor_id": owner_id,
                "delegation_id": template.id,
                "delegation_revision": template.revision,
                "authorized_at": now.isoformat(),
            },
            intent_hash="",
        )
        model_setup.intent_hash = _intent_hash(model_setup)
        model_work = SetupWork(
            tenant_id=tenant_id,
            work_id="model-check-work",
            operation_id=model_setup.operation_id,
            generation=1,
            due_at=now,
            lease_owner="model-check-worker",
            fence=1,
            lease_until=datetime.now(UTC) + timedelta(seconds=45),
        )
        db_session.add_all(
            [
                model_setup,
                model_work,
                BindingOperation(
                    tenant_id=tenant_id,
                    operation_id=model_setup.operation_id,
                    binding_id=binding.binding_id,
                    action="check",
                    state="accepted",
                    revision=1,
                    gaps=[],
                    attempts=0,
                ),
            ]
        )
        await db_session.commit()
        model_request = credential_request.model_copy(
            update={
                "delegation_id": template.id,
                "delegation_revision": template.revision,
                "operation_id": model_setup.operation_id,
                "attempt_id": model_work.work_id,
                "lease_owner": model_work.lease_owner,
                "lease_fence": model_work.fence,
            }
        )
        model_session = await test_client.post("/internal/v1/l5/credential-session", json=model_request.model_dump())
        assert model_session.status_code == 200, model_session.text
        model_policy = json.loads(assumed.call_args.kwargs["session_policy"])["Statement"]
        assert len(model_policy) == 2
        assert model_policy[1]["Action"] == ["iam:PassRole"]
        assert model_policy[1]["Resource"] == "arn:aws:iam::123456789012:role/SourceReader"
        assert model_policy[1]["Condition"]["StringEquals"] == {"iam:PassedToService": "securityagent.amazonaws.com"}
        assert model_policy[0]["Resource"] == "arn:aws:securityagent:us-east-1:123456789012:agent-space/space-one"
        assert {"securityagent:CreateThreatModel", "securityagent:ListThreatModels", "securityagent:BatchGetThreatModels"} <= set(
            model_policy[0]["Action"]
        )
        assert "securityagent:CreateAgentSpace" not in model_policy[0]["Action"]
        original_capabilities = template.capabilities
        assumed_count = assumed.call_count
        for capabilities in (["cost_inquiry"], ["architecture_review"], []):
            template.capabilities = capabilities
            await db_session.commit()
            denied_setup = await test_client.post("/internal/v1/l5/credential-session", json=model_request.model_dump())
            assert denied_setup.status_code == 409, denied_setup.text
            assert assumed.call_count == assumed_count
        template.capabilities = original_capabilities
        await db_session.commit()
        restored_setup = await test_client.post("/internal/v1/l5/credential-session", json=model_request.model_dump())
        assert restored_setup.status_code == 200, restored_setup.text
        return
    assert policy["Statement"][0]["Resource"] == "arn:aws:securityagent:us-east-1:123456789012:agent-space/space-one"
    assert policy["Statement"][1]["Resource"] == "arn:aws:s3:::example-source-staging/security-agent/source/*"
    assert all(statement["Condition"]["DateLessThan"]["aws:CurrentTime"] == lease_end.isoformat() for statement in policy["Statement"])
    replay = await backend.submit(product_id, project_id, "security", run_input, "review-once")
    assert replay.run_id == run.run_id and capture.await_count == 1
    work.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    expired = await test_client.post("/internal/v1/l5/credential-session", json=credential_request.model_dump())
    assert expired.status_code == 409 and assumed.call_count == 1
    work.lease_owner = "replacement-worker"
    work.lease_until = datetime.now(UTC) + timedelta(seconds=45)
    work.fence = 2
    await db_session.commit()
    stale = await test_client.post("/internal/v1/l5/credential-session", json=credential_request.model_dump())
    assert stale.status_code == 409 and assumed.call_count == 1
    renewed_request = credential_request.model_copy(update={"lease_owner": work.lease_owner, "lease_fence": work.fence})
    renewed = await test_client.post("/internal/v1/l5/credential-session", json=renewed_request.model_dump())
    assert renewed.status_code == 200, renewed.text
    await db_session.refresh(child)
    assert assumed.call_count == 2 and child.lease_fence == 2
    assert json.loads(assumed.call_args.kwargs["session_policy"])["Statement"][0]["Condition"]["DateLessThan"] == {
        "aws:CurrentTime": work.lease_until.replace(tzinfo=UTC).isoformat()
    }
    assert run.admission["source_snapshots"][0]["snapshot_id"] == snapshot.id
    source_client = ProductClient(test_client, child.id)
    source_request = {
        "delegation_id": child.id,
        "delegation_revision": child.revision,
        "operation_id": run.run_id,
        "attempt_id": work.work_id,
        "lease_fence": work.fence,
        "lease_owner": work.lease_owner,
    }
    assert await source_client.read_source(product_id, project_id, snapshot.id, source_request) == run.admission["source_snapshots"][0]
    assert await source_client.read_source_manifest(product_id, project_id, snapshot.id, source_request) == listing
    received = b"".join([chunk async for chunk in source_client.stream_source(product_id, project_id, snapshot.id, source_request)])
    assert received == source_bytes
    work.lease_owner = "later-worker"
    work.lease_until = datetime.now(UTC) + timedelta(seconds=45)
    work.fence = 3
    await db_session.commit()
    with pytest.raises(httpx.HTTPStatusError) as old_source:
        await source_client.read_source(product_id, project_id, snapshot.id, source_request)
    assert old_source.value.response.status_code == 409
    next_request = {**source_request, "lease_owner": work.lease_owner, "lease_fence": work.fence}
    assert await source_client.read_source(product_id, project_id, snapshot.id, next_request) == run.admission["source_snapshots"][0]
    await db_session.refresh(child)
    assert child.lease_fence == 3 and run.admission["source_snapshots"][0]["snapshot_id"] == snapshot.id

    class OfflineArchiveStore:
        content = None

        async def put_archive(self, *_args):
            raise AssertionError("code review must upload its archive from disk")

        async def get_archive(self, *_args):
            raise AssertionError("code review must read its archive from disk")

        async def put_archive_file(self, run_id, path):
            assert run_id == run.run_id and path.is_file()
            self.content = path.read_bytes()
            self.path = tmp_path / "retained-worker-archive.zip"
            self.path.write_bytes(self.content)
            return {
                "artifact_id": "offline-worker-archive",
                "version": "v1",
                "sha256": sha256(self.content).hexdigest(),
                "media_type": "application/zip",
                "bytes": len(self.content),
            }

        @asynccontextmanager
        async def get_archive_file(self, run_id, reference):
            assert run_id == run.run_id and reference["sha256"] == sha256(self.content).hexdigest()
            yield self.path

    archive_store = OfflineArchiveStore()
    materialized, inputs = await materialize_inputs(
        run,
        SimpleNamespace(work_id=work.work_id, fence=work.fence, owner=work.lease_owner),
        source_client,
        archive_store=archive_store,
        now=datetime.now(UTC),
    )
    assert inputs == [] and archive_store.content == source_bytes
    assert materialized["source_snapshot_ids"] == [snapshot.id]
    assert materialized["files"][0]["sha256"] == sha256(source_file).hexdigest()
    followup = Work(
        tenant_id=tenant_id,
        work_id="followup-work",
        run_id=run.run_id,
        kind="submit",
        generation=1,
        step=1,
        due_at=datetime.now(UTC),
        lease_owner="followup-worker",
        lease_until=datetime.now(UTC) + timedelta(seconds=45),
        fence=1,
    )
    db_session.add(followup)
    await db_session.commit()
    followup_request = {**next_request, "attempt_id": followup.work_id, "lease_owner": followup.lease_owner, "lease_fence": followup.fence}
    with pytest.raises(httpx.HTTPStatusError) as overlapping:
        await source_client.read_source(product_id, project_id, snapshot.id, followup_request)
    assert overlapping.value.response.status_code == 409 and child.attempt_id == work.work_id
    work.completed_at = datetime.now(UTC)
    await db_session.commit()
    with pytest.raises(httpx.HTTPStatusError) as completed_source:
        await source_client.read_source(product_id, project_id, snapshot.id, next_request)
    assert completed_source.value.response.status_code in {403, 404, 409}
    assert await source_client.read_source(product_id, project_id, snapshot.id, followup_request) == run.admission["source_snapshots"][0]
    await db_session.refresh(child)
    assert child.attempt_id == followup.work_id and child.lease_fence == 1
    followup_session = credential_request.model_copy(
        update={"attempt_id": followup.work_id, "lease_owner": followup.lease_owner, "lease_fence": followup.fence}
    )
    delivered_followup = await test_client.post("/internal/v1/l5/credential-session", json=followup_session.model_dump())
    assert delivered_followup.status_code == 200, delivered_followup.text
    assert assumed.call_count == 3
    assert run.manifest is None
    followup.kind = "execute"
    followup.lease_owner = None
    followup.lease_until = None
    await db_session.execute(delete(WorkerAttempt).where(WorkerAttempt.run_id == run.run_id))
    await db_session.commit()

    test_loop = asyncio.get_running_loop()

    async def change_authority_after_session():
        async with backend.sessions() as independent:
            if provider_boundary == "revoked_after_session":
                parent = await independent.get(CredentialServiceDelegation, template.id)
                parent.revoked_at = datetime.now(UTC)
            elif provider_boundary == "binding_revoked_after_session":
                selected_binding = await independent.get(Binding, (tenant_id, binding.binding_id))
                selected_binding.readiness = []
            else:
                claimed = await independent.scalar(select(Work).where(Work.run_id == run.run_id, Work.kind == "submit"))
                if provider_boundary == "expired_after_session":
                    claimed.lease_until = clock["now"] - timedelta(seconds=1)
                else:
                    claimed.fence += 1
                    claimed.lease_owner = "replacement-worker"
            await independent.commit()

    class OfflineReview:
        started = 0
        effects = []

        def batch_get_agent_spaces(self, **kwargs):
            assert kwargs["agentSpaceIds"] == ["space-one"]
            if provider_boundary in {"revoked_after_session", "expired_after_session", "stale_after_session", "binding_revoked_after_session"}:
                asyncio.run_coroutine_threadsafe(change_authority_after_session(), test_loop).result(timeout=10)
            return {
                "agentSpaces": [
                    {
                        "agentSpaceId": "space-one",
                        "name": "review-space",
                        "createdAt": now,
                        "awsResources": {
                            "iamRoles": ["arn:aws:iam::123456789012:role/SourceReader"],
                            "s3Buckets": ["arn:aws:s3:::example-source-staging"],
                        },
                    }
                ]
            }

        def update_agent_space(self, **kwargs):
            assert kwargs["agentSpaceId"] == "space-one"
            self.effects.append("update")
            return {"agentSpaceId": "space-one"}

        def create_code_review(self, **kwargs):
            assert kwargs["agentSpaceId"] == "space-one"
            self.effects.append("create")
            return {"codeReviewId": "review-one"}

        def start_code_review_job(self, **kwargs):
            self.effects.append("start")
            self.started += 1
            return {"codeReviewJobId": "job-one"}

        def batch_get_code_review_jobs(self, **kwargs):
            assert kwargs["codeReviewJobIds"] == ["job-one"]
            return {
                "codeReviewJobs": [
                    {
                        "codeReviewJobId": "job-one",
                        "codeReviewId": "review-one",
                        "status": "COMPLETED",
                        "steps": [{"name": "STATIC_ANALYSIS", "status": "COMPLETED"}],
                    }
                ]
            }

        def list_code_review_job_tasks(self, **kwargs):
            return {"codeReviewJobTaskSummaries": []}

        def list_findings(self, **kwargs):
            assert kwargs["codeReviewJobId"] == "job-one"
            return {"findingsSummaries": []}

    class OfflineStaging:
        content = None

        def put_object(self, **kwargs):
            assert kwargs["IfNoneMatch"] == "*"
            self.content = kwargs["Body"].read()
            assert kwargs["ContentLength"] == len(self.content)
            self.digest = kwargs["Metadata"]["sha256"]

        def head_object(self, **kwargs):
            return {"ContentLength": len(self.content), "Metadata": {"sha256": self.digest}, "VersionId": "staging-v1"}

        def get_object(self, **kwargs):
            assert kwargs["VersionId"] == "staging-v1"
            return {"Body": io.BytesIO(self.content), "ContentLength": len(self.content), "VersionId": "staging-v1"}

    native, staging = OfflineReview(), OfflineStaging()

    messages = []

    async def publish(envelope, delay):
        messages.append(envelope)

    class TakeoverTransport:
        armed = False

        async def post(self, path, json):
            if self.armed and path == "/internal/v1/l5/credential-session":
                self.armed = False
                async with backend.sessions() as independent:
                    competing = await independent.scalar(select(Work).where(Work.work_id == json["attempt_id"]))
                    if provider_boundary == "stale_fence":
                        competing.fence += 1
                        competing.lease_owner = "replacement-worker"
                    else:
                        competing.lease_until = clock["now"] - timedelta(seconds=1)
                    await independent.commit()
            return await test_client.post(path, json=json)

        async def request(self, *args, **kwargs):
            return await test_client.request(*args, **kwargs)

        def stream(self, *args, **kwargs):
            return test_client.stream(*args, **kwargs)

    transport = TakeoverTransport()
    selected_runtime = _archive_runtime(tmp_path) if provider_boundary == "accepted" else runtime
    if selected_runtime is not runtime:
        monkeypatch.setitem(sys.modules, selected_runtime.__name__, selected_runtime)
    monkeypatch.setenv("L5_WORKER_FACTORY", selected_runtime.__name__ + ":factory")
    monkeypatch.setattr(selected_runtime, "_gateway_transport", lambda: transport)
    monkeypatch.setattr(selected_runtime, "_sessions", lambda: backend.sessions)
    monkeypatch.setattr(selected_runtime, "_archive_store", lambda: archive_store)
    monkeypatch.setattr(selected_runtime, "_queue_publisher", lambda: publish)
    monkeypatch.setattr(
        selected_runtime,
        "_provider_session",
        lambda credentials, region: SimpleNamespace(client=lambda service, region_name: {"securityagent": native, "s3": staging}[service]),
    )
    clock = {"now": datetime.now(UTC)}
    worker = load_service()
    worker.clock = lambda: clock["now"]
    worker.adapter.clock = lambda: clock["now"]
    initial_message = {"schema_version": "1.0", "work_id": followup.work_id, "generation": followup.generation}
    assert await worker.execute(initial_message)
    assert not await worker.execute(initial_message)
    assert native.started == 0 and len(messages) == 1
    submit_message = messages.pop()
    if provider_boundary not in {"accepted", "draft_selection"}:
        issued_before_submit = assumed.call_count
        if provider_boundary == "revoked":
            template.revoked_at = datetime.now(UTC)
        elif provider_boundary == "foreign_product":
            child.product_id = "foreign-product"
        elif provider_boundary == "foreign_connection":
            binding.connection_ref = {**binding.connection_ref, "record_id": "foreign-connection"}
        elif provider_boundary == "credential_stale":
            credential.aws_verified_version_id = "replaced-version"
        elif provider_boundary == "expired_grant":
            child.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif provider_boundary in {"expired_lease", "stale_fence"}:
            transport.armed = True
        await db_session.commit()
        assert await worker.execute(submit_message) is (
            provider_boundary not in {"expired_lease", "stale_fence", "expired_after_session", "stale_after_session"}
        )
        if provider_boundary not in {"expired_lease", "expired_after_session"}:
            assert not await worker.execute(submit_message)
        async with backend.sessions() as independent:
            retained = await independent.get(Run, (tenant_id, run.run_id))
            attempt = (await independent.scalars(select(WorkerAttempt).where(WorkerAttempt.run_id == run.run_id))).one()
            owed = await independent.scalar(select(Work).where(Work.work_id == submit_message["work_id"]))
            results = (await independent.scalars(select(AssessmentResult).where(AssessmentResult.run_id == run.run_id))).all()
            assert attempt.status == "prepared" and results == []
            assert retained.admission["source_snapshots"] == run.admission["source_snapshots"]
            if provider_boundary in {"expired_lease", "stale_fence", "expired_after_session", "stale_after_session"}:
                assert retained.state == "queued" and owed.completed_at is None
            else:
                assert retained.state == "blocked" and retained.provider_state == "unknown"
        expected_sessions = issued_before_submit + (
            2
            if provider_boundary in {"revoked_after_session", "expired_after_session", "stale_after_session", "binding_revoked_after_session"}
            else 0
        )
        assert assumed.call_count == expected_sessions and native.effects == [] and staging.content is None
        return
    assert await worker.execute(submit_message)
    assert not await worker.execute(submit_message)
    assert native.started == 1 and len(messages) == 1
    async with backend.sessions() as independent:
        sealed = await independent.get(Run, (tenant_id, run.run_id))
        assert {key: value for key, value in sealed.manifest.items() if key != "sealed_at"} == {
            key: value for key, value in materialized.items() if key != "sealed_at"
        }
        assert sealed.admission["source_snapshots"] == run.admission["source_snapshots"]
    async with backend.sessions() as independent:
        persisted = await independent.get(Run, (tenant_id, run.run_id))
        assert persisted.state == "running" and persisted.native_job_ref["record_id"] == "job-one"
        assert (await independent.scalar(select(WorkerAttempt).where(WorkerAttempt.run_id == run.run_id))).status == "accepted"
        assert (await independent.scalars(select(AssessmentResult).where(AssessmentResult.run_id == run.run_id))).all() == []
    poll_message = messages.pop()
    async with backend.sessions() as independent:
        poll = await independent.scalar(select(Work).where(Work.work_id == poll_message["work_id"]))
        clock["now"] = max(clock["now"], poll.due_at.replace(tzinfo=UTC))
    assert await worker.execute(poll_message)
    assert await worker.execute(messages.pop())
    assert not await worker.execute(submit_message)
    async with backend.sessions() as independent:
        persisted = await independent.get(Run, (tenant_id, run.run_id))
        results = (await independent.scalars(select(AssessmentResult).where(AssessmentResult.run_id == run.run_id))).all()
        assert persisted.state == "succeeded" and persisted.provider_state == "completed"
        attempts = (await independent.scalars(select(WorkerAttempt).where(WorkerAttempt.run_id == run.run_id))).all()
        work_rows = (await independent.scalars(select(Work).where(Work.run_id == run.run_id))).all()
        assert len(results) == 1 and results[0].payload["finding_count"] == 0
        assert len(attempts) == 1 and attempts[0].status == "accepted"
        assert all(item.completed_at is not None for item in work_rows)
    assert native.started == 1 and native.effects == ["update", "create", "start"]
    assert staging.content == archive_store.content

    binding.readiness = [
        {**binding.readiness[0], "registration": {"bucket": "wrong-staging", "service_role": "arn:aws:iam::210987654321:role/Foreign"}}
    ]
    await db_session.commit()
    assert current_registration(binding) is None
    with pytest.raises(HTTPException) as invalid_readback:
        await backend.submit(product_id, project_id, "security", run_input, "another-review")
    assert invalid_readback.value.status_code == 409
    with pytest.raises(HTTPException) as stale_work:
        await get_l5_work_authorizer(db_session)(child, request)
    assert stale_work.value.status_code == 409 and capture.await_count == 1
    binding.readiness = []
    await db_session.commit()
    with pytest.raises(HTTPException) as missing_readback:
        await get_l5_work_authorizer(db_session)(child, request)
    assert missing_readback.value.status_code == 409
    binding.readiness = readback["readiness"]
    if provider_boundary in {"accepted", "draft_selection"}:
        setup = SetupIntent(
            tenant_id=tenant_id,
            operation_id="setup-operation",
            product_id=product_id,
            project_id=project_id,
            service_id="security",
            actor_id=owner_id,
            idempotency_key="setup-key",
            configuration_id=configuration.configuration_id,
            scope=scope,
            request={"action": "check", "configuration_revision": 1, "binding_id": binding.binding_id},
            binding_generation=2,
            authorization={
                "actor_id": owner_id,
                "delegation_id": template.id,
                "delegation_revision": template.revision,
                "authorized_at": now.isoformat(),
            },
            intent_hash="",
        )
        setup.intent_hash = _intent_hash(setup)
        setup_work = SetupWork(
            tenant_id=tenant_id,
            work_id="setup-work",
            operation_id=setup.operation_id,
            generation=1,
            due_at=now,
            lease_owner="setup-worker",
            fence=1,
            lease_until=datetime.now(UTC) + timedelta(seconds=45),
        )
        db_session.add_all(
            [
                setup,
                setup_work,
                BindingOperation(
                    tenant_id=tenant_id,
                    operation_id=setup.operation_id,
                    binding_id=binding.binding_id,
                    action="check",
                    state="accepted",
                    revision=1,
                    gaps=[],
                    attempts=0,
                    reconcile_checks=0,
                ),
            ]
        )
        await db_session.commit()
        setup_request = credential_request.model_copy(
            update={
                "delegation_id": template.id,
                "delegation_revision": template.revision,
                "operation_id": setup.operation_id,
                "attempt_id": setup_work.work_id,
                "lease_owner": setup_work.lease_owner,
                "lease_fence": setup_work.fence,
            }
        )
        setup_session = await test_client.post("/internal/v1/l5/credential-session", json=setup_request.model_dump())
        assert setup_session.status_code == 200, setup_session.text
        assert setup_session.json()["setup_verified"]["connection_ref"] == connection_ref
        assert template.operation_id is None and template.attempt_id is None
        check_policy = json.loads(assumed.call_args.kwargs["session_policy"])["Statement"]
        assert len(check_policy) == 1
        assert check_policy[0]["Resource"] == "arn:aws:securityagent:us-east-1:123456789012:agent-space/space-one"
        assert "securityagent:CreateAgentSpace" not in check_policy[0]["Action"]
        if provider_boundary == "accepted":
            create_setup = SetupIntent(
                tenant_id=tenant_id,
                operation_id="create-operation",
                product_id=product_id,
                project_id=project_id,
                service_id="security",
                actor_id=owner_id,
                idempotency_key="create-key",
                configuration_id=configuration.configuration_id,
                scope=scope,
                request={"action": "create", "configuration_revision": 1},
                binding_generation=template.binding_generation,
                authorization={
                    "actor_id": owner_id,
                    "delegation_id": template.id,
                    "delegation_revision": template.revision,
                    "authorized_at": now.isoformat(),
                },
                intent_hash="",
            )
            create_setup.intent_hash = _intent_hash(create_setup)
            create_work = SetupWork(
                tenant_id=tenant_id,
                work_id="create-work",
                operation_id=create_setup.operation_id,
                generation=1,
                due_at=now,
                lease_owner="create-worker",
                fence=1,
                lease_until=datetime.now(UTC) + timedelta(seconds=45),
            )
            db_session.add_all(
                [
                    create_setup,
                    create_work,
                    BindingOperation(
                        tenant_id=tenant_id,
                        operation_id=create_setup.operation_id,
                        action="create",
                        state="accepted",
                        revision=1,
                        gaps=[],
                        attempts=0,
                    ),
                ]
            )
            await db_session.commit()
            create_request = setup_request.model_copy(
                update={
                    "operation_id": create_setup.operation_id,
                    "attempt_id": create_work.work_id,
                    "lease_owner": create_work.lease_owner,
                    "lease_fence": create_work.fence,
                }
            )
            create_session = await test_client.post("/internal/v1/l5/credential-session", json=create_request.model_dump())
            assert create_session.status_code == 200, create_session.text
            create_policy = json.loads(assumed.call_args.kwargs["session_policy"])["Statement"]
            assert create_policy[0]["Resource"] == "*"
            assert create_policy[0]["Condition"]["StringEquals"] == {"aws:RequestedRegion": "us-east-1"}
            assert create_policy[1]["Resource"] == "arn:aws:securityagent:us-east-1:123456789012:agent-space/*"
            assert create_policy[1]["Condition"]["StringEquals"] == {"aws:ResourceTag/adp-setup-operation": create_setup.operation_id}
            assert all(
                item["Condition"]["DateLessThan"]["aws:CurrentTime"] == create_work.lease_until.replace(tzinfo=UTC).isoformat()
                for item in create_policy
            )
        setup_claim = SimpleNamespace(
            operation_id=setup.operation_id, work_id=setup_work.work_id, owner=setup_work.lease_owner, fence=setup_work.fence
        )
        verified_setup, setup_adapter = await worker.binding_authorize(setup, setup_claim)
        assert verified_setup["configuration_id"] == configuration.configuration_id
        native_readback = await setup_adapter._read(setup_adapter.client.batch_get_agent_spaces, agentSpaceIds=["space-one"])
        assert native_readback["agentSpaces"][0]["agentSpaceId"] == "space-one"
        setup_work.fence += 1
        await db_session.commit()
        stale_setup = await test_client.post("/internal/v1/l5/credential-session", json=setup_request.model_dump())
        assert stale_setup.status_code == 409
        with pytest.raises(AuthorityRevoked, match="current setup work unavailable"):
            await setup_adapter._read(setup_adapter.client.batch_get_agent_spaces, agentSpaceIds=["space-one"])
        setup_work.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        assert await repair_once(worker, now=datetime.now(UTC)) >= 1
        assert {"schema_version": "1.0", "work_id": setup_work.work_id, "generation": 1} in messages
        setup_enqueued = messages.count({"schema_version": "1.0", "work_id": setup_work.work_id, "generation": 1})
        assert await repair_once(worker, now=datetime.now(UTC)) == 0
        assert messages.count({"schema_version": "1.0", "work_id": setup_work.work_id, "generation": 1}) == setup_enqueued
        worker.clock = lambda: datetime.now(UTC)
        setup_envelope = {"schema_version": "1.0", "work_id": setup_work.work_id, "generation": 1}
        assert await worker.execute(setup_envelope)
        assert not await worker.execute(setup_envelope)
        async with backend.sessions() as reader:
            completed_setup = await reader.get(SetupWork, (tenant_id, setup_work.work_id))
            completed_operation = await reader.get(BindingOperation, (tenant_id, setup.operation_id))
            assert completed_setup.completed_at is not None
            assert completed_operation.state == "succeeded"
    issued_before_revocation = assumed.call_count
    template.revoked_at = datetime.now(UTC)
    await db_session.commit()
    with pytest.raises(httpx.HTTPStatusError) as revoked_read:
        await source_client.read_source(product_id, project_id, snapshot.id, followup_request)
    assert revoked_read.value.response.status_code in {403, 404, 409}
    revoked_session = await test_client.post("/internal/v1/l5/credential-session", json=renewed_request.model_dump())
    assert revoked_session.status_code in {403, 409} and assumed.call_count == issued_before_revocation
    with pytest.raises(HTTPException) as revoked:
        await backend.submit(product_id, project_id, "security", run_input, "another-review")
    assert revoked.value.status_code in {403, 404}
    assert capture.await_count == 1
