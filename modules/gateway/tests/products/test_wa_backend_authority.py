"""Scoped service authority with real records; only AWS and GitHub I/O are mocked."""

import base64
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException, Request
from l5_services.adapters.well_architected.binding import ProfileAdapter, ProfileSelection
from l5_services.adapters.well_architected.results import normalize_wa_result
from l5_services.api.configuration import view
from l5_services.models.base import Base as L5Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.evidence import AssessmentResult
from l5_services.models.preparation import AdmissionPreparation
from l5_services.models.run import Run
from l5_services.models.setup import SetupWork
from l5_services.source_manifest import package_architecture_sources
from sqlalchemy import select

from src.auth.aws_connection_authority import connection_binding
from src.internal.assume_role_routes import get_secrets_manager
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.l5_credential_routes import ConnectionRef, CredentialSessionRequest, get_l5_work_authorizer
from src.l5_backend_well_architected import gateway_backends
from src.l5_integration import GatewayConfigurationBackend, backend
from src.l5_wa_provider_selection import ProviderSelection, provider_selection
from src.products.models import ProductProjectBinding, ProductSourceBinding, ProductSourceSnapshot
from src.products.source_capture import canonical_repository
from src.shared.models.budget import BudgetConfig
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation, UserCredential
from src.shared.services.secrets_manager import SecretsManagerHelper
from tests.products.test_i01 import HEADERS, REQUEST


