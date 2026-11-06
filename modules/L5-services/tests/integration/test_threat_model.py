"""Verify the Security adapter against pinned Product snapshots and native readback."""

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.adapters.security.threat_model import ThreatModelAdapter
from l5_services.models import AssessmentResult, Configuration, ConfigurationVersion, Finding, Run, Work, WorkerAttempt
from l5_services.models.base import Base
from l5_services.source_manifest import materialize_inputs
from l5_services.workers.execution import AuthorityRevoked, ExecutionService


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def case(tenant, product, revision="rev-1"):
    source = {"tenant_id": tenant, "project_id": f"project-{product}", "source_connection_id": "connector",
              "record_type": "document", "record_id": "architecture", "record_revision": revision}
    document = f"architecture for {tenant}/{product} at {revision}".encode()
    entry = {"source_ref": source, "path": "architecture.md", "sha256": sha256(document).hexdigest(),
             "bytes": len(document)}
    file_manifest = json.dumps({"source_ref": source, "files": [entry], "exclusions": []}).encode()

    def artifact(content, media_type):
        return {"artifact_id": "snapshot-content", "version": "1", "sha256": sha256(content).hexdigest(),
                "media_type": media_type, "bytes": len(content)}

    snapshot = {"snapshot_id": f"snapshot-{tenant}-{product}-{revision}", "source_ref": source,
                "content": artifact(document, "text/markdown"),
                "file_manifest": artifact(file_manifest, "application/json"),
                "file_count": 1, "exclusions": [], "expires_at": (NOW + timedelta(hours=2)).isoformat()}
    admission = {"source_snapshots": [snapshot], "delegation_id": "grant",
                 "delegation_version": "rev-1", "exclusions": [], "configuration_hash": "f" * 64,
                 "scope": {"tenant_id": tenant, "product_id": product, "project_id": source["project_id"]},
                 "profile_ref": source, "adapter_revision": "adapter-1"}
    run = SimpleNamespace(tenant_id=tenant, product_id=product, project_id=source["project_id"],
                          service_id="security", run_id=f"run-{tenant}-{product}-{revision}",
                          binding_id=f"binding-{product}", configuration_revision=1, binding_generation=1,
                          input={"capability": "threat_model", "sources": [source]}, admission=admission,
                          capture_limits={"max_bytes": 1024, "max_files": 10}, manifest=None)
    return run, snapshot, document, file_manifest


class SourceClient:
    def __init__(self, snapshot, document, file_manifest):
        self.snapshot = snapshot
        self.document = document
        self.file_manifest = file_manifest
        self.revoked = False
        self.reads = []

    async def read_source(self, product, project, snapshot_id, request):
        self.reads.append((product, project, snapshot_id, request))
        if self.revoked == "http":
            response = httpx.Response(403, request=httpx.Request("POST", "https://example.invalid/source"))
            raise httpx.HTTPStatusError("source access revoked", request=response.request, response=response)
        if self.revoked:
            raise PermissionError("delegation revoked")
        return self.snapshot

    async def read_source_manifest(self, *args):
        return self.file_manifest

    async def stream_source(self, *args):
        yield self.document


