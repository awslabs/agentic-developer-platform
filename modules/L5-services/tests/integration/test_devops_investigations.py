"""DevOps submission uses the same committed SQL work and queued worker as Security."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from botocore.exceptions import ReadTimeoutError
from fastapi import FastAPI, HTTPException
from jsonschema import Draft202012Validator
from pathlib import Path
import json
import hmac
from hashlib import sha256
from types import SimpleNamespace

from l5_services.adapters.devops.investigations import InvestigationAdapter
from l5_services.devops.incident_events import receive_incident_event
from l5_services.events.sources import admit_source_receipt
from l5_services.models.event import EventReceipt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.admission import admit_run, submit_and_dispatch, reconcile_and_dispatch
from l5_services.api.router import create_router
from l5_services.models.base import Base
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.receipt import RunReceipt
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.run import Run
from l5_services.models.work import Work
from l5_services.models.evidence import AssessmentResult
from l5_services.findings import ResultStore
from l5_services.run_access import RunStore
from l5_services.workers.execution import AuthorityRevoked, ExecutionService
from l5_services.workers.relay import repair_once

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
SCHEMA = json.loads((Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1/schemas.json").read_text())


async def scoped_test_budget(session, limits, scope, now, replay):
    assert limits["currency"] == "USD" and limits["policy_ref"]["tenant_id"] == scope["tenant_id"]
    assert now == NOW
    return await replay()


async def _investigation_scenario(tmp_path, lost_ack, unresolved, crash=False, finish="collect"):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'devops.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scope = {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one",
             "relationship_revision": "1", "owner_id": "owner-one", "visibility": "only_me"}
    resource = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "aws-one",
                "record_type": "resource", "record_id": "resource-one", "record_revision": "revision-one"}
    request = {"service_id": "devops", "configuration_revision": 1, "binding_id": "binding-one",
               "capability": "investigation", "devops_input": {"kind": "investigation",
               "question": "What changed?", "resource_refs": [resource], "telemetry_refs": []}}
    assert not list(Draft202012Validator({**SCHEMA, "$ref": "#/$defs/RunInput"}).iter_errors(request))
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
    connection_ref = {**resource, "record_type": "connection", "record_id": "aws-one"}
    native_space_ref = {**resource, "record_type": "agent_space", "record_id": "space-one"}
    provider_identity = {"account_id": "123456789012", "region": "us-east-1",
                         "role_arn": "arn:aws:iam::123456789012:role/example",
                         "verified_at": "2026-10-26T09:00:00Z"}
    verified = {"scope": scope, "service_id": "devops", "actor_id": "owner-one", "actor_type": "human",
                "configuration_id": "configuration-one", "configuration_revision": 1,
                "configuration_hash": "f" * 64, "configuration_enabled": True,
                "binding_id": "binding-one", "binding_generation": 2, "binding_state": "ready",
                "connection_ref": connection_ref, "binding_connection_ref": connection_ref,
                "native_space_ref": native_space_ref, "provider_identity": provider_identity,
                "delegation_id": "delegation-one", "delegation_version": 1, "delegation_active": True,
                "profile_ref": {**resource, "record_type": "product", "record_id": "product-one"},
                "trigger": {"trigger_id": "manual-one", "kind": "manual", "enabled": True,
                            "capability": "investigation", "binding_id": "binding-one",
                            "resource_refs": [resource], "telemetry_refs": []},
                "adapter_revision": "devops-v1", "visibility": "only_me", "classification": "internal",
                "causal_origin": "manual", "exclusions": [], "max_bytes": 1024, "max_files": 10,
                "monitoring_policy": {"poll_interval_seconds": 600, "poll_grace_seconds": 300},
                "limits": {"deadline_seconds": 3600, "max_provider_attempts": 1, "max_polls": 10,
                           "concurrency": 1, "max_cost": "1.00", "currency": "USD"}}

    async def no_manual_budget(*args):
        raise AssertionError("A manual investigation must not require or reserve an organization budget")
    messages = []
    fail_next = {"value": False}

    async def publish(envelope, delay):
        if fail_next["value"]:
            fail_next["value"] = False
            raise OSError("synthetic publication outage")
        messages.append((envelope, delay))

    class Backend:
        async def submit(self, product_id, project_id, service_id, run_input, key):
            assert (product_id, project_id, service_id) == ("product-one", "project-one", "devops")
            async with sessions() as session:
                return await submit_and_dispatch(session, sessions, None, publish, run_input, verified, key,
                                                 now=NOW, budget_guard=no_manual_budget)

    app = FastAPI()
    app.include_router(create_router(lambda: Backend()))
    path = "/v1/products/product-one/projects/project-one/services/devops/runs"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.post(path, json=request, headers={"Idempotency-Key": "manual-devops-one-00001"})
        assert response.status_code == 202, response.text
        replay = await client.post(path, json=request, headers={"Idempotency-Key": "manual-devops-one-00001"})
        assert replay.status_code == 202 and replay.json() == response.json()
    async with sessions() as independent:
        run = (await independent.scalars(select(Run))).one()
        receipt = (await independent.scalars(select(RunReceipt))).one()
        work = (await independent.scalars(select(Work))).one()
        assert run.run_id == receipt.run_id == work.run_id == response.json()["run_id"]
        assert run.service_id == "devops" and run.input == request and run.configuration_revision == 1
        assert run.admission["scope"] == scope and run.binding_generation == 2
        assert messages == [({"schema_version": "1.0", "work_id": work.work_id, "generation": 1}, 0)]

    class Adapter:
        async def submit(self, *args):
            raise AssertionError("first work message must only materialize inputs")

    async def authorize(run, claim, action):
        assert (run.run_id, run.input["devops_input"]["resource_refs"]) == (receipt.run_id, [resource])
        return "authorized"

    service = ExecutionService(sessions, None, Adapter(), publish, authorize, clock=lambda: NOW)
    assert await service.execute(messages[0][0]) is True
    async with sessions() as independent:
        stored = await independent.get(Run, ("tenant-one", run.run_id))
        assert stored.manifest["resource_refs"] == [resource]
        assert stored.manifest["scope"] == scope and stored.manifest["coverage_mode"] == "aws_only"
        assert stored.state == "queued" and len(messages) == 2
    class NativeProvider:
        def __init__(self):
            self.tasks = {}
            self.calls = 0
            self.lose_ack = lost_ack
            self.read_calls = 0
            self.cancel_calls = 0

        def get_agent_space(self, *, agentSpaceId):
            return {"agentSpace": {"agentSpaceId": agentSpaceId}}

        def create_backlog_task(self, **request):
            from botocore.session import Session
            from botocore.validate import validate_parameters

            validate_parameters(request, Session().get_service_model("devops-agent").operation_model("CreateBacklogTask").input_shape)
            agentSpaceId, taskType, title, description, clientToken = (
                request[field] for field in ("agentSpaceId", "taskType", "title", "description", "clientToken"))
            assert request["priority"] == "MEDIUM"
            assert agentSpaceId == "space-one" and taskType == "INVESTIGATION"
            assert title == "What changed?" and len(clientToken) == 64
            self.calls += 1
            task = {"agentSpaceId": agentSpaceId, "taskType": taskType,
                    "description": description, "taskId": "task-one", "executionId": "execution-one"}
            self.tasks[clientToken] = task
            if self.lose_ack:
                raise ReadTimeoutError(endpoint_url="https://offline.invalid")
            return {"task": task}

        def get_backlog_task(self, *, agentSpaceId, taskId):
            assert agentSpaceId == "space-one" and taskId == "task-one"
            self.read_calls += 1
            return {"task": next(iter(self.tasks.values()))}

        def update_backlog_task(self, **selection):
            assert selection["agentSpaceId"] == "space-one" and selection["taskId"] == "task-one"
            assert selection["taskStatus"] == "CANCELED" and len(selection["clientToken"]) == 64
            self.cancel_calls += 1
            next(iter(self.tasks.values()))["status"] = "CANCELED"
            if finish == "cancel-lost-ack":
                raise ReadTimeoutError(endpoint_url="https://offline.invalid")
            return {"task": next(iter(self.tasks.values()))}

        def list_backlog_tasks(self, *, agentSpaceId, limit):
            assert agentSpaceId == "space-one" and limit == 100
            return {"tasks": list(self.tasks.values())}

        def list_executions(self, **selection):
            assert selection == {"agentSpaceId": "space-one", "taskId": "task-one", "limit": 100}
            return {"executions": [{"agentSpaceId": "space-one", "executionId": "execution-one",
                                    "executionStatus": "STOPPED", "updatedAt": NOW}]}

        def list_journal_records(self, **selection):
            assert selection == {"agentSpaceId": "space-one", "executionId": "execution-one", "limit": 100}
            return {"records": [{"agentSpaceId": "space-one", "executionId": "execution-one",
                                 "recordId": "record-one", "content": {"summary": "Configuration changed"},
                                 "createdAt": NOW}]}

        def list_recommendations(self, **selection):
            assert selection == {"agentSpaceId": "space-one", "taskId": "task-one", "limit": 100}
            return {"recommendations": [{"recommendationId": "recommendation-one", "version": 1,
                                         "taskId": "task-one"}]}

        def get_recommendation(self, **selection):
            assert selection == {"agentSpaceId": "space-one", "recommendationId": "recommendation-one",
                                 "recommendationVersion": 1}
            return {"recommendation": {"agentSpaceArn": "arn:aws:aidevops:us-east-1:123456789012:agentspace/space-one",
                                       "recommendationId": "recommendation-one", "version": 1,
                                       "taskId": "task-one", "title": "Restore configuration",
                                       "content": {"summary": "Restore the previous timeout"}}}

    provider = NativeProvider()
    provider_session = SimpleNamespace(account_id=provider_identity["account_id"],
                                       region=provider_identity["region"],
                                       role_arn=provider_identity["role_arn"],
                                       client=lambda service_name: provider if service_name == "devops-agent" else None)

    authorization = {"active": True}

    async def provider_authorize(run, claim, action):
        assert run.admission["native_space_ref"] == native_space_ref
        assert run.admission["provider_identity"] == provider_identity
        if not authorization["active"]:
            raise AuthorityRevoked("current membership revoked")
        return provider_session

    class InterruptedAdapter(InvestigationAdapter):
        async def submit(self, *args):
            await super().submit(*args)
            raise RuntimeError("synthetic worker crash after provider acceptance")

    worker = ExecutionService(sessions, None, InterruptedAdapter() if crash else InvestigationAdapter(),
                              publish, provider_authorize,
                              clock=lambda: NOW)
    fail_next["value"] = lost_ack
    if crash:
        with pytest.raises(RuntimeError, match="synthetic worker crash"):
            await worker.execute(messages[1][0])
        async with sessions() as independent:
            interrupted = await independent.get(Run, ("tenant-one", run.run_id))
            original_attempt = (await independent.scalars(select(WorkerAttempt))).one()
            original_work = await independent.get(Work, ("tenant-one", messages[1][0]["work_id"]))
            assert interrupted.state == "queued" and interrupted.native_job_ref is None
            assert original_attempt.status == "prepared" and original_attempt.first_work_id == original_work.work_id
            assert original_work.completed_at is None and original_work.fence == original_attempt.first_fence == 1
            assert provider.calls == len(provider.tasks) == 1 and len(messages) == 2
        recovered_at = NOW + timedelta(seconds=61)
        restarted = ExecutionService(sessions, None, InvestigationAdapter(), publish, provider_authorize,
                                     clock=lambda: recovered_at)
        assert await restarted.execute(messages[1][0]) is True
    else:
        assert await worker.execute(messages[1][0]) is True
    if lost_ack:
        async with sessions() as independent:
            uncertain = await independent.get(Run, ("tenant-one", run.run_id))
            original_attempt = (await independent.scalars(select(WorkerAttempt))).one()
            owed = (await independent.scalars(select(Work).where(Work.completed_at.is_(None)))).one()
            assert (uncertain.state, uncertain.provider_state, uncertain.native_job_ref) == (
                "outcome_unknown", "unknown", None)
            assert original_attempt.status == "outcome_unknown"
            assert (owed.kind, owed.last_error) == ("reconcile", "publication_failed")
            assert provider.calls == 1 and len(messages) == 2
        recovered_at = NOW + timedelta(minutes=10)
        restarted = ExecutionService(sessions, None, InvestigationAdapter(), publish, provider_authorize,
                                     clock=lambda: recovered_at)
        assert await repair_once(restarted, now=recovered_at) == 1
        assert len(messages) == 3 and messages[-1][0]["work_id"] == owed.work_id
        if unresolved:
            next(iter(provider.tasks.values())).pop("executionId")
        assert await restarted.execute(messages[-1][0]) is True
        if unresolved:
            further = ExecutionService(sessions, None, InvestigationAdapter(), publish, provider_authorize,
                                       clock=lambda: recovered_at + timedelta(minutes=10))
            assert await further.execute(messages[-1][0]) is True
            async with sessions() as independent:
                uncertain = await independent.get(Run, ("tenant-one", run.run_id))
                persistent_attempt = (await independent.scalars(select(WorkerAttempt))).one()
                owed = (await independent.scalars(select(Work).where(Work.completed_at.is_(None)))).one()
                assert (uncertain.state, uncertain.provider_state, uncertain.native_job_ref) == (
                    "outcome_unknown", "unknown", None)
                assert (persistent_attempt.attempt_id, persistent_attempt.idempotency_token) == (
                    original_attempt.attempt_id, original_attempt.idempotency_token)
                assert persistent_attempt.status == "outcome_unknown" and persistent_attempt.reconcile_checks == 2
                assert owed.kind == "reconcile" and len(messages) == 5
                assert provider.calls == len(provider.tasks) == 1
            await engine.dispose()
            return
    async with sessions() as independent:
        stored = await independent.get(Run, ("tenant-one", run.run_id))
        assert stored.native_job_ref["record_id"] == "execution-one"
        assert stored.state == "running" and len(messages) == (4 if lost_ack else 3)
        attempt = (await independent.scalars(select(WorkerAttempt))).one()
        assert attempt.status == "accepted"
        if lost_ack:
            assert attempt.attempt_id == original_attempt.attempt_id
            assert attempt.idempotency_token == original_attempt.idempotency_token
            assert attempt.reconcile_checks == 1 and attempt.native_job_ref == stored.native_job_ref
        if crash:
            completed = await independent.get(Work, ("tenant-one", messages[1][0]["work_id"]))
            assert completed.completed_at is not None and completed.fence == 2
            assert attempt.attempt_id == original_attempt.attempt_id
            assert attempt.first_work_id == completed.work_id and attempt.first_fence == 1
            assert attempt.idempotency_token == original_attempt.idempotency_token
            assert attempt.input_hash == original_attempt.input_hash
            assert attempt.reconcile_checks == 1 and attempt.native_job_ref == stored.native_job_ref
        submitted = next(iter(provider.tasks.values()))
        assert stored.native_job_ref["record_revision"] == submitted["taskId"]
        submitted_input = json.loads(submitted["description"])
        assert submitted_input["scope"] == stored.admission["scope"]
        assert submitted_input["resource_refs"] == stored.manifest["resource_refs"]
        assert submitted_input["configuration_revision"] == stored.configuration_revision
        assert submitted_input["binding_generation"] == stored.binding_generation
        assert submitted_input["attempt_id"] == attempt.attempt_id
    assert provider.calls == 1
    assert not list(Draft202012Validator({**SCHEMA, "$ref": "#/$defs/Admission"}).iter_errors(stored.admission))
    provider.tasks[attempt.idempotency_token]["status"] = "IN_PROGRESS"
    observed_at = NOW + timedelta(minutes=25)
    if finish.startswith("reconcile"):
        if finish == "reconcile-late":
            observed_at = NOW + timedelta(hours=2)
        async with sessions() as session:
            recovering = await session.get(Run, (stored.tenant_id, stored.run_id))
            recovering.state = "outcome_unknown"
            await session.commit()
            store = RunStore(session, stored.tenant_id, stored.product_id, stored.project_id, "devops", "owner-one")
            response = await reconcile_and_dispatch(store, sessions, publish, stored.run_id,
                "Collect the original investigation", "recover-investigation-0001", now=observed_at)
            assert not list(Draft202012Validator({**SCHEMA, "$ref": "#/$defs/Run"}).iter_errors(response))
            assert "devops_recovery" not in response.get("monitoring", {})
            message_count = len(messages)
            replay = await reconcile_and_dispatch(store, sessions, publish, stored.run_id,
                "Collect the original investigation", "recover-investigation-0001", now=observed_at)
            assert replay == response and len(messages) == message_count
        async with sessions() as session:
            pending = (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).all()
            assert len(pending) == 1 and pending[0].kind == "poll"
            assert messages[-1][0]["work_id"] == pending[0].work_id
            retained = await session.get(Run, (stored.tenant_id, stored.run_id))
            assert retained.admission == stored.admission and retained.native_job_ref == stored.native_job_ref
            assert retained.deadline_at == stored.deadline_at
    lifecycle = ExecutionService(sessions, None, InvestigationAdapter(clock=lambda: observed_at),
                                 publish, provider_authorize, clock=lambda: observed_at)
    if finish == "denied":
        reads_before = provider.read_calls
        authorization["active"] = False
        assert await lifecycle.execute(messages[-1][0]) is True
        async with sessions() as independent:
            denied = await independent.get(Run, (stored.tenant_id, stored.run_id))
            assert denied.state == "outcome_unknown" and denied.native_job_ref == stored.native_job_ref
            assert denied.manifest == stored.manifest and denied.admission == stored.admission
            assert (await independent.scalars(select(AssessmentResult))).all() == []
        assert provider.read_calls == reads_before and provider.calls == 1 and provider.cancel_calls == 0
        await engine.dispose()
        return
    if finish.startswith("cancel"):
        async with sessions() as session:
            store = RunStore(session, stored.tenant_id, stored.product_id, stored.project_id, "devops", "owner-one")
            cancelled = await store.cancel_queued(stored.run_id, "stop investigation", "cancel-investigation-0001", now=observed_at)
            assert cancelled["state"] == "cancel_requested"
        assert await repair_once(lifecycle, now=observed_at) == 1
        cancel_message = messages[-1][0]
        if finish == "cancel-lost-ack":
            with pytest.raises(ReadTimeoutError):
                await lifecycle.execute(cancel_message)
            assert await lifecycle.execute(cancel_message) is False
            observed_at += timedelta(seconds=61)
        assert await lifecycle.execute(cancel_message) is True
        assert await lifecycle.execute(cancel_message) is False
        async with sessions() as independent:
            cancelled = await independent.get(Run, (stored.tenant_id, stored.run_id))
            assert cancelled.state == "cancelled" and cancelled.provider_state == "stopped"
            assert cancelled.native_job_ref == stored.native_job_ref and cancelled.manifest == stored.manifest
            assert (await independent.scalars(select(WorkerAttempt))).one().attempt_id == attempt.attempt_id
        assert provider.cancel_calls == provider.calls == 1
        await engine.dispose()
        return
    assert await lifecycle.execute(messages[-1][0]) is True
    async with sessions() as independent:
        progressing = await independent.get(Run, (stored.tenant_id, stored.run_id))
        assert progressing.state == ("expired" if finish == "reconcile-late" else "running")
        assert progressing.native_job_ref == stored.native_job_ref
    provider.tasks[attempt.idempotency_token]["status"] = "COMPLETED"
    observed_at += timedelta(minutes=10)
    assert await lifecycle.execute(messages[-1][0]) is True
    collection_message = messages[-1][0]
    assert await lifecycle.execute(collection_message) is True
    assert await lifecycle.execute(collection_message) is False
    async with sessions() as independent:
        completed = await independent.get(Run, (stored.tenant_id, stored.run_id))
        result = (await independent.scalars(select(AssessmentResult))).one()
        assert completed.state == "succeeded" and completed.provider_state == "completed"
        assert completed.native_job_ref == stored.native_job_ref and provider.calls == 1
        assert (await independent.scalars(select(WorkerAttempt))).one().attempt_id == attempt.attempt_id
        store = ResultStore(independent, [completed], service_id="devops")

        class ReadBackend:
            async def result_store(self, product_id, project_id, service_id):
                if (product_id, project_id, service_id) != (completed.product_id, completed.project_id, "devops"):
                    raise HTTPException(404, detail={"code": "not_found"})
                return store

        reader_app = FastAPI()
        reader_app.include_router(create_router(lambda: ReadBackend()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=reader_app), base_url="http://local.test") as reader:
            result_path = path.removesuffix("/runs") + f"/results/{result.result_id}"
            response = await reader.get(result_path)
            assert response.status_code == 200
            detail = response.json()
            assert not list(Draft202012Validator({**SCHEMA, "$ref": "#/$defs/Result"}).iter_errors(detail))
            assert detail["origin"] == "adp_submitted" and detail["finding_count"] == 0
            assert detail["costs"] == [{"kind": "unknown", "currency": "USD"}]
            assert (await reader.get(result_path.replace("product-one", "foreign-product"))).status_code == 404
            assert (await reader.get(result_path.replace("project-one", "foreign-project"))).status_code == 404
        reference, contents = await store.artifact(detail["evidence_ref"]["artifact_id"])
        evidence = json.loads(contents)
        assert sha256(contents).hexdigest() == reference["sha256"]
        assert evidence["execution"]["executionId"] == "execution-one"
        assert evidence["journal_records"][0]["content"]["summary"] == "Configuration changed"
        assert evidence["operational_recommendations"][0]["title"] == "Restore configuration"
        assert evidence["usage_coverage"]["status"] == "unavailable"
        assert await store.findings(result.result_id) == []
        with pytest.raises(HTTPException) as denied:
            await ResultStore(independent, [], service_id="devops").artifact(reference["artifact_id"])
        assert denied.value.status_code == 404
    adapter = InvestigationAdapter()
    assert (await adapter.reconcile_submit(stored, attempt, provider_session)).native_job_ref == stored.native_job_ref
    identity = {"run_id": attempt.run_id, "input_hash": attempt.input_hash,
                "idempotency_token": attempt.idempotency_token, "attempt_id": attempt.attempt_id}
    for field, invalid_value in (("run_id", "foreign-run"), ("input_hash", "0" * 64),
                                 ("idempotency_token", "0" * 64)):
        forged_attempt = SimpleNamespace(**{**identity, field: invalid_value})
        with pytest.raises(ValueError, match="attempt does not match pinned provider scope"):
            await adapter.reconcile_submit(stored, forged_attempt, provider_session)
    assert provider.calls == 1
    provider.tasks[attempt.idempotency_token].pop("executionId")
    assert (await adapter.reconcile_submit(stored, attempt, provider_session)).classification == "unknown"
    provider.tasks[attempt.idempotency_token]["executionId"] = "execution-one"
    provider.tasks[attempt.idempotency_token]["description"] = "another project's investigation"
    assert (await adapter.reconcile_submit(stored, attempt, provider_session)).classification == "unknown"
    assert provider.calls == 1

    wrong_session = SimpleNamespace(**{**vars(provider_session), "role_arn": "arn:aws:iam::123456789012:role/other"})
    with pytest.raises(ValueError, match="pinned provider scope"):
        await InvestigationAdapter().submit(stored, stored.manifest, attempt, wrong_session)
    assert provider.calls == 1
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_ack, unresolved", [(False, False), (True, False), (True, True)],
                         ids=["normal", "lost-ack", "unresolved"])
async def test_immediate_admission(tmp_path, lost_ack, unresolved):
    await _investigation_scenario(tmp_path, lost_ack, unresolved)


@pytest.mark.asyncio
async def test_submission_recovery(tmp_path):
    await _investigation_scenario(tmp_path, False, False, crash=True)


@pytest.mark.parametrize("finish", ["cancel", "cancel-lost-ack", "denied"])
async def test_durable_investigation_cancellation_and_revocation(tmp_path, finish):
    await _investigation_scenario(tmp_path, False, False, finish=finish)


@pytest.mark.parametrize("finish", ["reconcile", "reconcile-late"])
async def test_known_investigation_recovers_without_another_submission(tmp_path, finish):
    await _investigation_scenario(tmp_path, False, False, finish=finish)


@pytest.mark.asyncio
async def test_immediate_incident_admission(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'incident.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scope = {"tenant_id": "tenant-incident", "product_id": "product-incident",
             "project_id": "project-incident", "relationship_revision": "2",
             "owner_id": "owner-incident", "visibility": "only_me"}
    reference = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                 "source_connection_id": "cloudwatch-incident", "record_type": "resource",
                 "record_id": "resource-incident", "record_revision": "resource-v1"}
    source = {**reference, "record_type": "incident", "record_id": "source-incident"}
    trigger = {"trigger_id": "incident-trigger", "kind": "native_event", "enabled": True,
               "capability": "investigation", "binding_id": "binding-incident",
               "resource_refs": [reference], "telemetry_refs": [],
               "severities": ["high"], "environment_refs": [reference], "maintenance_windows": []}
    settings = {"enabled": True, "devops": {"incident_sources": [{
        "source_id": "source-incident", "secret_ref": "secret-reference-incident",
        "source_ref": source, "environment_ref": reference, "trigger_id": "incident-trigger",
    }], "trigger_policies": [trigger]}}
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(Configuration(tenant_id=scope["tenant_id"], configuration_id="config-incident",
                                  product_id=scope["product_id"], project_id=scope["project_id"],
                                  service_id="devops", relationship_revision="2", owner_id="owner-incident",
                                  current_revision=1, handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id=scope["tenant_id"], configuration_id="config-incident",
                                         revision=1, settings=settings, inherited_fields=[], gaps=[],
                                         state="ready", hash="a" * 64))
        await session.commit()

    event = {"event_id": "incident-event-1", "environment_ref": reference,
             "question": "Which service is unhealthy?", "severity": "high"}
    body = json.dumps(event, sort_keys=True).encode()
    signature = "sha256=" + hmac.new(b"test-only-secret", body, sha256).hexdigest()
    secret_refs = []

    async def load_secret(secret_ref):
        secret_refs.append(secret_ref)
        assert secret_ref == "secret-reference-incident"
        return "test-only-secret"

    messages = []

    async def publish(envelope, delay):
        messages.append((envelope, delay))

    async def verify_target(receipt):
        assert (receipt.tenant_id, receipt.product_id, receipt.project_id) == (
            "tenant-incident", "product-incident", "project-incident")
        assert receipt.event_id == event["event_id"]
        connection_ref = {**reference, "record_type": "connection", "record_id": "cloudwatch-incident"}
        return ({"scope": scope, "service_id": "devops", "actor_id": "service-incident",
                 "actor_type": "service", "configuration_id": "config-incident",
                 "configuration_revision": 1, "configuration_hash": "a" * 64,
                 "configuration_enabled": True, "event_id": event["event_id"],
                 "event_source_ref": receipt.source_ref, "event_kind": receipt.event_kind,
                 "binding_id": "binding-incident", "binding_generation": 3,
                 "binding_state": "ready", "connection_ref": connection_ref,
                 "binding_connection_ref": connection_ref,
                 "native_space_ref": {**reference, "record_type": "agent_space", "record_id": "space-incident"},
                 "provider_identity": {"account_id": "123456789012", "region": "us-east-1",
                                       "role_arn": "arn:aws:iam::123456789012:role/incident",
                                       "verified_at": "2026-10-26T09:00:00Z"},
                 "delegation_id": "delegation-incident", "delegation_version": 1,
                 "delegation_active": True, "profile_ref": {**reference, "record_type": "product"},
                 "trigger": trigger, "adapter_revision": "devops-v1", "visibility": "only_me",
                 "classification": "internal", "causal_origin": "incident",
                 "exclusions": [], "max_bytes": 1024, "max_files": 10,
                 "monitoring_policy": {"poll_interval_seconds": 600, "poll_grace_seconds": 300},
                 "limits": {"deadline_seconds": 3600, "max_provider_attempts": 1, "max_polls": 10,
                            "concurrency": 1, "max_cost": "1.00", "currency": "USD",
                            "policy_ref": {**reference, "record_type": "policy"}}}, None)

    async with sessions() as session:
        with pytest.raises(Exception) as invalid:
            await receive_incident_event(session, source_id="source-incident", payload=body,
                                         signature="sha256=" + "0" * 64, load_secret=load_secret, now=NOW)
        assert invalid.value.status_code == 401
        receipt = await receive_incident_event(session, source_id="source-incident", payload=body,
                                               signature=signature, load_secret=load_secret, now=NOW)
        assert receipt.state == "pending" and receipt.run_input["binding_id"] == "binding-incident"
        run = await admit_source_receipt(session, sessions, receipt, verify_target, publish,
                                         now=NOW, budget_guard=scoped_test_budget)
    async with sessions() as independent:
        stored = (await independent.scalars(select(EventReceipt))).one()
        persisted = (await independent.scalars(select(Run))).one()
        owed = (await independent.scalars(select(Work))).one()
        assert (stored.run_id, persisted.run_id, owed.run_id) == (run.run_id,) * 3
        assert persisted.admission["event_id"] == event["event_id"]
        assert persisted.admission["event_source_ref"] == stored.source_ref == source
        assert persisted.admission["event_kind"] == stored.event_kind == "native_event"
        assert persisted.input["devops_input"]["resource_refs"] == [reference]
        assert persisted.admission["scope"] == scope and persisted.binding_generation == 3
        assert messages == [({"schema_version": "1.0", "work_id": owed.work_id, "generation": 1}, 0)]
    async with sessions() as session:
        version = await session.get(ConfigurationVersion, (scope["tenant_id"], "config-incident", 1))
        version.settings = {**settings, "devops": {**settings["devops"], "paused": True}}
        await session.commit()
        duplicate = await receive_incident_event(session, source_id="source-incident", payload=body,
                                                 signature=signature, load_secret=load_secret, now=NOW)
        assert duplicate.run_id == run.run_id

        async def unexpected_authority(*args):
            raise AssertionError("admitted event replay must not request new work")

        repeated = await admit_source_receipt(session, sessions, duplicate, unexpected_authority, publish,
                                              now=NOW, budget_guard=unexpected_authority)
        assert repeated.run_id == run.run_id
        altered_body = json.dumps({**event, "question": "Another investigation"}, sort_keys=True).encode()
        altered_signature = "sha256=" + hmac.new(b"test-only-secret", altered_body, sha256).hexdigest()
        with pytest.raises(HTTPException) as conflict:
            await receive_incident_event(session, source_id="source-incident", payload=altered_body,
                                         signature=altered_signature, load_secret=load_secret, now=NOW)
        assert conflict.value.status_code == 409
        assert conflict.value.detail == {"code": "idempotency_conflict"}
    async with sessions() as independent:
        assert [item.run_id for item in (await independent.scalars(select(Run))).all()] == [run.run_id]
        assert [item.run_id for item in (await independent.scalars(select(Work))).all()] == [run.run_id]
        assert len(messages) == 1
    assert secret_refs
    await engine.dispose()


def test_native_investigation_sdk_idempotency_contract():
    from botocore.session import Session

    model = Session().get_service_model("devops-agent")
    operation = model.operation_model("CreateBacklogTask")
    assert operation.input_shape.members["clientToken"].metadata["idempotencyToken"] is True
    assert "INVESTIGATION" in operation.input_shape.members["taskType"].metadata["enum"]
    assert {"taskId", "executionId", "description", "agentSpaceId"} <= set(
        model.operation_model("GetBacklogTask").output_shape.members["task"].members)