@pytest.fixture
async def installed_wa(test_client, test_engine, db_session, app, source_member, monkeypatch):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    tenant, owner = source_member.org_id, source_member.user_id
    now = datetime.now(UTC)
    account, region = "123456789012", "us-east-1"
    role = f"arn:aws:iam::{account}:role/Selected"
    execution_role = f"arn:aws:iam::{account}:role/SourceReader"
    profile_arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/example"
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    monkeypatch.setenv("PRODUCT_SOURCE_SNAPSHOT_BUCKET", "fictional-snapshots")
    monkeypatch.setenv("L5_ARCHIVE_BUCKET", "fictional-wa-bucket")
    monkeypatch.setenv("L5_WELL_ARCHITECTED_WORK_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/000000000000/wa-work")
    monkeypatch.delenv("L5_WA_PEER_CONTRACTS", raising=False)
    mapping = ChannelTenantMap(org_id=tenant, provider="github", provider_scope_id="example-team", installation_id="17", ownership_disputed=False)
    credential = UserCredential(
        org_id=tenant,
        user_id=owner,
        agent_consent=True,
        service="aws",
        credential_type="aws_role",
        label="offline-role",
        secret_arn="offline-secret",
        aws_external_id="offline-external-id",
        aws_verified_at=now,
        aws_verification_attempt="verification-one",
        aws_verified_version_id="version-one",
        scopes={"account_id": account, "role_arn": role, "source": "imported_role", "status": "verified"},
    )
    db_session.add_all([mapping, credential])
    await db_session.flush()
    credential.aws_verified_binding = connection_binding(credential)
    source = {
        "tenant_id": tenant,
        "project_id": project_id,
        "source_connection_id": mapping.id,
        "record_type": "repository_commit",
        "record_id": "27",
        "record_revision": "a" * 40,
    }
    connection_ref = {
        **source,
        "source_connection_id": credential.id,
        "record_type": "connection",
        "record_id": credential.id,
        "record_revision": "version-one",
    }
    for native_id in (account, region, execution_role, "fictional-wa-bucket"):
        db_session.add(
            ProductSourceBinding(
                tenant_id=tenant,
                project_id=project_id,
                credential_id=credential.id,
                kind="environment",
                provider="aws",
                namespace=account,
                native_id=native_id,
                desired={"source_revision": "version-one"},
                observed={"source_revision": "version-one"},
                status="healthy",
            )
        )
    source_binding = ProductSourceBinding(
        tenant_id=tenant,
        project_id=project_id,
        kind="repository",
        provider="github",
        installation_id=mapping.id,
        namespace="example-team",
        native_id="27",
        desired={"source_revision": "a" * 40},
        observed={"source_revision": "a" * 40},
        status="healthy",
    )
    db_session.add(source_binding)
    relationship = await db_session.scalar(select(ProductProjectBinding).where(ProductProjectBinding.product_id == product_id))
    configuration = Configuration(
        tenant_id=tenant,
        configuration_id="wa-config",
        product_id=product_id,
        project_id=project_id,
        service_id="well_architected",
        owner_id=owner,
        relationship_revision=str(relationship.relationship_revision),
        current_revision=1,
        handoff_state="linked",
    )
    settings = {
        "service_id": "well_architected",
        "enabled": True,
        "capabilities": ["architecture_review"],
        "connection_ref": connection_ref,
        "source_refs": [source],
        "profile_ref": source,
        "triggers": [
            {
                "trigger_id": "manual-review",
                "kind": "manual",
                "enabled": True,
                "capability": "architecture_review",
                "binding_id": "wa-profile",
                "source_refs": [source],
            }
        ],
        "region": region,
        "purpose": "Review",
        "classification": "internal",
        "visibility": "only_me",
        "limits": {
            "deadline_seconds": 3600,
            "max_cost": "300",
            "currency": "USD",
            "concurrency": 5,
            "max_polls": 20,
            "max_provider_attempts": 1,
            "policy_ref": {**connection_ref, "record_type": "policy", "record_id": "wa-budget"},
        },
        "well_architected": {
            "review_mode": "source_only",
            "pillars": ["SECURITY"],
            "execution_role_ref": {**connection_ref, "record_type": "role", "record_id": execution_role},
            "source_store_ref": {**connection_ref, "record_type": "artifact_store", "record_id": "fictional-wa-bucket"},
        },
    }
    version = ConfigurationVersion(
        tenant_id=tenant,
        configuration_id=configuration.configuration_id,
        revision=1,
        settings=settings,
        inherited_fields=[],
        gaps=["provider_unqualified"],
        state="needs_input",
        hash="b" * 64,
    )
    scope = view(configuration, version)["scope"]
    identity = {"account_id": account, "role_arn": role, "region": region}
    native = {
        "arn": profile_arn,
        "aggregationConfiguration": [],
        "executionRoleArn": execution_role,
        "pillars": ["SECURITY"],
        "eligibleForArchitectureGeneration": True,
        "eligibleForScheduledGeneration": False,
        "fieldErrors": [],
    }
    state = SimpleNamespace(
        native=native,
        calls=[],
        queue=[],
        objects={},
        policies=[],
        native_reads=[],
        support=True,
        region=region,
        extra_trust=[],
        source_binding=source_binding,
        credential=credential,
        version=version,
    )

    class AWSBoundary:
        def client(self, service, **kwargs):
            return self

        def get_agent_profile(self, **kwargs):
            state.calls.append("profile")
            if getattr(state, "profile_error", None):
                raise ClientError({"Error": {"Code": state.profile_error}}, "GetAgentProfile")
            return state.native

        def list_agent_contexts(self, **kwargs):
            return {"items": []}

        def get_agent_recommendation_generation(self, **kwargs):
            state.native_reads.append(("generation", kwargs))
            return state.generation

        def list_agent_recommendations(self, **kwargs):
            state.native_reads.append(("recommendations", kwargs))
            return {"items": state.recommendations}

        def get_agent_recommendation(self, **kwargs):
            state.native_reads.append(("recommendation", kwargs))
            return next(item for item in state.recommendations if item["recommendationArn"] == kwargs["recommendationArn"])

        def get_role(self, **kwargs):
            state.calls.append("role")
            return {
                "Role": {
                    "Arn": execution_role,
                    "RoleId": "source-reader-role-id",
                    "AssumeRolePolicyDocument": {
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "sts:AssumeRole",
                                "Principal": {"Service": "wellarchitected.amazonaws.com"},
                                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
                            },
                            *state.extra_trust,
                        ]
                    },
                }
            }

        def simulate_principal_policy(self, **kwargs):
            return {
                "EvaluationResults": [
                    {
                        "EvalActionName": "s3:GetObjectVersion",
                        "EvalResourceName": kwargs["ResourceArns"][0],
                        "EvalDecision": "allowed",
                        "MissingContextValues": [],
                    }
                ]
            }

        def get_bucket_location(self, **kwargs):
            return {"LocationConstraint": state.region}

        def get_bucket_versioning(self, **kwargs):
            return {"Status": "Enabled"}

        def get_public_access_block(self, **kwargs):
            return {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                }
            }

        def describe_services(self):
            if not state.support:
                raise ClientError({"Error": {"Code": "SubscriptionRequiredException"}}, "DescribeServices")
            return {"services": []}

        def describe_secret(self, **kwargs):
            return {"VersionIdsToStages": {"version-one": ["AWSCURRENT"]}}

        def get_secret_value(self, **kwargs):
            return {
                "VersionId": "version-one",
                "SecretString": json.dumps({"role_arn": role, "account_id": account, "external_id": "offline-external-id"}),
            }

        def assume_role(self, **kwargs):
            state.calls.append("assume")
            state.policies.append(json.loads(kwargs["Policy"]))
            assert kwargs["RoleArn"] == role and kwargs["ExternalId"] == "offline-external-id"
            assert "StartAgentRecommendationGeneration" not in kwargs.get("Policy", "")
            state.assumed_arn = f"arn:aws:sts::{account}:assumed-role/Selected/{kwargs['RoleSessionName']}"
            return {
                "Credentials": {
                    "AccessKeyId": "fictional",
                    "SecretAccessKey": "fictional",
                    "SessionToken": "fictional",
                    "Expiration": datetime.now(UTC) + timedelta(minutes=15),
                },
                "AssumedRoleUser": {"Arn": state.assumed_arn, "AssumedRoleId": "fictional:session"},
            }

        def get_caller_identity(self):
            return {"Account": account, "Arn": state.assumed_arn}

        def put_object(self, **kwargs):
            state.objects[kwargs["Key"]] = kwargs["Body"]
            return {"VersionId": "v1"}

        def get_object(self, **kwargs):
            content = state.objects[kwargs["Key"]]
            return {"VersionId": "v1", "ContentLength": len(content), "Body": io.BytesIO(content)}

        def send_message(self, **kwargs):
            state.queue.append(json.loads(kwargs["MessageBody"]))

    aws = AWSBoundary()
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: aws)
    monkeypatch.setattr("boto3.Session", lambda **kwargs: aws)
    app.dependency_overrides[get_secrets_manager] = lambda: SecretsManagerHelper(region_name=region, client=aws)

    def no_aws_transport(*args, **kwargs):
        raise AssertionError("offline authority tests must not send AWS requests")

    monkeypatch.setattr("botocore.httpsession.URLLib3Session.send", no_aws_transport)
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr("root/design.txt", b"Encrypt stored data.\n")

    async def github_archive(binding, installation, request):
        assert binding.id == source_binding.id and installation.id == mapping.id
        return canonical_repository(raw.getvalue(), request)

    monkeypatch.setattr("src.products.source_capture.github_capture", github_archive)
    qualification = {
        **identity,
        "broker_role_arn": role,
        "execution_role_arn": execution_role,
        "trust": "qualified",
        "source_permissions": "qualified",
        "support": "eligible",
    }
    selection = ProfileSelection(tenant, product_id, project_id, account, region, role, execution_role, ("SECURITY",), qualification)
    profile_hash = ProfileAdapter(aws, account_id=account, region=region).adopt(selection, profile_arn).snapshot_hash
    native_ref = {**connection_ref, "record_type": "agent_profile", "record_id": profile_arn, "record_revision": profile_hash}
    binding = Binding(
        tenant_id=tenant,
        binding_id="wa-profile",
        configuration_id=configuration.configuration_id,
        product_id=product_id,
        project_id=project_id,
        service_id="well_architected",
        scope=scope,
        revision=1,
        generation=1,
        state="ready",
        ownership="customer_linked",
        connection_ref=connection_ref,
        connection_key="connection",
        native_resource_ref=native_ref,
        native_key="profile",
        provider_identity={**identity, "execution_role_arn": execution_role},
        readiness=[{"snapshot_hash": profile_hash}],
        gaps=[],
    )
    template = CredentialServiceDelegation(
        tenant_id=tenant,
        credential_id=credential.id,
        service_id="l5-well-architected-worker",
        product_id=product_id,
        project_id=project_id,
        created_by=owner,
        operations=["product:read", "source:pin", "source:read", "credential:session"],
        sources=[{**connection_ref, "record_type": "credential"}, source],
        capabilities=["architecture_review"],
        visibility="only_me",
        revision=1,
        binding_generation=1,
        expires_at=now + timedelta(hours=3),
    )
    db_session.add_all(
        [
            configuration,
            version,
            binding,
            template,
            BudgetConfig(
                id="wa-budget",
                org_id=tenant,
                entity_type="org",
                entity_id=tenant,
                period_type="monthly",
                budget_amount_usd=Decimal("2000"),
                enforcement_mode="hard",
            ),
        ]
    )
    await db_session.commit()
    state.template, state.binding, state.scope = template, binding, scope
    state.product_id, state.project_id = product_id, project_id
    state.input = {
        "service_id": "well_architected",
        "configuration_revision": 1,
        "binding_id": binding.binding_id,
        "capability": "architecture_review",
        "sources": [source],
    }
    state.path = f"/v1/products/{product_id}/projects/{project_id}/services/well_architected"
    for name, dependency in gateway_backends().items():
        setattr(app.state, name, dependency)
    incoming = Request({"type": "http", "app": app})
    state.backend = backend("well_architected", incoming, db_session, source_member, x_l5_delegation_id=None)
    selected = await provider_selection(
        state.backend,
        project_id,
        ProviderSelection(
            connection_ref=connection_ref,
            region=region,
            execution_role_arn=execution_role,
            source_bucket="fictional-wa-bucket",
        ),
    )
    version.settings = {
        **settings,
        "well_architected": {
            **settings["well_architected"],
            **{field: selected[field] for field in ("execution_role_ref", "source_store_ref")},
        },
    }
    await db_session.commit()
    state.calls.clear()
    state.policies.clear()
    return state


