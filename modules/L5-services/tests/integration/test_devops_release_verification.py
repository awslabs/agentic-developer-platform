"""Offline release-verification admission, provider submission, and readback."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import ReadTimeoutError
from fastapi import FastAPI, HTTPException
from jsonschema import Draft202012Validator
from l5_services.adapters.devops.release_verification import ReleaseVerificationAdapter
from l5_services.admission import submit_and_dispatch
from l5_services.api.router import create_router
from l5_services.findings import ResultStore
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.base import Base
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.evidence import AssessmentResult
from l5_services.models.preparation import AdmissionPreparation
from l5_services.models.receipt import RunReceipt
from l5_services.models.result import ResultDetail
from l5_services.models.run import Run
from l5_services.models.work import Work
from l5_services.run_access import RunStore
from l5_services.workers.execution import ExecutionService
from sqlalchemy import null, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2026, 10, 26, 9, tzinfo=UTC)
SCHEMA = json.loads((Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1/schemas.json").read_text())


def fixture_data(*, historical=False):
    scope = {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one",
             "relationship_revision": "1", "owner_id": "owner-one", "visibility": "only_me"}

    def reference(kind, identifier, revision="revision-one"):
        return {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                "source_connection_id": "connection-one", "record_type": kind,
                "record_id": identifier, "record_revision": revision}

    scenarios = [{"artifact_id": "scenario-good", "version": "1", "sha256": "a" * 64,
                  "media_type": "application/vnd.adp.release-scenario+json", "bytes": 2048},
                 {"artifact_id": "scenario-bad", "version": "1", "sha256": "b" * 64,
                  "media_type": "application/vnd.adp.release-scenario+json", "bytes": 2048}]
    options = {"kind": "release_verification", "source_ref": reference("repository_commit", "repo", "a" * 40),
               "deployment_ref": reference("deployment", "deploy"),
               "target_ref": reference("verification_target", "isolated-network"),
               "credential_ref": reference("secret", "nonadmin-test-user"), "scenarios": scenarios}
    request = {"service_id": "devops", "configuration_revision": 1, "binding_id": "binding-one",
               "capability": "release_verification", "devops_input": options}
    observation = {key: options[key] for key in ("source_ref", "deployment_ref", "target_ref", "credential_ref")}
    observation.update(deployed_sha="a" * 40, isolated=True, admin=False,
                       network_scope="isolated-network", deployment_adapter="fixture-adapter",
                       checked_at="2026-10-26T09:00:00Z", expires_at="2026-10-26T12:00:00Z",
                       scenario_specs=[{"artifact": artifact, "max_requests": 3, "timeout_seconds": 60,
                                        "network_scope": "isolated-network",
                                        "positive_expected": "allowed", "negative_expected": "denied",
                                        "cleanup_ref": reference("fixture_data", artifact["artifact_id"])}
                                       for artifact in scenarios])
    identity = {"account_id": "123456789012", "region": "us-east-1",
                "role_arn": "arn:aws:iam::123456789012:role/example",
                "verified_at": "2026-10-26T09:00:00Z"}
    verified = {"scope": scope, "service_id": "devops", "actor_id": "owner-one", "actor_type": "human",
                "configuration_id": "configuration-one", "configuration_revision": 1,
                "configuration_hash": "f" * 64, "configuration_enabled": True,
                "binding_id": "binding-one", "binding_generation": 2, "binding_state": "ready",
                "connection_ref": reference("connection", "connection-one"),
                "binding_connection_ref": reference("connection", "connection-one"),
                "native_space_ref": reference("agent_space", "space-one"), "provider_identity": identity,
                "delegation_id": "delegation-one", "delegation_version": 1, "delegation_active": True,
                "profile_ref": reference("product", "product-one"),
                "trigger": {"trigger_id": "trigger-one", "kind": "manual", "enabled": True,
                            "capability": "release_verification", "binding_id": "binding-one",
                            "resource_refs": [], "telemetry_refs": []},
                "adapter_revision": "devops-v1", "visibility": "only_me", "classification": "internal",
                "causal_origin": "manual", "exclusions": [], "max_bytes": 1024, "max_files": 10,
                "monitoring_policy": {"poll_interval_seconds": 600, "poll_grace_seconds": 300},
                "limits": {"deadline_seconds": 3600, "max_provider_attempts": 1, "max_polls": 10,
                           "concurrency": 1, "max_cost": "1.00", "currency": "USD",
                           "policy_ref": reference("policy", "budget")}}
    if not historical:
        profile = reference("test_profile", "api-profile", "3")
        options["test_profile_ref"] = profile
        verified["trigger"]["test_profile_ref"] = profile
        verified["adapter_revision"] = "devops-release-verification-v2"
    return scope, request, observation, verified


class EvidenceReader:
    def __init__(self, observation):
        self.observation = observation
        self.calls = 0
        self.results = []
        self.fixture_data = {}
        self.delete_count = 0
        self.fail_after_cleanup = False
        self.change_deployment_after_cleanup = False

    async def read_deployment(self, deployment_ref, target_ref):
        self.calls += 1
        if self.delete_count and self.fail_after_cleanup:
            self.fail_after_cleanup = False
            raise OSError("synthetic reporting failure after cleanup")
        if self.delete_count and self.change_deployment_after_cleanup:
            return {**self.observation, "deployed_sha": "b" * 40}
        return dict(self.observation)

    async def read_scenarios(self, scenarios):
        assert scenarios == [spec["artifact"] for spec in self.observation["scenario_specs"]]
        return self.observation["scenario_specs"]

    async def read_fixture_data(self, reference):
        return dict(self.fixture_data[reference["record_id"]])

    async def delete_fixture_data(self, reference, *, owner_run_id, idempotency_key):
        fixture = self.fixture_data[reference["record_id"]]
        assert fixture["owner_run_id"] == owner_run_id and fixture["state"] == "present"
        self.delete_count += 1
        self.fixture_data[reference["record_id"]] = {
            "fixture_ref": reference, "state": "absent", "owner_run_id": owner_run_id,
            "deletion_token": idempotency_key}

    async def read_results(self, deployment_ref, execution_id):
        assert deployment_ref == self.observation["deployment_ref"] and execution_id == "execution-one"
        return self.results


class NativeProvider:
    def __init__(self, reader=None):
        self.reader = reader
        self.calls = 0
        self.get_space_calls = 0
        self.task = None
        self.native_receipt = None
        self.lose_ack = False

    async def read_native_results(self, agent_space_id, task_id, execution_id):
        assert agent_space_id == "space-one" and task_id == "task-one" and execution_id == "execution-one"
        return self.native_receipt

    def get_agent_space(self, *, agentSpaceId):
        self.get_space_calls += 1
        return {"agentSpace": {"agentSpaceId": agentSpaceId}}

    def create_backlog_task(self, *, agentSpaceId, taskType, title, description, clientToken):
        assert taskType == "RELEASE_TESTING" and len(clientToken) == 64
        self.calls += 1
        self.task = {"agentSpaceId": agentSpaceId, "taskType": taskType, "description": description,
                     "taskId": "task-one", "executionId": "execution-one", "status": "IN_PROGRESS"}
        if self.reader is not None:
            payload = json.loads(description)
            for spec in payload["scenario_specs"]:
                reference = spec["cleanup_ref"]
                self.reader.fixture_data[reference["record_id"]] = {
                    "fixture_ref": reference, "state": "present", "owner_run_id": payload["run_id"],
                    "execution_id": "execution-one"}
        if self.lose_ack:
            raise ReadTimeoutError(endpoint_url="https://offline.invalid")
        return {"task": self.task}

    def get_backlog_task(self, *, agentSpaceId, taskId):
        assert taskId == "task-one" and agentSpaceId == "space-one"
        return {"task": self.task}

    def list_backlog_tasks(self, *, agentSpaceId, limit):
        assert agentSpaceId == "space-one" and limit == 100
        return {"tasks": [self.task] if self.task else []}


async def seed_historical_run(session, request, observation, verified, key, *, status="prepared"):
    """Persist pre-profile history directly; never authorize a new legacy effect."""
    scope = verified["scope"]
    run_id, work_id, attempt_id = (str(uuid4()) for _ in range(3))
    deadline = NOW + timedelta(hours=1)
    fields = ("scope", "profile_ref", "connection_ref", "delegation_id", "delegation_version",
              "binding_generation", "actor_id", "configuration_hash", "adapter_revision", "limits",
              "visibility", "classification", "causal_origin", "exclusions", "monitoring_policy",
              "service_id", "native_space_ref", "provider_identity")
    admission = {**{key: verified[key] for key in fields}, "verification": observation, "source_snapshots": [],
                 "admitted_at": NOW.isoformat().replace("+00:00", "Z"),
                 "expires_at": deadline.isoformat().replace("+00:00", "Z")}
    manifest = {"schema_version": "1.0", "scope": scope, "run_id": run_id,
                "configuration_revision": 1, "binding_generation": 2, "sources": [],
                "verification": observation, "scenarios": request["devops_input"]["scenarios"],
                "coverage_mode": "deployment_verification"}
    digest = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    run = Run(tenant_id=scope["tenant_id"], run_id=run_id, product_id=scope["product_id"],
              project_id=scope["project_id"], service_id="devops", configuration_id="configuration-one",
              configuration_revision=1, binding_id="binding-one", trigger_id="trigger-one",
              binding_generation=2, input=request, admission=admission, deadline_at=deadline,
              state="outcome_unknown", provider_state="unknown", manifest=manifest,
              capture_limits={"max_bytes": 1024, "max_files": 10})
    session.add(run)
    await session.flush()
    session.add_all([
        RunReceipt(tenant_id=scope["tenant_id"], product_id=scope["product_id"],
                   project_id=scope["project_id"], service_id="devops", actor_id=scope["owner_id"],
                   operation="submit", idempotency_key=key, run_id=run_id, expires_at=deadline,
                   request_hash=sha256(json.dumps({"input": request, "event_id": None, "exclusions": []},
                       sort_keys=True, separators=(",", ":")).encode()).hexdigest()),
        Work(tenant_id=scope["tenant_id"], work_id=work_id, run_id=run_id, kind="reconcile",
             generation=1, step=1, due_at=NOW),
        WorkerAttempt(tenant_id=scope["tenant_id"], attempt_id=attempt_id, run_id=run_id,
                      operation="submit", first_work_id=work_id, first_fence=1, input_hash=digest,
                      idempotency_token=sha256(f"{scope['tenant_id']}:{run_id}:{digest}".encode()).hexdigest(),
                      started_at=NOW, status=status),
    ])
    await session.commit()
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "request", "no_receipt", "revoked"])
async def test_historical_receipt_replay_never_admits_legacy_work(tmp_path, change):
    from copy import deepcopy
    from l5_services.admission import admit_run

    scope, request, observation, verified = fixture_data(historical=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'historical.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        original = await seed_historical_run(session, request, observation, verified, "historical-key-0001")
        body, authority = deepcopy(request), deepcopy(verified)
        if change == "request":
            body["devops_input"]["target_ref"]["record_id"] = "other-target"
        if change == "revoked":
            authority["delegation_active"] = False
        key = "different-key-0001" if change == "no_receipt" else "historical-key-0001"
        if change:
            with pytest.raises(HTTPException) as caught:
                await admit_run(session, None, body, authority, key, now=NOW)
            assert caught.value.status_code == {"request": 409, "revoked": 404, "no_receipt": 409}[change]
        else:
            replay = await admit_run(session, None, body, authority, key, now=NOW)
            assert replay is original and replay.input == request
        assert len((await session.scalars(select(Run))).all()) == 1
        assert len((await session.scalars(select(WorkerAttempt))).all()) == 1
    await engine.dispose()


async def _release_verification_scenario(tmp_path, fault):
    scope, request, observation, verified = fixture_data(historical=True)
    native_failure = fault is not None and fault.startswith("native_failure")
    scenarios_pass = fault in {"all_pass", "native_failure_all_pass"}
    assert list(Draft202012Validator({**SCHEMA, "$ref": "#/$defs/RunInput"}).iter_errors(request))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'release.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(Configuration(tenant_id=scope["tenant_id"], configuration_id="configuration-one",
                                  product_id=scope["product_id"], project_id=scope["project_id"],
                                  service_id="devops", relationship_revision="1", owner_id="owner-one",
                                  current_revision=1, handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id=scope["tenant_id"], configuration_id="configuration-one",
                                         revision=1, settings={}, inherited_fields=[], gaps=[],
                                         state="ready", hash="f" * 64))
        await session.commit()
        await seed_historical_run(session, request, observation, verified, "release-fixture-one-00001",
                                  status="outcome_unknown" if fault == "lost_ack" else "prepared")
    messages = []

    async def publish(envelope, delay):
        messages.append(envelope)

    reader = EvidenceReader(observation)

    from l5_services.dispatcher import dispatch_work
    async with sessions() as session:
        work = (await session.scalars(select(Work))).one()
        work_id = work.work_id
    await dispatch_work(sessions, scope["tenant_id"], work_id, publish, now=NOW)

    async def budget_guard(session, limits, scope, now, replay):
        return await replay()

    class Backend:
        def __init__(self, session):
            self.session = session

        async def submit(self, product_id, project_id, service_id, body, key):
            assert (product_id, project_id, service_id) == ("product-one", "project-one", "devops")
            async with sessions() as session:
                return await submit_and_dispatch(session, sessions, None, publish, body, verified, key,
                                                 now=NOW, budget_guard=budget_guard,
                                                 verification_reader=reader)

        async def get_run(self, product_id, project_id, service_id, run_id):
            return await RunStore(self.session, "tenant-one", product_id, project_id, service_id,
                                  "owner-one", "1").get(run_id)

        async def result_store(self, product_id, project_id, service_id):
            runs = await RunStore(self.session, "tenant-one", product_id, project_id, service_id,
                                  "owner-one", "1").list()
            return ResultStore(self.session, runs, actor_id="owner-one", clock=lambda: NOW)

    async def backend():
        async with sessions() as session:
            yield Backend(session)

    app = FastAPI()
    app.include_router(create_router(backend))
    path = "/v1/products/product-one/projects/project-one/services/devops"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline.invalid") as client:
        response = await client.post(path + "/runs", json=request,
                                     headers={"Idempotency-Key": "release-fixture-one-00001"})
        assert response.status_code == 202, response.text
        replay = await client.post(path + "/runs", json=request,
                                   headers={"Idempotency-Key": "release-fixture-one-00001"})
        assert replay.status_code == 202 and replay.json() == response.json()
    assert reader.calls == 0 and len(messages) == 1
    async with sessions() as independent:
        run = (await independent.scalars(select(Run))).one()
        assert run.admission["verification"] == observation and run.input == request
        assert run.run_id == response.json()["run_id"]
    provider = NativeProvider(reader)
    identity = verified["provider_identity"]
    provider_session = SimpleNamespace(**{key: identity[key] for key in ("account_id", "region", "role_arn")},
                                       client=lambda service: provider if service == "devops-agent" else None)

    async def authorize(run, claim, operation):
        return provider_session

    clock = {"now": NOW}
    def create_worker(missing_native_reader=False):
        adapter = ReleaseVerificationAdapter(reader, None if missing_native_reader else provider,
                                             clock=lambda: clock["now"])
        return ExecutionService(sessions, None, adapter, publish, authorize, clock=lambda: clock["now"])

    # Model an already accepted historical effect, including lost acknowledgement
    # or worker state. The current adapter must only read it, never resubmit it.
    async with sessions() as session:
        attempt = (await session.scalars(select(WorkerAttempt))).one()
        description = ReleaseVerificationAdapter(reader)._description(run, attempt)
    provider.task = {"agentSpaceId": "space-one", "taskType": "RELEASE_TESTING",
                     "description": description, "taskId": "task-one", "executionId": "execution-one",
                     "status": "IN_PROGRESS"}
    for spec in observation["scenario_specs"]:
        reference = spec["cleanup_ref"]
        reader.fixture_data[reference["record_id"]] = {
            "fixture_ref": reference, "state": "present", "owner_run_id": run.run_id,
            "execution_id": "execution-one"}
    worker = create_worker()
    assert await worker.execute(messages[0]) is True
    assert provider.calls == 0 and reader.calls == 0
    async with sessions() as independent:
        run = await independent.get(Run, ("tenant-one", run.run_id))
        assert run.native_job_ref["record_id"] == "execution-one" and run.state == "running"
        recovered = (await independent.scalars(select(WorkerAttempt))).one()
        assert recovered.attempt_id == attempt.attempt_id
        assert recovered.idempotency_token == attempt.idempotency_token
    def evidence(identifier):
        return {"artifact_id": identifier, "version": "1",
                "sha256": sha256(identifier.encode()).hexdigest(),
                "media_type": "application/json", "bytes": 42}

    provider.task["status"] = "COMPLETED"
    provider.native_receipt = {
        "task_id": "task-one", "execution_id": "execution-one",
        "deployment_ref": request["devops_input"]["deployment_ref"],
        "source_ref": request["devops_input"]["source_ref"],
        "scenario_results": [
            {"scenario_id": "scenario-good", "status": "passed", "evidence_ref": evidence("native-good")},
            {"scenario_id": "scenario-bad", "status": "passed" if scenarios_pass else "failed",
             "evidence_ref": evidence("native-bad")}]}
    reader.results = [
        {"scenario_id": identifier, "status": independent_status,
         "deployment_ref": request["devops_input"]["deployment_ref"],
         "deployed_sha": request["devops_input"]["source_ref"]["record_revision"],
         "evidence_ref": evidence(f"independent-{identifier}"),
         "positive": {"expected_outcome": "allowed", "observed_outcome": "allowed",
                      "evidence_ref": evidence(f"positive-{identifier}")},
         "negative": {"expected_outcome": "denied", "observed_outcome": negative_observed,
                      "evidence_ref": evidence(f"negative-{identifier}")}}
        for identifier, independent_status, negative_observed in (
            ("scenario-good", "passed", "denied"),
            ("scenario-bad", "passed" if scenarios_pass else "failed",
             "denied" if scenarios_pass else "allowed"))]
    if native_failure:
        provider.task["status"] = "TIMED_OUT" if fault == "native_failure_timeout" else "FAILED"
        if fault == "native_failure_partial":
            provider.native_receipt["scenario_results"] = provider.native_receipt["scenario_results"][1:]
            reader.results = reader.results[1:]
        elif fault == "native_failure_empty":
            provider.native_receipt = None
            reader.results = []
        elif fault == "native_failure_native_only":
            reader.results = None
        elif fault == "native_failure_independent_only":
            provider.native_receipt = None
    if fault == "oracle_mismatch":
        reader.results[1]["negative"]["observed_outcome"] = "denied"
    if fault == "native_receipt_mismatch":
        provider.native_receipt["source_ref"] = {**request["devops_input"]["source_ref"],
                                                  "record_revision": "b" * 40}
    if fault == "oracle_deployment_mismatch":
        reader.results[0]["deployed_sha"] = "b" * 40
    clock["now"] += timedelta(seconds=600)
    poll_index = 1
    assert await worker.execute(messages[poll_index]) is True
    collect_index = poll_index + 1
    if native_failure:
        async with sessions() as independent:
            failed = await independent.get(Run, ("tenant-one", run.run_id))
            outstanding = (await independent.scalars(select(Work).where(Work.completed_at.is_(None)))).one()
            assert failed.state == "collecting" and failed.provider_state == "failed"
            assert outstanding.kind == "collect" and provider.calls == 0
    if fault in {"oracle_mismatch", "native_receipt_mismatch", "oracle_deployment_mismatch"}:
        expected_error = ("native scenario receipt" if fault == "native_receipt_mismatch"
                          else "independent scenario outcome" if fault == "oracle_mismatch"
                          else "independent scenario observation")
        with pytest.raises(ValueError, match=expected_error):
            await worker.execute(messages[collect_index])
        async with sessions() as independent:
            assert (await independent.get(Run, ("tenant-one", run.run_id))).state == "collecting"
            assert not (await independent.scalars(select(ResultDetail))).all()
        assert reader.delete_count == 0 and provider.calls == 0
        if fault == "oracle_mismatch":
            reader.results[1]["negative"]["observed_outcome"] = "allowed"
        elif fault == "native_receipt_mismatch":
            provider.native_receipt["source_ref"] = request["devops_input"]["source_ref"]
        else:
            reader.results[0]["deployed_sha"] = request["devops_input"]["source_ref"]["record_revision"]
        clock["now"] += timedelta(seconds=61)
    if fault == "missing_native_reader":
        with pytest.raises(ValueError, match="qualified native scenario-results reader required"):
            await create_worker(missing_native_reader=True).execute(messages[collect_index])
        async with sessions() as independent:
            assert (await independent.get(Run, ("tenant-one", run.run_id))).state == "collecting"
            assert not (await independent.scalars(select(ResultDetail))).all()
        assert provider.calls == 0 and reader.delete_count == 0
        clock["now"] += timedelta(seconds=61)
    if fault in {"reporting_failure", "deployment_changed_at_cleanup", "native_failure_reporting"}:
        reader.fail_after_cleanup = fault in {"reporting_failure", "native_failure_reporting"}
        reader.change_deployment_after_cleanup = fault == "deployment_changed_at_cleanup"
        with pytest.raises((OSError, ValueError), match=("synthetic reporting failure" if
                             fault != "deployment_changed_at_cleanup" else "deployment changed during fixture cleanup")):
            await worker.execute(messages[collect_index])
        assert provider.calls == 0 and reader.delete_count == 2
        async with sessions() as independent:
            assert (await independent.get(Run, ("tenant-one", run.run_id))).state == "collecting"
            assert not (await independent.scalars(select(ResultDetail))).all()
        reader.change_deployment_after_cleanup = False
        clock["now"] += timedelta(seconds=61)
    assert await create_worker().execute(messages[collect_index]) is True
    async with sessions() as independent:
        stored = await independent.get(Run, ("tenant-one", run.run_id))
        detail = (await independent.scalars(select(ResultDetail))).one()
        raw = (await independent.scalars(select(AssessmentResult))).one()
        assert stored.state == ("succeeded" if fault == "all_pass" else "failed") and provider.calls == 0
        assert detail.payload["scenarios"] == (provider.native_receipt or {}).get("scenario_results", [])
        assert detail.payload["independent_observations"] == (reader.results or [])
        if native_failure:
            assert stored.provider_state == "failed"
            assert detail.payload["coverage"] == ("unknown" if fault == "native_failure_empty" else
                                                  "partial" if fault in {"native_failure_partial", "native_failure_native_only",
                                                                         "native_failure_independent_only"} else "complete")
            assert not (await independent.scalars(select(Work).where(Work.completed_at.is_(None)))).all()
            assert all(row["state"] == "absent" for row in reader.fixture_data.values())
        assert len(detail.payload["cleanup"]) == 2
        assert all(row["state"] == "absent" and row["owner_run_id"] == run.run_id
                   for row in detail.payload["cleanup"])
        assert reader.delete_count == 2 and reader.observation["deployed_sha"] == "a" * 40
        assert reader.calls >= 1
        assert raw.payload["provider_outcome"] == ("completed" if fault == "all_pass" else "failed")
        assert detail.payload["admission"] == stored.admission
        assert not list(Draft202012Validator({**SCHEMA, "$ref": "#/$defs/Result"}).iter_errors(detail.payload))
        if fault == "native_failure_empty":
            validator = Draft202012Validator({**SCHEMA, "$ref": "#/$defs/Result"})
            assert not validator.is_valid({**detail.payload, "provider_outcome": "completed"})
            assert not validator.is_valid({**detail.payload, "coverage": "complete"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline.invalid") as client:
        response = await client.get(path + "/results/" + detail.result_id)
        assert response.status_code == 200, response.text
        assert response.json()["scenarios"] == (provider.native_receipt or {}).get("scenario_results", [])
        assert response.json()["independent_observations"] == (reader.results or [])
        assert response.json()["deployment_ref"] == request["devops_input"]["deployment_ref"]
        assert response.json()["deployed_sha"] == request["devops_input"]["source_ref"]["record_revision"]
        assert response.json()["source_ref"] == request["devops_input"]["source_ref"]
        assert response.json()["native_task_id"] == "task-one"
        assert len(response.json()["cleanup"]) == 2
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "all_pass", "oracle_mismatch", "native_receipt_mismatch",
                                   "oracle_deployment_mismatch"])
async def test_release_verification(tmp_path, fault):
    await _release_verification_scenario(tmp_path, fault)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["lost_ack", "submit_crash", "reporting_failure",
                                   "deployment_changed_at_cleanup", "missing_native_reader"],
                         ids=["lost-acknowledgement", "lost-worker", "lost-report",
                              "deployment-drift-during-cleanup", "native-reader-unavailable"])
async def test_verification_recovery(tmp_path, fault):
    await _release_verification_scenario(tmp_path, fault)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["native_failure_full", "native_failure_partial", "native_failure_empty",
                                   "native_failure_timeout", "native_failure_reporting", "native_failure_all_pass",
                                   "native_failure_native_only", "native_failure_independent_only"])
async def test_verification_recovery_cleans_failed_execution(tmp_path, fault):
    await _release_verification_scenario(tmp_path, fault)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["stale_sha", "expired_receipt", "receipt_expires_early", "admin_credential",
                                     "outside_scope", "duplicate_cleanup", "foreign_reference", "no_reader",
                                     "oversized_description", "trigger_resources", "trigger_telemetry"],
                         ids=["stale-deployment", "expired-receipt", "expiring-receipt", "admin-credential",
                              "out-of-scope-target", "duplicate-fixture-cleanup", "foreign-project", "no-evidence-adapter",
                              "maximum-scenario-count", "mismatched-trigger-resources", "mismatched-trigger-telemetry"])
async def test_verification_target_denial(tmp_path, invalid):
    scope, request, observation, verified = fixture_data()
    if invalid == "oversized_description":
        scenarios = [{**request["devops_input"]["scenarios"][0], "artifact_id": f"scenario-{index}"}
                     for index in range(20)]
        request["devops_input"]["scenarios"] = scenarios
        specification = observation["scenario_specs"][0]
        observation["scenario_specs"] = [
            {**specification, "artifact": artifact,
             "cleanup_ref": {**specification["cleanup_ref"], "record_id": artifact["artifact_id"]}}
            for artifact in scenarios]
        assert not list(Draft202012Validator({**SCHEMA, "$ref": "#/$defs/RunInput"}).iter_errors(request))
    elif invalid == "stale_sha":
        observation["deployed_sha"] = "b" * 40
    elif invalid == "expired_receipt":
        observation["checked_at"] = "2026-10-26T08:50:00Z"
    elif invalid == "receipt_expires_early":
        observation["expires_at"] = "2026-10-26T09:15:00Z"
    elif invalid == "admin_credential":
        observation["admin"] = True
    elif invalid == "outside_scope":
        observation["network_scope"] = "production-network"
    elif invalid == "duplicate_cleanup":
        observation["scenario_specs"][1]["cleanup_ref"] = observation["scenario_specs"][0]["cleanup_ref"]
    elif invalid == "foreign_reference":
        request["devops_input"]["target_ref"] = {
            **request["devops_input"]["target_ref"], "project_id": "other-project"}
    elif invalid in {"trigger_resources", "trigger_telemetry"}:
        field = "resource_refs" if invalid == "trigger_resources" else "telemetry_refs"
        verified["trigger"][field] = [request["devops_input"]["target_ref"]]
    reader = EvidenceReader(observation)
    provider = NativeProvider()
    messages = []
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'denial.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def budget_guard(session, limits, current_scope, now, replay):
        return await replay()

    class Backend:
        async def submit(self, product_id, project_id, service_id, body, key):
            assert (product_id, project_id, service_id) == ("product-one", "project-one", "devops")
            async with sessions() as session:
                return await submit_and_dispatch(
                    session, sessions, None, lambda envelope, delay: messages.append(envelope),
                    body, verified, key, now=NOW, budget_guard=budget_guard,
                    verification_reader=None if invalid == "no_reader" else reader,
                )

    app = FastAPI()
    app.include_router(create_router(lambda: Backend()))
    path = "/v1/products/product-one/projects/project-one/services/devops/runs"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline.invalid") as client:
        response = await client.post(path, json=request,
                                     headers={"Idempotency-Key": "denied-verification-00001"})
        if invalid in {"trigger_resources", "trigger_telemetry"}:
            assert response.status_code == 404, response.text
            assert response.json()["detail"]["code"] == "not_found"
        elif invalid == "foreign_reference":
            assert response.status_code == 422, response.text
            assert response.json()["detail"]["code"] == "validation_error"
        elif invalid == "no_reader":
            assert response.status_code == 503, response.text
            assert response.json()["detail"]["code"] == "deployment_evidence_adapter_unavailable"
        elif invalid == "oversized_description":
            assert response.status_code == 422, response.text
            assert response.json()["detail"]["code"] == "verification_description_too_large"
        else:
            assert response.status_code == 409, response.text
            reason = {"stale_sha": "verification_deployment_stale",
                      "expired_receipt": "verification_deployment_stale",
                      "receipt_expires_early": "verification_deployment_stale",
                      "admin_credential": "verification_fixture_privileged",
                      "outside_scope": "verification_target_out_of_scope",
                      "duplicate_cleanup": "verification_target_unverified"}[invalid]
            assert response.json()["detail"]["code"] == reason
    async with sessions() as independent:
        assert not (await independent.scalars(select(Run))).all()
        assert not (await independent.scalars(select(Work))).all()
        assert not (await independent.scalars(select(AdmissionPreparation))).all()
        assert not (await independent.scalars(select(RunReceipt))).all()
        assert not (await independent.scalars(select(WorkerAttempt))).all()
    assert messages == [] and provider.calls == 0
    assert reader.calls == (0 if invalid in {"foreign_reference", "no_reader", "trigger_resources", "trigger_telemetry"} else 1)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", [None, "delegated_capability", "trigger_capability", "binding", "owner",
                                  "revoked_membership", "missing_team_membership"])
async def test_gateway_release_verification_authorization(tmp_path, denial):
    from decimal import Decimal

    from l5_services.models.binding import Binding
    from src import l5_integration
    from src.auth.aws_connection_authority import connection_binding
    from src.products.models import Product
    from src.shared.models.base import Base as GatewayBase
    from src.shared.models.budget import BudgetConfig
    from src.shared.models.onboarding import TenantMembership
    from src.shared.models.organization import Organization, Team, TeamMembership, User
    from src.shared.models.vault import CredentialServiceDelegation, UserCredential

    scope, request, observation, verified = fixture_data()
    now = datetime.now(UTC)
    observation.update(checked_at=now.isoformat(), expires_at=(now + timedelta(hours=3)).isoformat())
    reader = EvidenceReader(observation)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gateway-release.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(GatewayBase.metadata.create_all)
    trigger = dict(verified["trigger"])
    if denial == "trigger_capability":
        trigger["capability"] = "investigation"
    if denial == "binding":
        request["binding_id"] = "other-binding"
    settings = {key: verified[key] for key in ("profile_ref", "visibility", "classification", "limits")}
    settings.update(enabled=True, devops={
        "region": verified["provider_identity"]["region"],
        "hosting_connection_ref": verified["connection_ref"], "trigger_policies": [trigger]})
    credential = UserCredential(
        id="connection-one", org_id=scope["tenant_id"], user_id=scope["owner_id"], service="aws",
        credential_type="aws_role", label="release-verification", secret_arn="offline-secret",
        aws_external_id="offline-external-id", aws_verified_at=now,
        aws_verification_attempt="verification-one", aws_verified_version_id="revision-one",
        scopes={**verified["provider_identity"], "status": "verified"})
    grant = CredentialServiceDelegation(
        id=verified["delegation_id"], tenant_id=scope["tenant_id"], product_id=scope["product_id"],
        project_id=scope["project_id"], credential_id=credential.id, created_by=scope["owner_id"],
        service_id="l5-devops-worker", revision=1, binding_generation=2,
        operations=["product:read", "credential:session"],
        capabilities=["investigation" if denial == "delegated_capability" else "release_verification"],
        sources=[{**verified["connection_ref"], "record_type": "credential"}],
        visibility="only_me", expires_at=now + timedelta(hours=4))
    async with sessions() as session:
        session.add_all([
            Organization(id=scope["tenant_id"], name="Release verification fixture", created_via="operator"),
            Team(id="team-one", org_id=scope["tenant_id"], department_id="department-one", name="Release team"),
            User(id=scope["owner_id"], org_id=scope["tenant_id"], team_id="team-one", email="owner@example.test"),
            TenantMembership(user_id=scope["owner_id"], tenant_id=scope["tenant_id"], is_active=True,
                             revoked_at=now if denial == "revoked_membership" else None),
            Product(tenant_id=scope["tenant_id"], id=scope["product_id"], name="Release product",
                    purpose="Offline authorization fixture", intended_users=[], owner_user_id=scope["owner_id"],
                    team_id="team-one", starting_point="new", created_by=scope["owner_id"]),
            credential, grant,
        ])
        if denial != "missing_team_membership":
            session.add(TeamMembership(user_id=scope["owner_id"], team_id="team-one", org_id=scope["tenant_id"]))
        session.add(Configuration(
            tenant_id=scope["tenant_id"], configuration_id=verified["configuration_id"],
            product_id=scope["product_id"], project_id=scope["project_id"], service_id="devops",
            relationship_revision="1", owner_id=scope["owner_id"], current_revision=1, handoff_state="linked"))
        session.add(ConfigurationVersion(
            tenant_id=scope["tenant_id"], configuration_id=verified["configuration_id"], revision=1,
            settings=settings, inherited_fields=[], gaps=[], state="ready", hash=verified["configuration_hash"]))
        session.add(Binding(
            tenant_id=scope["tenant_id"], binding_id=verified["binding_id"], configuration_id=verified["configuration_id"],
            product_id=scope["product_id"], project_id=scope["project_id"], service_id="devops", scope=scope,
            revision=1, generation=2, state="ready", ownership="adopted", connection_ref=verified["connection_ref"],
            connection_key="a" * 64, native_key="b" * 64, native_resource_ref=verified["native_space_ref"],
            provider_identity=verified["provider_identity"], readiness=[], gaps=[]))
        session.add(BudgetConfig(
            id=verified["limits"]["policy_ref"]["record_id"], org_id=scope["tenant_id"], entity_type="org",
            entity_id=scope["tenant_id"], period_type="daily", budget_amount_usd=Decimal(100), enforcement_mode="hard"))
        await session.flush()
        credential.aws_verified_binding = connection_binding(credential)
        await session.commit()

    class GatewayBackend(l5_integration.GatewayConfigurationBackend):
        async def current(self, product_id, project_id, service_id):
            return await self.store.current(product_id, project_id, service_id)

    messages = []

    async def publish(envelope, delay):
        messages.append(envelope)

    actor = SimpleNamespace(org_id=scope["tenant_id"], account_type="human", team_id="team-one", is_admin=False,
                            user_id="other-owner" if denial == "owner" else scope["owner_id"])

    async def backend():
        async with sessions() as session:
            yield GatewayBackend(session, actor, publish=publish, verification_reader=reader)

    app = FastAPI()
    app.include_router(create_router(backend))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline.invalid") as client:
        response = await client.post("/v1/products/product-one/projects/project-one/services/devops/runs",
                                     json=request, headers={"Idempotency-Key": "gateway-release-00001"})
        if denial is None:
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "test_profile_schema_unqualified"
    async with sessions() as independent:
        if denial is None:
            assert not (await independent.scalars(select(Run))).all()
            assert not (await independent.scalars(select(Work))).all()
            assert not messages and reader.calls == 1
        else:
            assert response.status_code == (403 if denial in {
                "delegated_capability", "revoked_membership", "missing_team_membership"} else 404), response.text
            assert not (await independent.scalars(select(Run))).all()
            assert not (await independent.scalars(select(Work))).all()
            assert not (await independent.scalars(select(AdmissionPreparation))).all()
            assert (await independent.scalars(select(CredentialServiceDelegation))).one().id == grant.id
            assert not messages and reader.calls == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_refuses_unowned_fixture():
    _, request, observation, _ = fixture_data()
    reader = EvidenceReader(observation)
    reference = observation["scenario_specs"][0]["cleanup_ref"]
    reader.fixture_data[reference["record_id"]] = {
        "fixture_ref": reference, "state": "present", "owner_run_id": "another-run",
        "execution_id": "execution-one"}
    run = SimpleNamespace(tenant_id="tenant-one", run_id="original-run",
                          admission={"verification": observation}, input=request)
    adapter = ReleaseVerificationAdapter(reader, clock=lambda: NOW)
    with pytest.raises(ValueError, match="fixture cleanup ownership unverified"):
        await adapter._cleanup(run, {"record_id": "execution-one"})
    assert reader.delete_count == 0
    assert reader.fixture_data[reference["record_id"]]["state"] == "present"
    assert reader.observation["deployment_ref"] == request["devops_input"]["deployment_ref"]


def test_result_readers_must_be_distinct():
    reader = EvidenceReader(fixture_data()[2])
    with pytest.raises(ValueError, match="distinct readers"):
        ReleaseVerificationAdapter(reader, reader)


@pytest.mark.asyncio
@pytest.mark.parametrize("historical", [True, False])
@pytest.mark.parametrize("drift", [None, "stale", "admin", "network"])
async def test_no_profile_contract_can_start_a_native_test(historical, drift):
    scope, request, observation, verified = fixture_data(historical=historical)
    reader, provider = EvidenceReader(observation), NativeProvider()
    manifest = {"scope": scope, "run_id": "run-one", "verification": observation,
                "scenarios": request["devops_input"]["scenarios"]}
    run = SimpleNamespace(service_id="devops", tenant_id=scope["tenant_id"], product_id=scope["product_id"],
                          project_id=scope["project_id"], run_id="run-one", input=request,
                          admission={**verified, "verification": observation}, manifest=manifest,
                          deadline_at=NOW + timedelta(hours=1))
    if drift:
        field, value = {"stale": ("deployed_sha", "b" * 40), "admin": ("admin", True),
                        "network": ("network_scope", "other-network")}[drift]
        reader.observation = {**observation, field: value}
    session = SimpleNamespace(**{key: verified["provider_identity"][key]
                                 for key in ("account_id", "region", "role_arn")}, client=lambda name: provider)
    outcome = await ReleaseVerificationAdapter(reader, clock=lambda: NOW).submit(run, manifest, None, session)
    assert outcome.classification == "definitive_failure"
    assert outcome.observation["code"] == ({
        "stale": "verification_deployment_stale", "admin": "verification_fixture_privileged",
        "network": "verification_target_out_of_scope",
    }[drift] if drift else "native_test_profile_required" if historical else "test_profile_schema_unqualified")
    assert provider.calls == provider.get_space_calls == 0


@pytest.mark.asyncio
async def test_v2_exact_description_reconciliation_does_not_resubmit():
    from copy import deepcopy

    scope, request, observation, verified = fixture_data()
    reader, provider = EvidenceReader(observation), NativeProvider()
    manifest = {"scope": scope, "run_id": "run-one", "verification": observation,
                "scenarios": request["devops_input"]["scenarios"],
                "adapter_revision": verified["adapter_revision"],
                "test_profile_ref": request["devops_input"]["test_profile_ref"]}
    run = SimpleNamespace(service_id="devops", tenant_id=scope["tenant_id"], product_id=scope["product_id"],
                          project_id=scope["project_id"], run_id="run-one", input=request,
                          admission={**verified, "verification": observation}, manifest=manifest)
    digest = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    attempt = SimpleNamespace(run_id=run.run_id, attempt_id="original-attempt", input_hash=digest,
                              idempotency_token=sha256(f"{run.tenant_id}:{run.run_id}:{digest}".encode()).hexdigest())
    adapter = ReleaseVerificationAdapter(reader)
    description = adapter._description(run, attempt)
    provider.task = {"agentSpaceId": "space-one", "taskType": "RELEASE_TESTING", "description": description,
                     "taskId": "task-one", "executionId": "execution-one", "status": "IN_PROGRESS"}
    session = SimpleNamespace(**{key: verified["provider_identity"][key]
                                 for key in ("account_id", "region", "role_arn")}, client=lambda name: provider)
    outcome = await adapter.reconcile_submit(run, attempt, session)
    assert outcome.classification == "accepted"
    assert (await adapter.observe(run, outcome.native_job_ref, attempt, session)).classification == "running"
    message = json.loads(json.loads(description)["webhookEventMessage"])
    assert message["attempt_id"] == attempt.attempt_id and message["idempotency_token"] == attempt.idempotency_token
    changed = deepcopy(attempt)
    changed.attempt_id = "another-attempt"
    assert (await adapter.reconcile_submit(run, changed, session)).classification == "unknown"
    with pytest.raises(ValueError, match="pinned verification"):
        await adapter.observe(run, outcome.native_job_ref, changed, session)
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("historical", [True, False])
async def test_materialization_preserves_legacy_and_pins_v2_profile(tmp_path, historical):
    scope, request, observation, verified = fixture_data(historical=historical)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'materialization.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        run = await seed_historical_run(session, request, observation, verified, "materialization-0001")
        run.state, run.provider_state, run.manifest = "admitted", "not_started", null()
        work = (await session.scalars(select(Work))).one()
        work.kind, work.step = "execute", 0
        run_id, work_id = run.run_id, work.work_id
        await session.delete((await session.scalars(select(WorkerAttempt))).one())
        await session.commit()
    messages = []

    async def publish(envelope, delay):
        messages.append(envelope)

    async def authorize(*args):
        return None

    from l5_services.dispatcher import dispatch_work
    await dispatch_work(sessions, scope["tenant_id"], work_id, publish, now=NOW)
    worker = ExecutionService(sessions, None, ReleaseVerificationAdapter(EvidenceReader(observation)),
                              publish, authorize, clock=lambda: NOW)
    assert await worker.execute(messages[0]) is True
    async with sessions() as session:
        materialized = await session.get(Run, (scope["tenant_id"], run_id))
        manifest = materialized.manifest
    if historical:
        assert set(manifest) == {"schema_version", "scope", "run_id", "configuration_revision",
                                 "binding_generation", "sources", "coverage_mode", "verification", "scenarios"}
    else:
        assert manifest["adapter_revision"] == verified["adapter_revision"]
        assert manifest["test_profile_ref"] == request["devops_input"]["test_profile_ref"]
    await engine.dispose()


@pytest.mark.parametrize("changed_attempt", [False, True])
async def test_historical_verification_cancel_retry_uses_original_attempt(tmp_path, changed_attempt):
    """Cancellation stays unsupported; a retried read must use the SQL attempt."""
    from l5_services.work_store import WorkClaim

    scope, request, observation, verified = fixture_data(historical=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel-retry.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    reader, provider = EvidenceReader(observation), NativeProvider()
    adapter = ReleaseVerificationAdapter(reader)
    async with sessions() as session:
        run = await seed_historical_run(session, request, observation, verified,
                                        "historical-cancel-retry-0001", status="accepted")
        attempt = (await session.scalars(select(WorkerAttempt))).one()
        description = adapter._description(run, attempt)
        provider.task = {"agentSpaceId": "space-one", "taskType": "RELEASE_TESTING", "description": description,
                         "taskId": "task-one", "executionId": "execution-one", "status": "IN_PROGRESS"}
        native = adapter._outcome(run, "space-one", provider.task, description).native_job_ref
        run.native_job_ref = attempt.native_job_ref = native
        run.state, run.provider_state = "cancel_requested", "running"
        work = (await session.scalars(select(Work))).one()
        work.kind = "cancel"
        envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": 1}
        original_id, original_token = attempt.attempt_id, attempt.idempotency_token
        await session.commit()
    authority = []
    current = {"now": NOW}

    async def authorize(run, claim, operation):
        assert isinstance(claim, WorkClaim)
        authority.append((claim.fence, operation))
        return SimpleNamespace(**verified["provider_identity"], client=lambda name: provider)

    async def publish(envelope, delay):
        pass

    class InterruptedCancellation(ReleaseVerificationAdapter):
        async def cancel(self, run, native_job_ref, attempt, provider_session):
            assert attempt.attempt_id == original_id
            assert (await super().cancel(run, native_job_ref, attempt, provider_session)).classification == "unknown"
            raise RuntimeError("offline crash after unsupported cancellation")

    worker = ExecutionService(sessions, None, InterruptedCancellation(reader), publish, authorize,
                              clock=lambda: current["now"])
    try:
        with pytest.raises(RuntimeError, match="crash after unsupported cancellation"):
            await worker.execute(envelope)
        worker.adapter = adapter
        current["now"] += timedelta(seconds=61)
        provider.task["status"] = "CANCELED"
        if changed_attempt:
            changed = json.loads(description)
            changed["attempt_id"] = "foreign-attempt"
            provider.task["description"] = json.dumps(changed, sort_keys=True, separators=(",", ":"))
            with pytest.raises(ValueError, match="native task differs from pinned verification"):
                await worker.execute(envelope)
        else:
            assert await worker.execute(envelope) is True
        async with sessions() as session:
            stored = await session.get(Run, (scope["tenant_id"], run.run_id))
            original = (await session.scalars(select(WorkerAttempt))).one()
            assert stored.state == ("cancel_requested" if changed_attempt else "cancelled")
            assert original.native_job_ref == stored.native_job_ref == native
            assert (original.attempt_id, original.idempotency_token) == (original_id, original_token)
        assert authority == [(1, "cancel"), (2, "observe")]
        assert provider.calls == 0
    finally:
        await engine.dispose()
