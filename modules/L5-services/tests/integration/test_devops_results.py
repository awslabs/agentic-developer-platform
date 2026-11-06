"""Isolated provider observations through the shared scoped run API."""

from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

from botocore.session import Session
from botocore.validate import validate_parameters
import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.adapters.devops.observations import import_native_task, readback_native_tasks
from l5_services.api.router import create_router
from l5_services.findings import ResultStore, normalize_result
from l5_services.models.base import Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.devops_observation import NativeRunIdentity
from l5_services.models.devops_event import NativeEventReceipt
from l5_services.models.evidence import AssessmentResult
from l5_services.models.result import ResultDetail
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run
from l5_services.run_access import RunStore
from l5_services.devops.monitoring import reconcile_native_run
from l5_services.devops.native_events import ingest_native_event
from l5_services.devops.results import record_native_result, evidence_bytes
from l5_services.workers.devops_readback import sweep_native_bindings

NOW = datetime(2026, 10, 26, 9, tzinfo=UTC)
SCHEMA = json.loads((Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1/schemas.json").read_text())


@pytest.fixture
async def native_scenario(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'observations.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scope = {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one",
             "relationship_revision": "1", "owner_id": "owner-one", "visibility": "product_team"}
    reference = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "connection-one",
                 "record_type": "agent_space", "record_id": "space-one", "record_revision": "1"}
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
        binding = Binding(tenant_id="tenant-one", binding_id="binding-one", configuration_id="configuration-one",
                          product_id="product-one", project_id="project-one", service_id="devops", scope=scope,
                          revision=1, generation=1, state="ready", ownership="adopted",
                          connection_ref=reference, connection_key="a" * 64, native_resource_ref=reference,
                          native_key="b" * 64, readiness=[], gaps=[],
                          provider_identity={"account_id": "123456789012", "region": "us-east-1",
                                             "role_arn": "arn:aws:iam::123456789012:role/example"})
        session.add(binding)
        await session.commit()

    class Backend:
        def __init__(self, session, request):
            self.session = session
            self.actor_id = request.headers.get("X-Test-Actor", "member-one")
            self.relationship_revision = request.headers.get("X-Test-Relationship", "1")

        def store(self, product_id, project_id, service_id):
            return RunStore(self.session, scope["tenant_id"], product_id, project_id,
                            service_id, self.actor_id, self.relationship_revision)

        async def list_runs(self, product_id, project_id, service_id, cursor):
            return await self.store(product_id, project_id, service_id).list(cursor=cursor)

        async def get_run(self, product_id, project_id, service_id, run_id):
            return await self.store(product_id, project_id, service_id).get(run_id)

        async def result_store(self, product_id, project_id, service_id):
            return ResultStore(self.session, await self.store(product_id, project_id, service_id).list(),
                               actor_id=self.actor_id, clock=lambda: NOW)

        async def cancel_run(self, product_id, project_id, service_id, run_id, reason, idempotency_key):
            return await self.store(product_id, project_id, service_id).cancel_native(
                run_id, reason, idempotency_key, now=NOW)

    app = FastAPI()
    async def backend(request: Request):
        async with sessions() as session:
            yield Backend(session, request)

    app.include_router(create_router(backend))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline.invalid") as client:
        yield sessions, scope, binding, client
    await engine.dispose()


async def test_native_import_is_idempotent(native_scenario):
    sessions, scope, binding, client = native_scenario
    task = {"agentSpaceId": "space-one", "taskType": "INVESTIGATION", "executionId": "execution-one",
            "status": "IN_PROGRESS"}
    async with sessions() as session:
        imported = await import_native_task(session, binding, scope, task, now=NOW)
        duplicate = await import_native_task(session, binding, scope, task, now=NOW)
        assert imported.run_id == duplicate.run_id
        assert (await session.scalars(select(NativeRunIdentity))).one().run_id == imported.run_id
        assert (await session.scalars(select(RunReceipt))).all() == []
    path = "/v1/products/product-one/projects/project-one/services/devops/runs"
    listing = await client.get(path)
    detail = await client.get(f"{path}/{imported.run_id}")
    assert listing.status_code == detail.status_code == 200
    assert listing.json()["items"] == [detail.json()]
    assert detail.json()["origin"] == "native" and "admission" not in detail.json()
    assert [str(error) for error in Draft202012Validator({**SCHEMA, "$ref": "#/$defs/Run"}).iter_errors(detail.json())] == []
    assert detail.json()["native_job_ref"]["record_id"] == "execution-one"
    assert detail.json()["attribution"] == "unverified"


async def test_native_work_wrong_scope(native_scenario):
    sessions, scope, binding, client = native_scenario
    task = {"agentSpaceId": "space-one", "taskType": "INVESTIGATION", "executionId": "execution-one"}
    async with sessions() as session:
        for changed_scope, changed_task in (({**scope, "project_id": "foreign-project"}, task),
                                            (scope, {**task, "agentSpaceId": "foreign-space"})):
            with pytest.raises(HTTPException) as error:
                await import_native_task(session, binding, changed_scope, changed_task, now=NOW)
            assert error.value.status_code == 404
        imported = await import_native_task(session, binding, scope, task, now=NOW)
    foreign = await client.get(f"/v1/products/product-one/projects/foreign-project/services/devops/runs/{imported.run_id}")
    assert foreign.status_code == 404
    assert (await client.get("/v1/products/product-one/projects/foreign-project/services/devops/runs")).json() == {"items": []}


async def test_native_work_readback(native_scenario):
    sessions, scope, binding, client = native_scenario
    native_ref = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "connection-one",
                  "record_type": "native_job", "record_id": "admitted-execution", "record_revision": "admitted-execution"}
    async with sessions() as session:
        session.add(Run(tenant_id="tenant-one", run_id="admitted-run", product_id="product-one",
                        project_id="project-one", service_id="devops", configuration_id="configuration-one",
                        configuration_revision=1, binding_id="binding-one", binding_generation=1,
                        trigger_id="manual", input={"service_id": "devops"},
                        admission={"scope": scope, "native_space_ref": binding.native_resource_ref},
                        state="running", provider_state="running", native_job_ref=native_ref,
                        deadline_at=NOW + timedelta(days=1)))
        session.add(RunReceipt(tenant_id="tenant-one", product_id="product-one", project_id="project-one",
                               service_id="devops", actor_id="owner-one", operation="submit",
                               idempotency_key="admitted-key", request_hash="a" * 64,
                               run_id="admitted-run", expires_at=NOW + timedelta(days=1)))
        await session.commit()

    event = {"source": "aws.aidevops", "id": "native-started", "detail-type": "Investigation In Progress",
             "account": "123456789012", "region": "us-east-1", "time": "2026-10-26T09:00:00Z",
             "resources": ["arn:aws:aidevops:us-east-1:123456789012:agentspace/space-one"],
             "detail": {"version": "1.0.0", "metadata": {"agent_space_id": "space-one",
                       "task_id": "task-native-execution", "execution_id": "native-execution"},
                        "data": {"task_type": "INVESTIGATION", "status": "IN_PROGRESS",
                                 "updated_at": "2026-10-26T09:00:00Z"}}}
    event_run = await ingest_native_event(sessions, event, now=NOW)

    class Provider:
        def get_agent_space(self, *, agentSpaceId):
            assert agentSpaceId == "space-one"
            return {"agentSpace": {"agentSpaceId": agentSpaceId}}

        def list_backlog_tasks(self, *, agentSpaceId, limit):
            assert agentSpaceId == "space-one" and limit == 100
            return {"tasks": [{"agentSpaceId": agentSpaceId, "taskType": "INVESTIGATION",
                               "executionId": execution_id, "taskId": f"task-{execution_id}"}
                              for execution_id in ("admitted-execution", "native-execution")]}

    provider = Provider()
    identity = SimpleNamespace(**binding.provider_identity, client=lambda service: provider)
    async with sessions() as session:
        first = await readback_native_tasks(session, binding, scope, identity, now=NOW)
        again = await readback_native_tasks(session, binding, scope, identity, now=NOW)
        assert [run.run_id for run in first] == [run.run_id for run in again]
        assert first[0].run_id == "admitted-run" and first[1].run_id == event_run.run_id
        assert len((await session.scalars(select(Run))).all()) == 2
        assert len((await session.scalars(select(RunReceipt))).all()) == 1
        assert len((await session.scalars(select(NativeEventReceipt))).all()) == 1
    task = {"agentSpaceId": "space-one", "taskId": "task-native-execution",
            "executionId": "native-execution", "status": "COMPLETED"}
    evidence = await record_native_result(sessions, event_run, task, now=NOW + timedelta(minutes=1))
    await reconcile_native_run(sessions, "tenant-one", event_run.run_id, now=NOW + timedelta(minutes=1),
                               observation={"observed_at": "2026-10-26T09:01:00Z",
                                            "native_job_ref": event_run.native_job_ref, "outcome": "completed",
                                            "progress_coverage": "unavailable"}, provider_readback_ref=evidence)
    response = await client.get("/v1/products/product-one/projects/project-one/services/devops/runs")
    assert response.status_code == 200
    by_id = {item["run_id"]: item for item in response.json()["items"]}
    assert by_id["admitted-run"]["origin"] == "adp_submitted"
    assert by_id["admitted-run"]["admission"]["scope"] == scope
    assert by_id[first[1].run_id]["origin"] == "native"
    assert "admission" not in by_id[first[1].run_id]
    assert by_id[first[1].run_id]["attribution"] == "unverified"
    for run_id in ("admitted-run", first[1].run_id):
        detail = await client.get(f"/v1/products/product-one/projects/project-one/services/devops/runs/{run_id}")
        assert detail.status_code == 200 and detail.json() == by_id[run_id]
    results = await client.get("/v1/products/product-one/projects/project-one/services/devops/results")
    assert results.status_code == 200 and [row["run_id"] for row in results.json()["items"]] == [event_run.run_id]
    assert results.json()["items"][0]["costs"] == [{"kind": "unknown", "currency": "USD"}]

    wrong_identity = SimpleNamespace(**{**binding.provider_identity, "role_arn": "wrong"}, client=lambda _: provider)
    async with sessions() as session:
        with pytest.raises(HTTPException) as error:
            await readback_native_tasks(session, binding, scope, wrong_identity, now=NOW)
        assert error.value.status_code == 404


