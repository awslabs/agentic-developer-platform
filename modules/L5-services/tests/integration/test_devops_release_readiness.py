"""Offline, independent source snapshots and native release-review readbacks."""

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
import httpx
from fastapi import FastAPI
from l5_services.api.router import create_router

from jsonschema import Draft202012Validator
import pytest
from fastapi import HTTPException
from botocore.exceptions import ReadTimeoutError

from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.adapters.devops.release_readiness import (
    QUALIFICATION_CODE, QUALIFICATION_MESSAGE, ReleaseReadinessAdapter,
    _description, _outcome, _reference, _selection,
)
from l5_services.devops.release_prerequisites import INDEXING_UNAVAILABLE, release_association_matches, release_eligible
from l5_services.admission import admit_run
from l5_services.admission import reconcile_and_dispatch, submit_and_dispatch
from l5_services.findings import ResultStore
from l5_services.run_access import RunStore
from l5_services.models import (AssessmentResult, Configuration, ConfigurationVersion, Finding,
                                ProviderObservation, ResultDetail, Run, RunReceipt, Work, WorkerAttempt)
from l5_services.models.base import Base
from l5_services.workers.execution import ExecutionService
from l5_services.workers.relay import repair_once


NOW = datetime(2026, 10, 26, 9, tzinfo=UTC)
SCHEMA = json.loads((Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1/schemas.json").read_text())


class HistoricalSubmission(ReleaseReadinessAdapter):
    """Offline fixture for effects accepted before the new-write gate existed.

    This deliberately uses the old description/token without enabling the
    production adapter to submit. All post-upgrade recovery uses the real adapter.
    """

    async def submit(self, run, manifest, attempt, provider_session):
        space_id, client, eligibility = _selection(run, manifest, provider_session)
        description = _description(run, manifest, attempt)
        reference = _reference(eligibility)
        created = client.create_backlog_task(
            agentSpaceId=space_id, taskType="RELEASE_READINESS_REVIEW",
            title=f"Release readiness: {manifest['head_revision']}", description=description,
            reference=reference, clientToken=attempt.idempotency_token)
        task = client.get_backlog_task(agentSpaceId=space_id, taskId=created["task"]["taskId"])["task"]
        return _outcome(run, space_id, task, description, reference)


def artifact(content):
    return {"artifact_id": sha256(content).hexdigest()[:16], "version": "1",
            "sha256": sha256(content).hexdigest(), "media_type": "application/zip", "bytes": len(content)}



def test_native_release_sdk_contract():
    from botocore.session import Session

    model = Session().get_service_model("devops-agent")
    operation = model.operation_model("CreateBacklogTask")
    assert "RELEASE_READINESS_REVIEW" in operation.input_shape.members["taskType"].metadata["enum"]
    assert operation.input_shape.members["clientToken"].metadata["idempotencyToken"] is True
    assert {"system", "referenceId", "referenceUrl", "associationId"} <= set(
        operation.input_shape.members["reference"].required_members)
    assert {"taskId", "executionId", "description", "reference"} <= set(
        model.operation_model("GetBacklogTask").output_shape.members["task"].members)
def zip_content(text):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("service/handler.py", text)
    return output.getvalue()


async def _scenario(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'release.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scope = {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one",
             "relationship_revision": "1", "owner_id": "owner-one", "visibility": "only_me"}
    reference = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "github-one",
                 "record_type": "repository_commit", "record_id": "other-team/portable-project",
                 "record_revision": "head-123"}
    eligibility_ref = {**reference, "record_type": "native_eligibility", "record_id": "check-1"}
    options = {"kind": "release_readiness", "repository_ref": reference, "base_revision": "base-123",
               "head_revision": "head-123", "eligibility_ref": eligibility_ref}
    request = {"service_id": "devops", "configuration_revision": 1, "binding_id": "binding-one",
               "capability": "release_readiness", "devops_input": options}
    assert not list(Draft202012Validator({**SCHEMA, "$ref": "#/$defs/RunInput"}).iter_errors(request))
    eligibility = {"status": "ready", "task_permission": "allowed", "source_indexed": True,
                   "permission_action": "CreateBacklogTask", "permission_space_id": "space-one",
                   "indexed_revisions": ["base-123", "head-123"],
                   "repository_ref": reference, "base_revision": "base-123", "head_revision": "head-123",
                   "eligibility_ref": eligibility_ref, "association_id": "association-one",
                   "reference_id": "portable-project", "reference_url": "https://offline.invalid/change/123",
                   "deployment_context": {"target": "staging", "revision": "head-123"}}
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(Configuration(tenant_id="tenant-one", configuration_id="configuration-one",
                                  product_id="product-one", project_id="project-one", service_id="devops",
                                  relationship_revision="1", owner_id="owner-one", current_revision=1,
                                  handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id="tenant-one", configuration_id="configuration-one",
                                         revision=1, settings={}, inherited_fields=[], gaps=[],
                                         state="ready", hash="f" * 64))
        await session.commit()
    base_text, head_text = "def answer():\n    return 1\n", "def answer():\n    return 2\n"
    texts = [base_text, head_text]
    zipped = [zip_content(text) for text in texts]
    source_refs = [{**reference, "record_revision": "base-123"}, reference]
    file_entries = [{"source_ref": source, "path": "service/handler.py", "sha256": sha256(text.encode()).hexdigest(),
                     "bytes": len(text.encode())} for source, text in zip(source_refs, texts, strict=True)]
    manifests = [json.dumps({"source_ref": source, "files": [entry], "exclusions": []}).encode()
                 for source, entry in zip(source_refs, file_entries, strict=True)]
    snapshots = [{"snapshot_id": f"snapshot-{index}", "source_ref": source,
                  "content": artifact(zipped[index]), "file_manifest": artifact(manifests[index]),
                  "file_count": 1, "exclusions": [], "expires_at": (NOW + timedelta(hours=2)).isoformat()}
                 for index, source in enumerate(source_refs)]

    class SourceClient:
        pins = 0

        async def pin_source(self, product_id, project_id, payload, *, idempotency_key):
            assert (product_id, project_id) == ("product-one", "project-one")
            self.pins += 1
            return snapshots[source_refs.index(payload["source_ref"])]

        async def read_source(self, product_id, project_id, snapshot_id, payload):
            return snapshots[int(snapshot_id[-1])]

        async def read_source_manifest(self, product_id, project_id, snapshot_id, payload):
            return manifests[int(snapshot_id[-1])]

        async def stream_source(self, product_id, project_id, snapshot_id, payload):
            yield zipped[int(snapshot_id[-1])]

    class ArchiveStore:
        async def put_archive(self, run_id, content):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                assert archive.namelist() == ["0/service/handler.py", "1/service/handler.py"]
            return artifact(content)

    connection_ref = {**reference, "source_connection_id": "aws-host-one",
                      "record_type": "connection", "record_id": "aws-host-one"}
    identity = {"account_id": "000000000000", "region": "us-east-1", "role_arn": "role-test"}
    verified = {"scope": scope, "service_id": "devops", "actor_id": "owner-one", "actor_type": "human",
                "configuration_id": "configuration-one", "configuration_revision": 1,
                "configuration_hash": "f" * 64, "configuration_enabled": True,
                "binding_id": "binding-one", "binding_generation": 2, "binding_state": "ready",
                "connection_ref": connection_ref, "binding_connection_ref": connection_ref,
                "native_space_ref": {**reference, "source_connection_id": "aws-host-one",
                                     "record_type": "agent_space", "record_id": "space-one"},
                "provider_identity": identity, "release_eligibility": eligibility,
                "delegation_id": "delegation-one", "delegation_version": 1, "delegation_active": True,
                "profile_ref": {**reference, "record_type": "product", "record_id": "product-one"},
                "trigger": {"trigger_id": "manual-one", "kind": "manual", "enabled": True,
                            "capability": "release_readiness", "binding_id": "binding-one",
                            "source_refs": [reference]},
                "adapter_revision": "release-diff-v1", "visibility": "only_me", "classification": "internal",
                "causal_origin": "manual", "exclusions": [], "max_bytes": 4096, "max_files": 10,
                "monitoring_policy": {"poll_interval_seconds": 600, "poll_grace_seconds": 300},
                "limits": {"deadline_seconds": 3600, "max_provider_attempts": 1, "max_polls": 10,
                           "concurrency": 1, "max_cost": "1.00", "currency": "USD",
                           "policy_ref": {**reference, "record_type": "policy"}}}

    async def budget_guard(session, limits, bound_scope, now, replay):
        assert bound_scope == scope
        return await replay()

    return engine, sessions, scope, reference, eligibility, request, verified, SourceClient(), ArchiveStore(), budget_guard


def use_native_association(eligibility):
    eligibility.update(source_indexing=dict(INDEXING_UNAVAILABLE), source_indexed=None, indexed_revisions=[],
                       native_association={"agentSpaceId": "space-one", "associationId": "association-one",
                           "status": "valid", "serviceId": "github-service-one",
                           "configuration": {"github": {"owner": "other-team", "repoName": "portable-project"}},
                           "capabilities": {"RELEASE_READINESS_REVIEW": {"enabled": True}}})


@pytest.mark.parametrize("repository_id", ["other-team/portable-project", "12345"])
def test_native_association_accepts_the_selected_repository_identity(repository_id):
    eligibility = {}
    use_native_association(eligibility)
    association = eligibility["native_association"]
    association["configuration"]["github"]["repoId"] = "12345"
    assert release_association_matches(association, {"record_id": repository_id}, "space-one", "association-one")
    assert not release_association_matches(association, {"record_id": "foreign"}, "space-one", "association-one")


@pytest.mark.parametrize("change", [
    lambda record: record.update(source_indexed=True),
    lambda record: record.update(indexed_revisions=["base-123", "head-123"]),
    lambda record: record["source_indexing"].update(source="unverified"),
    lambda record: record["native_association"].update(agentSpaceId="foreign"),
    lambda record: record["native_association"].update(associationId="foreign"),
    lambda record: record["native_association"].update(status="invalid"),
    lambda record: record["native_association"].update(status="pending-confirmation"),
    lambda record: record["native_association"]["configuration"]["github"].update(owner="foreign"),
    lambda record: record["native_association"]["capabilities"]["RELEASE_READINESS_REVIEW"].update(enabled=False),
    lambda record: record.update(head_revision="foreign"),
])
async def test_unavailable_indexing_requires_matching_native_association(tmp_path, change):
    engine, _, scope, _, eligibility, request, verified, *_ = await _scenario(tmp_path)
    try:
        use_native_association(eligibility)
        arguments = (request["devops_input"], eligibility, scope,
                     verified["connection_ref"], verified["native_space_ref"])
        assert release_eligible(*arguments)
        change(eligibility)
        assert not release_eligible(*arguments)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("indexing_available", [True, False], ids=["legacy-indexed", "native-indexing-unavailable"])
@pytest.mark.parametrize("malformed_citations", [None, 1, True],
                         ids=["valid", "numeric-citations", "boolean-citations"])
async def test_release_review(tmp_path, malformed_citations, indexing_available):
    engine, sessions, scope, reference, eligibility, request, verified, source_client, archive_store, budget_guard = (
        await _scenario(tmp_path))
    if not indexing_available:
        use_native_association(eligibility)
    messages = []

    async def publish(envelope, delay):
        messages.append(envelope)
    class Backend:
        def __init__(self, session):
            self.session = session

        async def submit(self, product_id, project_id, service_id, run_input, key):
            assert (product_id, project_id, service_id) == ("product-one", "project-one", "devops")
            return await submit_and_dispatch(self.session, sessions, source_client, publish,
                                             run_input, verified, key, now=NOW, budget_guard=budget_guard)

        def store(self):
            return RunStore(self.session, "tenant-one", "product-one", "project-one", "devops", "owner-one", "1")

        async def get_run(self, product_id, project_id, service_id, run_id):
            return await self.store().get(run_id)

        async def list_runs(self, product_id, project_id, service_id, cursor=None):
            return await self.store().list(cursor=cursor)

        async def result_store(self, product_id, project_id, service_id):
            return ResultStore(self.session, await self.list_runs(product_id, project_id, service_id), actor_id="owner-one")

    async def backend():
        async with sessions() as session:
            yield Backend(session)

    app = FastAPI()
    app.include_router(create_router(backend))
    path = "/v1/products/product-one/projects/project-one/services/devops"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        submitted = await client.post(path + "/runs", json=request,
                                      headers={"Idempotency-Key": "release-review-once-00001"})
        assert submitted.status_code == 202, submitted.text
        assert submitted.json()["input"]["devops_input"]["repository_ref"] == reference
        run_id = submitted.json()["run_id"]
        readback = await client.get(path + "/runs/" + run_id)
        assert readback.status_code == 200 and readback.json()["run_id"] == run_id
        replay = await client.post(path + "/runs", json=request,
                                   headers={"Idempotency-Key": "release-review-once-00001"})
        assert replay.status_code == 202 and replay.json()["run_id"] == run_id
    async with sessions() as session:
        run = await session.get(Run, ("tenant-one", run_id))

    recommendation_ids = ["recommendation-one"]
    if malformed_citations is not None:
        recommendation_ids.append("recommendation-malformed")

    class Provider:
        task = None
        calls = 0

        def get_association(self, *, agentSpaceId, associationId):
            assert (agentSpaceId, associationId) == ("space-one", "association-one")
            return {"association": eligibility["native_association"]}

        def get_agent_space(self, *, agentSpaceId):
            return {"agentSpace": {"agentSpaceId": agentSpaceId}}

        def create_backlog_task(self, *, agentSpaceId, taskType, title, description, reference, clientToken):
            assert taskType == "RELEASE_READINESS_REVIEW" and agentSpaceId == "space-one"
            assert reference["associationId"] == "association-one"
            assert json.loads(description)["head_revision"] == "head-123"
            self.calls += 1
            self.task = {"agentSpaceId": agentSpaceId, "taskType": taskType, "taskId": "native-task-one",
                         "executionId": "native-execution-one", "description": description,
                         "reference": reference, "status": "IN_PROGRESS"}
            return {"task": self.task}

        def get_backlog_task(self, *, agentSpaceId, taskId):
            assert (agentSpaceId, taskId) == ("space-one", "native-task-one")
            return {"task": self.task}

        def list_recommendations(self, *, agentSpaceId, taskId, limit):
            return {"recommendations": [{"recommendationId": recommendation_id, "version": 1,
                                         "taskId": taskId} for recommendation_id in recommendation_ids]}

        def get_recommendation(self, *, agentSpaceId, recommendationId, recommendationVersion):
            citations = [{"path": "service/handler.py", "line": 2,
                          "sha256": sha256(b"    return 2").hexdigest()}]
            if recommendationId == "recommendation-malformed":
                citations = malformed_citations
            return {"recommendation": {"recommendationId": recommendationId, "version": recommendationVersion,
                                       "taskId": "native-task-one", "content": {"summary": "Change needs review"},
                                       "priority": "HIGH", "additionalContext": json.dumps({"citations": citations})}}

    provider = Provider()

    async def authorize(run, claim, operation):
        return SimpleNamespace(**verified["provider_identity"],
                               client=lambda name: provider if name == "devops-agent" else None)

    current = {"value": NOW}
    worker = ExecutionService(sessions, source_client, ReleaseReadinessAdapter(clock=lambda: current["value"]), publish, authorize,
                              archive_store=archive_store, clock=lambda: current["value"])
    assert await worker.execute({"schema_version": "1.0", "work_id": (
        await _first_work(sessions)).work_id, "generation": 1}) is True
    async with sessions() as independent:
        persisted = await independent.get(Run, ("tenant-one", run.run_id))
        assert persisted.manifest["changed_paths"] == ["service/handler.py"]
        assert persisted.manifest["changed_lines"]["service/handler.py"]["2"] == sha256(b"    return 2").hexdigest()
    worker.adapter = HistoricalSubmission(clock=lambda: current["value"])
    assert await worker.execute(messages[-1]) is True
    worker.adapter = ReleaseReadinessAdapter(clock=lambda: current["value"])
    async with sessions() as independent:
        persisted = await independent.get(Run, ("tenant-one", run.run_id))
        assert persisted.native_job_ref["record_revision"] == "native-task-one"
    provider.task["status"] = "COMPLETED"
    current["value"] += timedelta(minutes=10)
    assert await worker.execute(messages[-1]) is True
    assert await worker.execute(messages[-1]) is True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        run_readback = await client.get(path + "/runs/" + run_id)
        assert run_readback.status_code == 200
        assert run_readback.json()["native_job_ref"]["record_revision"] == "native-task-one"
        results = await client.get(path + "/results")
        assert results.status_code == 200 and len(results.json()["items"]) == 1
        published = results.json()["items"][0]
        assert published["native_job_ref"]["record_revision"] == "native-task-one"
        assert published["finding_count"] == len(recommendation_ids)
        assert published["coverage"] == "unknown"
        assert published["coverage_mode"] == "native_observation"
        assert run_readback.json()["state"] == "partial"
        assert run_readback.json()["monitoring"]["last_poll_error"]["code"] == QUALIFICATION_CODE
        cited = await client.get(path + "/results/" + published["result_id"] + "/findings")
        assert cited.status_code == 200 and len(cited.json()["items"]) == len(recommendation_ids)
        findings = {finding["native_id"]: finding for finding in cited.json()["items"]}
        assert set(findings) == set(recommendation_ids)
        assert findings["recommendation-one"]["validation"] == "supported"
        evidence = findings["recommendation-one"]["evidence"][0]
        assert evidence["path"] == "service/handler.py" and evidence["line"] == 2
        assert evidence["revision"] == "head-123"
        assert evidence["sha256"] == sha256(b"    return 2").hexdigest()
        if malformed_citations is not None:
            malformed = findings["recommendation-malformed"]
            assert malformed["claim"] == "Change needs review"
            assert malformed["validation"] == "unverified"
            assert malformed["source_refs"] == malformed["evidence"] == malformed["locations"] == []

    async with sessions() as independent:
        assert (await independent.get(Run, ("tenant-one", run.run_id))).state == "partial"
        result = (await independent.scalars(select(AssessmentResult))).one()
        findings = {finding.native_id: finding for finding in (await independent.scalars(select(Finding))).all()}
        assert set(findings) == set(recommendation_ids)
        finding = findings["recommendation-one"]
        assert result.payload["native_task_id"] == "native-task-one"
        assert finding.payload["validation"] == "supported"
        assert finding.payload["evidence"][0]["revision"] == "head-123"
        if malformed_citations is not None:
            assert findings["recommendation-malformed"].payload["validation"] == "unverified"
        assert provider.calls == 1

    await engine.dispose()

@pytest.mark.parametrize("indexing_available", [True, False], ids=["legacy-indexed", "native-association"])
async def test_new_native_readiness_is_blocked_with_actionable_reason(tmp_path, indexing_available):
    engine, sessions, _, _, eligibility, request, verified, source_client, archive_store, budget_guard = (
        await _scenario(tmp_path))
    if not indexing_available:
        use_native_association(eligibility)
    async with sessions() as session:
        run = await admit_run(session, source_client, request, verified, "unqualified-readiness-00001",
                              now=NOW, budget_guard=budget_guard)
    messages = []

    async def publish(envelope, delay):
        messages.append(envelope)

    class NoNativeCalls:
        def __getattr__(self, name):
            raise AssertionError(f"unqualified new submission called provider: {name}")

    async def authorize(run, claim, operation):
        return SimpleNamespace(**verified["provider_identity"], client=lambda name: NoNativeCalls())

    worker = ExecutionService(sessions, source_client, ReleaseReadinessAdapter(clock=lambda: NOW),
                              publish, authorize, archive_store=archive_store, clock=lambda: NOW)
    assert await worker.execute({"schema_version": "1.0", "work_id": (
        await _first_work(sessions)).work_id, "generation": 1}) is True
    assert await worker.execute(messages[-1]) is True
    async with sessions() as independent:
        stored = await independent.get(Run, ("tenant-one", run.run_id))
        attempt = (await independent.scalars(select(WorkerAttempt))).one()
        assert stored.state == "blocked" and stored.native_job_ref is None
        assert attempt.status == "definitive_failure" and attempt.native_job_ref is None
        assert stored.monitoring["last_poll_error"] == {
            "code": QUALIFICATION_CODE, "message": QUALIFICATION_MESSAGE}
        assert not list(Draft202012Validator({**SCHEMA, "$ref": "#/$defs/RunMonitoring"}).iter_errors(stored.monitoring))
        assert (await independent.scalars(select(AssessmentResult))).all() == []
        assert len(messages) == 1
    assert await worker.execute(messages[-1]) is False
    await engine.dispose()


@pytest.mark.parametrize("failure", [
    "task_permission", "permission_action", "permission_space_id", "source_indexed",
    "indexed_revisions", "head_revision", "repository_ref", "deployment_context",
    "repository_connection", "requested_revision",
])
async def test_release_prerequisites(tmp_path, failure):
    engine, sessions, scope, reference, eligibility, request, verified, source_client, _, budget_guard = (
        await _scenario(tmp_path))
    if failure == "repository_connection":
        request["devops_input"]["repository_ref"] = {
            **reference, "source_connection_id": "another-connection"}
        request["devops_input"]["eligibility_ref"] = {
            **request["devops_input"]["eligibility_ref"], "source_connection_id": "another-connection"}
        verified["trigger"]["source_refs"] = [request["devops_input"]["repository_ref"]]
        eligibility = dict(eligibility)
    elif failure == "requested_revision":
        request["devops_input"]["head_revision"] = "another-revision"
    else:
        invalid = {"task_permission": "denied", "permission_action": "ListBacklogTasks",
                   "permission_space_id": "another-space", "source_indexed": False,
                   "indexed_revisions": ["base-123"], "head_revision": "older-head",
                   "repository_ref": {**reference, "record_id": "other-repository"},
                   "deployment_context": {"target": "staging", "revision": "other-head"}}
        eligibility = {**eligibility, failure: invalid[failure]}
    verified["release_eligibility"] = eligibility
    sent = []

    async def publish(envelope, delay):
        sent.append(envelope)

    class Backend:
        def __init__(self, session):
            self.session = session

        async def submit(self, product_id, project_id, service_id, run_input, key):
            assert (product_id, project_id, service_id) == ("product-one", "project-one", "devops")
            return await submit_and_dispatch(self.session, sessions, source_client, publish, run_input,
                                             verified, key, now=NOW, budget_guard=budget_guard)

    async def backend():
        async with sessions() as session:
            yield Backend(session)

    app = FastAPI()
    app.include_router(create_router(backend))
    path = "/v1/products/product-one/projects/project-one/services/devops/runs"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.post(path, json=request, headers={"Idempotency-Key": "release-denial-00001"})
        assert denied.status_code in {409, 422}, denied.text
        assert denied.json()["detail"]["code"] in {"release_prerequisite_unavailable", "validation_error"}
    async with sessions() as independent:
        assert (await independent.scalars(select(Run))).all() == []
        assert (await independent.scalars(select(Work))).all() == []
        assert (await independent.scalars(select(AssessmentResult))).all() == []
        assert source_client.pins == 0 and sent == []

    resource = {**reference, "record_type": "resource", "record_id": "resource-one"}
    investigation = {"service_id": "devops", "configuration_revision": 1, "binding_id": "binding-one",
                     "capability": "investigation", "devops_input": {"kind": "investigation",
                     "question": "What changed?", "resource_refs": [resource], "telemetry_refs": []}}
    verified["trigger"] = {**verified["trigger"], "capability": "investigation",
                           "resource_refs": [resource], "telemetry_refs": []}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        unaffected = await client.post(path, json=investigation, headers={"Idempotency-Key": "investigation-00001"})
        assert unaffected.status_code == 202, unaffected.text
        assert unaffected.json()["input"]["capability"] == "investigation"
    async with sessions() as independent:
        assert len((await independent.scalars(select(Run))).all()) == 1
        assert len((await independent.scalars(select(Work))).all()) == 1
    await engine.dispose()


async def test_release_revision_validation_rejects_floating_refs(tmp_path):
    engine, sessions, _, reference, _, request, verified, source_client, _, budget_guard = (
        await _scenario(tmp_path))
    for revision in ("latest", "HEAD", "null"):
        request["devops_input"]["base_revision"] = revision
        async with sessions() as session:
            with pytest.raises(HTTPException) as denied:
                await admit_run(session, source_client, request, verified,
                                f"floating-revision-{revision}-00001", now=NOW, budget_guard=budget_guard)
            assert denied.value.status_code == 422
    assert source_client.pins == 0
    await engine.dispose()


@pytest.mark.parametrize("failure", ["lost_ack", "manual_reconcile", "crash", "publish_collect", "cleanup", "terminal_cleanup"])
async def test_release_review_recovery(tmp_path, failure):
    engine, sessions, _, _, _, request, verified, source_client, archive_store, budget_guard = (
        await _scenario(tmp_path))
    sent = []
    publication_attempts = []
    fail_publication = {"next": False}
    current = {"value": NOW}

    async def publish(envelope, delay):
        publication_attempts.append(envelope)
        if fail_publication["next"]:
            fail_publication["next"] = False
            raise ConnectionError("offline SQS acknowledgement lost")
        sent.append(envelope)

    class Backend:
        def __init__(self, session):
            self.session = session

        def store(self):
            return RunStore(self.session, "tenant-one", "product-one", "project-one", "devops", "owner-one", "1")

        async def submit(self, product_id, project_id, service_id, run_input, key):
            return await submit_and_dispatch(self.session, sessions, source_client, publish, run_input,
                                             verified, key, now=current["value"], budget_guard=budget_guard)

        async def get_run(self, product_id, project_id, service_id, run_id):
            return await self.store().get(run_id)

        async def list_runs(self, product_id, project_id, service_id, cursor=None):
            return await self.store().list(cursor=cursor)

        async def result_store(self, product_id, project_id, service_id):
            return ResultStore(self.session, await self.list_runs(product_id, project_id, service_id), actor_id="owner-one")

        async def reconcile_run(self, product_id, project_id, service_id, run_id, reason, key):
            return await reconcile_and_dispatch(self.store(), sessions, publish, run_id, reason, key,
                                                now=current["value"])

    async def backend():
        async with sessions() as session:
            yield Backend(session)

    app = FastAPI()
    app.include_router(create_router(backend))
    path = "/v1/products/product-one/projects/project-one/services/devops"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        submitted = await client.post(path + "/runs", json=request,
                                      headers={"Idempotency-Key": "recover-release-review-00001"})
        assert submitted.status_code == 202, submitted.text
        run_id = submitted.json()["run_id"]
        assert (await client.get(path + "/runs/" + run_id)).json()["state"] == "admitted"
    async with sessions() as session:
        run = await session.get(Run, ("tenant-one", run_id))
    admitted_snapshots = run.admission["source_snapshots"]

    class Provider:
        task = None
        calls = 0
        update_calls = 0

        def get_agent_space(self, *, agentSpaceId):
            return {"agentSpace": {"agentSpaceId": agentSpaceId}}

        def create_backlog_task(self, *, agentSpaceId, taskType, title, description, reference, clientToken):
            self.calls += 1
            assert self.calls == 1 and taskType == "RELEASE_READINESS_REVIEW"
            self.task = {"agentSpaceId": agentSpaceId, "taskType": taskType,
                         "taskId": "native-task-one", "executionId": "native-execution-one",
                         "description": description, "reference": reference, "status": "IN_PROGRESS"}
            if failure in {"lost_ack", "manual_reconcile"}:
                raise ReadTimeoutError(endpoint_url="https://offline.invalid")
            return {"task": self.task}

        def list_backlog_tasks(self, *, agentSpaceId, limit, **kwargs):
            assert agentSpaceId == "space-one" and limit == 100
            return {"tasks": [self.task]}

        def get_backlog_task(self, *, agentSpaceId, taskId):
            assert (agentSpaceId, taskId) == ("space-one", "native-task-one")
            return {"task": self.task}

        def list_recommendations(self, *, agentSpaceId, taskId, limit):
            return {"recommendations": [{"recommendationId": "recommendation-one", "version": 1,
                                         "taskId": taskId}]}

        def get_recommendation(self, *, agentSpaceId, recommendationId, recommendationVersion):
            return {"recommendation": {"recommendationId": recommendationId, "version": recommendationVersion,
                                       "taskId": "native-task-one", "content": {"summary": "Changed result"},
                                       "priority": "HIGH", "additionalContext": json.dumps({"citations": [
                                           {"path": "service/handler.py", "line": 2,
                                            "sha256": sha256(b"    return 2").hexdigest()}]})}}

        def update_backlog_task(self, *, agentSpaceId, taskId, taskStatus):
            self.update_calls += 1
            assert taskStatus == "CANCELED" and taskId == "native-task-one"
            self.task["status"] = "CANCELED"
            return {"task": self.task}

    provider = Provider()

    async def authorize(stored, claim, operation):
        return SimpleNamespace(**verified["provider_identity"],
                               client=lambda name: provider if name == "devops-agent" else None)

    def worker(adapter=None):
        return ExecutionService(sessions, source_client,
                                adapter or ReleaseReadinessAdapter(clock=lambda: current["value"]),
                                publish, authorize, archive_store=archive_store,
                                clock=lambda: current["value"])

    assert await worker().execute({"schema_version": "1.0", "work_id": (
        await _first_work(sessions)).work_id, "generation": 1}) is True
    submit_message = sent[-1]

    if failure == "crash":
        class InterruptedAdapter(HistoricalSubmission):
            async def submit(self, *args):
                await super().submit(*args)
                raise RuntimeError("offline worker crash after native acceptance")

        with pytest.raises(RuntimeError, match="worker crash"):
            await worker(InterruptedAdapter(clock=lambda: current["value"])).execute(submit_message)
        async with sessions() as independent:
            attempt = (await independent.scalars(select(WorkerAttempt))).one()
            assert attempt.status == "prepared" and attempt.first_fence == 1
            assert attempt.first_work_id == submit_message["work_id"]
            first_attempt_id = attempt.attempt_id
            assert (await independent.get(Run, ("tenant-one", run.run_id))).native_job_ref is None
        current["value"] += timedelta(seconds=61)
        assert await worker().execute(submit_message) is True
    elif failure in {"lost_ack", "manual_reconcile"}:
        fail_publication["next"] = True
        assert await worker(HistoricalSubmission()).execute(submit_message) is True
        async with sessions() as independent:
            attempt = (await independent.scalars(select(WorkerAttempt))).one()
            owed = (await independent.scalars(select(Work).where(Work.completed_at.is_(None)))).one()
            assert attempt.status == "outcome_unknown" and owed.kind == "reconcile"
            assert owed.last_error == "publication_failed" and provider.calls == 1
            attempt_id = attempt.attempt_id
        current["value"] += timedelta(minutes=10)
        if failure == "manual_reconcile":
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                pending = await client.get(path + "/runs/" + run_id)
                assert pending.status_code == 200 and pending.json()["state"] == "outcome_unknown"
                reconcile_path = path + "/runs/" + run_id + "/reconcile"
                headers = {"Idempotency-Key": "reconcile-release-00001"}
                first = await client.post(reconcile_path, json={"reason": "native task readback"}, headers=headers)
                assert first.status_code == 202, first.text
                replay = await client.post(reconcile_path, json={"reason": "native task readback"}, headers=headers)
                assert replay.status_code == 202 and replay.json() == first.json()
            async with sessions() as independent:
                receipt = (await independent.scalars(select(RunReceipt).where(RunReceipt.operation == "reconcile"))).one()
                assert receipt.response == first.json() and receipt.run_id == run_id
            assert await worker().execute(sent[-1]) is True
            poll_message = sent[-1]
            assert provider.calls == 1
        assert await repair_once(worker(), now=current["value"]) == 1
        assert sent[-1]["work_id"] == owed.work_id
        assert await worker().execute(sent[-1]) is True
        async with sessions() as independent:
            attempt = (await independent.scalars(select(WorkerAttempt))).one()
            assert attempt.status == "accepted" and attempt.attempt_id == attempt_id
            assert attempt.reconcile_checks == 1
            if failure == "manual_reconcile":
                assert (await independent.get(Work, ("tenant-one", owed.work_id))).completed_at is not None
    else:
        assert await worker(HistoricalSubmission()).execute(submit_message) is True

    async with sessions() as independent:
        stored = await independent.get(Run, ("tenant-one", run.run_id))
        attempt = (await independent.scalars(select(WorkerAttempt))).one()
        assert stored.state == "running" and stored.native_job_ref["record_revision"] == "native-task-one"
        assert stored.admission["source_snapshots"] == admitted_snapshots
        assert attempt.native_job_ref == stored.native_job_ref and attempt.status == "accepted"
        assert provider.calls == 1
        if failure == "crash":
            submitted = await independent.get(Work, ("tenant-one", submit_message["work_id"]))
            assert attempt.attempt_id == first_attempt_id
            assert attempt.reconcile_checks == 1 and submitted.fence == 2
        if failure in {"lost_ack", "manual_reconcile"}:
            assert len(publication_attempts) >= 3

    if failure in {"cleanup", "terminal_cleanup"}:
        if failure == "terminal_cleanup":
            provider.task["status"] = "COMPLETED"
        current["value"] = NOW + timedelta(hours=1)
        assert await worker().execute(sent[-1]) is True
        async with sessions() as independent:
            stored = await independent.get(Run, ("tenant-one", run.run_id))
            observations = (await independent.scalars(select(ProviderObservation))).all()
            assert stored.state == "expired" and len(observations) == 1
            assert observations[0].payload["cleanup"] == {
                "action": "retain_terminal_task" if failure == "terminal_cleanup" else "cancel_native_task",
                "outcome": "already_terminal" if failure == "terminal_cleanup" else "confirmed"}
            assert (await independent.scalars(select(AssessmentResult))).all() == []
            assert provider.update_calls == (0 if failure == "terminal_cleanup" else 1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get(path + "/runs/" + run_id)).json()["state"] == "expired"
            assert (await client.get(path + "/results")).json()["items"] == []
        await engine.dispose()
        return

    provider.task["status"] = "COMPLETED"
    current["value"] += timedelta(minutes=10)
    if failure == "publish_collect":
        fail_publication["next"] = True
    assert await worker().execute(poll_message if failure == "manual_reconcile" else sent[-1]) is True
    if failure == "publish_collect":
        async with sessions() as independent:
            owed = (await independent.scalars(select(Work).where(Work.completed_at.is_(None)))).one()
            assert owed.kind == "collect" and owed.last_error == "publication_failed"
            assert owed.attempts == 1
            assert (await independent.scalars(select(AssessmentResult))).all() == []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get(path + "/results")).json()["items"] == []
        assert await repair_once(worker(), now=current["value"]) == 1
        assert sent[-1]["work_id"] == owed.work_id
        async with sessions() as independent:
            dispatched = await independent.get(Work, ("tenant-one", owed.work_id))
            assert dispatched.attempts == 2 and dispatched.last_error is None

    assert await worker().execute(sent[-1]) is True
    async with sessions() as independent:
        stored = await independent.get(Run, ("tenant-one", run.run_id))
        result = (await independent.scalars(select(AssessmentResult))).one()
        detail = (await independent.scalars(select(ResultDetail))).one()
        attempt = (await independent.scalars(select(WorkerAttempt))).one()
        assert stored.state == "partial" and result.payload["native_task_id"] == "native-task-one"
        assert result.payload["cleanup"] == {"action": "retain_native_task", "outcome": "not_required"}

        assert detail.payload["finding_count"] == 1 and attempt.status == "accepted"
        assert len((await independent.scalars(select(ProviderObservation))).all()) == 1
        assert provider.calls == 1 and provider.update_calls == 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        run_readback = await client.get(path + "/runs/" + run_id)
        assert run_readback.status_code == 200 and run_readback.json()["state"] == "partial"
        assert run_readback.json()["native_job_ref"]["record_revision"] == "native-task-one"
        results = await client.get(path + "/results")
        assert results.status_code == 200 and len(results.json()["items"]) == 1
        result_id = results.json()["items"][0]["result_id"]
        findings = await client.get(path + "/results/" + result_id + "/findings")
        assert findings.status_code == 200 and len(findings.json()["items"]) == 1
        assert findings.json()["items"][0]["evidence"][0]["sha256"] == sha256(b"    return 2").hexdigest()
    assert await repair_once(worker(), now=current["value"]) == 0
    assert await worker().execute(submit_message) is False
    async with sessions() as independent:
        stored = await independent.get(Run, ("tenant-one", run.run_id))
        assert stored.admission["source_snapshots"] == admitted_snapshots
    assert provider.calls == 1 and source_client.pins == 2
    await engine.dispose()