async def test_scoped_backend_admits_and_reads_source_bound_result(installed_wa, db_session, source_member, test_client):
    state = installed_wa
    selected = state.backend
    assert isinstance(selected, GatewayConfigurationBackend)
    assert selected.wa_authority.__self__.backend is selected and selected.wa_qualify.__self__.backend is selected
    headers = {**HEADERS, "Idempotency-Key": "wa-production-once-0001"}
    admitted = await test_client.post(f"{state.path}/runs", json=state.input, headers=headers)
    assert admitted.status_code in {200, 201, 202}, admitted.text
    run_id = admitted.json()["run_id"]
    run = await db_session.get(Run, (source_member.org_id, run_id))
    snapshot = await db_session.scalar(select(ProductSourceSnapshot).where(ProductSourceSnapshot.delegation_id == run.admission["delegation_id"]))
    assert set(run.admission["architecture_coverage"]) == {snapshot.id}
    assert set(run.admission["architecture_coverage"][snapshot.id]["files"]) == {"design.txt"}
    assert run.admission["provider_identity"] == state.binding.provider_identity
    assert run.admission["generation_scope"] == {"pillars": ["SECURITY"]}
    assert "assume" in state.calls and "profile" in state.calls
    assert state.queue and await db_session.scalar(select(AdmissionPreparation)) is not None
    replay = await test_client.post(f"{state.path}/runs", json=state.input, headers=headers)
    assert replay.status_code in {200, 201, 202}, replay.text
    assert replay.json()["run_id"] == run_id
    assert len((await db_session.scalars(select(Run))).all()) == 1
    native = {
        "tenant_id": run.tenant_id,
        "project_id": run.project_id,
        "profile_arn": state.binding.native_resource_ref["record_id"],
        "generation_id": "generation-example",
    }
    run.native_job_ref, run.provider_state, run.state = native, "completed", "succeeded"
    payload = {"native_job_ref": native, "recommendations": []}
    detail = normalize_wa_result(run, "result-example", 1, payload, datetime.now(UTC))
    db_session.add_all([detail, AssessmentResult(tenant_id=run.tenant_id, run_id=run_id, result_id="result-example", version=1, payload=payload)])
    await db_session.commit()
    result = await test_client.get(f"{state.path}/results/result-example", headers=HEADERS)
    assert result.status_code == 200, result.text
    assert result.json()["native_job_ref"] == native
    state.source_binding.status = "revoked"
    await db_session.commit()
    denied = await test_client.get(f"{state.path}/results/result-example", headers=HEADERS)
    assert denied.status_code == 404