async def test_monitoring_recovery(native_scenario):
    sessions, scope, binding, client = native_scenario
    async with sessions() as session:
        run = await import_native_task(session, binding, scope,
                                       {"agentSpaceId": "space-one", "taskType": "INVESTIGATION",
                                        "executionId": "recover-execution"}, now=NOW)
    def observed(minutes, outcome, fingerprint=None):
        return {"observed_at": (NOW + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
                "native_job_ref": run.native_job_ref, "outcome": outcome, "progress_coverage": "complete",
                "progress_fingerprint": fingerprint or "a" * 64}

    early = await reconcile_native_run(sessions, "tenant-one", run.run_id, now=NOW + timedelta(minutes=1),
                                       observation=observed(1, "completed"), source="event")
    assert early.state == "running" and early.provider_state == "unknown"
    assert early.monitoring["poll_attempts"] == 0 and early.next_check_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=1)
    reading = await reconcile_native_run(sessions, "tenant-one", run.run_id, now=NOW + timedelta(minutes=2),
                                         observation=observed(2, "running"))
    assert reading.state == "running" and reading.monitoring["state"] == "current"
    degraded = await reconcile_native_run(sessions, "tenant-one", run.run_id, now=NOW + timedelta(minutes=3),
                                          error="provider_unavailable")
    assert degraded.state == "running" and degraded.monitoring["state"] == "degraded"
    assert degraded.next_check_at is not None
    stalled = await reconcile_native_run(sessions, "tenant-one", run.run_id, now=NOW + timedelta(minutes=35),
                                         observation=observed(35, "running"))
    assert stalled.monitoring["progress_state"] == "suspected_stalled" and stalled.state == "running"
    stale = await reconcile_native_run(sessions, "tenant-one", run.run_id, now=NOW + timedelta(minutes=4),
                                       observation=observed(1, "failed"), source="event")
    assert stale.revision == stalled.revision and stale.provider_state == "running"
    evidence = {"artifact_id": "readback-one", "version": "1", "sha256": "a" * 64,
                "media_type": "application/json", "bytes": 12}
    completed = await reconcile_native_run(sessions, "tenant-one", run.run_id, now=NOW + timedelta(minutes=36),
                                           observation=observed(36, "completed"), provider_readback_ref=evidence)
    assert completed.state == "succeeded" and completed.provider_state == "completed"
    assert completed.next_check_at is None and completed.monitoring["state"] == "finished"
    duplicate = await reconcile_native_run(sessions, "tenant-one", run.run_id, now=NOW + timedelta(minutes=37),
                                           observation=observed(37, "failed"), provider_readback_ref=evidence)
    assert duplicate.revision == completed.revision and duplicate.state == "succeeded"
    response = await client.get(f"/v1/products/product-one/projects/project-one/services/devops/runs/{run.run_id}")
    assert response.json()["provider_readback_ref"] == evidence
    assert [str(error) for error in Draft202012Validator({**SCHEMA, "$ref": "#/$defs/Run"}).iter_errors(response.json())] == []

    async with sessions() as session:
        missed = await import_native_task(session, binding, scope,
                                          {"agentSpaceId": "space-one", "taskType": "INVESTIGATION",
                                           "executionId": "missed-event"}, now=NOW)
    observed_without_event = {**observed(1, "completed"), "native_job_ref": missed.native_job_ref}
    recovered = await reconcile_native_run(sessions, "tenant-one", missed.run_id, now=NOW + timedelta(minutes=1),
                                           observation=observed_without_event, provider_readback_ref=evidence)
    assert recovered.state == "succeeded" and recovered.provider_state == "completed"


