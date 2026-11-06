"""Normal Transform requests use real Product, delegation and source-capture checks."""

import io
import json
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from sqlalchemy import select

pytest.importorskip("src.l5_backend_registry", reason="requires the published shared Gateway binding hook")

from l5_services.adapters.transform.artifacts import TransformArtifacts
from l5_services.adapters.transform.provider_job import NDJSON_BINARY_SHA256, NDJSON_INTERFACE
from l5_services.models import Binding, Configuration, ConfigurationVersion, Run, Work
from l5_services.models.base import Base as L5Base
from l5_services.models.evidence import AssessmentResult
from l5_services.models.result import ResultDetail

from src import l5_backend_transform
from src.auth.aws_connection_authority import connection_binding
from src.l5_backend_registry import install_l5_backends
from src.products.models import ProductSourceBinding, ProductSourceSnapshot
from src.shared.models.budget import BudgetConfig
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from tests.products.test_l5_snapshots import snapshot_fixture


def test_transform_exports_import_and_bind_without_startup_aws_io():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
from unittest.mock import patch
with patch('boto3.client', side_effect=AssertionError('startup AWS I/O')):
    from src.l5_backend_transform import gateway_backends
    exports = gateway_backends()
    assert set(exports) == {
        'l5_transform_work_authority',
        'l5_transform_executor_qualification_reader',
        'l5_transform_artifact_store',
    }
    first, second = object(), object()
    for export in exports.values():
        assert export.backend is None
        assert export.bind_backend(first).backend is first
        assert export.bind_backend(second).backend is second
        assert export.backend is None
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
async def transform_gateway(test_client, db_session, test_engine, app, source_member, monkeypatch):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    template, snapshot, captured, _, _, _ = await snapshot_fixture(test_client, db_session, app, source_member, monkeypatch)
    now = datetime.now(UTC)
    credential = await db_session.get(UserCredential, template.credential_id)
    credential.service, credential.credential_type = "aws", "aws_role"
    credential.aws_external_id = "fixture-external"
    credential.aws_verified_at, credential.aws_verification_attempt = now, "fixture-verified"
    credential.aws_verified_version_id = "version-a"
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/example", "region": "us-east-1"}
    credential.scopes = {**identity, "status": "verified", "verified_at": now.isoformat(), "routing_capable": True, "routing_reason": "verified"}
    credential.aws_verified_binding = connection_binding(credential)
    source = {**snapshot.source_ref.model_dump(), "record_type": "repository_commit", "record_revision": "a" * 40}
    source_binding = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.credential_id == credential.id))
    source_binding.provider, source_binding.kind = "aws", "repository"
    source_binding.desired = {"source_revision": source["record_revision"]}
    source_binding.observed = {
        "source_revision": source["record_revision"],
        "observed_at": now.isoformat(),
        "stale_after": (now + timedelta(hours=2)).isoformat(),
    }
    connection_ref = {**source, "record_type": "connection", "record_id": credential.id, "record_revision": "version-a"}
    executor = {**connection_ref, "record_type": "executor_binding", "record_id": "executor-a"}
    policy_ref = {**executor, "record_type": "test_policy", "record_id": "policy-a"}
    definition = {**executor, "record_type": "transformation_definition", "record_id": "definition-a"}
    for reference in (executor, policy_ref, definition):
        db_session.add(
            ProductSourceBinding(
                tenant_id=template.tenant_id,
                project_id=template.project_id,
                credential_id=credential.id,
                provider="aws",
                kind="document",
                namespace="examples",
                native_id=reference["record_id"],
                desired={"source_revision": reference["record_revision"]},
                observed={"source_revision": reference["record_revision"]},
                status="healthy",
            )
        )
    template.service_id = "l5-transform-worker"
    template.operation_id, template.attempt_id, template.lease_fence = None, None, None
    template.operations = ["product:read", "source:pin", "source:read", "credential:session"]
    template.sources = [{**connection_ref, "record_type": "credential"}, source]
    template.capabilities = ["refactor"]
    scope = {
        "tenant_id": template.tenant_id,
        "product_id": template.product_id,
        "project_id": template.project_id,
        "relationship_revision": "1",
        "owner_id": source_member.user_id,
        "visibility": "only_me",
    }
    goal = {"kind": "refactor", "summary": "Simplify the example"}
    settings = {
        "service_id": "transform",
        "enabled": True,
        "capabilities": ["refactor"],
        "source_refs": [source],
        "profile_ref": {**source, "record_type": "product", "record_id": template.product_id, "record_revision": "1"},
        "transform": {
            "connection_ref": connection_ref,
            "executor_binding_ref": executor,
            "test_policy_ref": policy_ref,
            "definition_ref": definition,
            "region": "us-east-1",
            "read_paths": ["src"],
            "write_paths": ["src/app.py"],
            "excluded_paths": [".git"],
            "goal_defaults": goal,
        },
        "triggers": [
            {
                "trigger_id": "manual-a",
                "kind": "manual",
                "enabled": True,
                "capability": "refactor",
                "binding_id": "binding-a",
                "source_refs": [source],
            }
        ],
        "visibility": "only_me",
        "classification": "internal",
        "limits": {
            "deadline_seconds": 3600,
            "agent_minutes": 10,
            "max_cost": "300",
            "policy_ref": {"tenant_id": template.tenant_id, "project_id": template.project_id, "record_type": "policy", "record_id": "budget-a"},
        },
    }
    db_session.add(
        BudgetConfig(
            id="budget-a",
            org_id=template.tenant_id,
            entity_type="org",
            entity_id=template.tenant_id,
            period_type="daily",
            budget_amount_usd=Decimal("2000"),
            enforcement_mode="hard",
        )
    )
    db_session.add(
        Configuration(
            tenant_id=template.tenant_id,
            configuration_id="config-a",
            product_id=template.product_id,
            project_id=template.project_id,
            service_id="transform",
            relationship_revision="1",
            owner_id=source_member.user_id,
            current_revision=1,
            handoff_state="linked",
        )
    )
    db_session.add(
        ConfigurationVersion(
            tenant_id=template.tenant_id,
            configuration_id="config-a",
            revision=1,
            settings=settings,
            inherited_fields=[],
            gaps=[],
            state="ready",
            hash="e" * 64,
        )
    )
    binding = Binding(
        tenant_id=template.tenant_id,
        binding_id="binding-a",
        configuration_id="config-a",
        product_id=template.product_id,
        project_id=template.project_id,
        service_id="transform",
        scope=scope,
        revision=1,
        generation=1,
        state="ready",
        ownership="customer_linked",
        connection_ref=connection_ref,
        connection_key="b" * 64,
        provider_identity=identity,
        readiness=[],
        gaps=[],
    )
    db_session.add(binding)
    await db_session.commit()
    qualification = {
        "schema_version": "1.0",
        "interface": NDJSON_INTERFACE,
        "cli_version": "3.19.0",
        "binary_sha256": NDJSON_BINARY_SHA256,
        "image_digest": "sha256:" + "b" * 64,
        "definition_revision": definition["record_revision"],
        "region_qualification": "qualified",
        "executor_qualification": "qualified",
        **{
            key: {"status": "supported", "reason": "synthetic external fixture", "receipt_sha256": "c" * 64}
            for key in ("tool_isolation", "submit", "read", "resume")
        },
        "cancel": {"status": "unsupported", "reason": "local stop only"},
    }
    records = [
        {
            "scope": {key: scope[key] for key in ("tenant_id", "product_id", "project_id")},
            "executor_binding_ref": executor,
            "provider_identity": identity,
            "expires_at": (now + timedelta(hours=2)).isoformat(),
            "transform_qualification": qualification,
            "transform_test_policy": {
                "test_policy_ref": policy_ref,
                "image_digest": "sha256:" + "b" * 64,
                "bundle_sha256": "c" * 64,
                "lock_sha256": "d" * 64,
                "oracle_version": "pytest-v1",
                "commands": [{"argv": ["pytest", "-q"], "cwd": ".", "environment": {}, "timeout_seconds": 120, "minimum_test_count": 1}],
            },
        }
    ]
    s3_calls = []
    artifacts = {}

    def get_object(**request):
        s3_calls.append(request)
        if request["Key"] != "transform/catalog.json":
            stored = artifacts[(request["Key"], request["VersionId"])]
            return {
                "VersionId": request["VersionId"],
                "Body": io.BytesIO(stored["Body"]),
                "ContentLength": len(stored["Body"]),
                "Metadata": stored["Metadata"],
            }
        contents = json.dumps(records, sort_keys=True).encode()
        return {"VersionId": "catalog-a", "Body": io.BytesIO(contents), "ContentLength": len(contents)}

    def put_object(**request):
        version = str(len(artifacts) + 1)
        artifacts[(request["Key"], version)] = request
        return {"VersionId": version}

    def pin_catalog():
        monkeypatch.setenv(
            "L5_TRANSFORM_QUALIFICATION_REF",
            json.dumps(
                {
                    "bucket": "example-qualification",
                    "key": "transform/catalog.json",
                    "version": "catalog-a",
                    "sha256": sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
                }
            ),
        )

    pin_catalog()
    s3 = SimpleNamespace(get_object=get_object, put_object=put_object)
    monkeypatch.setattr(l5_backend_transform.boto3, "client", lambda *args, **kwargs: s3)
    monkeypatch.setenv("L5_TRANSFORM_ARTIFACT_BUCKET", "example-artifacts")
    capture_calls = []

    async def capture_source(binding, mapping, request, objects):
        capture_calls.append(request)
        return captured.model_copy(update={"source_ref": request.source_ref, "exclusions": request.exclusions, "expires_at": request.retain_until})

    monkeypatch.setattr("src.products.l5_admission_capture.capture_source", capture_source)
    monkeypatch.setattr("src.products.l5_admission_capture.get_snapshot_objects", lambda: object())
    install_l5_backends(app)
    assert s3_calls == []
    app.state.l5_transform_work_publisher = AsyncMock()
    run_input = {
        "service_id": "transform",
        "configuration_revision": 1,
        "binding_id": "binding-a",
        "capability": "refactor",
        "sources": [source],
        "transform_input": {"goal": goal},
    }
    return SimpleNamespace(
        path=f"/v1/products/{template.product_id}/projects/{template.project_id}/services/transform",
        body=run_input,
        template=template,
        binding=binding,
        records=records,
        pin_catalog=pin_catalog,
        captures=capture_calls,
        s3_calls=s3_calls,
        s3=s3,
        publish=app.state.l5_transform_work_publisher,
    )