@pytest.fixture
async def completed_wa(installed_wa, db_session, test_client):
    state = installed_wa
    response = await test_client.post(f"{state.path}/runs", json=state.input, headers={**HEADERS, "Idempotency-Key": "wa-native-observation-0001"})
    assert response.status_code == 202, response.text
    run = await db_session.get(Run, (state.scope["tenant_id"], response.json()["run_id"]))
    source = state.input["sources"][0]
    content = b"Encrypt stored data.\n"
    packed, architecture = package_architecture_sources(
        [
            {
                "source_ref": source,
                "snapshot_id": "snapshot-" + run.run_id,
                "revision": source["record_revision"],
                "selected_paths": ["design.txt"],
                "selected_hashes": {"design.txt": hashlib.sha256(content).hexdigest()},
                "files": [{"path": "design.txt", "content": content, "component": "service", "exclusion": None, "entry_point": True}],
                "unresolved": [],
            }
        ],
        scope=state.scope,
        max_files=1,
        max_bytes=8192,
    )
    reference = {
        "artifact_id": f"l5/well_architected/work/{run.run_id}/archive.zip",
        "media_type": "application/zip",
        "version": "v1",
        "sha256": hashlib.sha256(packed).hexdigest(),
        "bytes": len(packed),
    }
    run.manifest = {
        "archive": reference,
        "architecture": architecture,
        "provider_source_uri": "s3://fictional-wa-bucket/" + reference["artifact_id"],
        "provider_coverage": "provider_coverage_unverified",
    }
    state.objects[reference["artifact_id"]] = packed
    native = {"tenant_id": run.tenant_id, "project_id": run.project_id, "profile_arn": state.native["arn"], "generation_id": "generation-example"}
    run.native_job_ref, run.provider_state, run.state = native, "completed", "succeeded"
    state.generation = {"id": native["generation_id"], "profileArn": state.native["arn"], "status": "COMPLETED"}
    state.recommendations = [
        {
            "recommendationArn": "arn:aws:wellarchitected:us-east-1:123456789012:agent-recommendation/00000000-0000-0000-0000-000000000001",
            "profileArn": state.native["arn"],
            "generationId": native["generation_id"],
            "title": "Encrypt data",
            "sources": [{"url": run.manifest["provider_source_uri"]}],
        }
    ]
    payload = {"native_job_ref": native, "recommendations": state.recommendations}
    detail = normalize_wa_result(run, "result-example", 1, payload, datetime.now(UTC))
    db_session.add_all([detail, AssessmentResult(tenant_id=run.tenant_id, run_id=run.run_id, result_id="result-example", version=1, payload=payload)])
    await db_session.commit()
    state.run = run
    state.observation_path = f"{state.path}/runs/{run.run_id}/native-observations"
    state.calls.clear()
    state.policies.clear()
    return state