async def test_monitoring_rejects_event_evidence(native_scenario):
    sessions, scope, binding, _ = native_scenario
    async with sessions() as session:
        run = await import_native_task(session, binding, scope,
                                       {"agentSpaceId": "space-one", "taskType": "INVESTIGATION",
                                        "executionId": "event-only"}, now=NOW)
    observation = {"observed_at": NOW.isoformat().replace("+00:00", "Z"),
                   "native_job_ref": run.native_job_ref, "outcome": "failed", "progress_coverage": "unavailable"}
    with pytest.raises(ValueError, match="events cannot establish"):
        await reconcile_native_run(sessions, "tenant-one", run.run_id, now=NOW,
                                   observation=observation, provider_readback_ref={"artifact_id": "unverified"},
                                   source="event")
    with pytest.raises(ValueError, match="different native execution"):
        await reconcile_native_run(sessions, "tenant-one", run.run_id, now=NOW,
                                   observation={**observation, "native_job_ref": {"record_id": "foreign"}})
    timed_out = await reconcile_native_run(sessions, "tenant-one", run.run_id, now=NOW + timedelta(minutes=1),
                                           error="provider_timeout")
    assert timed_out.state == "running" and timed_out.provider_state == "unknown"
    assert timed_out.monitoring["state"] == "degraded" and timed_out.monitoring["last_poll_error"]["code"] == "provider_timeout"