class NativeSecurity:
    def __init__(self, space, model):
        self.space = space
        self.model = model
        self.artifacts = {}
        self.scope_docs = []
        self.jobs = {}
        self.threats = {}
        self.starts = 0

    def batch_get_threat_models(self, *, agentSpaceId, threatModelIds):
        assert agentSpaceId == self.space and threatModelIds == [self.model]
        return {"threatModels": [{"agentSpaceId": self.space, "threatModelId": self.model,
                                  "scopeDocs": self.scope_docs.copy(),
                                  "serviceRole": "arn:aws:iam::123456789012:role/security-execution"}]}

    def add_artifact(self, *, agentSpaceId, artifactContent, artifactType, fileName):
        assert agentSpaceId == self.space and artifactType == "MD"
        artifact_id = f"artifact-{len(self.artifacts) + 1}"
        self.artifacts[artifact_id] = (fileName, artifactContent)
        return {"artifactId": artifact_id}

    def update_threat_model(self, *, agentSpaceId, threatModelId, scopeDocs, serviceRole, **kwargs):
        assert agentSpaceId == self.space and threatModelId == self.model
        assert not kwargs, "Threat models reject documents in assets; send only scopeDocs"
        assert serviceRole == "arn:aws:iam::123456789012:role/security-execution"
        self.scope_docs = scopeDocs.copy()

    def start_threat_model_job(self, *, agentSpaceId, threatModelId):
        assert agentSpaceId == self.space and threatModelId == self.model
        self.starts += 1
        job_id = f"job-{self.starts}"
        self.jobs[job_id] = {"agentSpaceId": self.space, "threatModelId": self.model,
                             "threatModelJobId": job_id, "scopeDocs": self.scope_docs.copy(),
                             "status": "COMPLETED", "createdAt": NOW}
        self.threats[job_id] = [{"threatId": f"threat-{self.starts}", "threatJobId": job_id,
                                 "title": "Verify boundaries", "statement": "Shared document permissions",
                                 "severity": "HIGH", "recommendation": "Scope document access",
                                 "evidence": [{"path": "architecture.md"}]}]
        return {"threatModelJobId": job_id}

    def batch_get_threat_model_jobs(self, *, agentSpaceId, threatModelJobIds):
        assert agentSpaceId == self.space
        return {"threatModelJobs": [self.jobs[job_id] for job_id in threatModelJobIds]}

    def list_threat_model_job_tasks(self, *, agentSpaceId, threatModelJobId, maxResults, nextToken=None):
        assert agentSpaceId == self.space and threatModelJobId in self.jobs
        return {"threatModelJobTaskSummaries": [{"taskId": "task-1", "agentSpaceId": self.space,
                                                 "threatModelId": self.model,
                                                 "threatModelJobId": threatModelJobId,
                                                 "executionStatus": "COMPLETED", "updatedAt": NOW}]}

    def list_threats(self, *, agentSpaceId, threatJobId, maxResults, nextToken=None):
        assert agentSpaceId == self.space and maxResults == 100
        return {"threats": self.threats[threatJobId]}

    def batch_get_threats(self, *, agentSpaceId, threatIds):
        assert agentSpaceId == self.space
        return {"threats": [item for entries in self.threats.values() for item in entries
                            if item["threatId"] in threatIds]}

    def list_artifacts(self, *, agentSpaceId, maxResults, nextToken=None):
        return {"artifactSummaries": [{"artifactId": artifact_id, "fileName": name}
                                      for artifact_id, (name, _) in self.artifacts.items()]}

    def list_threat_model_jobs(self, *, agentSpaceId, threatModelId, maxResults, nextToken=None):
        return {"threatModelJobSummaries": list(self.jobs.values())}


async def prepared(tenant, product, revision="rev-1"):
    run, snapshot, document, file_manifest = case(tenant, product, revision)
    sources = SourceClient(snapshot, document, file_manifest)
    attempt = SimpleNamespace(work_id="work-1", fence=1, attempt_id="attempt-1")
    run.manifest, _ = await materialize_inputs(run, attempt, sources, now=NOW)
    native = NativeSecurity(f"space-{tenant}", f"model-{product}")
    space_ref = {"tenant_id": tenant, "project_id": run.project_id,
                 "source_connection_id": "connector", "record_type": "security_space",
                 "record_id": native.space, "record_revision": NOW.isoformat()}
    model_ref = {**space_ref, "record_type": "threat_model", "record_id": native.model}
    session = {"client": native, "tenant_id": tenant, "project_id": run.project_id,
               "binding_id": run.binding_id, "native_space_ref": space_ref,
               "native_model_ref": model_ref}
    run.admission["native_space_ref"] = session["native_space_ref"]
    run.admission["native_model_ref"] = session["native_model_ref"]
    return run, sources, native, session, attempt


@pytest.mark.asyncio
async def test_source_attributed_assessment():
    for tenant, product in (("tenant-one", "product-one"), ("tenant-two", "product-two")):
        run, sources, native, session, attempt = await prepared(tenant, product)
        adapter = ThreatModelAdapter(sources, clock=lambda: NOW)
        accepted = await adapter.submit(run, run.manifest, attempt, session)
        job_ref = accepted.native_job_ref
        assert accepted.classification == "accepted"
        assert native.artifacts["artifact-1"] == (f"{run.run_id}.md", sources.document)
        assert native.jobs[job_ref["threat_model_job_id"]]["scopeDocs"] == [{"artifactId": "artifact-1"}]
        assert sources.reads[-1][3]["delegation_id"] == "grant"
        observed = await adapter.observe(run, job_ref, attempt, session)
        assert observed.classification == observed.observation["outcome"] == "completed"
        assert observed.observation["tasks"][0]["status"] == "COMPLETED"
        assert observed.observation["tasks"][0]["updatedAt"] == NOW.isoformat().replace("+00:00", "Z")
        collected = await adapter.collect(run, job_ref, attempt, session)
        assert collected.result["coverage"] == "complete"
        assert collected.result["findings"][0]["source_refs"] == run.input["sources"]
        assert collected.result["findings"][0]["validation"] == "candidate"
        assert collected.result["findings"][0]["native_id"] == native.threats[job_ref["threat_model_job_id"]][0]["threatId"]