@pytest.mark.parametrize("recommendation_count", [1, 25])
async def test_native_observations_use_current_product_authority_and_exact_read_policy(completed_wa, test_client, db_session, recommendation_count):
    state = completed_wa
    state.recommendations = [
        {**state.recommendations[0], "recommendationArn": state.recommendations[0]["recommendationArn"][:-12] + f"{index:012x}"}
        for index in range(1, recommendation_count + 1)
    ]
    result = await db_session.scalar(select(AssessmentResult).where(AssessmentResult.run_id == state.run.run_id))
    result.payload = {**result.payload, "recommendations": state.recommendations}
    await db_session.commit()
    response = await test_client.get(state.observation_path, headers=HEADERS)
    assert response.status_code == 200, response.text
    observed = response.json()
    assert observed["native_job_ref"] == state.run.native_job_ref
    assert observed["recommendations"] == state.recommendations
    assert base64.b64decode(observed["source_archive_base64"]) == state.objects[state.run.manifest["archive"]["artifact_id"]]
    assert observed["limitations"] == ["provider_source_coverage_unverified"]
    assert response.headers["cache-control"] == "no-store"
    assert not any(field in response.text for field in ("AccessKeyId", "SecretAccessKey", "SessionToken", "offline-secret"))
    assert len(state.policies) == 1 + (recommendation_count + 7) // 8
    statements = [statement for policy in state.policies for statement in policy["Statement"]]
    assert all(len(json.dumps(policy, separators=(",", ":"))) <= 2048 for policy in state.policies)
    assert {action for statement in statements for action in statement["Action"]} == {
        "sts:GetCallerIdentity",
        "wellarchitected:GetAgentProfile",
        "wellarchitected:ListAgentContexts",
        "wellarchitected:GetAgentRecommendationGeneration",
        "wellarchitected:ListAgentRecommendations",
        "wellarchitected:GetAgentRecommendation",
        "s3:GetObjectVersion",
    }
    assert statements[1]["Resource"] == state.native["arn"]
    assert [
        arn for statement in statements if statement["Action"] == ["wellarchitected:GetAgentRecommendation"] for arn in statement["Resource"]
    ] == [item["recommendationArn"] for item in state.recommendations]
    assert statements[2]["Resource"] == "arn:aws:s3:::fictional-wa-bucket/" + state.run.manifest["archive"]["artifact_id"]
    assert statements[2]["Condition"]["StringEquals"] == {"s3:VersionId": "v1"}
    assert all("DateLessThan" in statement["Condition"] for statement in statements)
    assert len(state.queue) == 1


@pytest.mark.parametrize("claimed_project", ["selected", "foreign"])
async def test_runtime_observation_projects_only_authorized_durable_facts(completed_wa, test_client, db_session, claimed_project):
    from l5_services.api.bindings import _digest
    from l5_services.models.attempt import WorkerAttempt
    from l5_services.models.generation_collection import GenerationCollection
    from l5_services.models.profile_claim import ProfileClaim

    state, now = completed_wa, datetime.now(UTC)
    identity = state.binding.provider_identity
    claim_key = _digest((identity["account_id"], identity["region"], state.native["arn"]))
    db_session.add_all(
        [
            ProfileClaim(
                service_id="well_architected",
                native_identity_key=claim_key,
                tenant_id=state.run.tenant_id,
                product_id=state.product_id,
                project_id=state.project_id if claimed_project == "selected" else "foreign-project",
            ),
            WorkerAttempt(
                tenant_id=state.run.tenant_id,
                attempt_id="observed-attempt",
                run_id=state.run.run_id,
                operation="start",
                first_work_id="observed-work",
                first_fence=3,
                idempotency_token="PRIVATE-TOKEN",
                input_hash="a" * 64,
                started_at=now,
                status="completed",
                native_job_ref=state.run.native_job_ref,
                prepared_request={"private": "PRIVATE-SOURCE"},
            ),
            GenerationCollection(
                tenant_id=state.run.tenant_id,
                run_id=state.run.run_id,
                native_job_ref=state.run.native_job_ref,
                page_count=2,
                recommendations=[],
                pending_recommendations=[],
                result_id="result-example",
            ),
        ]
    )
    await db_session.commit()
    response = await test_client.get(state.observation_path, headers=HEADERS)
    assert response.status_code == 200, response.text
    runtime = response.json()["runtime"]
    assert runtime["scope"] == {field: state.scope[field] for field in ("tenant_id", "product_id", "project_id")}
    assert runtime["run_id"] == state.run.run_id
    assert runtime["profile_claim_matches"] is (claimed_project == "selected")
    assert [attempt["attempt_id"] for attempt in runtime["attempts"]] == ["observed-attempt"]
    assert runtime["collection"] == {"native_job_ref": state.run.native_job_ref, "page_count": 2, "complete": True, "result_id": "result-example"}
    assert runtime["limitations"] == ["fault_history_not_recorded", "provider_start_call_count_not_observed"]
    assert "PRIVATE-" not in response.text and "foreign-project" not in response.text