async def test_documented_event_receipt_is_scoped_and_not_terminal(native_scenario):
    sessions, scope, binding, client = native_scenario
    event = {"source": "aws.aidevops", "id": "event-one", "detail-type": "Investigation Completed",
             "account": "123456789012", "region": "us-east-1", "time": "2026-10-26T09:00:00Z",
             "resources": ["arn:aws:aidevops:us-east-1:123456789012:agentspace/space-one"],
             "detail": {"version": "1.0.0", "metadata": {"agent_space_id": "space-one",
                       "task_id": "task-one", "execution_id": "execution-event"},
                        "data": {"task_type": "INVESTIGATION", "status": "COMPLETED",
                                 "updated_at": "2026-10-26T09:00:00Z"}}}
    first = await ingest_native_event(sessions, event, now=NOW)
    replay = await ingest_native_event(sessions, event, now=NOW)
    assert first.run_id == replay.run_id and first.state == replay.state == "running"
    async with sessions() as session:
        assert len((await session.scalars(select(NativeEventReceipt))).all()) == 1
        assert len((await session.scalars(select(RunReceipt))).all()) == 0
        assert first.monitoring["poll_attempts"] == 0 and "last_successful_poll_at" not in first.monitoring
    with pytest.raises(HTTPException) as conflict:
        await ingest_native_event(sessions, {**event, "detail-type": "Investigation Failed",
                                            "detail": {**event["detail"], "data": {**event["detail"]["data"],
                                                                                    "status": "FAILED"}}}, now=NOW)
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException) as foreign:
        await ingest_native_event(sessions, {**event, "id": "foreign-event", "resources": ["foreign"]}, now=NOW)
    assert foreign.value.status_code == 404
    with pytest.raises(HTTPException) as undocumented:
        await ingest_native_event(sessions, {**event, "id": "unknown-event", "detail-type": "Prevention Completed"}, now=NOW)
    assert undocumented.value.status_code == 422
    response = await client.get(f"/v1/products/product-one/projects/project-one/services/devops/runs/{first.run_id}")
    assert response.status_code == 200 and response.json()["origin"] == "native"
    assert response.json()["provider_state"] == "unknown"

    mitigation = {**event, "id": "mitigation-one", "detail-type": "Mitigation Completed",
                  "detail": {**event["detail"], "metadata": {**event["detail"]["metadata"],
                                                             "execution_id": "mitigation-execution"}}}
    imported = await ingest_native_event(sessions, mitigation, now=NOW)
    assert imported.input["capability"] == "mitigation" and imported.state == "running"