async def _first_work(sessions):
    async with sessions() as session:
        return (await session.scalars(select(Work))).one()


@pytest.mark.parametrize("changed_record", ["admission", "manifest"])
async def test_release_review_rejects_snapshot_drift_before_paid_effect(tmp_path, changed_record):
    engine, sessions, _, _, _, request, verified, source_client, archive_store, budget_guard = (
        await _scenario(tmp_path))
    async with sessions() as session:
        run = await admit_run(session, source_client, request, verified, "release-snapshot-fence-00001",
                              now=NOW, budget_guard=budget_guard)
    messages = []

    async def publish(envelope, delay):
        messages.append(envelope)

    class Provider:
        calls = 0

        def get_agent_space(self, **kwargs):
            self.calls += 1
            raise AssertionError("snapshot drift must stop before provider access")

    provider = Provider()

    async def authorize(stored, claim, operation):
        return SimpleNamespace(**verified["provider_identity"],
                               client=lambda service: provider if service == "devops-agent" else None)

    worker = ExecutionService(sessions, source_client, ReleaseReadinessAdapter(clock=lambda: NOW),
                              publish, authorize, archive_store=archive_store, clock=lambda: NOW)
    first = await _first_work(sessions)
    assert await worker.execute({"schema_version": "1.0", "work_id": first.work_id, "generation": first.generation}) is True
    assert len(messages) == 1 and source_client.pins == 2
    async with sessions() as session:
        persisted = await session.get(Run, ("tenant-one", run.run_id))
        original_ids = persisted.manifest["source_snapshot_ids"]
        original_snapshots = persisted.admission["source_snapshots"]
        if changed_record == "admission":
            changed_snapshots = [{**original_snapshots[0], "snapshot_id": "foreign-snapshot"}, original_snapshots[1]]
            persisted.admission = {**persisted.admission, "source_snapshots": changed_snapshots}
        else:
            persisted.manifest = {**persisted.manifest, "source_snapshot_ids": ["foreign-snapshot", original_ids[1]]}
        await session.commit()
    with pytest.raises(ValueError, match="release review does not match pinned provider scope"):
        await worker.execute(messages[0])
    assert await worker.execute(messages[0]) is False
    async with sessions() as session:
        persisted = await session.get(Run, ("tenant-one", run.run_id))
        attempts = (await session.scalars(select(WorkerAttempt))).all()
        assert persisted.state == "queued" and persisted.native_job_ref is None
        assert len(attempts) == 1 and attempts[0].status == "prepared"
        assert (persisted.manifest["source_snapshot_ids"] if changed_record == "admission" else
                [snapshot["snapshot_id"] for snapshot in persisted.admission["source_snapshots"]]) == original_ids
        assert (persisted.manifest["admission_hash"] == sha256(json.dumps(
            {**persisted.admission, "source_snapshots": original_snapshots},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    assert provider.calls == 0 and source_client.pins == 2 and len(messages) == 1
    await engine.dispose()


@pytest.fixture
async def historical_readiness(tmp_path):
    engine, sessions, _, _, _, request, verified, sources, archive, budget_guard = await _scenario(tmp_path)
    sent, authority = [], []
    current = {"value": NOW, "revoked": False}

    async def publish(envelope, delay):
        sent.append(envelope)

    class Provider:
        creates = 0
        reads = 0
        cancels = 0
        result_reads = 0
        crash_cancel = False

        def create_backlog_task(self, **request):
            self.creates += 1
            self.task = {key: request[key] for key in ("agentSpaceId", "taskType", "description", "reference")}
            self.task.update(taskId="native-task-one", executionId="native-execution-one", status="IN_PROGRESS")
            return {"task": self.task}

        def get_backlog_task(self, *, agentSpaceId, taskId):
            assert (agentSpaceId, taskId) == ("space-one", "native-task-one")
            self.reads += 1
            return {"task": self.task}

        def list_backlog_tasks(self, **request):
            return {"tasks": [self.task]}

        def list_recommendations(self, **request):
            self.result_reads += 1
            return {"recommendations": []}

        def update_backlog_task(self, *, agentSpaceId, taskId, taskStatus):
            assert (agentSpaceId, taskId, taskStatus) == ("space-one", "native-task-one", "CANCELED")
            self.cancels += 1
            self.task["status"] = "CANCELED"
            if self.crash_cancel:
                self.crash_cancel = False
                raise RuntimeError("offline cancellation acknowledgement lost")
            return {"task": self.task}

    provider = Provider()

    async def authorize(run, claim, operation):
        from l5_services.workers.execution import AuthorityRevoked
        from l5_services.work_store import WorkClaim

        assert isinstance(claim, WorkClaim)
        assert claim.tenant_id == run.tenant_id and claim.run_id == run.run_id and claim.fence > 0
        authority.append((claim.work_id, claim.fence, operation))
        if current["revoked"]:
            raise AuthorityRevoked("offline current delegation revoked")
        return SimpleNamespace(**verified["provider_identity"], client=lambda name: provider)

    worker = ExecutionService(sessions, sources, HistoricalSubmission(), publish, authorize,
                              archive_store=archive, clock=lambda: current["value"])
    async with sessions() as session:
        run = await admit_run(session, sources, request, verified, "historical-readiness-fixture-00001",
                              now=NOW, budget_guard=budget_guard)
    first = await _first_work(sessions)
    assert await worker.execute({"schema_version": "1.0", "work_id": first.work_id, "generation": 1}) is True
    assert await worker.execute(sent[-1]) is True
    worker.adapter = ReleaseReadinessAdapter(clock=lambda: current["value"])
    async with sessions() as session:
        original = (await session.scalars(select(WorkerAttempt))).one()
        assert original.status == "accepted"
        original_id, original_token = original.attempt_id, original.idempotency_token
    fixture = SimpleNamespace(sessions=sessions, provider=provider, worker=worker, sent=sent,
                              current=current, authority=authority, run_id=run.run_id,
                              original_id=original_id, original_token=original_token,
                              original_description=provider.task["description"])
    try:
        yield fixture
    finally:
        await engine.dispose()


async def request_historical_cancel(fixture):
    from l5_services.admission import cancel_and_dispatch

    async with fixture.sessions() as session:
        store = RunStore(session, "tenant-one", "product-one", "project-one", "devops", "owner-one", "1")
        await cancel_and_dispatch(store, fixture.sessions, fixture.worker.publish, fixture.run_id,
                                  "Stop existing native task", "historical-cancel-00001", now=fixture.current["value"])


@pytest.mark.parametrize("phase", ["poll", "collect", "cancel"])
@pytest.mark.parametrize("mismatch", ["attempt_id", "taskId", "executionId", "missing_attempt", "attempt_native"])
async def test_recovery_rejects_changed_original_identity(historical_readiness, phase, mismatch):
    fixture = historical_readiness
    fixture.current["value"] += timedelta(minutes=10)
    if phase == "collect":
        fixture.provider.task["status"] = "COMPLETED"
        assert await fixture.worker.execute(fixture.sent[-1]) is True
    elif phase == "cancel":
        await request_historical_cancel(fixture)
    if mismatch == "attempt_id":
        description = json.loads(fixture.provider.task["description"])
        description["attempt_id"] = "another-attempt"
        fixture.provider.task["description"] = json.dumps(description, sort_keys=True, separators=(",", ":"))
    elif mismatch in {"taskId", "executionId"}:
        fixture.provider.task[mismatch] = "foreign-native-identity"
    else:
        async with fixture.sessions() as session:
            original = (await session.scalars(select(WorkerAttempt))).one()
            if mismatch == "missing_attempt":
                await session.delete(original)
            else:
                original.native_job_ref = {**original.native_job_ref, "record_id": "foreign-execution"}
            await session.commit()
    with pytest.raises(ValueError, match="original accepted attempt|native task readback differs"):
        await fixture.worker.execute(fixture.sent[-1])
    async with fixture.sessions() as session:
        assert (await session.scalars(select(AssessmentResult))).all() == []
        stored = await session.get(Run, ("tenant-one", fixture.run_id))
        assert stored.state != "succeeded"
    assert fixture.provider.creates == 1 and fixture.provider.cancels == fixture.provider.result_reads == 0


async def test_cancellation_retry_observes_original_attempt_without_second_write(historical_readiness):
    fixture = historical_readiness
    await request_historical_cancel(fixture)
    fixture.provider.crash_cancel = True
    cancellation = fixture.sent[-1]
    with pytest.raises(RuntimeError, match="cancellation acknowledgement lost"):
        await fixture.worker.execute(cancellation)
    fixture.current["value"] += timedelta(seconds=61)
    assert await fixture.worker.execute(cancellation) is True
    async with fixture.sessions() as session:
        stored = await session.get(Run, ("tenant-one", fixture.run_id))
        original = (await session.scalars(select(WorkerAttempt))).one()
        assert stored.state == "cancelled" and stored.provider_state == "stopped"
        assert (original.attempt_id, original.idempotency_token) == (fixture.original_id, fixture.original_token)
        assert original.native_job_ref == stored.native_job_ref
    assert fixture.provider.task["description"] == fixture.original_description
    assert fixture.provider.creates == fixture.provider.cancels == 1
    assert fixture.authority[-1] == (cancellation["work_id"], 2, "observe")


@pytest.mark.parametrize("phase", ["poll", "collect", "cancel"])
async def test_historical_recovery_requires_current_authority(historical_readiness, phase):
    fixture = historical_readiness
    fixture.current["value"] += timedelta(minutes=10)
    if phase == "collect":
        fixture.provider.task["status"] = "COMPLETED"
        assert await fixture.worker.execute(fixture.sent[-1]) is True
    elif phase == "cancel":
        await request_historical_cancel(fixture)
    fixture.current["revoked"] = True
    previous_reads = fixture.provider.reads
    assert await fixture.worker.execute(fixture.sent[-1]) is True
    assert fixture.provider.reads == previous_reads
    assert fixture.provider.creates == 1 and fixture.provider.cancels == fixture.provider.result_reads == 0
    async with fixture.sessions() as session:
        assert (await session.scalars(select(AssessmentResult))).all() == []
        stored = await session.get(Run, ("tenant-one", fixture.run_id))
        assert stored.state == "outcome_unknown"


async def test_reconcile_rejects_task_identity_changed_between_list_and_read(historical_readiness):
    fixture = historical_readiness
    listed = dict(fixture.provider.task)
    fixture.provider.list_backlog_tasks = lambda **kwargs: {"tasks": [listed]}
    fixture.provider.task["taskId"] = "another-native-task"
    async with fixture.sessions() as session:
        run = await session.get(Run, ("tenant-one", fixture.run_id))
        attempt = (await session.scalars(select(WorkerAttempt))).one()
    identity = SimpleNamespace(**run.admission["provider_identity"], client=lambda name: fixture.provider)
    outcome = await fixture.worker.adapter.reconcile_submit(run, attempt, identity)
    assert outcome.classification == "unknown" and outcome.native_job_ref is None
    assert fixture.provider.creates == 1