async def test_runtime_projection_drift_after_native_read_is_rejected(completed_wa, test_client, db_session, monkeypatch):
    from l5_services.models.work import Work

    from src import l5_wa_observations

    state = completed_wa
    original = l5_wa_observations.run_in_threadpool

    async def changed_work(function, *args, **kwargs):
        result = await original(function, *args, **kwargs)
        work = await db_session.scalar(select(Work).where(Work.run_id == state.run.run_id))
        work.attempts += 1
        await db_session.commit()
        return result

    monkeypatch.setattr(l5_wa_observations, "run_in_threadpool", changed_work)
    response = await test_client.get(state.observation_path, headers=HEADERS)
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": {"code": "native_observation_scope_changed"}}
    assert "runtime" not in response.text and "source_archive_base64" not in response.text


@pytest.mark.parametrize(
    "failure", ["grant", "expiry", "consent", "credential", "source", "binding", "configuration", "foreign_run", "foreign_product"]
)
async def test_native_observations_deny_invalid_scope_before_provider_reads(completed_wa, test_client, db_session, failure):
    state = completed_wa
    path = state.observation_path
    if failure == "grant":
        state.template.revoked_at = datetime.now(UTC)
    elif failure == "expiry":
        state.template.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif failure == "consent":
        state.credential.agent_consent = False
    elif failure == "credential":
        state.credential.aws_verified_version_id = "stale"
    elif failure == "source":
        state.source_binding.status = "revoked"
    elif failure == "binding":
        state.binding.generation += 1
    elif failure == "configuration":
        state.version.hash = "c" * 64
    elif failure == "foreign_run":
        path = path.replace(state.run.run_id, "foreign-run")
    else:
        path = path.replace(state.product_id, "foreign-product")
    await db_session.commit()
    response = await test_client.get(path, headers=HEADERS)
    assert response.status_code in {403, 404, 409}, response.text
    assert not state.calls and not state.native_reads and not state.policies


@pytest.mark.parametrize("failure", ["grant", "source", "consent", "binding", "result"])
async def test_native_observations_recheck_authority_after_aws_reads(completed_wa, test_client, db_session, monkeypatch, failure):
    from src import l5_wa_observations

    state = completed_wa
    original = l5_wa_observations.run_in_threadpool

    async def changing_authority(function, *args, **kwargs):
        value = await original(function, *args, **kwargs)
        if failure == "grant":
            state.template.revoked_at = datetime.now(UTC)
        elif failure == "source":
            state.source_binding.status = "revoked"
        elif failure == "consent":
            state.credential.agent_consent = False
        elif failure == "binding":
            state.binding.revision += 1
        else:
            state.run.revision += 1
        await db_session.commit()
        return value

    monkeypatch.setattr(l5_wa_observations, "run_in_threadpool", changing_authority)
    response = await test_client.get(state.observation_path, headers=HEADERS)
    assert response.status_code in {403, 404, 409}, response.text
    assert state.native_reads
    assert "source_archive_base64" not in response.text and "recommendations" not in response.text


async def test_native_observations_recheck_grant_after_broker_before_native_reads(completed_wa, test_client, db_session, monkeypatch):
    from src.l5_backend_well_architected import WellArchitectedService

    state = completed_wa
    original = WellArchitectedService.selected_provider

    async def revoked_during_session(service, *args, **kwargs):
        provider = await original(service, *args, **kwargs)
        state.template.revoked_at = datetime.now(UTC)
        await db_session.commit()
        return provider

    monkeypatch.setattr(WellArchitectedService, "selected_provider", revoked_during_session)
    response = await test_client.get(state.observation_path, headers=HEADERS)
    assert response.status_code == 403, response.text
    assert state.policies and not state.native_reads
    assert "source_archive_base64" not in response.text