@pytest.mark.parametrize("status,state,outcome", [
    ("COMPLETED", "succeeded", "completed"), ("CANCELED", "cancelled", "stopped"),
])
async def test_due_readback_discovers_missed_event(native_scenario, status, state, outcome):
    sessions, scope, binding, client = native_scenario
    task = {"agentSpaceId": "space-one", "taskType": "INVESTIGATION",
            "taskId": "task-missed", "executionId": "execution-missed", "status": status,
            "title": "Investigate missed event", "priority": "HIGH", "version": 1,
            "createdAt": NOW.astimezone(timezone(timedelta(hours=5, minutes=30))),
            "updatedAt": NOW + timedelta(minutes=1)}
    model = Session().get_service_model("devops-agent")
    validate_parameters({"task": task}, model.operation_model("GetBacklogTask").output_shape)
    validate_parameters({"tasks": [task]}, model.operation_model("ListBacklogTasks").output_shape)

    class Provider:
        def get_agent_space(self, *, agentSpaceId):
            return {"agentSpace": {"agentSpaceId": agentSpaceId}}

        def list_backlog_tasks(self, *, agentSpaceId, limit):
            return {"tasks": [task]}

        def get_backlog_task(self, *, agentSpaceId, taskId):
            assert agentSpaceId == "space-one" and taskId == "task-missed"
            return {"task": self.list_backlog_tasks(agentSpaceId=agentSpaceId, limit=100)["tasks"][0]}

    identity = SimpleNamespace(**binding.provider_identity, client=lambda service: Provider())

    async def authorize(selected):
        assert selected.binding_id == "binding-one" and selected.scope == scope
        return identity

    assert await sweep_native_bindings(sessions, authorize, now=NOW) == 1
    async with sessions() as session:
        imported = (await session.scalars(select(Run))).one()
        assert imported.origin == "native" and imported.next_check_at is not None
        assert (await session.scalars(select(NativeEventReceipt))).all() == []
    assert await sweep_native_bindings(sessions, authorize, now=NOW + timedelta(minutes=5)) == 1
    async with sessions() as session:
        recovered = await session.get(Run, ("tenant-one", imported.run_id))
        assert recovered.monitoring["latest_observation"]["outcome"] == outcome
        assert recovered.state == state and recovered.provider_readback_ref is not None
        assert recovered.monitoring["poll_attempts"] == 1
        assert len((await session.scalars(select(Run))).all()) == 1
        stored = (await session.scalars(select(AssessmentResult))).one()
        expected = {**task, "createdAt": "2026-10-26T09:00:00Z", "updatedAt": "2026-10-26T09:01:00Z"}
        assert stored.payload == expected
    reference = recovered.provider_readback_ref
    assert await record_native_result(sessions, imported, {**task, "createdAt": NOW},
                                      now=NOW + timedelta(minutes=6)) == reference
    encoded = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert reference["bytes"] == len(encoded) and reference["sha256"] == sha256(encoded).hexdigest()
    root = "/v1/products/product-one/projects/project-one/services/devops"
    downloaded = await client.get(root + f"/artifacts/{reference['artifact_id']}/content")
    assert downloaded.status_code == 200 and downloaded.content == encoded
    assert downloaded.headers["x-adp-content-sha256"] == sha256(encoded).hexdigest()
    assert isinstance(task["createdAt"], datetime) and isinstance(task["updatedAt"], datetime)