@pytest.mark.asyncio
async def test_transform_normal_admission_and_duplicate_use_scoped_capture(transform_gateway, test_client, db_session):
    scenario = transform_gateway
    headers = {"Idempotency-Key": "transform-request-0001"}
    response = await test_client.post(scenario.path + "/runs", json=scenario.body, headers=headers)
    assert response.status_code == 202, response.text
    replay = await test_client.post(scenario.path + "/runs", json=scenario.body, headers=headers)
    assert replay.status_code == 202 and replay.json()["run_id"] == response.json()["run_id"]
    run = await db_session.get(Run, (scenario.template.tenant_id, response.json()["run_id"]))
    grant = await db_session.get(CredentialServiceDelegation, run.admission["delegation_id"])
    assert grant.parent_delegation_id == scenario.template.id and grant.service_id == "l5-transform-worker"
    assert len(scenario.captures) == 1 and scenario.publish.await_count == 1
    assert (await db_session.scalars(select(Work).where(Work.run_id == run.run_id))).one()
    assert (await db_session.scalars(select(ProductSourceSnapshot).where(ProductSourceSnapshot.delegation_id == grant.id))).one()


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["revoked", "stale", "expired", "foreign", "wrong_revision", "wrong_catalog", "inconclusive"])
async def test_transform_normal_request_denials_have_no_capture_or_work(transform_gateway, test_client, db_session, denial):
    scenario = transform_gateway
    body = deepcopy(scenario.body)
    if denial == "revoked":
        scenario.template.revoked_at = datetime.now(UTC)
    elif denial == "expired":
        scenario.template.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif denial == "stale":
        scenario.binding.generation += 1
    elif denial == "foreign":
        body["sources"][0]["project_id"] = "foreign-project"
    elif denial == "wrong_revision":
        scenario.records[0]["executor_binding_ref"] = {**scenario.records[0]["executor_binding_ref"], "record_revision": "other"}
        scenario.pin_catalog()
    elif denial == "wrong_catalog":
        scenario.records[0]["expires_at"] = "2000-01-01T00:00:00+00:00"
    else:
        scenario.records[0]["transform_qualification"]["region_qualification"] = "unqualified"
        scenario.pin_catalog()
    await db_session.commit()
    response = await test_client.post(scenario.path + "/runs", json=body, headers={"Idempotency-Key": "transform-denial-0001"})
    assert response.status_code in {403, 404, 409, 503}, response.text
    assert scenario.captures == [] and scenario.publish.await_count == 0
    assert (await db_session.scalars(select(Run))).all() == []