@pytest.mark.asyncio
async def test_revision_and_replay():
    first, first_source, native, session, attempt = await prepared("tenant-one", "product-one")
    first_adapter = ThreatModelAdapter(first_source, clock=lambda: NOW)
    first_job = (await first_adapter.submit(first, first.manifest, attempt, session)).native_job_ref
    first_result = (await first_adapter.collect(first, first_job, attempt, session)).result
    replay = await first_adapter.reconcile_submit(first, attempt, session)
    assert replay.native_job_ref == first_job and native.starts == 1
    second, second_source, _, _, second_attempt = await prepared("tenant-one", "product-one", "rev-2")
    second_adapter = ThreatModelAdapter(second_source, clock=lambda: NOW)
    second_job = (await second_adapter.submit(second, second.manifest, second_attempt, session)).native_job_ref
    second_result = (await second_adapter.collect(second, second_job, second_attempt, session)).result
    assert native.starts == 2
    assert native.artifacts["artifact-1"][1] == first_source.document
    assert native.artifacts["artifact-2"][1] == second_source.document
    assert native.scope_docs == [{"artifactId": "artifact-2"}]
    assert native.jobs[first_job["threat_model_job_id"]]["scopeDocs"] == [{"artifactId": "artifact-1"}]
    assert first_result["findings"][0]["source_refs"] == first.input["sources"]
    assert second_result["findings"][0]["source_refs"] == second.input["sources"]
    assert first_result["findings"][0]["native_id"] != second_result["findings"][0]["native_id"]


@pytest.mark.asyncio
async def test_invalid_and_partial_evidence():
    run, sources, native, session, attempt = await prepared("tenant-one", "product-one")
    adapter = ThreatModelAdapter(sources, clock=lambda: NOW)
    foreign = {**session, "tenant_id": "tenant-two"}
    with pytest.raises(PermissionError):
        await adapter.submit(run, run.manifest, attempt, foreign)
    sources.revoked = True
    with pytest.raises(PermissionError):
        await adapter.submit(run, run.manifest, attempt, session)
    sources.revoked = False
    sources.snapshot = {**sources.snapshot, "source_ref": {**sources.snapshot["source_ref"],
                                                           "tenant_id": "tenant-two"}}
    assert (await adapter.submit(run, run.manifest, attempt, session)).classification == "definitive_failure"
    sources.snapshot = run.admission["source_snapshots"][0]
    adapter.clock = lambda: NOW + timedelta(hours=3)
    assert (await adapter.submit(run, run.manifest, attempt, session)).classification == "definitive_failure"
    adapter.clock = lambda: NOW
    assert native.starts == 0 and not native.artifacts
    accepted = await adapter.submit(run, run.manifest, attempt, session)
    job_ref = accepted.native_job_ref
    native.threats[job_ref["threat_model_job_id"]][0]["threatJobId"] = "foreign-job"
    with pytest.raises(ValueError, match="foreign"):
        await adapter.collect(run, job_ref, attempt, session)
    native.threats[job_ref["threat_model_job_id"]][0]["threatJobId"] = job_ref["threat_model_job_id"]
    original_details = native.batch_get_threats

    def foreign_details(**kwargs):
        response = original_details(**kwargs)
        return {"threats": [{**response["threats"][0], "threatJobId": "foreign-job"}]}

    native.batch_get_threats = foreign_details
    with pytest.raises(ValueError, match="another job"):
        await adapter.collect(run, job_ref, attempt, session)
    native.batch_get_threats = original_details
    original_list = native.list_threats

    def partial_page(**kwargs):
        return {"threats": original_list(**kwargs)["threats"] if not kwargs.get("nextToken") else [],
                "nextToken": "repeat"}

    native.list_threats = partial_page
    incomplete = await adapter.collect(run, job_ref, attempt, session)
    assert incomplete.classification == "unknown" and incomplete.native_job_ref == job_ref
    assert incomplete.result is None
    native.list_threats = original_list
    def missing_detail(**kwargs):
        return {"threats": [], "notFound": kwargs["threatIds"]}

    native.batch_get_threats = missing_detail
    missing = await adapter.collect(run, job_ref, attempt, session)
    assert missing.classification == "unknown" and missing.native_job_ref == job_ref
    native.batch_get_threats = original_details
    native.threats[job_ref["threat_model_job_id"]].append({
        "threatId": "threat-2", "threatJobId": job_ref["threat_model_job_id"],
        "statement": "Extra exposed asset", "severity": "MEDIUM"})

    def paged(**kwargs):
        threats = original_list(**kwargs)["threats"]
        if kwargs.get("nextToken"):
            return {"threats": threats[1:]}
        return {"threats": threats[:1], "nextToken": "page-two"}

    native.list_threats = paged
    complete = await adapter.collect(run, job_ref, attempt, session)
    assert complete.result["finding_count"] == 2 and complete.result["coverage"] == "complete"
    native.jobs[job_ref["threat_model_job_id"]]["scopeDocs"] = [{"artifactId": "foreign-artifact"}]
    with pytest.raises(ValueError, match="readback mismatch"):
        await adapter.observe(run, job_ref, attempt, session)
    native.jobs[job_ref["threat_model_job_id"]]["scopeDocs"] = [{"artifactId": job_ref["artifact_id"]}]
    native.jobs[job_ref["threat_model_job_id"]]["status"] = "FAILED"
    native.jobs[job_ref["threat_model_job_id"]]["errorInformation"] = {
        "code": "CLIENT_ERROR", "message": "Invalid document"}
    failed = await adapter.observe(run, job_ref, attempt, session)
    assert failed.classification == "failed"
    assert failed.observation["error"]["code"] == "CLIENT_ERROR"
    assert failed.observation["createdAt"] == NOW.isoformat().replace("+00:00", "Z")