@pytest.mark.parametrize("candidate_origin", ["native", "adp_submitted"])
async def test_native_result_comparisons_are_incomparable(native_scenario, candidate_origin):
    sessions, scope, binding, client = native_scenario
    async with sessions() as session:
        baseline = await import_native_task(session, binding, scope,
                                            {"agentSpaceId": "space-one", "taskType": "INVESTIGATION",
                                             "executionId": "baseline-execution", "taskId": "baseline-task"}, now=NOW)
    await record_native_result(sessions, baseline,
                               {"agentSpaceId": "space-one", "executionId": "baseline-execution",
                                "taskId": "baseline-task", "status": "COMPLETED"}, now=NOW)
    if candidate_origin == "native":
        async with sessions() as session:
            candidate = await import_native_task(session, binding, scope,
                                                 {"agentSpaceId": "space-one", "taskType": "INVESTIGATION",
                                                  "executionId": "candidate-execution", "taskId": "candidate-task"}, now=NOW)
        await record_native_result(sessions, candidate,
                                   {"agentSpaceId": "space-one", "executionId": "candidate-execution",
                                    "taskId": "candidate-task", "status": "COMPLETED"}, now=NOW)
    else:
        async with sessions() as session:
            candidate = Run(tenant_id="tenant-one", run_id="admitted-run", product_id="product-one",
                            project_id="project-one", service_id="devops", configuration_id="configuration-one",
                            configuration_revision=1, binding_id="binding-one", binding_generation=1,
                            trigger_id="manual", input={"service_id": "devops", "capability": "investigation"},
                            admission={"scope": scope, "adapter_revision": "devops-investigation-v1",
                                       "native_space_ref": binding.native_resource_ref},
                            state="succeeded", provider_state="completed", deadline_at=NOW + timedelta(days=1),
                            native_job_ref={**baseline.native_job_ref, "record_id": "admitted-execution",
                                            "record_revision": "admitted-task"})
            raw = {"kind": "devops_investigation", "native_job_ref": candidate.native_job_ref,
                   "task": {"agentSpaceId": "space-one", "taskId": "admitted-task",
                            "executionId": "admitted-execution", "status": "COMPLETED"},
                   "execution": {"agentSpaceId": "space-one", "executionId": "admitted-execution"},
                   "journal_records": [], "operational_recommendations": [],
                   "costs": [{"kind": "unknown", "currency": "USD"}]}
            detail, findings = normalize_result(candidate, "admitted-result", 1, raw, NOW)
            assert findings == []
            session.add(candidate)
            session.add(AssessmentResult(tenant_id="tenant-one", result_id="admitted-result",
                                         run_id=candidate.run_id, version=1, payload=raw))
            session.add(detail)
            await session.commit()
    root = "/v1/products/product-one/projects/project-one/services/devops"
    listing = await client.get(root + "/results")
    assert listing.status_code == 200
    results = {result["run_id"]: result["result_id"] for result in listing.json()["items"]}
    for baseline_id, candidate_id in ((results[baseline.run_id], results[candidate.run_id]),
                                      (results[candidate.run_id], results[baseline.run_id])):
        response = await client.post(root + "/comparisons", json={
            "baseline_result_id": baseline_id, "candidate_result_id": candidate_id,
        })
        assert response.status_code == 200
        comparison = response.json()
        assert comparison["comparability"] == "incomparable"
        assert "adapter_version_changed_or_unknown" in comparison["reasons"]
        assert comparison["new_findings"] == comparison["unchanged_findings"] == comparison["resolved_findings"] == []
        Draft202012Validator({**SCHEMA, "$ref": "#/$defs/Comparison"}).validate(comparison)


async def test_due_readback_outage_preserves_native_state(native_scenario):
    sessions, scope, binding, _ = native_scenario
    async with sessions() as session:
        run = await import_native_task(session, binding, scope,
                                       {"agentSpaceId": "space-one", "taskType": "INVESTIGATION",
                                        "executionId": "timeout-execution", "taskId": "timeout-task"}, now=NOW)

    async def unavailable(binding):
        raise TimeoutError("offline provider timeout")

    assert await sweep_native_bindings(sessions, unavailable, now=NOW + timedelta(minutes=1)) == 1
    async with sessions() as session:
        degraded = await session.get(Run, ("tenant-one", run.run_id))
        assert degraded.state == "running" and degraded.provider_state == "unknown"
        assert degraded.monitoring["state"] == "degraded" and degraded.monitoring["consecutive_poll_errors"] == 1
        scheduled = await session.get(Binding, ("tenant-one", "binding-one"))
        assert scheduled.next_check_at is not None