@pytest.mark.asyncio
async def test_transform_result_readback_rechecks_scope_and_revocation(transform_gateway, test_client, db_session):
    scenario = transform_gateway
    response = await test_client.post(scenario.path + "/runs", json=scenario.body, headers={"Idempotency-Key": "transform-readback-0001"})
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    run = await db_session.get(Run, (scenario.template.tenant_id, run_id))
    storage = TransformArtifacts(scenario.s3, "example-artifacts")
    authority = SimpleNamespace(scope=run.admission["scope"], source_request={"operation_id": run_id}, assert_current=lambda: None)
    reference = await storage.put(
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        run_id=run_id,
        name="original_patch",
        contents=b"original patch bytes",
        media_type="text/x-diff",
        authority=authority,
    )
    db_session.add(AssessmentResult(tenant_id=run.tenant_id, result_id="result-a", run_id=run_id, version=1, payload={}))
    db_session.add(
        ResultDetail(
            tenant_id=run.tenant_id,
            result_id="result-a",
            payload={"service_id": "transform", "result_id": "result-a", "run_id": run_id, "retained_artifacts": {"original_patch": reference}},
        )
    )
    await db_session.commit()
    path = scenario.path + "/artifacts/" + quote(reference["artifact_id"], safe="") + "/content"
    downloaded = await test_client.get(path)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == b"original patch bytes"
    calls = len(scenario.s3_calls)
    foreign = await test_client.get(path.replace(scenario.template.product_id, "foreign-product"))
    assert foreign.status_code == 404
    assert len(scenario.s3_calls) == calls
    scenario.template.revoked_at = datetime.now(UTC)
    await db_session.commit()
    denied = await test_client.get(path)
    assert denied.status_code == 404
    assert len(scenario.s3_calls) == calls