@pytest.mark.parametrize("failure", ["profile", "generation", "recommendation", "archive"])
async def test_native_observations_reject_wrong_native_identity_or_source_bytes(completed_wa, test_client, failure):
    state = completed_wa
    if failure == "profile":
        state.generation = {**state.generation, "profileArn": state.native["arn"] + "-foreign"}
    elif failure == "generation":
        state.generation = {**state.generation, "id": "foreign-generation"}
    elif failure == "recommendation":
        state.recommendations = [{**state.recommendations[0], "profileArn": state.native["arn"] + "-foreign"}]
    else:
        key = state.run.manifest["archive"]["artifact_id"]
        state.objects[key] = b"corrupted" + state.objects[key][9:]
    response = await test_client.get(state.observation_path, headers=HEADERS)
    assert response.status_code == 503, response.text
    assert "source_archive_base64" not in response.text


@pytest.mark.parametrize("failure", [None, "support", "revoked", "consent", "source", "capability"])
async def test_bootstrap_prerequisites_check_authority_without_setup_or_generation(installed_wa, test_client, db_session, failure):
    state = installed_wa
    if failure == "support":
        state.support = False
    elif failure == "revoked":
        state.template.revoked_at = datetime.now(UTC)
    elif failure == "consent":
        state.credential.agent_consent = False
    elif failure == "source":
        state.source_binding.status = "revoked"
    elif failure == "capability":
        state.template.capabilities = []
    await db_session.commit()
    response = await test_client.get(f"{state.path}/qualification-prerequisites", headers=HEADERS)
    if failure is None:
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ready_for_setup"
        assert response.json()["configuration"]["revision"] == 1
        assert response.json()["provider_identity"]["role_arn"] == state.credential.scopes["role_arn"]
    else:
        assert response.status_code in {403, 404, 409}, response.text
    assert not state.queue and not state.native_reads
    assert not (await db_session.scalars(select(Run))).all()
    assert not (await db_session.scalars(select(SetupWork))).all()