async def test_native_result_persists_partial_cost_and_evidence(native_scenario):
    sessions, scope, binding, _ = native_scenario
    async with sessions() as session:
        run = await import_native_task(session, binding, scope,
                                       {"agentSpaceId": "space-one", "taskType": "INVESTIGATION",
                                        "executionId": "results-execution", "taskId": "results-task"}, now=NOW)
    task = {"agentSpaceId": "space-one", "taskId": "results-task", "executionId": "results-execution",
            "status": "COMPLETED", "costs": [{"kind": "observed", "currency": "USD", "amount": "1.250000"},
                                            {"kind": "unknown", "currency": "USD"}]}
    reference = await record_native_result(sessions, run, task, now=NOW + timedelta(minutes=1))
    assert await record_native_result(sessions, run, task, now=NOW + timedelta(minutes=2)) == reference
    async with sessions() as session:
        assert len((await session.scalars(select(AssessmentResult))).all()) == 1
        detail = (await session.scalars(select(ResultDetail))).one().payload
        assert detail["scope"] == scope and detail["origin"] == "native" and "admission" not in detail
        assert detail["costs"][0]["amount"] == "1.250000"
        assert detail["costs"][1] == {"kind": "unknown", "currency": "USD"}
        assert detail["evidence_ref"] == reference
        assert [str(error) for error in Draft202012Validator({**SCHEMA, "$ref": "#/$defs/Result"}).iter_errors(detail)] == []
        stored = (await session.scalars(select(AssessmentResult))).one()
        assert stored.payload == task
        assert reference["bytes"] == len(evidence_bytes(stored.payload))
        accessible = await RunStore(session, "tenant-one", "product-one", "project-one", "devops",
                                    "member-one", "1").list()
        result_store = ResultStore(session, accessible, actor_id="member-one", clock=lambda: NOW)
        assert await result_store.get(detail["result_id"]) == detail
        assert await result_store.list() == [detail]
    with pytest.raises(ValueError, match="immutable result"):
        await record_native_result(sessions, run, {**task, "status": "FAILED"}, now=NOW + timedelta(minutes=3))


async def test_provider_evidence_required_for_native_cancellation(native_scenario):
    sessions, scope, binding, _ = native_scenario
    async with sessions() as session:
        run = await import_native_task(session, binding, scope,
                                       {"agentSpaceId": "space-one", "taskType": "INVESTIGATION",
                                        "executionId": "cancel-execution", "taskId": "cancel-task"}, now=NOW)
    observation = {"native_job_ref": run.native_job_ref, "outcome": "stopped", "native_status": "CANCELLED",
                   "progress_coverage": "unavailable", "observed_at": "2026-10-26T09:01:00Z"}
    unconfirmed = await reconcile_native_run(sessions, "tenant-one", run.run_id, now=NOW + timedelta(minutes=1),
                                             observation=observation, source="event")
    assert unconfirmed.state == "running" and unconfirmed.provider_state == "unknown"
    task = {"agentSpaceId": "space-one", "taskId": "cancel-task", "executionId": "cancel-execution",
            "status": "CANCELLED"}
    reference = await record_native_result(sessions, run, task, now=NOW + timedelta(minutes=2))
    confirmed = await reconcile_native_run(sessions, "tenant-one", run.run_id, now=NOW + timedelta(minutes=2),
                                           observation={**observation, "observed_at": "2026-10-26T09:02:00Z"},
                                           provider_readback_ref=reference)
    assert confirmed.state == "cancelled" and confirmed.provider_state == "stopped"
    async with sessions() as session:
        detail = (await session.scalars(select(ResultDetail))).one().payload
        assert detail["provider_outcome"] == "stopped" and detail["costs"] == [{"kind": "unknown", "currency": "USD"}]
        assert [str(error) for error in Draft202012Validator({**SCHEMA, "$ref": "#/$defs/Result"}).iter_errors(detail)] == []


