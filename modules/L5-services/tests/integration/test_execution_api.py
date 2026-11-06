"""Isolated two-Product execution API scenarios with independent SQL readback."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.admission import cancel_and_dispatch, reconcile_and_dispatch, retry_and_dispatch, submit_and_dispatch
from l5_services.api.router import create_router
from l5_services.models.base import Base
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.preparation import AdmissionPreparation
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run, view_run
from l5_services.models.work import Work
from l5_services.run_access import RunStore

FIXED_TIME = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
SCHEMA = json.loads((Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1/schemas.json").read_text())


@pytest.fixture
async def execution_scenario(tmp_path):
    database = tmp_path / "runs.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")

    @event.listens_for(engine.sync_engine, "connect")
    def enforce_foreign_keys(connection, connection_record):
        connection.execute("PRAGMA foreign_keys=ON")

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    products = {}
    for suffix in ("a", "b"):
        tenant_id = f"tenant-{suffix}"
        project_id = f"project-{suffix}"
        sources = [
            {"tenant_id": tenant_id, "project_id": project_id, "source_connection_id": f"connector-{suffix}",
             "record_type": "document", "record_id": f"document-{index}", "record_revision": f"revision-{index}"}
            for index in (1, 2)
        ]
        products[tenant_id] = {"tenant_id": tenant_id, "product_id": f"product-{suffix}",
                               "project_id": project_id, "configuration_id": str(uuid4()), "sources": sources}
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        for fixture in products.values():
            session.add(Configuration(
                tenant_id=fixture["tenant_id"], configuration_id=fixture["configuration_id"],
                product_id=fixture["product_id"], project_id=fixture["project_id"], service_id="security",
                relationship_revision="1", owner_id=f"owner-{fixture['tenant_id']}",
                current_revision=1, handoff_state="linked",
            ))
            session.add(ConfigurationVersion(
                tenant_id=fixture["tenant_id"], configuration_id=fixture["configuration_id"],
                revision=1, settings={"enabled": True}, inherited_fields=[], gaps=[], state="ready",
                hash="f" * 64,
            ))
        await session.commit()

    current = {"tenant_id": "tenant-a", "actor_id": None, "trigger_enabled": True}
    faults = {"queue": False}
    captures = []
    delivered = []

    class Capturer:
        async def pin_source(self, product_id, project_id, request, *, idempotency_key):
            captures.append((product_id, project_id, request, idempotency_key))
            return {
                "snapshot_id": f"snapshot-{len(captures)}", "source_ref": request["source_ref"],
                "content": {"artifact_id": f"content-{len(captures)}", "version": "v1",
                            "sha256": "a" * 64, "media_type": "application/octet-stream", "bytes": 7},
                "file_manifest": {"artifact_id": f"manifest-{len(captures)}", "version": "v1",
                                  "sha256": "b" * 64, "media_type": "application/json", "bytes": 15},
                "file_count": 1, "exclusions": request["exclusions"], "expires_at": request["retain_until"],
            }

    async def publish(envelope, delay):
        delivered.append((envelope, delay))
        if faults["queue"]:
            raise ConnectionError("injected queue failure")

    class Backend:
        def scoped_store(self, product_id, project_id, service_id, session):
            fixture = products[current["tenant_id"]]
            if (product_id, project_id, service_id) != (fixture["product_id"], fixture["project_id"], "security"):
                raise HTTPException(404, detail={"code": "not_found"})
            return RunStore(session, fixture["tenant_id"], product_id, project_id, service_id,
                            current["actor_id"] or f"owner-{fixture['tenant_id']}", "1")

        async def get_run(self, product_id, project_id, service_id, run_id):
            async with sessions() as session:
                return await self.scoped_store(product_id, project_id, service_id, session).get(run_id)

        async def list_runs(self, product_id, project_id, service_id, cursor):
            async with sessions() as session:
                return await self.scoped_store(product_id, project_id, service_id, session).list(cursor=cursor)

        async def cancel_run(self, product_id, project_id, service_id, run_id, reason, idempotency_key):
            async with sessions() as session:
                return await cancel_and_dispatch(
                    self.scoped_store(product_id, project_id, service_id, session), sessions,
                    publish, run_id, reason, idempotency_key, now=FIXED_TIME,
                )

        async def reconcile_run(self, product_id, project_id, service_id, run_id, reason, idempotency_key):
            async with sessions() as session:
                store = self.scoped_store(product_id, project_id, service_id, session)
                return await reconcile_and_dispatch(store, sessions, publish, run_id, reason,
                                                    idempotency_key, now=FIXED_TIME)

        async def retry_run(self, product_id, project_id, service_id, run_id, reason, idempotency_key):
            async with sessions() as session:
                await self.scoped_store(product_id, project_id, service_id, session).owned(run_id)
                return await retry_and_dispatch(session, sessions, Capturer(), publish,
                    self.authority(product_id, project_id, service_id), run_id, reason, idempotency_key,
                    now=FIXED_TIME)

        def authority(self, product_id, project_id, service_id):
            fixture = products[current["tenant_id"]]
            if (product_id, project_id, service_id) != (fixture["product_id"], fixture["project_id"], "security"):
                raise HTTPException(404, detail={"code": "not_found"})
            source = fixture["sources"][0]
            connection = {**source, "record_type": "connection", "record_id": source["source_connection_id"],
                          "record_revision": "1"}
            space = {**connection, "record_type": "security_space", "record_id": f"space-{fixture['tenant_id']}"}
            return {
                "scope": {"tenant_id": fixture["tenant_id"], "product_id": product_id,
                          "project_id": project_id, "relationship_revision": "1",
                          "owner_id": f"owner-{fixture['tenant_id']}", "visibility": "only_me"},
                "service_id": "security", "actor_id": f"owner-{fixture['tenant_id']}", "actor_type": "human",
                "configuration_id": fixture["configuration_id"], "configuration_revision": 1,
                "configuration_hash": "f" * 64, "configuration_enabled": True,
                "profile_ref": {**source, "source_connection_id": "adp.profile", "record_type": "product",
                                "record_id": product_id, "record_revision": "1"},
                "connection_ref": connection, "binding_connection_ref": connection,
                "native_space_ref": space,
                "native_model_ref": {**space, "record_type": "threat_model",
                                     "record_id": f"model-{fixture['tenant_id']}"},
                "provider_identity": {"account_id": "123456789012", "region": "us-east-1",
                                      "role_arn": "arn:aws:iam::123456789012:role/TestSelected",
                                      "verified_at": FIXED_TIME.isoformat().replace("+00:00", "Z")},
                "source_registration": {"service_role": "arn:aws:iam::123456789012:role/TestSecurity",
                                        "bucket": "example-source-staging"},
                "binding_id": f"binding-{fixture['tenant_id']}", "binding_generation": 2,
                "binding_state": "ready", "delegation_id": f"delegation-{fixture['tenant_id']}",
                "delegation_version": 1, "delegation_active": True,
                "trigger": {"trigger_id": f"manual-{fixture['tenant_id']}", "kind": "manual",
                            "enabled": current["trigger_enabled"], "capability": "threat_model",
                            "binding_id": f"binding-{fixture['tenant_id']}",
                            "source_refs": fixture["sources"]},
                "adapter_revision": "adapter-v1",
                "limits": {"deadline_seconds": 3600, "max_provider_attempts": 1, "max_polls": 10,
                           "concurrency": 1, "max_cost": "1.00", "currency": "USD",
                           "policy_ref": {**source, "record_type": "policy", "record_id": "policy-a",
                                          "record_revision": "1"}},
                "visibility": "only_me", "classification": "internal", "causal_origin": "manual",
                "exclusions": ["generated"], "max_bytes": 1024, "max_files": 10,
                "monitoring_policy": {"poll_interval_seconds": 600, "poll_grace_seconds": 300},
            }
        async def submit(self, product_id, project_id, service_id, run_input, idempotency_key):
            verified = self.authority(product_id, project_id, service_id)
            async with sessions() as session:
                return await submit_and_dispatch(session, sessions, Capturer(), publish,
                                                 run_input, verified, idempotency_key, now=FIXED_TIME)

    backend = Backend()
    app = FastAPI()
    app.include_router(create_router(lambda: backend))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        yield {"client": client, "sessions": sessions, "engine": engine, "products": products,
               "current": current, "captures": captures, "delivered": delivered, "faults": faults,
               "publish": publish, "backend": backend}
    await engine.dispose()
    database.unlink()


def run_request(fixture):
    return {"configuration_revision": 1, "binding_id": f"binding-{fixture['tenant_id']}",
            "capability": "threat_model", "sources": fixture["sources"]}


def run_path(fixture):
    return f"/v1/products/{fixture['product_id']}/projects/{fixture['project_id']}/services/security/runs"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "provider_attempt", "expired", "pending"])
async def test_reconcile_pre_submission_block_preserves_run_and_authority(execution_scenario, failure):
    from l5_services.models.attempt import WorkerAttempt

    scenario = execution_scenario
    fixture = scenario["products"]["tenant-a"]
    path = run_path(fixture)
    original = (await scenario["client"].post(path, json=run_request(fixture),
                headers={"Idempotency-Key": "pre-submit-blocked-run-01"})).json()
    run_id = original["run_id"]
    async with scenario["sessions"]() as session:
        run = await session.get(Run, (fixture["tenant_id"], run_id))
        run.state, run.provider_state = "blocked", "unknown"
        work = await session.scalar(select(Work).where(Work.run_id == run_id))
        if failure != "pending":
            work.completed_at = FIXED_TIME
        if failure == "expired":
            run.deadline_at = FIXED_TIME - timedelta(seconds=1)
        if failure == "provider_attempt":
            session.add(WorkerAttempt(
                tenant_id=run.tenant_id, attempt_id=str(uuid4()), run_id=run_id,
                operation="submit", first_work_id=work.work_id, first_fence=1,
                idempotency_token="a" * 64, input_hash="b" * 64,
                started_at=FIXED_TIME, status="outcome_unknown",
            ))
        await session.commit()
    body, headers = {"reason": "Recover corrected source authorization"}, {"Idempotency-Key": "recover-before-provider-01"}
    response = await scenario["client"].post(f"{path}/{run_id}/reconcile", json=body, headers=headers)
    assert response.status_code == (409 if failure == "pending" else 202), response.text
    if failure == "pending":
        return
    replay = await scenario["client"].post(f"{path}/{run_id}/reconcile", json=body, headers=headers)
    assert replay.json() == response.json()
    assert response.json()["input"] == original["input"]
    assert response.json()["admission"] == original["admission"]
    async with scenario["sessions"]() as session:
        assert len((await session.scalars(select(Run))).all()) == 1
        owed = await session.scalar(select(Work).where(Work.run_id == run_id, Work.completed_at.is_(None)))
        assert owed.kind == ("execute" if failure is None else "reconcile")
        assert response.json()["state"] == ("admitted" if failure is None else "blocked")
        assert scenario["delivered"][-1][0]["work_id"] == owed.work_id


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [{}, {"service_id": "security"}], ids=["omitted", "explicit-security"])
async def test_security_selector_contract_compatibility(execution_scenario, selector):
    scenario = execution_scenario
    fixture = scenario["products"]["tenant-a"]
    request = {**run_request(fixture), **selector}
    Draft202012Validator({**SCHEMA, "$ref": "#/$defs/RunInput"}).validate(request)
    headers = {"Idempotency-Key": "security-selector-compatibility-01"}
    first = await scenario["client"].post(run_path(fixture), json=request, headers=headers)
    assert first.status_code == 202, first.text
    replay = await scenario["client"].post(run_path(fixture), json=request, headers=headers)
    assert replay.status_code == 202 and replay.json() == first.json()
    Draft202012Validator({**SCHEMA, "$ref": "#/$defs/Run"}).validate(first.json())
    async with scenario["sessions"]() as independent:
        run = (await independent.scalars(select(Run))).one()
        receipt = (await independent.scalars(select(RunReceipt))).one()
        work = (await independent.scalars(select(Work))).one()
        assert run.run_id == receipt.run_id == work.run_id == first.json()["run_id"]
        assert run.service_id == "security" and run.input == request
    assert len(scenario["captures"]) == 2 and len(scenario["delivered"]) == 1


@pytest.mark.asyncio
async def test_security_route_rejects_valid_devops_input(execution_scenario):
    scenario = execution_scenario
    fixture = scenario["products"]["tenant-a"]
    request = {
        "configuration_revision": 1, "binding_id": f"binding-{fixture['tenant_id']}",
        "service_id": "devops", "capability": "investigation",
        "devops_input": {
            "kind": "investigation", "question": "What changed?",
            "resource_refs": [{**fixture["sources"][0], "record_type": "resource"}],
            "telemetry_refs": [{**fixture["sources"][0], "record_type": "telemetry"}],
        },
    }
    Draft202012Validator({**SCHEMA, "$ref": "#/$defs/RunInput"}).validate(request)
    response = await scenario["client"].post(run_path(fixture), json=request,
                                            headers={"Idempotency-Key": "devops-on-security-route-01"})
    assert response.status_code == 422
    assert scenario["captures"] == [] and scenario["delivered"] == []
    async with scenario["sessions"]() as independent:
        for model in (Run, RunReceipt, Work, AdmissionPreparation):
            assert (await independent.scalars(select(model))).all() == []


@pytest.mark.asyncio
async def test_submit_replay(execution_scenario):
    scenario = execution_scenario
    first_fixture = scenario["products"]["tenant-a"]
    second_fixture = scenario["products"]["tenant-b"]
    key = "scenario_submit_replay_00001"
    client = scenario["client"]
    first = await client.post(run_path(first_fixture), json=run_request(first_fixture),
                              headers={"Idempotency-Key": key})
    replay = await client.post(run_path(first_fixture), json=run_request(first_fixture),
                               headers={"Idempotency-Key": key})
    changed = await client.post(run_path(first_fixture), json={**run_request(first_fixture), "configuration_revision": 2},
                                headers={"Idempotency-Key": key})
    assert (first.status_code, replay.status_code, changed.status_code) == (202, 202, 409)
    validator = Draft202012Validator({**SCHEMA, "$ref": "#/$defs/Run"}, format_checker=FormatChecker())
    assert not list(validator.iter_errors(first.json()))
    assert replay.json() == first.json()
    assert [entry["source_ref"] for entry in first.json()["admission"]["source_snapshots"]] == first_fixture["sources"]
    assert len(scenario["captures"]) == 2 and len(scenario["delivered"]) == 1

    scenario["current"]["tenant_id"] = "tenant-b"
    foreign = await client.post(run_path(first_fixture), json=run_request(first_fixture),
                                headers={"Idempotency-Key": key})
    second = await client.post(run_path(second_fixture), json=run_request(second_fixture),
                               headers={"Idempotency-Key": key})
    assert foreign.status_code == 404 and second.status_code == 202
    assert second.json()["run_id"] != first.json()["run_id"]
    async with scenario["sessions"]() as independent:
        for fixture, response in ((first_fixture, first), (second_fixture, second)):
            tenant = fixture["tenant_id"]
            runs = (await independent.scalars(select(Run).where(Run.tenant_id == tenant))).all()
            receipts = (await independent.scalars(select(RunReceipt).where(RunReceipt.tenant_id == tenant))).all()
            work = (await independent.scalars(select(Work).where(Work.tenant_id == tenant))).all()
            assert len(runs) == len(receipts) == len(work) == 1
            assert runs[0].run_id == receipts[0].run_id == work[0].run_id == response.json()["run_id"]
            assert runs[0].trigger_id == f"manual-{tenant}"
            assert runs[0].admission["source_snapshots"] == response.json()["admission"]["source_snapshots"]
            assert work[0].attempts == 1 and work[0].completed_at is None
    assert len(scenario["captures"]) == 4 and len(scenario["delivered"]) == 2


@pytest.mark.asyncio
async def test_cancellation_receipt_binds_target_and_reason(execution_scenario):
    scenario = execution_scenario
    fixture = scenario["products"]["tenant-a"]
    client, path = scenario["client"], run_path(fixture)
    runs = [(await client.post(path, json=run_request(fixture),
             headers={"Idempotency-Key": f"cancel-fixture-{index:04}"})).json() for index in (1, 2)]
    url = f"{path}/{runs[0]['run_id']}/cancel"
    headers = {"Idempotency-Key": "cancel-receipt-key-01"}
    first = await client.post(url, json={"reason": "stop"}, headers=headers)
    replay = await client.post(url, json={"reason": "stop"}, headers=headers)
    changed = await client.post(url, json={"reason": "changed"}, headers=headers)
    other = await client.post(f"{path}/{runs[1]['run_id']}/cancel", json={"reason": "stop"}, headers=headers)
    assert (first.status_code, replay.status_code, changed.status_code, other.status_code) == (202, 202, 409, 409)
    assert replay.json() == first.json() and replay.headers["etag"] == first.headers["etag"]
    async with scenario["sessions"]() as independent:
        receipts = (await independent.scalars(select(RunReceipt).where(RunReceipt.operation == "cancel"))).all()
        assert len(receipts) == 1 and receipts[0].response == first.json()
        assert receipts[0].request == {"run_id": runs[0]["run_id"], "reason": "stop"}
        untouched = await independent.get(Run, (fixture["tenant_id"], runs[1]["run_id"]))
        work = (await independent.scalars(select(Work).where(Work.run_id == untouched.run_id))).one()
        assert untouched.state == "admitted" and work.completed_at is None and work.fence == 0
    scenario["current"]["actor_id"] = "not-the-owner"
    assert (await client.post(url, json={"reason": "stop"}, headers=headers)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"configuration_revision": True}, {"unexpected": "value"}, {"parent_task_ref": {}},
    {"parent_task_ref": None}, {"pr_head": "revision-2"}, {"sources": []},
    {"service_id": None}, {"service_id": "devops"}, {"service_id": "unknown"},
    {"service_id": True}, {"service_id": 1}, {"service_id": {}}, {"service_id": []},
    {"service_id": "security", "devops_input": {}}, {"service_id": "security", "unexpected": "value"},
])
async def test_invalid_input_has_no_capture_or_storage(execution_scenario, invalid):
    scenario = execution_scenario
    fixture = scenario["products"]["tenant-a"]
    response = await scenario["client"].post(run_path(fixture), json={**run_request(fixture), **invalid},
                                            headers={"Idempotency-Key": "invalid-contract-input-01"})
    assert response.status_code == 422
    assert scenario["captures"] == [] and scenario["delivered"] == []
    async with scenario["sessions"]() as independent:
        for model in (Run, RunReceipt, Work, AdmissionPreparation):
            assert (await independent.scalars(select(model))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_state,kind", [("running", "poll"), ("completed", "collect")])
@pytest.mark.parametrize("leased", [False, True])
@pytest.mark.parametrize("expired", [False, True])
async def test_security_reconcile_recovers_existing_job_without_resubmission(execution_scenario, provider_state, kind, leased, expired):
    scenario = execution_scenario
    fixture = scenario["products"]["tenant-a"]
    client, path = scenario["client"], run_path(fixture)
    original = (await client.post(path, json=run_request(fixture),
                                 headers={"Idempotency-Key": "native-recovery-fixture-01"})).json()
    run_id = original["run_id"]
    native = {"record_id": "existing-job", "record_revision": "existing-review"}
    async with scenario["sessions"]() as session:
        run = await session.get(Run, (fixture["tenant_id"], run_id))
        run.state = "collecting" if provider_state == "completed" else "running"
        run.provider_state, run.native_job_ref = provider_state, native
        if expired:
            run.state = "expired"
            run.deadline_at = FIXED_TIME - timedelta(hours=2)
        await session.execute(update(Work).where(Work.run_id == run_id).values(
            kind=kind, lease_owner="active-worker" if leased else None,
            lease_until=FIXED_TIME + timedelta(minutes=1) if leased else None))
        await session.commit()
    headers = {"Idempotency-Key": "native-recovery-operation-01"}
    response = await client.post(f"{path}/{run_id}/reconcile", json={"reason": "recover dropped observation"}, headers=headers)
    assert response.status_code == (409 if leased else 202)
    async with scenario["sessions"]() as session:
        pending = (await session.scalars(select(Work).where(Work.run_id == run_id, Work.completed_at.is_(None)))).all()
        assert len(pending) == 1 and pending[0].kind == kind
        assert pending[0].lease_owner == ("active-worker" if leased else None)
        run = await session.get(Run, (fixture["tenant_id"], run_id))
        assert run.native_job_ref == native and run.input == original["input"]
        if expired and not leased:
            assert run.monitoring["state"] == "recovery_pending"
            assert run.monitoring["security_recovery"]["native_job_ref"] == native
            assert run.deadline_at.replace(tzinfo=UTC) == FIXED_TIME - timedelta(hours=2)
        assert {**run.admission, "configuration_id": run.configuration_id,
                "configuration_revision": run.configuration_revision} == original["admission"]
    if not leased:
        replay = await client.post(f"{path}/{run_id}/reconcile", json={"reason": "recover dropped observation"}, headers=headers)
        assert replay.json() == response.json()


@pytest.mark.asyncio
async def test_reconcile_preserves_pins_and_recovers_publication(execution_scenario):
    from l5_services.dispatcher import repair_owed_work

    scenario = execution_scenario
    fixture = scenario["products"]["tenant-a"]
    client, path = scenario["client"], run_path(fixture)
    original = (await client.post(path, json=run_request(fixture),
                                headers={"Idempotency-Key": "reconcile-fixture-01"})).json()
    run_id = original["run_id"]
    async with scenario["sessions"]() as independent:
        await independent.execute(update(Run).where(Run.run_id == run_id).values(
            state="outcome_unknown", provider_state="unknown"))
        await independent.execute(update(Work).where(Work.run_id == run_id).values(completed_at=FIXED_TIME))
        await independent.commit()
    headers = {"Idempotency-Key": "reconcile-operation-01"}
    url = f"{path}/{run_id}/reconcile"
    scenario["faults"]["queue"] = True
    first = await client.post(url, json={"reason": "read back native state"}, headers=headers)
    assert first.status_code == 202
    replay = await client.post(url, json={"reason": "read back native state"}, headers=headers)
    assert replay.status_code == 202 and replay.json() == first.json()
    assert (await client.post(url, json={"reason": "changed"}, headers=headers)).status_code == 409
    assert first.json()["run_id"] == run_id and first.json()["state"] == "outcome_unknown"
    assert first.json()["admission"] == original["admission"] and first.json()["input"] == original["input"]
    async with scenario["sessions"]() as independent:
        assert len((await independent.scalars(select(Run))).all()) == 1
        owed = (await independent.scalars(select(Work).where(Work.kind == "reconcile"))).one()
        assert owed.run_id == run_id and owed.completed_at is None and owed.last_error == "publication_failed"
        receipt = (await independent.scalars(select(RunReceipt).where(RunReceipt.operation == "reconcile"))).one()
        assert receipt.response == first.json()
        work_id = owed.work_id
    scenario["faults"]["queue"] = False
    assert await repair_owed_work(scenario["sessions"], scenario["publish"], now=FIXED_TIME) == 1
    assert scenario["delivered"][-1][0]["work_id"] == work_id
    assert len(scenario["captures"]) == 2
    async with scenario["sessions"]() as independent:
        await independent.execute(update(Run).where(Run.run_id == run_id).values(revision=3, state="failed"))
        await independent.commit()
    replay = await client.post(url, json={"reason": "read back native state"}, headers=headers)
    assert replay.json() == first.json() and replay.headers["etag"] == first.headers["etag"]


def no_effect_proof(fixture):
    return {"classification": "definitive_no_effect", "evidence_ref": {
        **fixture["sources"][0], "record_type": "provider_observation", "record_id": "no-effect-readback",
    }}


@pytest.mark.asyncio
async def test_retry_requires_proof_and_creates_one_linked_admission(execution_scenario):
    scenario = execution_scenario
    fixture = scenario["products"]["tenant-a"]
    client, path = scenario["client"], run_path(fixture)
    original = (await client.post(path, json=run_request(fixture),
                                headers={"Idempotency-Key": "retry-fixture-0001"})).json()
    run_id = original["run_id"]
    url, headers = f"{path}/{run_id}/retry", {"Idempotency-Key": "retry-operation-0001"}
    for state, provider_state, proof in (
        ("failed", "failed", None), ("failed", "failed", {}),
        ("outcome_unknown", "unknown", no_effect_proof(fixture)),
    ):
        async with scenario["sessions"]() as independent:
            await independent.execute(update(Run).where(Run.run_id == run_id).values(
                state=state, provider_state=provider_state, no_effect_proof=proof))
            await independent.commit()
        assert (await client.post(url, json={"reason": "try again"}, headers=headers)).status_code == 409
    assert len(scenario["captures"]) == 2
    async with scenario["sessions"]() as independent:
        await independent.execute(update(Run).where(Run.run_id == run_id).values(
            state="failed", provider_state="failed", no_effect_proof=no_effect_proof(fixture)))
        await independent.commit()
    assert (await client.post(url, json={"reason": "try again"}, headers=headers)).status_code == 409
    async with scenario["sessions"]() as independent:
        await independent.execute(update(Work).where(Work.run_id == run_id).values(completed_at=FIXED_TIME))
        await independent.commit()
    scenario["current"]["trigger_enabled"] = False
    assert (await client.post(url, json={"reason": "try again"}, headers=headers)).status_code == 409
    assert len(scenario["captures"]) == 2
    scenario["current"]["trigger_enabled"] = True
    scenario["faults"]["queue"] = True
    first = await client.post(url, json={"reason": "try again"}, headers=headers)
    assert first.status_code == 202, first.text
    retried = first.json()
    assert retried["run_id"] != run_id and retried["retry_of"] == run_id
    assert retried["input"] == original["input"]
    assert not list(Draft202012Validator({**SCHEMA, "$ref": "#/$defs/Run"}).iter_errors(retried))
    async with scenario["sessions"]() as independent:
        stored = await independent.get(Run, (fixture["tenant_id"], run_id))
        assert stored.state == "failed" and view_run(stored)["admission"] == original["admission"]
        assert stored.configuration_id == original["admission"]["configuration_id"]
        assert stored.configuration_revision == original["admission"]["configuration_revision"]
        linked = await independent.get(Run, (fixture["tenant_id"], retried["run_id"]))
        assert linked.retry_of == run_id
        work = (await independent.scalars(select(Work).where(Work.run_id == linked.run_id))).one()
        assert work.kind == "execute" and work.completed_at is None and work.last_error == "publication_failed"
        assert len((await independent.scalars(select(Run))).all()) == 2
        receipt = (await independent.scalars(select(RunReceipt).where(RunReceipt.operation == "retry"))).one()
        assert receipt.response == retried and receipt.run_id == linked.run_id
        linked.state, linked.revision = "queued", 2
        await independent.commit()
    replay = await client.post(url, json={"reason": "try again"}, headers=headers)
    assert replay.status_code == 202 and replay.json() == retried
    assert (await client.post(url, json={"reason": "changed"}, headers=headers)).status_code == 409
    assert (await client.post(f"{path}/{retried['run_id']}/retry", json={"reason": "try again"},
                              headers=headers)).status_code == 409
    assert len(scenario["captures"]) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "reconcile", "retry"])
async def test_mutations_enforce_owner_scope_and_headers(execution_scenario, operation):
    scenario = execution_scenario
    fixture = scenario["products"]["tenant-a"]
    client, path = scenario["client"], run_path(fixture)
    original = (await client.post(path, json=run_request(fixture),
                                headers={"Idempotency-Key": "mutation-scope-fixture"})).json()
    url = f"{path}/{original['run_id']}/{operation}"
    assert (await client.post(url, json={"reason": "inspect"})).status_code == 428
    headers = {"Idempotency-Key": "mutation-scope-0001"}
    assert (await client.post(url, json={"reason": False}, headers=headers)).status_code == 422
    scenario["current"]["actor_id"] = "other-member"
    assert (await client.post(url, json={"reason": "inspect"}, headers=headers)).status_code == 404
    scenario["current"]["actor_id"] = None
    scenario["current"]["tenant_id"] = "tenant-b"
    assert (await client.post(url, json={"reason": "inspect"}, headers=headers)).status_code == 404
    async with scenario["sessions"]() as independent:
        assert len((await independent.scalars(select(RunReceipt))).all()) == 1
        assert len((await independent.scalars(select(Work))).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "reconcile", "retry"])
async def test_mutation_receipt_failure_rolls_back_effects(execution_scenario, operation):
    scenario = execution_scenario
    fixture = scenario["products"]["tenant-a"]
    client, path = scenario["client"], run_path(fixture)
    original = (await client.post(path, json=run_request(fixture),
                                headers={"Idempotency-Key": "mutation-rollback-fixture"})).json()
    run_id = original["run_id"]
    if operation == "retry":
        async with scenario["sessions"]() as independent:
            await independent.execute(update(Run).where(Run.run_id == run_id).values(
                state="failed", provider_state="failed", no_effect_proof=no_effect_proof(fixture)))
            await independent.execute(update(Work).where(Work.run_id == run_id).values(completed_at=FIXED_TIME))
            await independent.commit()

    def fail_receipt(session):
        if any(isinstance(row, RunReceipt) and row.operation == operation for row in session.new):
            raise RuntimeError("injected receipt commit failure")

    event.listen(Session, "before_commit", fail_receipt)
    try:
        with pytest.raises(RuntimeError, match="injected receipt commit failure"):
            await client.post(f"{path}/{run_id}/{operation}", json={"reason": "inspect"},
                              headers={"Idempotency-Key": "mutation-rollback-0001"})
    finally:
        event.remove(Session, "before_commit", fail_receipt)
    async with scenario["sessions"]() as independent:
        run = await independent.get(Run, (fixture["tenant_id"], run_id))
        assert run.revision == 1 and run.state == ("failed" if operation == "retry" else "admitted")
        work = (await independent.scalars(select(Work))).one()
        assert work.fence == 0
        assert len((await independent.scalars(select(RunReceipt))).all()) == 1
        assert len((await independent.scalars(select(Run))).all()) == 1


@pytest.mark.asyncio
async def test_outbox_recovery(execution_scenario):
    from datetime import timedelta

    from l5_services.dispatcher import repair_owed_work

    scenario = execution_scenario
    first_fixture = scenario["products"]["tenant-a"]
    second_fixture = scenario["products"]["tenant-b"]
    key = "scenario_recover_committed_0001"
    scenario["faults"]["queue"] = True
    accepted = await scenario["client"].post(run_path(first_fixture), json=run_request(first_fixture),
                                               headers={"Idempotency-Key": key})
    assert accepted.status_code == 202
    assert len(scenario["delivered"]) == 1
    async with scenario["sessions"]() as readback:
        run = await readback.get(Run, (first_fixture["tenant_id"], accepted.json()["run_id"]))
        receipt = await readback.get(RunReceipt, (
            first_fixture["tenant_id"], first_fixture["product_id"], first_fixture["project_id"],
            "security", f"owner-{first_fixture['tenant_id']}", "submit", key,
        ))
        work = await readback.scalar(select(Work).where(Work.tenant_id == first_fixture["tenant_id"]))
        assert run is not None and receipt.run_id == run.run_id == work.run_id
        assert [item["source_ref"] for item in run.admission["source_snapshots"]] == first_fixture["sources"]
        assert run.capture_limits == {"max_bytes": 1024, "max_files": 10}
        assert work.completed_at is None and work.last_enqueued_at is None
        assert work.last_error == "publication_failed" and work.attempts == 1
        work_id = work.work_id

    scenario["faults"]["queue"] = False
    scenario["current"]["tenant_id"] = "tenant-b"
    second = await scenario["client"].post(run_path(second_fixture), json=run_request(second_fixture),
                                            headers={"Idempotency-Key": key})
    assert second.status_code == 202 and second.json()["run_id"] != accepted.json()["run_id"]
    restarted = async_sessionmaker(scenario["engine"], expire_on_commit=False)
    assert await repair_owed_work(restarted, scenario["publish"], now=FIXED_TIME) == 1
    assert len(scenario["delivered"]) == 3
    assert scenario["delivered"][0] == scenario["delivered"][2]
    assert scenario["delivered"][0] == (
        {"schema_version": "1.0", "work_id": work_id, "generation": 1}, 0,
    )
    assert await repair_owed_work(restarted, scenario["publish"], now=FIXED_TIME + timedelta(minutes=1)) == 0
    async with restarted() as readback:
        first_work = await readback.get(Work, (first_fixture["tenant_id"], work_id))
        second_work = await readback.scalar(select(Work).where(Work.tenant_id == second_fixture["tenant_id"]))
        assert first_work.attempts == 2 and first_work.last_enqueued_at is not None
        assert first_work.completed_at is None and first_work.last_error is None
        assert second_work.attempts == 1 and second_work.last_enqueued_at is not None
        assert (await readback.scalar(select(Run.run_id).where(Run.tenant_id == first_fixture["tenant_id"]))) == accepted.json()["run_id"]
    assert len(scenario["captures"]) == 4


@pytest.mark.asyncio
async def test_admission_scope_and_cancel(execution_scenario):
    from l5_services.dispatcher import repair_owed_work

    scenario = execution_scenario
    client = scenario["client"]
    first = scenario["products"]["tenant-a"]
    second = scenario["products"]["tenant-b"]
    first_path = run_path(first)
    second_path = run_path(second)
    accepted = await client.post(first_path, json=run_request(first),
                                 headers={"Idempotency-Key": "scenario_scope_first_0001"})
    assert accepted.status_code == 202
    run_id = accepted.json()["run_id"]
    run_url = f"{first_path}/{run_id}"
    async with scenario["sessions"]() as independent:
        original_run = await independent.get(Run, (first["tenant_id"], run_id))
        original_work = await independent.scalar(select(Work).where(Work.run_id == run_id))
        assert original_run is not None and original_work is not None
        original_state = (original_run.revision, original_run.state, original_work.fence,
                          original_work.attempts, original_work.completed_at)
        work_id = original_work.work_id

    observed = await client.get(run_url)
    listed = await client.get(first_path)
    assert observed.status_code == listed.status_code == 200
    assert observed.json() == accepted.json()
    assert [entry["run_id"] for entry in listed.json()["items"]] == [run_id]
    assert len(scenario["captures"]) == 2 and len(scenario["delivered"]) == 1
    async with scenario["sessions"]() as independent:
        unchanged_run = await independent.get(Run, (first["tenant_id"], run_id))
        unchanged_work = await independent.get(Work, (first["tenant_id"], work_id))
        assert (unchanged_run.revision, unchanged_run.state, unchanged_work.fence,
                unchanged_work.attempts, unchanged_work.completed_at) == original_state

    scenario["current"]["tenant_id"] = "tenant-b"
    assert (await client.get(run_url)).status_code == 404
    assert (await client.get(first_path)).status_code == 404
    assert (await client.post(f"{run_url}/cancel", json={"reason": "foreign"},
                              headers={"Idempotency-Key": "scenario_foreign_cancel_01"})).status_code == 404

    scenario["current"]["trigger_enabled"] = False
    disabled = await client.post(second_path, json=run_request(second),
                                 headers={"Idempotency-Key": "scenario_disabled_event_01"})
    assert disabled.status_code == 409 and disabled.json()["detail"]["code"] == "trigger_disabled"
    async with scenario["sessions"]() as independent:
        assert (await independent.scalars(select(Run).where(Run.tenant_id == second["tenant_id"]))).all() == []
        assert (await independent.scalars(select(Work).where(Work.tenant_id == second["tenant_id"]))).all() == []
    assert len(scenario["captures"]) == 2 and len(scenario["delivered"]) == 1

    scenario["current"]["trigger_enabled"] = True
    own = await client.post(second_path, json=run_request(second),
                            headers={"Idempotency-Key": "scenario_scope_second_001"})
    assert own.status_code == 202
    own_url = f"{second_path}/{own.json()['run_id']}"
    scenario["current"]["actor_id"] = "other-team-member"
    assert (await client.get(own_url)).status_code == 404
    assert (await client.get(second_path)).json() == {"items": []}
    assert (await client.post(f"{own_url}/cancel", json={"reason": "private"},
                              headers={"Idempotency-Key": "scenario_private_cancel_01"})).status_code == 404
    scenario["current"]["actor_id"] = None
    assert [item["run_id"] for item in (await client.get(second_path)).json()["items"]] == [own.json()["run_id"]]

    scenario["current"]["tenant_id"] = "tenant-a"
    cancelled = await client.post(f"{run_url}/cancel", json={"reason": "no longer needed"},
                                  headers={"Idempotency-Key": "scenario_queued_cancel_01"})
    assert cancelled.status_code == 202
    assert cancelled.json()["state"] == "cancelled" and cancelled.json()["revision"] == original_state[0] + 1
    assert (await client.get(run_url)).json() == cancelled.json()
    assert await repair_owed_work(scenario["sessions"], scenario["publish"], now=FIXED_TIME) == 0
    async with scenario["sessions"]() as independent:
        fenced_run = await independent.get(Run, (first["tenant_id"], run_id))
        fenced_work = await independent.get(Work, (first["tenant_id"], work_id))
        assert fenced_run.state == "cancelled" and fenced_run.revision == original_state[0] + 1
        assert fenced_work.fence == original_state[2] + 1 and fenced_work.completed_at is not None
        assert fenced_work.attempts == original_state[3]
        assert len((await independent.scalars(select(Run))).all()) == 2
        assert len((await independent.scalars(select(RunReceipt))).all()) == 3
        assert len((await independent.scalars(select(Work))).all()) == 2
    assert len(scenario["captures"]) == 4 and len(scenario["delivered"]) == 2


@pytest.mark.asyncio
async def test_mcp_submit_and_get_share_one_independently_stored_run(execution_scenario, monkeypatch, request):
    from src.internal import tools_l5

    monkeypatch.setattr(tools_l5, "files", lambda package: Path(__file__).resolve().parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    request.addfinalizer(tools_l5.l5_tool_catalogue.cache_clear)
    request.addfinalizer(tools_l5.l5_operations.cache_clear)
    dispatch_l5 = tools_l5.dispatch_l5
    available_l5_catalogue = tools_l5.available_l5_catalogue
    scenario = execution_scenario
    fixture = scenario["products"]["tenant-a"]
    arguments = {"product_id": fixture["product_id"], "project_id": fixture["project_id"],
                 "service_id": "security", "input": run_request(fixture),
                 "idempotency_key": "mcp-replayed-submission-00001"}
    assert {"l5.submit_run", "l5.get_run"} <= {
        entry["name"] for entry in available_l5_catalogue()["tools"]
    }
    first = await dispatch_l5("l5___submit_run", arguments, scenario["backend"])
    replay = await dispatch_l5("l5___submit_run", arguments, scenario["backend"])
    assert replay == first
    content = first["structuredContent"]
    assert content["http_status"] == 202
    assert content["data"]["state"] != "succeeded"
    assert json.loads(first["content"][0]["text"]) == content
    descriptor = next(item for item in available_l5_catalogue()["tools"] if item["name"] == "l5.submit_run")
    Draft202012Validator(descriptor["outputSchema"]).validate(content)
    assert content["etag"] == f'"{content["data"]["revision"]}"'
    run_id = content["data"]["run_id"]
    read = await dispatch_l5("l5___get_run", {
        **{key: arguments[key] for key in ("product_id", "project_id", "service_id")},
        "assessment_ref": run_id,
    }, scenario["backend"])
    assert read["structuredContent"]["data"]["run_id"] == run_id
    assert read["structuredContent"]["data"]["input"] == arguments["input"]
    descriptor = next(item for item in available_l5_catalogue()["tools"] if item["name"] == "l5.get_run")
    Draft202012Validator(descriptor["outputSchema"]).validate(read["structuredContent"])
    direct = await scenario["client"].get(run_path(fixture) + "/" + run_id)
    assert direct.status_code == 200
    assert direct.json() == read["structuredContent"]["data"]
    foreign = scenario["products"]["tenant-b"]
    denied = await dispatch_l5("l5___get_run", {
        "product_id": foreign["product_id"], "project_id": foreign["project_id"],
        "service_id": "security", "assessment_ref": run_id,
    }, scenario["backend"])
    assert denied["isError"] is True
    assert denied["structuredContent"]["http_status"] == 404
    assert denied["structuredContent"]["error"]["code"] == "not_found"
    async with scenario["sessions"]() as independent:
        runs = (await independent.scalars(select(Run))).all()
        receipts = (await independent.scalars(select(RunReceipt))).all()
        work = (await independent.scalars(select(Work))).all()
        assert len(runs) == len(receipts) == len(work) == 1
        assert runs[0].run_id == receipts[0].run_id == work[0].run_id == run_id
        assert runs[0].tenant_id == fixture["tenant_id"]
    assert len(scenario["delivered"]) == 1
    assert len(scenario["captures"]) == 2


@pytest.mark.asyncio
async def test_mcp_pending_observation_is_not_provider_completion(execution_scenario, monkeypatch, request):
    from src.internal import tools_l5

    monkeypatch.setattr(tools_l5, "files", lambda package: Path(__file__).resolve().parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    request.addfinalizer(tools_l5.l5_tool_catalogue.cache_clear)
    request.addfinalizer(tools_l5.l5_operations.cache_clear)
    scenario = execution_scenario
    fixture = scenario["products"]["tenant-a"]
    target = {"product_id": fixture["product_id"], "project_id": fixture["project_id"],
              "service_id": "security"}
    submitted = await tools_l5.dispatch_l5("l5___submit_run", {
        **target, "input": run_request(fixture), "idempotency_key": "pending-observation-00001",
    }, scenario["backend"])
    assert submitted["structuredContent"]["http_status"] == 202
    run_id = submitted["structuredContent"]["data"]["run_id"]
    reference = {**fixture["sources"][0], "record_type": "code_review_job", "record_id": "job-pending",
                 "record_revision": "job-version-1"}
    observation = {"observed_at": FIXED_TIME.isoformat().replace("+00:00", "Z"),
                   "native_job_ref": reference, "outcome": "running", "progress_coverage": "unavailable"}
    monitoring = {"state": "current", "progress_state": "unknown", "poll_attempts": 1,
                  "consecutive_poll_errors": 0, "latest_observation": observation}
    async with scenario["sessions"]() as independent:
        run = await independent.get(Run, (fixture["tenant_id"], run_id))
        run.state = "running"
        run.provider_state = "running"
        run.native_job_ref = reference
        run.monitoring = monitoring
        run.revision += 1
        await independent.commit()
    pending = await tools_l5.dispatch_l5("l5___get_run", {**target, "assessment_ref": run_id}, scenario["backend"])
    data = pending["structuredContent"]["data"]
    assert pending["structuredContent"]["http_status"] == 200
    assert data["state"] == data["provider_state"] == "running"
    assert data["monitoring"] == monitoring
    assert data["native_job_ref"] == reference
    assert "percent_complete" not in str(pending)
    assert json.loads(pending["content"][0]["text"]) == pending["structuredContent"]
    descriptor = next(entry for entry in tools_l5.available_l5_catalogue()["tools"]
                      if entry["name"] == "l5.get_run")
    Draft202012Validator(descriptor["outputSchema"]).validate(pending["structuredContent"])
    direct = await scenario["client"].get(run_path(fixture) + "/" + run_id)
    assert direct.status_code == 200 and direct.json() == data
    async with scenario["sessions"]() as independent:
        stored = await independent.get(Run, (fixture["tenant_id"], run_id))
        assert stored.provider_state == "running" and stored.monitoring == monitoring
    assert len(scenario["delivered"]) == 1


@pytest.mark.asyncio
async def test_mcp_lost_response_replays_one_stored_run(execution_scenario, monkeypatch, request):
    from src import l5_integration
    from src.internal import tools_l5

    monkeypatch.setattr(tools_l5, "files", lambda package: Path(__file__).resolve().parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    request.addfinalizer(tools_l5.l5_tool_catalogue.cache_clear)
    request.addfinalizer(tools_l5.l5_operations.cache_clear)
    scenario = execution_scenario
    fixture = scenario["products"]["tenant-a"]
    target = {"product_id": fixture["product_id"], "project_id": fixture["project_id"],
              "service_id": "security"}
    arguments = {**target, "input": run_request(fixture), "idempotency_key": "lost_response_run_key_0001"}
    current_actor = SimpleNamespace(account_type="service", org_id=fixture["tenant_id"],
                                    agent_registry_id="registered-principal")
    grants = []

    class ProductGrants:
        async def scalars(self, statement):
            values = set(statement.compile().params.values())
            assert {fixture["tenant_id"], fixture["product_id"], fixture["project_id"],
                    "registered-principal"} <= values
            grants.append(values)
            return SimpleNamespace(all=lambda: ["authorized-grant"])

    def selected_backend(service_id, *, request, db, actor, x_l5_delegation_id, response):
        assert request.app is gateway
        assert isinstance(db, ProductGrants)
        assert (service_id, actor, x_l5_delegation_id) == ("security", current_actor, "authorized-grant")
        return scenario["backend"]

    monkeypatch.setattr(l5_integration, "backend", selected_backend)
    gateway = FastAPI()
    gateway.include_router(l5_integration.router)
    gateway.dependency_overrides[l5_integration.get_current_user] = lambda: current_actor
    gateway.dependency_overrides[l5_integration.get_db] = ProductGrants
    original = httpx.ASGITransport.handle_async_request
    dropped = {"once": False}

    async def lose_first_response(transport, request):
        response = await original(transport, request)
        if (request.url.host == "l5.internal" and request.method == "POST"
                and request.url.path.endswith("/runs") and not dropped["once"]):
            dropped["once"] = True
            await response.aread()
            raise httpx.ReadError("simulated response loss")
        return response

    monkeypatch.setattr(httpx.ASGITransport, "handle_async_request", lose_first_response)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gateway.test") as client:
        async def tool(name, params):
            return await client.post("/v1/l5/mcp/tools/call", json={"tool": name, "arguments": params})

        with pytest.raises(httpx.ReadError, match="simulated response loss"):
            await tool("l5___submit_run", arguments)
        assert dropped["once"] and len(grants) == 1
        async with scenario["sessions"]() as independent:
            persisted = (await independent.scalars(select(Run))).one()
            run_id = persisted.run_id
            assert (await independent.scalars(select(RunReceipt))).one().run_id == run_id
            assert (await independent.scalars(select(Work))).one().run_id == run_id
        assert len(scenario["delivered"]) == 1 and len(scenario["captures"]) == 2
        replay = await tool("l5___submit_run", arguments)
        assert replay.status_code == 200
        payload = replay.json()["structuredContent"]
        assert payload["http_status"] == 202 and payload["data"]["run_id"] == run_id
        assert json.loads(replay.json()["content"][0]["text"]) == payload
        reference = {**fixture["sources"][0], "record_type": "code_review_job", "record_id": "job-pending",
                     "record_revision": "job-version-1"}
        monitoring = {"state": "current", "progress_state": "unknown", "poll_attempts": 1,
                      "consecutive_poll_errors": 0,
                      "latest_observation": {"observed_at": FIXED_TIME.isoformat().replace("+00:00", "Z"),
                                             "native_job_ref": reference, "outcome": "running",
                                             "progress_coverage": "unavailable"}}
        async with scenario["sessions"]() as independent:
            stored = await independent.get(Run, (fixture["tenant_id"], run_id))
            stored.state = stored.provider_state = "running"
            stored.native_job_ref = reference
            stored.monitoring = monitoring
            stored.revision += 1
            await independent.commit()
        read = await tool("l5___get_run", {**target, "assessment_ref": run_id})
        assert read.status_code == 200
        content = read.json()["structuredContent"]
        assert content["data"]["monitoring"] == monitoring
        assert content["data"]["state"] == "running" and "percent_complete" not in str(read.json())
        direct = await scenario["client"].get(run_path(fixture) + "/" + run_id)
        assert direct.status_code == 200 and direct.json() == content["data"]
        async with scenario["sessions"]() as independent:
            runs = (await independent.scalars(select(Run))).all()
            receipts = (await independent.scalars(select(RunReceipt))).all()
            work = (await independent.scalars(select(Work))).all()
            assert len(runs) == len(receipts) == len(work) == 1
            assert runs[0].run_id == receipts[0].run_id == work[0].run_id == run_id
            assert runs[0].monitoring == monitoring
        assert len(scenario["delivered"]) == 1 and len(scenario["captures"]) == 2
        assert len(grants) == 3