def test_gateway_export_is_complete_and_has_no_startup_io(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("service export must not access mounted resources or AWS")

    monkeypatch.setattr("boto3.client", unexpected)
    monkeypatch.setattr("boto3.Session", unexpected)
    monkeypatch.setattr("src.l5_backend_well_architected.runtime_support.gateway_transport", unexpected)
    dependencies = gateway_backends()
    assert set(dependencies) == {"l5_wa_authority", "l5_wa_profile_client", "l5_wa_consumer_resolver", "l5_wa_qualify"}
    assert all(callable(dependency) for dependency in dependencies.values())
    first, second = object(), object()
    for dependency in dependencies.values():
        assert dependency.bind_backend(first).__self__.backend is first
        assert dependency.bind_backend(second).__self__.backend is second


async def test_exported_consumer_resolver_preserves_human_and_scoped_peer_contracts(installed_wa, monkeypatch):
    state = installed_wa
    resolver = state.backend.wa_consumer_resolver
    scope = {field: state.scope[field] for field in ("tenant_id", "product_id", "project_id")}
    assert resolver(scope) == {}
    contracts = {
        destination: {
            "kind": "well_architected_result",
            "schema_version": "1.0",
            "private_read_fencing": True,
            "task_fencing": True,
            "path": f"/internal/v1/{prefix}/wa-contributions",
            field: f"/internal/v1/{prefix}/wa-reuse",
        }
        for destination, prefix, field in (("product-context", "context", "query_path"), ("engineering-insights", "insights", "usage_path"))
    }
    monkeypatch.setenv("L5_WA_PEER_CONTRACTS", json.dumps(contracts))
    consumers = resolver(scope)
    assert set(consumers) == set(contracts)
    assert all(client.scope == scope and client.actor is state.backend.actor and client.reuse_path for client in consumers.values())
    # An execution delegation must not replace the human performing the read.
    state.backend.delegation_id = state.template.id
    assert all(client.actor is state.backend.actor for client in resolver(scope).values())
    monkeypatch.setenv("L5_WA_PEER_CONTRACTS", json.dumps({"product-context": contracts["product-context"]}))
    assert set(resolver(scope)) == {"product-context"}
    with pytest.raises(HTTPException):
        resolver({**scope, "tenant_id": "another-tenant"})
    contracts["product-context"]["query_path"] = "/internal/v1/context/../insights/wa-reuse"
    monkeypatch.setenv("L5_WA_PEER_CONTRACTS", json.dumps(contracts))
    with pytest.raises(ValueError):
        resolver(scope)


@pytest.mark.parametrize("failure", ["revoked", "stale", "support", "region", "foreign_source", "wildcard_trust", "unconstrained_service"])
async def test_scoped_backend_rejects_unqualified_admission(installed_wa, db_session, failure):
    state = installed_wa
    body = dict(state.input)
    if failure == "revoked":
        state.template.revoked_at = datetime.now(UTC)
    elif failure == "stale":
        body["configuration_revision"] = 2
    elif failure == "support":
        state.support = False
    elif failure == "region":
        state.region = "us-west-2"
    elif failure in {"wildcard_trust", "unconstrained_service"}:
        principal = {"AWS": "*"} if failure == "wildcard_trust" else {"Service": "wellarchitected.amazonaws.com"}
        state.extra_trust = [{"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": principal}]
    else:
        body["sources"] = [{**body["sources"][0], "record_id": "foreign-source"}]
    await db_session.commit()
    with pytest.raises(HTTPException) as denied:
        await state.backend.submit(state.product_id, state.project_id, "well_architected", body, "wa-production-denial-0001")
    assert denied.value.status_code in {403, 404, 409}
    assert not (await db_session.scalars(select(Run))).all()
    assert not state.queue


async def test_scoped_setup_creates_fenced_native_authority(installed_wa, db_session, test_client, app, source_member):
    state = installed_wa
    await state.backend.start_binding(
        state.product_id,
        state.project_id,
        "well_architected",
        {"action": "link", "configuration_revision": 1, "native_resource_ref": state.binding.native_resource_ref},
        "wa-setup-qualification-0001",
    )
    work = await db_session.scalar(select(SetupWork))
    work.lease_owner, work.fence = "worker-example", 1
    work.lease_until = datetime.now(UTC) + timedelta(seconds=60)
    await db_session.commit()
    request = CredentialSessionRequest(
        delegation_id=state.template.id,
        delegation_revision=state.template.revision,
        operation_id=work.operation_id,
        attempt_id=work.work_id,
        lease_owner=work.lease_owner,
        lease_fence=work.fence,
        connection_ref=ConnectionRef.model_validate({**state.binding.connection_ref, "record_type": "credential"}),
    )
    for capabilities in ([], ["code_review"], ["cost_inquiry"]):
        state.template.capabilities = capabilities
        await db_session.commit()
        with pytest.raises(HTTPException) as denied:
            await get_l5_work_authorizer(db_session)(state.template, request)
        assert denied.value.status_code == 409
        assert denied.value.detail == {"code": "stale_delegation_or_attempt"}
    state.template.capabilities = ["architecture_review"]
    await db_session.commit()
    stale_request = request.model_copy(update={"delegation_revision": state.template.revision + 1})
    with pytest.raises(HTTPException) as denied:
        await get_l5_work_authorizer(db_session)(state.template, stale_request)
    assert denied.value.status_code == 409
    assert denied.value.detail == {"code": "stale_delegation_or_attempt"}
    permit = await get_l5_work_authorizer(db_session)(state.template, request)
    policy = json.loads(permit.session_policy)
    assert "wellarchitected:CreateAgentProfile" in json.dumps(policy)
    assert "StartAgentRecommendationGeneration" not in json.dumps(policy)
    assert permit.setup_verified["scope"] == state.scope
    assert permit.setup_verified["well_architected_settings"] == state.version.settings
    worker = source_member.model_copy(
        update={
            "user_id": "iam-agent:l5-well-architected-worker",
            "agent_registry_id": "l5-well-architected-worker",
            "org_id": "platform-service",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "credential_scopes": ["l5:credential-session"],
        }
    )

    async def authenticate(incoming: Request):
        incoming.state.token_context = worker

    app.dependency_overrides[verify_internal_or_irsa] = authenticate
    response = await test_client.post("/internal/v1/l5/credential-session", json=request.model_dump())
    assert response.status_code == 200, response.text
    verified = response.json()["setup_verified"]
    selected_role = state.version.settings["well_architected"]["execution_role_ref"]["record_id"]
    assert verified["source_role_qualification"]["execution_role_arn"] == selected_role
    assert verified["source_role_qualification"]["source_permissions"] == "qualified"
    assert response.json()["provider_identity"]["role_arn"] == state.binding.provider_identity["role_arn"]
    state.support = False
    refused = await test_client.post("/internal/v1/l5/credential-session", json=request.model_dump())
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "provider_unqualified"
    state.support = True
    for principal in ({"AWS": "*"}, {"Service": "wellarchitected.amazonaws.com"}):
        state.extra_trust = [{"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": principal}]
        refused = await test_client.post("/internal/v1/l5/credential-session", json=request.model_dump())
        assert refused.status_code == 409
        assert refused.json() == {"detail": {"code": "provider_unqualified"}}