async def test_results_scope(native_scenario):
    sessions, scope, binding, client = native_scenario
    async with sessions() as session:
        run = await import_native_task(session, binding, scope,
                                       {"agentSpaceId": "space-one", "taskType": "INVESTIGATION",
                                        "executionId": "artifact-execution", "taskId": "artifact-task"}, now=NOW)
    task = {"agentSpaceId": "space-one", "taskId": "artifact-task", "executionId": "artifact-execution",
            "status": "COMPLETED", "costs": [{"kind": "observed", "currency": "USD", "amount": "2.5"},
                                            {"kind": "unknown", "currency": "USD"}]}
    await record_native_result(sessions, run, task, now=NOW)
    root = "/v1/products/product-one/projects/project-one/services/devops"
    results = await client.get(root + "/results")
    assert results.status_code == 200 and len(results.json()["items"]) == 1
    result = results.json()["items"][0]
    assert result["run_id"] == run.run_id and result["origin"] == "native"
    assert result["costs"][1] == {"kind": "unknown", "currency": "USD"}
    result_id = result["result_id"]
    assert (await client.get(root + f"/results/{result_id}")).json() == result
    artifact_id = result["evidence_ref"]["artifact_id"]
    access = await client.get(root + f"/artifacts/{artifact_id}")
    content = await client.get(root + f"/artifacts/{artifact_id}/content")
    assert access.status_code == content.status_code == 200
    assert access.json()["artifact"] == result["evidence_ref"]
    assert content.content == evidence_bytes(task)
    assert content.headers["x-adp-content-sha256"] == result["evidence_ref"]["sha256"]
    foreign = root.replace("project-one", "foreign-project")
    for path in (f"/results/{result_id}", f"/artifacts/{artifact_id}",
                 f"/artifacts/{artifact_id}/content"):
        assert (await client.get(foreign + path)).status_code == 404
        assert (await client.get(root + path, headers={"X-Test-Relationship": "2"})).status_code == 404
    for path in ("/artifacts/foreign-artifact", "/artifacts/foreign-artifact/content"):
        assert (await client.get(root + path)).status_code == 404
    assert (await client.get(root + "/results", headers={"X-Test-Relationship": "2"})).json() == {"items": []}


async def test_native_cancel_request_waits_for_provider_readback(native_scenario):
    sessions, scope, binding, client = native_scenario
    async with sessions() as session:
        run = await import_native_task(session, binding, scope,
                                       {"agentSpaceId": "space-one", "taskType": "INVESTIGATION",
                                        "executionId": "cancel-live", "taskId": "cancel-task"}, now=NOW)
    root = f"/v1/products/product-one/projects/project-one/services/devops/runs/{run.run_id}/cancel"
    headers = {"Idempotency-Key": "native_cancel_0001", "X-Test-Actor": "owner-one"}
    denied = await client.post(root, json={"reason": "pause work"}, headers={**headers, "X-Test-Actor": "member-one"})
    assert denied.status_code == 404
    requested = await client.post(root, json={"reason": "pause work"}, headers=headers)
    replay = await client.post(root, json={"reason": "pause work"}, headers=headers)
    assert requested.status_code == replay.status_code == 202
    assert requested.json() == replay.json() and requested.json()["state"] == "cancel_requested"
    assert (await client.post(root, json={"reason": "changed"}, headers=headers)).status_code == 409

    class Provider:
        def __init__(self):
            self.calls = []

        def get_agent_space(self, *, agentSpaceId):
            return {"agentSpace": {"agentSpaceId": agentSpaceId}}

        def list_backlog_tasks(self, *, agentSpaceId, limit):
            return {"tasks": [{"agentSpaceId": agentSpaceId, "taskType": "INVESTIGATION",
                               "taskId": "cancel-task", "executionId": "cancel-live", "status": "IN_PROGRESS"}]}

        def update_backlog_task(self, *, agentSpaceId, taskId, taskStatus, clientToken):
            self.calls.append((agentSpaceId, taskId, taskStatus, clientToken))
            return {"task": {"status": "CANCELED"}}

        def get_backlog_task(self, *, agentSpaceId, taskId):
            return {"task": {"agentSpaceId": agentSpaceId, "taskId": taskId,
                             "executionId": "cancel-live", "status": "CANCELED"}}

    provider = Provider()

    async def authorize(selected):
        return SimpleNamespace(**binding.provider_identity, client=lambda service: provider)

    assert await sweep_native_bindings(sessions, authorize, now=NOW + timedelta(minutes=5)) == 1
    assert len(provider.calls) == 1 and provider.calls[0][:3] == ("space-one", "cancel-task", "CANCELED")
    detail = await client.get(root.removesuffix("/cancel"))
    assert detail.json()["state"] == "cancelled" and detail.json()["provider_state"] == "stopped"
    assert (await client.get(root.replace(f"/runs/{run.run_id}/cancel", "/results"))).json()["items"][0]["costs"] == [
        {"kind": "unknown", "currency": "USD"}]
