"""Product authority and read-only Security Agent evidence, with AWS mocked."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from l5_services.api.runs import create_runs_router
from l5_services.models.receipt import RunReceipt

from src.products import l5_security_readback as readback
from src.shared.models.vault import CredentialServiceDelegation, UserCredential


@pytest.fixture
def selected(monkeypatch):
    now = datetime.now(UTC)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example", "owner_id": "owner-example"}
    space = {
        "tenant_id": scope["tenant_id"],
        "project_id": scope["project_id"],
        "source_connection_id": "connection-example",
        "record_type": "security_space",
        "record_id": "space-example",
        "record_revision": "revision-example",
    }
    model = {**space, "record_type": "threat_model", "record_id": "model-example"}
    connection = {**space, "record_type": "connection", "record_id": "connection-example"}
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Selected", "region": "us-east-1"}
    reference = {
        "tenant_id": scope["tenant_id"],
        "project_id": scope["project_id"],
        "agent_space_id": "space-example",
        "threat_model_id": "model-example",
        "threat_model_job_id": "job-example",
        "artifact_id": "artifact-example",
    }
    run = SimpleNamespace(
        tenant_id=scope["tenant_id"],
        run_id="run-example",
        state="succeeded",
        revision=3,
        product_id=scope["product_id"],
        project_id=scope["project_id"],
        service_id="security",
        configuration_id="configuration-example",
        configuration_revision=1,
        binding_id="binding-example",
        binding_generation=1,
        origin="adp_submitted",
        scope=scope,
        provider_state="completed",
        deadline_at=now + timedelta(hours=1),
        admission={
            "native_space_ref": space,
            "native_model_ref": model,
            "connection_ref": connection,
            "provider_identity": identity,
            "scope": scope,
            "admitted_at": now.isoformat(),
        },
        input={"capability": "threat_model", "sources": []},
        native_job_ref=reference,
        provider_readback_ref=None,
        monitoring=None,
        retry_of=None,
    )
    binding = SimpleNamespace(
        configuration_id=run.configuration_id,
        native_resource_ref=space,
        provider_identity=identity,
        connection_ref=connection,
        scope=scope,
        generation=1,
        state="ready",
        gaps=[],
    )
    grant = SimpleNamespace(capabilities=["threat_model"], binding_generation=1, revoked_at=None, revision=2, expires_at=now + timedelta(hours=1))
    credential = SimpleNamespace(
        id="connection-example",
        user_id="owner-example",
        label="selected-example",
        aws_verified_version_id="revision-example",
        scopes=identity,
        secret_arn="fictional-secret",
    )

    class Database:
        def expire_all(self):
            pass

        async def get(self, model_type, key, **kwargs):
            if model_type is RunReceipt:
                return (
                    SimpleNamespace(run_id=run.run_id)
                    if key
                    == (scope["tenant_id"], scope["product_id"], scope["project_id"], "security", "owner-example", "submit", "receipt-example-01")
                    else None
                )
            return grant if model_type is CredentialServiceDelegation else credential if model_type is UserCredential else None

    class Backend:
        actor = SimpleNamespace(
            account_type="human",
            auth_source="jwt",
            token_use="access",
            expires_at=now + timedelta(hours=1),
            org_id=scope["tenant_id"],
            user_id="owner-example",
        )
        db = Database()

        async def binding_authority(self, product_id, project_id, service_id):
            assert (product_id, project_id, service_id) == (scope["product_id"], scope["project_id"], "security")
            return None, {
                "authorization": {"delegation_id": "grant-example"},
                "scope": scope,
                "configuration_id": run.configuration_id,
                "configuration_revision": 1,
                "connection_ref": connection,
            }

        async def get_run(self, product_id, project_id, service_id, run_id):
            assert run_id == run.run_id
            return run

        async def binding_store(self, product_id, project_id, service_id):
            async def selected_binding(binding_id):
                assert binding_id == run.binding_id
                return binding

            return SimpleNamespace(binding=selected_binding)

        async def native_security_readback(self, product_id, project_id, run_id, action):
            return await readback.read_native(self, product_id, project_id, run_id, action)

        async def security_recovery_state(self, product_id, project_id, run_id):
            return await readback.read_recovery_state(self, product_id, project_id, run_id)

        async def security_submission_receipt(self, product_id, project_id, receipt_id):
            return await readback.read_submission_receipt(self, product_id, project_id, receipt_id)

    calls = []
    monkeypatch.setattr(readback, "current_registration", lambda selected_binding, **kwargs: {"bucket": "example-staging"})
    monkeypatch.setattr(readback, "current_model", lambda selected_binding, **kwargs: model)
    monkeypatch.setattr(readback, "verified_connection_evidence", lambda selected_credential: True)
    monkeypatch.setattr(
        readback,
        "connection_material",
        lambda payload, scopes, selected_credential: (identity["role_arn"], "fictional-external-id", identity["account_id"]),
    )
    monkeypatch.setattr(
        readback,
        "SecretsManagerHelper",
        lambda: SimpleNamespace(
            current_version_id=lambda secret: credential.aws_verified_version_id,
            get_secret_at_version=lambda secret, version: ('{"default_region":"us-east-1"}', version),
        ),
    )

    def assume(**kwargs):
        policy = json.loads(kwargs["session_policy"])
        calls.append(policy)
        assert (kwargs["role_arn"], kwargs["user_id"], kwargs["task_id"], kwargs["default_region"]) == (
            identity["role_arn"],
            "owner-example",
            run.run_id,
            identity["region"],
        )
        actions = policy["Statement"][0]["Action"]
        assert actions and all(
            action.startswith("securityagent:") and (action.split(":")[1].startswith("List") or action.split(":")[1].startswith("BatchGet"))
            for action in actions
        )
        assert policy["Statement"][0]["Resource"].endswith("agent-space/space-example")
        return SimpleNamespace(access_key_id="fictional-key", secret_access_key="fictional-secret", session_token="fictional-token")

    class Native:
        def batch_get_threat_model_jobs(self, **kwargs):
            return {
                "threatModelJobs": [
                    {
                        "agentSpaceId": space["record_id"],
                        "threatModelId": model["record_id"],
                        "threatModelJobId": reference["threat_model_job_id"],
                        "scopeDocs": [{"artifactId": reference["artifact_id"]}],
                        "status": "COMPLETED",
                    }
                ]
            }

        def list_threats(self, **kwargs):
            return {"threats": []}

        def list_threat_model_job_tasks(self, **kwargs):
            return {"threatModelJobTaskSummaries": []}

        def list_threat_model_jobs(self, **kwargs):
            return {"threatModelJobSummaries": [{"threatModelId": model["record_id"], "threatModelJobId": reference["threat_model_job_id"]}]}

        def batch_get_code_review_jobs(self, **kwargs):
            return {"codeReviewJobs": [{"codeReviewId": "review-example", "codeReviewJobId": "job-example", "status": "COMPLETED"}]}

        def list_findings(self, **kwargs):
            return {"findingsSummaries": []}

        def list_code_review_job_tasks(self, **kwargs):
            return {"codeReviewJobTaskSummaries": []}

        def list_code_reviews(self, **kwargs):
            return {"codeReviewSummaries": [{"codeReviewId": "review-example"}]}

        def list_code_review_jobs_for_code_review(self, **kwargs):
            return {"codeReviewJobSummaries": [{"codeReviewId": "review-example", "codeReviewJobId": "job-example"}]}

    monkeypatch.setattr(readback, "assume_role", assume)
    monkeypatch.setattr(readback, "require_assumed_identity", lambda assumed, role: None)

    def scoped_session(**kwargs):
        assert kwargs == {
            "aws_access_key_id": "fictional-key",
            "aws_secret_access_key": "fictional-secret",
            "aws_session_token": "fictional-token",
            "region_name": "us-east-1",
        }
        return SimpleNamespace(client=lambda service: Native() if service == "securityagent" else None)

    monkeypatch.setattr(readback.boto3, "Session", scoped_session)
    return Backend(), run, binding, grant, credential, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["code_review", "threat_model"])
async def test_product_native_readback_uses_gateway_read_only_session(selected, capability):
    backend, run, binding, grant, credential, calls = selected
    if capability == "code_review":
        run.input["capability"] = capability
        run.native_job_ref = {"record_type": "native_job", "record_id": "job-example", "record_revision": "review-example"}
        run.admission["source_registration"] = {"bucket": "example-staging"}
        grant.capabilities.append(capability)
    app = FastAPI()
    app.include_router(create_runs_router(lambda: backend), prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.invalid") as client:
        path = "/v1/products/product-example/projects/project-example/services/security/runs/run-example/native-readback/"
        job = await client.get(path + "job")
        inventory = await client.get(path + "inventory")
        assert job.status_code == inventory.status_code == 200
        assert job.headers["Cache-Control"] == inventory.headers["Cache-Control"] == "no-store"
        assert job.json()["native_job_ref"] == run.native_job_ref
        assert inventory.json() == [run.native_job_ref]
        assert (await client.get(path.replace("/security/", "/finops/") + "job")).status_code == 404
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["threat_model", "code_review"])
async def test_existing_job_readback_keeps_admission_verified_setup_after_freshness_expires(selected, monkeypatch, capability):
    from l5_services.security_registration import current_model, current_registration

    backend, run, binding, grant, credential, calls = selected
    admitted = datetime.now(UTC) - timedelta(hours=1)
    registration = {"service_role": "arn:aws:iam::123456789012:role/Selected", "bucket": "example-staging"}
    run.admission["admitted_at"] = admitted.isoformat()
    binding.readiness = [
        {"kind": "code_review_source_registration", "registration": registration, "observed_at": admitted.isoformat()},
        {"kind": "threat_model", "native_model_ref": run.admission["native_model_ref"], "observed_at": admitted.isoformat()},
    ]
    if capability == "code_review":
        run.input["capability"] = capability
        run.native_job_ref = {"record_type": "native_job", "record_id": "job-example", "record_revision": "review-example"}
        run.admission["source_registration"] = registration
        grant.capabilities.append(capability)
    monkeypatch.setattr(readback, "current_registration", current_registration)
    monkeypatch.setattr(readback, "current_model", current_model)
    assert current_registration(binding) is None
    result = await readback.read_native(backend, run.product_id, run.project_id, run.run_id, "job")
    assert result["native_job_ref"] == run.native_job_ref
    assert len(calls) == 1
    binding.readiness[0]["observed_at"] = (admitted - timedelta(minutes=16)).isoformat()
    with pytest.raises(HTTPException) as error:
        await readback.read_native(backend, run.product_id, run.project_id, run.run_id, "job")
    assert error.value.status_code == 404
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted_at", [None, "bad-time", "2026-01-01T00:00:00", "2999-01-01T00:00:00Z"])
async def test_existing_job_readback_requires_valid_admission_timestamp(selected, admitted_at):
    backend, run, binding, grant, credential, calls = selected
    run.admission["admitted_at"] = admitted_at
    with pytest.raises(HTTPException) as error:
        await readback.read_native(backend, run.product_id, run.project_id, run.run_id, "job")
    assert error.value.status_code == 404
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["foreign_tenant", "revoked", "stale_binding", "wrong_model", "expired_session", "missing_job", "wrong_actor", "changed_version", "wrong_role"],
)
async def test_native_readback_denies_changed_product_or_provider_authority(selected, monkeypatch, fault):
    backend, run, binding, grant, credential, calls = selected
    if fault == "foreign_tenant":
        run.tenant_id = "foreign"
    elif fault == "revoked":
        grant.revoked_at = datetime.now(UTC)
    elif fault == "stale_binding":
        binding.generation = 2
    elif fault == "wrong_model":
        run.admission["native_model_ref"] = {**run.admission["native_model_ref"], "record_id": "foreign"}
    elif fault == "expired_session":
        backend.actor.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif fault == "missing_job":
        run.native_job_ref = None
    elif fault == "wrong_actor":
        backend.actor.account_type = "service"
    elif fault == "wrong_role":
        credential.scopes = {**credential.scopes, "role_arn": "arn:aws:iam::123456789012:role/Foreign"}
    else:
        credential.aws_verified_version_id = "another-version"
    with pytest.raises(HTTPException) as denied:
        await readback.read_native(backend, "product-example", "project-example", run.run_id, "job")
    assert denied.value.status_code in {404, 409}
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "changed_revision"])
async def test_native_readback_rechecks_product_revocation_after_provider_read(selected, monkeypatch, change):
    backend, run, binding, grant, credential, calls = selected

    def revoke_after_read(client, selected_run):
        if change == "revoked":
            grant.revoked_at = datetime.now(UTC)
        else:
            run.revision += 1
        return {"native_job_ref": selected_run["native_job_ref"], "status": "COMPLETED"}

    monkeypatch.setattr(readback, "native_job", revoke_after_read)
    with pytest.raises(HTTPException) as denied:
        await readback.read_native(backend, "product-example", "project-example", run.run_id, "job")
    assert denied.value.status_code == (404 if change == "revoked" else 409)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "accepted",
        "foreign_owner",
        "changed_binding",
        "revoked",
        "expired",
        "revoked_during_read",
        "revision_during_read",
        "binding_during_read",
        "authority_during_read",
        "expired_rows",
        "oversized_readback",
    ],
)
async def test_recovery_state_reads_only_current_product_owner_and_durable_attempts(selected, fault):
    backend, run, binding, grant, credential, calls = selected
    attempt = SimpleNamespace(
        attempt_id="attempt-example",
        operation="submit",
        status="outcome_unknown",
        first_work_id="work-example",
        native_job_ref=None,
        reconcile_checks=1,
        started_at=datetime.now(UTC),
    )
    work = SimpleNamespace(
        work_id="work-example", kind="run", generation=1, fence=2, attempts=1, completed_at=datetime.now(UTC), last_enqueued_at=datetime.now(UTC)
    )
    publication = SimpleNamespace(
        publication_id="publication-example",
        result_id="result-example",
        result_version=1,
        state="published",
        schema_version="1.0",
        attempts=2,
        receipt={"receipt_id": "receipt-example", "contribution_id": "publication-example", "admitted_schema": "1.0"},
    )
    queries = []

    async def scoped_rows(statement):
        queries.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        if fault == "revoked_during_read" and len(queries) == 1:
            grant.revoked_at = datetime.now(UTC)
        elif fault == "revision_during_read" and len(queries) == 1:
            run.revision += 1
        elif fault == "binding_during_read" and len(queries) == 1:
            binding.generation += 1
        elif fault == "authority_during_read" and len(queries) == 1:
            grant.revision += 1
        records = {"WorkerAttempt": [attempt], "Work": [work], "Publication": [publication]}[entity.__name__]
        if fault == "oversized_readback":
            records *= 21
        return SimpleNamespace(all=lambda: records)

    backend.db.scalars = scoped_rows
    if fault == "expired_rows":
        expires = []

        def expire_rows():
            expires.append(True)
            if len(expires) == 2:
                attempt.native_job_ref = {"record_id": "altered-after-read"}
                work.completed_at = None
                publication.receipt = None

        backend.db.expire_all = expire_rows
    if fault == "foreign_owner":
        run.admission["scope"] = {**run.admission["scope"], "owner_id": "another-user"}
    elif fault == "changed_binding":
        binding.generation += 1
    elif fault == "revoked":
        grant.revoked_at = datetime.now(UTC)
    elif fault == "expired":
        grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    app = FastAPI()
    app.include_router(create_runs_router(lambda: backend), prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.test") as client:
        response = await client.get("/v1/products/product-example/projects/project-example/services/security/runs/run-example/recovery-state")
    if fault in {"accepted", "expired_rows"}:
        assert response.status_code == 200
        record = response.json()
        assert record["run"]["run_id"] == run.run_id
        assert record["attempts"] == [
            {
                "attempt_id": "attempt-example",
                "operation": "submit",
                "status": "outcome_unknown",
                "first_work_id": "work-example",
                "native_job_ref": None,
                "reconcile_checks": 1,
                "started_at": attempt.started_at.isoformat(),
            }
        ]
        assert len(queries) == 3
        assert record["publications"][0]["receipt"] == {
            "receipt_id": "receipt-example",
            "contribution_id": "publication-example",
            "admitted_schema": "1.0",
        }
        assert record["work"][0]["completed_at"] and not calls
    else:
        assert response.status_code == (409 if fault == "oversized_readback" else 404)
        assert len(queries) == (
            3 if fault in {"revoked_during_read", "revision_during_read", "binding_during_read", "authority_during_read", "oversized_readback"} else 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["accepted", "foreign_actor", "missing_receipt", "revoked", "stale_binding", "expired_access", "wrong_service"])
async def test_security_submission_receipt_reconciles_only_current_product_authority(selected, fault):
    backend, run, binding, grant, _, calls = selected

    async def rows(statement):
        return SimpleNamespace(all=lambda: [])

    backend.db.scalars = rows
    if fault == "foreign_actor":
        backend.actor.user_id = "another-user"
    elif fault == "revoked":
        grant.revoked_at = datetime.now(UTC)
    elif fault == "stale_binding":
        binding.generation += 1
    elif fault == "expired_access":
        backend.actor.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    app = FastAPI()
    app.include_router(create_runs_router(lambda: backend), prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")
    path = (
        "/v1/products/product-example/projects/project-example/services/"
        + ("finops" if fault == "wrong_service" else "security")
        + "/runs/receipts/"
        + ("missing-receipt-01" if fault == "missing_receipt" else "receipt-example-01")
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.invalid") as client:
        response = await client.get(path)
    if fault == "accepted":
        assert response.status_code == 200 and response.json() == {"run_id": run.run_id}
        assert response.headers["Cache-Control"] == "no-store"
    else:
        assert response.status_code == 404
    assert not calls


@pytest.mark.asyncio
async def test_submission_receipt_refuses_remapped_run_after_recovery_read(selected):
    backend, run, _, _, _, _ = selected

    async def rows(statement):
        return SimpleNamespace(all=lambda: [])

    backend.db.scalars = rows
    original_get = backend.db.get
    requests = []

    async def changed_receipt(model_type, key, **kwargs):
        if model_type is RunReceipt:
            requests.append(key)
            return SimpleNamespace(run_id=run.run_id if len(requests) == 1 else "foreign-run")
        return await original_get(model_type, key, **kwargs)

    backend.db.get = changed_receipt
    with pytest.raises(HTTPException) as rejected:
        await readback.read_submission_receipt(backend, "product-example", "project-example", "receipt-example-01")
    assert rejected.value.status_code == 404
    assert len(requests) == 2 and requests[0] == requests[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "present",
        "absent",
        "cleaned_present",
        "linked_missing",
        "wrong_space",
        "bad_receipt",
        "human_owned",
        "linked_binding",
        "used_binding",
        "foreign_scope",
        "revoked_during_read",
        "changed_during_read",
        "wrong_service",
    ],
)
async def test_owned_security_space_cleanup_readback_is_product_scoped(selected, monkeypatch, fault):
    from l5_services.models.setup import SetupIntent

    backend, run, linked, grant, _, calls = selected
    created = datetime.now(UTC)
    owned_ref = {**linked.native_resource_ref, "record_id": "owned-example", "record_revision": created.isoformat()}
    linked.native_resource_ref = {**linked.native_resource_ref, "record_revision": created.isoformat()}
    run.admission["native_space_ref"] = linked.native_resource_ref
    receipt = {
        "operation_id": "operation-example",
        "intent_hash": "intent-example",
        "native_resource_ref": owned_ref,
        "provider_identity": linked.provider_identity,
    }
    owned = SimpleNamespace(
        ownership="adp_created",
        state="ready",
        gaps=[],
        revision=1,
        configuration_id=linked.configuration_id,
        scope=linked.scope,
        connection_ref=linked.connection_ref,
        provider_identity=linked.provider_identity,
        native_resource_ref=owned_ref,
        creation_receipt=receipt,
    )
    intent = SimpleNamespace(
        operation_id="operation-example",
        configuration_id=linked.configuration_id,
        product_id=run.product_id,
        project_id=run.project_id,
        service_id="security",
        actor_id="owner-example",
        scope=linked.scope,
        request={"action": "create"},
        intent_hash="intent-example",
        owned_resource_receipt=None,
    )
    monkeypatch.setattr(readback, "_intent_hash", lambda value: "intent-example")
    original_get = backend.db.get

    async def get(model_type, key, **kwargs):
        if model_type is SetupIntent:
            return intent if key == (run.tenant_id, intent.operation_id) else None
        return await original_get(model_type, key, **kwargs)

    backend.db.get = get
    backend.db.scalar = lambda statement: _async_value("another-run" if fault == "used_binding" else None)

    async def binding(binding_id):
        return owned if binding_id == "owned-binding" else linked

    backend.binding_store = lambda *args: _async_value(SimpleNamespace(binding=binding))
    if fault == "bad_receipt":
        receipt["intent_hash"] = "fabricated"
    if fault == "human_owned":
        owned.ownership = "customer_linked"
    if fault == "linked_binding":
        owned.native_resource_ref = linked.native_resource_ref
    if fault == "foreign_scope":
        owned.scope = {**owned.scope, "tenant_id": "foreign"}
    if fault == "cleaned_present":
        owned.state = "cleaned"

    def assume(**kwargs):
        policy = json.loads(kwargs["session_policy"])["Statement"][0]
        assert policy["Action"] == ["securityagent:BatchGetAgentSpaces"]
        assert set(policy["Resource"]) == {
            f"arn:aws:securityagent:us-east-1:123456789012:agent-space/{space}" for space in ("space-example", "owned-example")
        }
        calls.append(policy)
        return SimpleNamespace(access_key_id="fictional-key", secret_access_key="fictional-secret", session_token="fictional-token")

    monkeypatch.setattr(readback, "assume_role", assume)
    reads = []

    class Native:
        def batch_get_agent_spaces(self, **request):
            assert set(request) == {"agentSpaceIds"}
            reads.append(request["agentSpaceIds"])
            if fault == "revoked_during_read":
                grant.revoked_at = datetime.now(UTC)
            if fault == "changed_during_read":
                owned.revision += 1
            records = [{"agentSpaceId": "space-example", "createdAt": created}]
            missing = []
            if fault not in {"absent", "linked_missing"}:
                records.append(
                    {
                        "agentSpaceId": "owned-example",
                        "createdAt": created,
                        "name": "adp-operation-example",
                        "description": "ADP setup intent-example",
                    }
                )
            else:
                missing.append("owned-example")
            if fault == "linked_missing":
                records = records[1:]
                missing.append("space-example")
            if fault == "wrong_space":
                records[0]["agentSpaceId"] = "different-space"
            return {"agentSpaces": records, "notFound": missing}

    monkeypatch.setattr(readback.boto3, "Session", lambda **kwargs: SimpleNamespace(client=lambda name: Native()))
    backend.native_security_readback = lambda product_id, project_id, run_id, action, **kwargs: (
        readback.read_native(backend, product_id, project_id, run_id, action, **kwargs)
    )
    app = FastAPI()
    app.include_router(create_runs_router(lambda: backend), prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")
    service = "finops" if fault == "wrong_service" else "security"
    route = f"/v1/products/product-example/projects/project-example/services/{service}/runs/run-example/native-readback/spaces/owned-binding"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.invalid") as client:
        response = await client.get(route)
    if fault in {"present", "absent"}:
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {
            "run_id": "run-example",
            "ownership": {"owned-example": "scenario_owned", "space-example": "customer_linked"},
            "resources": {"owned-example": fault, "space-example": "retained"},
        }
        assert reads == [["owned-example", "space-example"]]
    else:
        assert response.status_code in {404, 409}
        if fault in {"bad_receipt", "human_owned", "linked_binding", "used_binding", "foreign_scope", "wrong_service"}:
            assert not reads
        else:
            assert reads


def _async_value(value):
    async def resolve():
        return value

    return resolve()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "accepted",
        "wrong_service",
        "foreign_owner",
        "foreign_result",
        "foreign_payload",
        "missing",
        "invalidated",
        "bad_digest",
        "empty_result_receipt",
        "revoked_during_read",
        "changed_publication",
        "changed_run",
        "expired_access",
    ],
)
async def test_security_publication_evidence_is_pinned_and_reauthorized(selected, fault):
    from copy import deepcopy
    from hashlib import sha256

    from l5_services.models.evidence import AssessmentResult
    from l5_services.models.publication import Publication
    from l5_services.models.result import ResultDetail

    backend, run, _, grant, _, calls = selected
    source = {
        "tenant_id": run.tenant_id,
        "project_id": run.project_id,
        "source_connection_id": "repository-example",
        "record_type": "repository",
        "record_id": "repository-example",
        "record_revision": "a" * 40,
    }
    run.input["sources"] = [source]
    result_ref = {
        "tenant_id": run.tenant_id,
        "project_id": run.project_id,
        "source_connection_id": "l5-security",
        "record_type": "security_result",
        "record_id": "result-example",
        "record_revision": "1",
    }
    payload = {
        "schema_version": "1.0",
        "contribution_id": "publication-example",
        "scope": deepcopy(run.scope),
        "result_ref": result_ref,
        "run_id": run.run_id,
        "native_job_ref": deepcopy(run.native_job_ref),
        "source_refs": [source],
        "manifest_ref": {"artifact_id": "retained-manifest-example"},
        "claims": [],
        "evidence": [],
    }
    publication = SimpleNamespace(
        publication_id="publication-example",
        result_id="result-example",
        result_version=1,
        destination="product-context",
        schema_version="1.0",
        payload=payload,
        state="published",
        receipt={
            "receipt_id": "receipt-example",
            "contribution_id": "publication-example",
            "admitted_schema": "1.0",
            "consumer_revision": "1",
            "record_refs": [result_ref],
        },
        invalidation_watermark=0,
        revision=1,
        attempts=1,
        contribution_hash=sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    )
    result = SimpleNamespace(result_id="result-example", run_id=run.run_id, version=1)
    detail = SimpleNamespace(
        payload={"run_id": run.run_id, "revision": 1, "scope": deepcopy(run.scope), "native_job_ref": deepcopy(run.native_job_ref)}
    )
    if fault == "foreign_owner":
        backend.actor.user_id = "another-owner"
    elif fault == "foreign_result":
        result.run_id = "another-run"
    elif fault == "foreign_payload":
        payload["scope"]["product_id"] = "another-product"
    elif fault == "invalidated":
        publication.invalidation_watermark = 1
    elif fault == "bad_digest":
        publication.contribution_hash = "f" * 64
    elif fault == "empty_result_receipt":
        publication.receipt["record_refs"] = []
    original_get = backend.db.get
    publication_reads = []

    async def get(model, key, **kwargs):
        if model is Publication:
            assert key == (run.tenant_id, publication.publication_id)
            publication_reads.append(True)
            if fault == "missing":
                return None
            if fault == "changed_publication" and len(publication_reads) == 2:
                publication.revision += 1
            if fault == "expired_access" and len(publication_reads) == 2:
                backend.actor.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            return publication
        if model is AssessmentResult:
            return result
        if model is ResultDetail:
            if fault == "revoked_during_read":
                grant.revoked_at = datetime.now(UTC)
            if fault == "changed_run":
                run.revision += 1
            return detail
        return await original_get(model, key, **kwargs)

    async def rows(statement):
        entity = statement.column_descriptions[0]["entity"]
        return SimpleNamespace(all=lambda: [publication] if entity is Publication else [])

    backend.db.get, backend.db.scalars = get, rows
    backend.security_publication = lambda *args: readback.read_publication(backend, *args)
    app = FastAPI()
    app.include_router(create_runs_router(lambda: backend), prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")
    service = "devops" if fault == "wrong_service" else "security"
    path = f"/v1/products/product-example/projects/project-example/services/{service}/runs/run-example/publications/publication-example"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.invalid") as client:
        response = await client.get(path)
    if fault == "accepted":
        assert response.status_code == 200
        packet = response.json()
        assert packet["run_id"] == run.run_id
        assert packet["publication"]["payload"] == payload
        assert packet["publication"]["receipt"]["record_refs"] == [result_ref]
        assert response.headers["Cache-Control"] == "no-store"
        assert len(publication_reads) == 2
    else:
        assert response.status_code == 404
        assert response.json() == {"detail": {"code": "not_found"}}
    assert not calls