def test_securityagent_sdk_shapes():
    import botocore
    import botocore.loaders
    import botocore.session

    assert botocore.__version__ == "1.43.119"
    model = botocore.session.get_session().get_service_model("securityagent")
    service_model = botocore.loaders.create_loader().load_data(
        f"securityagent/{model.api_version}/service-2")
    assert sha256(json.dumps(service_model, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == (
        "422dd9181e1f42d0ea5d991c8d79252d9c17894ad232754b076368be20d13e24")
    shapes = {
        "AddArtifact": ("artifactContent", "artifactType", "fileName"),
        "CreateThreatModel": ("scopeDocs", "serviceRole", "agentSpaceId"),
        "UpdateThreatModel": ("assets", "threatModelId", "scopeDocs"),
        "StartThreatModelJob": ("agentSpaceId", "threatModelId"),
        "BatchGetThreatModelJobs": ("agentSpaceId", "threatModelJobIds"),
        "ListThreatModelJobTasks": ("agentSpaceId", "threatModelJobId"),
        "ListThreats": ("agentSpaceId", "threatJobId"),
        "BatchGetThreats": ("agentSpaceId", "threatIds"),
    }
    for operation, members in shapes.items():
        assert set(members) <= set(model.operation_model(operation).input_shape.members)


@pytest.mark.asyncio
@pytest.mark.parametrize(("tenant", "product", "deny", "lost_start"), (
    ("tenant-one", "product-one", False, False),
    ("tenant-two", "product-two", False, False),
    ("tenant-three", "product-three", "revoked", False),
    ("tenant-four", "product-four", False, True),
    ("tenant-five", "product-five", "foreign", False),
    ("tenant-six", "product-six", "stale", False),
    ("tenant-seven", "product-seven", "expired", False),
    ("tenant-eight", "product-eight", "scope_rejected", False),
))
async def test_worker_source_attributed_readback(tmp_path, tenant, product, deny, lost_start):
    run, sources, native, provider_session, _ = await prepared(tenant, product)
    if deny == "scope_rejected":
        from botocore.exceptions import ClientError

        def reject_scope(**kwargs):
            raise ClientError({"Error": {"Code": "ValidationException",
                                         "Message": "private provider document details"}}, "UpdateThreatModel")

        native.update_threat_model = reject_scope
    if lost_start:
        original_start = native.start_threat_model_job

        def start_without_receipt(**kwargs):
            original_start(**kwargs)
            raise TimeoutError("native start response lost")

        native.start_threat_model_job = start_without_receipt
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'assessment.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as database:
        database.add(Configuration(tenant_id=run.tenant_id, configuration_id="configuration",
                                   product_id=run.product_id, project_id=run.project_id, service_id="security",
                                   relationship_revision="1", owner_id="owner", current_revision=1,
                                   handoff_state="linked"))
        database.add(ConfigurationVersion(tenant_id=run.tenant_id, configuration_id="configuration",
                                          revision=1, settings={}, inherited_fields=[], gaps=[],
                                          state="ready", hash="f" * 64))
        database.add(Run(tenant_id=run.tenant_id, run_id=run.run_id, product_id=run.product_id,
                         project_id=run.project_id, service_id="security", configuration_id="configuration",
                         configuration_revision=1, binding_id=run.binding_id, trigger_id="trigger",
                         binding_generation=1, input=run.input,
                         admission={**run.admission, "monitoring_policy": {"poll_interval_seconds": 600}},
                         capture_limits=run.capture_limits, deadline_at=(
                             NOW - timedelta(seconds=1) if deny == "expired" else NOW + timedelta(hours=1))))
        database.add(Work(tenant_id=run.tenant_id, work_id="work-start", run_id=run.run_id,
                          kind="execute", generation=1, step=0, due_at=NOW))
        await database.commit()
    delivered = []
    clock = {"now": NOW}

    async def publish(envelope, delay):
        delivered.append(envelope)

    async def authorize(stored, claim, operation):
        assert stored.tenant_id == claim.tenant_id and stored.binding_id == provider_session["binding_id"]
        if deny == "foreign" and operation == "submit":
            raise AuthorityRevoked("foreign Product credential receipt")
        return provider_session

    service = ExecutionService(sessions, sources, None, publish, authorize,
                               security_threat_adapter=ThreatModelAdapter(sources, clock=lambda: clock["now"]),
                               clock=lambda: clock["now"])
    delivered.append({"schema_version": "1.0", "work_id": "work-start", "generation": 1})
    try:
        if deny == "stale":
            assert not await service.execute({**delivered[0], "generation": 2})
            assert native.starts == 0 and not native.artifacts
        if deny == "expired":
            assert await service.execute(delivered[0])
            async with sessions() as database:
                stored = await database.get(Run, (run.tenant_id, run.run_id))
                assert stored.state == "expired"
                assert (await database.scalars(select(WorkerAttempt))).all() == []
            assert native.starts == 0 and not native.artifacts
            return
        for step in range(5 if lost_start else 4):
            if step == 1 and deny == "revoked":
                sources.revoked = "http"
                assert await service.execute(delivered[step])
                async with sessions() as database:
                    denied = await database.get(Run, (run.tenant_id, run.run_id))
                    assert denied.state == "blocked" and denied.native_job_ref is None
                assert native.starts == 0 and not native.artifacts
                return
            if step == 1 and deny == "foreign":
                assert await service.execute(delivered[step])
                async with sessions() as database:
                    rejected = await database.get(Run, (run.tenant_id, run.run_id))
                    attempts = (await database.scalars(select(WorkerAttempt))).all()
                    assert rejected.state == "blocked" and rejected.native_job_ref is None
                    assert len(attempts) == 1 and attempts[0].status == "prepared"
                assert native.starts == 0 and not native.artifacts
                assert not await service.execute(delivered[step])
                return
            if step == 3 and not lost_start:
                original_list = native.list_threats

                def partial_page(**kwargs):
                    return {"threats": [], "nextToken": "repeat"}

                native.list_threats = partial_page
                with pytest.raises(ValueError, match="incomplete result collection"):
                    await service.execute(delivered[step])
                async with sessions() as database:
                    retained = await database.get(Run, (run.tenant_id, run.run_id))
                    assert retained.state == "collecting"
                    assert retained.native_job_ref["threat_model_job_id"] == "job-1"
                native.list_threats = original_list
                clock["now"] += timedelta(seconds=61)
            assert await service.execute(delivered[step])
            if step == 1 and deny == "scope_rejected":
                async with sessions() as database:
                    stored = await database.get(Run, (run.tenant_id, run.run_id))
                    attempts = (await database.scalars(select(WorkerAttempt))).all()
                    assert stored.state == "failed" and stored.provider_state == "not_started"
                    assert stored.native_job_ref is None
                    assert len(attempts) == 1 and attempts[0].status == "definitive_failure"
                    diagnosis = stored.monitoring["last_poll_error"]
                    assert diagnosis["stage"] == "update_threat_model"
                    assert diagnosis["cause"] == "ValidationException"
                    assert "No assessment was started" in diagnosis["next_action"]
                    assert "private provider" not in json.dumps(stored.monitoring)
                assert native.starts == 0 and len(delivered) == 2
                assert not await service.execute(delivered[step])
                return
            if step == 1 and lost_start:
                async with sessions() as database:
                    stored = await database.get(Run, (run.tenant_id, run.run_id))
                    attempts = (await database.scalars(select(WorkerAttempt).where(
                        WorkerAttempt.run_id == run.run_id))).all()
                    results = (await database.scalars(select(AssessmentResult).where(
                        AssessmentResult.run_id == run.run_id))).all()
                    assert stored.state == "outcome_unknown" and stored.native_job_ref is None
                    assert len(attempts) == 1 and attempts[0].status == "outcome_unknown"
                    assert results == [] and native.starts == 1
                assert not await service.execute(delivered[1])
            if (step == 1 and not lost_start) or (step == 2 and lost_start):
                async with sessions() as database:
                    stored = await database.get(Run, (run.tenant_id, run.run_id))
                    assert stored.native_job_ref["threat_model_job_id"] == "job-1"
                assert native.starts == 1
            if step == 1 or (step == 2 and lost_start):
                clock["now"] += timedelta(minutes=10)
        async with sessions() as database:
            stored = await database.get(Run, (run.tenant_id, run.run_id))
            findings = (await database.scalars(select(Finding))).all()
            results = (await database.scalars(select(AssessmentResult))).all()
            assert stored.state == "succeeded" and stored.native_job_ref["artifact_id"] == "artifact-1"
            assert len(findings) == len(results) == 1
            assert findings[0].payload["source_refs"] == run.input["sources"]
            assert findings[0].payload["validation"] == "candidate"
            assert results[0].payload["native_job_ref"] == stored.native_job_ref
            if lost_start:
                attempts = (await database.scalars(select(WorkerAttempt).where(
                    WorkerAttempt.run_id == run.run_id))).all()
                assert len(attempts) == 1 and attempts[0].status == "accepted"
        assert not await service.execute(delivered[1])
        assert native.starts == 1
        if lost_start:
            native.start_threat_model_job = original_start
        revised, revised_source, _, _, _ = await prepared(tenant, product, "rev-2")
        async with sessions() as database:
            database.add(Run(tenant_id=tenant, run_id=revised.run_id, product_id=product,
                             project_id=revised.project_id, service_id="security", configuration_id="configuration",
                             configuration_revision=1, binding_id=revised.binding_id, trigger_id="trigger",
                             binding_generation=1, input=revised.input,
                             admission={**revised.admission, "monitoring_policy": {"poll_interval_seconds": 600}},
                             capture_limits=revised.capture_limits, deadline_at=NOW + timedelta(hours=2)))
            database.add(Work(tenant_id=tenant, work_id="work-revision", run_id=revised.run_id,
                              kind="execute", generation=1, step=0, due_at=clock["now"]))
            await database.commit()
        revision_delivery = [{"schema_version": "1.0", "work_id": "work-revision", "generation": 1}]

        async def publish_revision(envelope, delay):
            revision_delivery.append(envelope)

        revised_service = ExecutionService(sessions, revised_source, None, publish_revision, authorize,
                                           security_threat_adapter=ThreatModelAdapter(
                                               revised_source, clock=lambda: clock["now"]),
                                           clock=lambda: clock["now"])
        for step in range(4):
            assert await revised_service.execute(revision_delivery[step])
            if step == 1:
                clock["now"] += timedelta(minutes=10)
        assert not await service.execute(delivered[0])
        assert native.starts == 2 and native.scope_docs == [{"artifactId": "artifact-2"}]
        assert native.jobs["job-1"]["scopeDocs"] == [{"artifactId": "artifact-1"}]
        async with sessions() as database:
            results = (await database.scalars(select(AssessmentResult).order_by(AssessmentResult.run_id))).all()
            findings = (await database.scalars(select(Finding).order_by(Finding.native_id))).all()
            assert len(results) == len(findings) == 2
            assert {result.run_id for result in results} == {run.run_id, revised.run_id}
            assert {finding.payload["source_refs"][0]["record_revision"] for finding in findings} == {
                "rev-1", "rev-2"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_workers_serialize_jobs_on_one_product_model(tmp_path):
    first, first_source, native, first_session, _ = await prepared("tenant-one", "product-one")
    second, second_source, _, second_session, _ = await prepared("tenant-one", "product-one", "rev-2")
    second_session["client"] = native
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'shared-model.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as database:
        database.add(Configuration(tenant_id=first.tenant_id, configuration_id="configuration",
                                   product_id=first.product_id, project_id=first.project_id, service_id="security",
                                   relationship_revision="1", owner_id="owner", current_revision=1,
                                   handoff_state="linked"))
        database.add(ConfigurationVersion(tenant_id=first.tenant_id, configuration_id="configuration",
                                          revision=1, settings={}, inherited_fields=[], gaps=[],
                                          state="ready", hash="f" * 64))
        for index, run in enumerate((first, second), 1):
            database.add(Run(tenant_id=run.tenant_id, run_id=run.run_id, product_id=run.product_id,
                             project_id=run.project_id, service_id="security", configuration_id="configuration",
                             configuration_revision=1, binding_id=run.binding_id, trigger_id="trigger",
                             binding_generation=1, input=run.input,
                             admission={**run.admission, "monitoring_policy": {"poll_interval_seconds": 600}},
                             manifest=run.manifest, state="queued", capture_limits=run.capture_limits,
                             deadline_at=NOW + timedelta(hours=1)))
            database.add(Work(tenant_id=run.tenant_id, work_id=f"submit-{index}", run_id=run.run_id,
                              kind="submit", generation=1, step=1, due_at=NOW))
        await database.commit()

    started = threading.Event()
    release = threading.Event()
    original_start = native.start_threat_model_job

    def pause_first_start(**kwargs):
        if not started.is_set():
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("first native start was not released")
        return original_start(**kwargs)

    native.start_threat_model_job = pause_first_start

    async def authorize(run, claim, operation):
        assert operation == "submit" and run.run_id == claim.run_id
        return first_session if run.run_id == first.run_id else second_session

    async def publish(envelope, delay):
        pass

    workers = [ExecutionService(sessions, source, None, publish, authorize,
                                security_threat_adapter=ThreatModelAdapter(source, clock=lambda: NOW),
                                clock=lambda: NOW)
               for source in (first_source, second_source)]
    first_message = {"schema_version": "1.0", "work_id": "submit-1", "generation": 1}
    second_message = {"schema_version": "1.0", "work_id": "submit-2", "generation": 1}
    try:
        first_task = asyncio.create_task(workers[0].execute(first_message))
        assert await asyncio.to_thread(started.wait, 3)
        second_task = asyncio.create_task(workers[1].execute(second_message))
        await asyncio.sleep(0.15)
        assert native.starts == 0
        release.set()
        assert await asyncio.wait_for(first_task, 5)
        assert await asyncio.wait_for(second_task, 5)
        async with sessions() as database:
            stored = (await database.scalars(select(Run).where(Run.run_id.in_((first.run_id, second.run_id))))).all()
            attempts = (await database.scalars(select(WorkerAttempt))).all()
        assert len(attempts) == 2 and all(attempt.status == "accepted" for attempt in attempts)
        assert len(stored) == 2 and native.starts == 2
        for run in stored:
            artifact_id = run.native_job_ref["artifact_id"]
            assert native.jobs[run.native_job_ref["threat_model_job_id"]]["scopeDocs"] == [
                {"artifactId": artifact_id}]
            assert native.artifacts[artifact_id][0] == f"{run.run_id}.md"
        assert not await workers[0].execute(first_message)
        assert not await workers[1].execute(second_message)
        assert native.starts == 2
    finally:
        release.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_installed_security_factory_executes_document_model_without_ambient_provider_identity(tmp_path, monkeypatch):
    from l5_services import runtime
    from l5_services.adapters.security.code_review import CodeReviewAdapter

    run, sources, native, _, _ = await prepared("tenant-one", "product-one")
    run.admission["connection_ref"] = {"tenant_id": run.tenant_id, "project_id": run.project_id,
                                       "source_connection_id": "connector", "record_type": "connection",
                                       "record_id": "connector", "record_revision": "1"}
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Selected",
                "region": "us-east-1"}
    run.admission["provider_identity"] = identity
    run.admission["monitoring_policy"] = {"poll_interval_seconds": 600}
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'installed.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as database:
        database.add(Configuration(tenant_id=run.tenant_id, configuration_id="configuration",
                                   product_id=run.product_id, project_id=run.project_id, service_id="security",
                                   relationship_revision="1", owner_id="owner", current_revision=1,
                                   handoff_state="linked"))
        database.add(ConfigurationVersion(tenant_id=run.tenant_id, configuration_id="configuration",
                                          revision=1, settings={}, inherited_fields=[], gaps=[],
                                          state="ready", hash="f" * 64))
        database.add(Run(tenant_id=run.tenant_id, run_id=run.run_id, product_id=run.product_id,
                         project_id=run.project_id, service_id="security", configuration_id="configuration",
                         configuration_revision=1, binding_id=run.binding_id, trigger_id="trigger",
                         binding_generation=1, input=run.input, admission=run.admission,
                         capture_limits=run.capture_limits, deadline_at=NOW + timedelta(hours=1)))
        database.add(Work(tenant_id=run.tenant_id, work_id="work-start", run_id=run.run_id,
                          kind="execute", generation=1, step=0, due_at=NOW))
        await database.commit()

    class Gateway:
        async def request(self, *args, **kwargs):
            raise AssertionError("source access must use Product-scoped client")

        def stream(self, *args, **kwargs):
            raise AssertionError("source access must use Product-scoped client")

        async def post(self, path, *, json):
            assert path == "/internal/v1/l5/credential-session"
            async with sessions() as database:
                claimed = await database.get(Work, (run.tenant_id, json["attempt_id"]))
                assert claimed.lease_owner == json["lease_owner"] and claimed.fence == json["lease_fence"]
            receipt = {"operation_id": json["operation_id"], "attempt_id": json["attempt_id"],
                       "delegation_revision": json["delegation_revision"],
                       "provider_identity": identity,
                       "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
            return SimpleNamespace(json=lambda: receipt, raise_for_status=lambda: None)

    messages = [{"schema_version": "1.0", "work_id": "work-start", "generation": 1}]

    async def publish(envelope, delay):
        messages.append(envelope)

    monkeypatch.setenv("L5_SERVICE_ID", "security")
    monkeypatch.setattr(runtime, "_gateway_transport", Gateway)
    monkeypatch.setattr(runtime, "_sessions", lambda: sessions)
    monkeypatch.setattr(runtime, "_archive_store", lambda: SimpleNamespace(put_archive=lambda *args: None,
                                                                           get_archive=lambda *args: None))
    monkeypatch.setattr(runtime, "_queue_publisher", lambda: publish)
    monkeypatch.setattr(runtime, "_ScopedSources", lambda transport: sources)
    monkeypatch.setattr(runtime, "_provider_session", lambda receipt, region: SimpleNamespace(
        client=lambda service, region_name: native if service == "securityagent" else None))
    worker = runtime.factory()
    clock = {"now": NOW}
    worker.clock = lambda: clock["now"]
    worker.security_threat_adapter.clock = lambda: clock["now"]
    assert isinstance(worker.adapter, CodeReviewAdapter)
    assert isinstance(worker.security_threat_adapter, ThreatModelAdapter)
    try:
        for step in range(4):
            assert await worker.execute(messages[step])
            if step == 1:
                clock["now"] += timedelta(minutes=10)
        async with sessions() as database:
            stored = await database.get(Run, (run.tenant_id, run.run_id))
            results = (await database.scalars(select(AssessmentResult))).all()
            assert stored.state == "succeeded" and stored.native_job_ref["threat_model_id"] == native.model
            assert results[0].payload["findings"][0]["source_refs"] == run.input["sources"]
        assert native.starts == 1
        assert not await worker.execute(messages[1])
        assert native.starts == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["ValidationException", "AccessDeniedException", "ResourceNotFoundException",
                                 "InternalServerException", "ThrottlingException"])
async def test_native_scope_error_classification(code):
    from botocore.exceptions import ClientError

    run, sources, native, session, attempt = await prepared("tenant", "product")
    def reject(**kwargs):
        raise ClientError({"Error": {"Code": code, "Message": "private document contents"}}, "UpdateThreatModel")
    native.update_threat_model = reject
    adapter = ThreatModelAdapter(sources, clock=lambda: NOW)
    if code in {"InternalServerException", "ThrottlingException"}:
        with pytest.raises(ClientError):
            await adapter.submit(run, run.manifest, attempt, session)
    else:
        result = await adapter.submit(run, run.manifest, attempt, session)
        assert result.classification == "definitive_failure"
        assert result.observation["cause"] == code
        assert "private document" not in json.dumps(result.observation)
    assert native.starts == 0
