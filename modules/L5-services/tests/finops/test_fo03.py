"""FinOps inquiry identity is retained even when a worker loses its lease."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.models.base import Base
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.evidence import ProviderObservation
from l5_services.models.evidence import AssessmentResult
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run
from l5_services.models.work import Work
from l5_services.work_store import claim_work, prepare_submit_attempt


NOW = datetime(2026, 10, 28, tzinfo=UTC)


def test_optional_finops_context_stays_in_project():
    from fastapi import HTTPException
    from l5_services.admission import _exact_sources

    scope = {"tenant_id": "tenant-example", "project_id": "project-example"}
    source = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
              "source_connection_id": "connection-example", "record_type": "document",
              "record_id": "context-example", "record_revision": "revision-one"}
    assert _exact_sources({"service_id": "finops", "capability": "cost_inquiry", "sources": []}, scope) == []
    assert _exact_sources({"service_id": "finops", "capability": "cost_inquiry", "sources": [source]}, scope) == [source]
    with pytest.raises(HTTPException) as failure:
        _exact_sources({"service_id": "finops", "capability": "cost_inquiry",
                        "sources": [{**source, "project_id": "foreign-project"}]}, scope)
    assert failure.value.status_code == 422


async def test_prepared_inquiry_survives_restart_with_one_question_hash(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'inquiry.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example"}
    question = "What was spent in January?"
    query = {"metric": "UnblendedCost"}
    agent_ref = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                 "record_type": "finops_agent", "record_id": "agent-example"}
    billing_scope = {"scope_generation": 3}
    manifest = {"scope": scope, "question_hash": sha256(question.encode()).hexdigest(),
                "binding_generation": 3, "binding_id": "binding-example",
                "configuration_revision": 1, "run_id": "run-example", "query": query,
                "native_agent_ref": agent_ref, "billing_scope": billing_scope}
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Run(tenant_id=scope["tenant_id"], run_id="run-example",
                            product_id=scope["product_id"], project_id=scope["project_id"],
                            service_id="finops", configuration_id="configuration-example",
                            configuration_revision=1, binding_id="binding-example", trigger_id="manual-example",
                            binding_generation=3, input={"question": question, "query": query}, admission={"scope": scope,
                            "limits": {"max_polls": 2}, "native_agent_ref": agent_ref,
                            "billing_scope": billing_scope}, state="queued", manifest=manifest,
                            deadline_at=NOW + timedelta(hours=1)))
            session.add(RunReceipt(tenant_id=scope["tenant_id"], product_id=scope["product_id"],
                                   project_id=scope["project_id"], service_id="finops", actor_id="owner-example",
                                   operation="submit", idempotency_key="unique-key-example-0001",
                                   request_hash="f" * 64, run_id="run-example",
                                   expires_at=NOW + timedelta(hours=1)))
            session.add(Work(tenant_id=scope["tenant_id"], work_id="work-example", run_id="run-example",
                             kind="submit", generation=1, step=1, due_at=NOW))
            await session.commit()
        first_claim = await claim_work(sessions, "work-example", 1, now=NOW, lease_seconds=1)
        first, created = await prepare_submit_attempt(sessions, first_claim, now=NOW)
        assert created and first.question_hash == manifest["question_hash"]
        async with sessions() as session:
            assert len((await session.scalars(select(WorkerAttempt))).all()) == 1
            stored = await session.get(WorkerAttempt, (scope["tenant_id"], first.attempt_id))
            assert stored.status == "prepared" and stored.question_hash == first.question_hash
        second_claim = await claim_work(sessions, "work-example", 1, now=NOW + timedelta(seconds=2))
        second, created = await prepare_submit_attempt(sessions, second_claim, now=NOW + timedelta(seconds=2))
        assert not created and second.attempt_id == first.attempt_id
        assert second.idempotency_token == first.idempotency_token
        assert second.question_hash == first.question_hash and second.can_reconcile
        async with sessions() as session:
            stored = await session.get(WorkerAttempt, (scope["tenant_id"], first.attempt_id))
            assert stored.reconcile_checks == 1
            receipt = await session.get(RunReceipt, (scope["tenant_id"], scope["product_id"],
                scope["project_id"], "finops", "owner-example", "submit", "unique-key-example-0001"))
            assert receipt.run_id == "run-example"
            run = await session.get(Run, (scope["tenant_id"], "run-example"))
            run.input = {"question": "A different question", "query": query}
            await session.commit()
        with pytest.raises(ValueError, match="frozen FinOps inquiry changed"):
            await prepare_submit_attempt(sessions, second_claim, now=NOW + timedelta(seconds=2))
        async with sessions() as session:
            assert len((await session.scalars(select(WorkerAttempt))).all()) == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("terminal_state", ["cancelled", "expired"])
@pytest.mark.parametrize("native_answer", ["answered", "refused"])
async def test_terminal_recovery(tmp_path, monkeypatch, terminal_state, native_answer):
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("BG_TOKEN_SECRET_KEY", "fictional-test-key-0123456789abcdef")
    from l5_integration import GatewayConfigurationBackend
    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services import run_access
    from l5_services.api.router import create_router
    from l5_services.workers import execution
    from l5_services.workers.relay import repair_once

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(run_access, "datetime", FixedClock)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'terminal.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example", "owner_id": "owner-example",
             "relationship_revision": "1", "visibility": "only_me"}
    question = "What was spent?"
    native_ref = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                  "agent_id": "agent-example", "conversation_id": "conversation-example",
                  "turn_id": "turn-example", "question_hash": sha256(question.encode()).hexdigest()}
    answer_ref = {"kind": "finops_native_answer", "native_job_ref": native_ref,
                  "status": native_answer, "observed_at": NOW.isoformat(),
                  "answer_artifact": {"artifact_id": "answer-example", "version": "version-one",
                                      "sha256": "a" * 64}}
    deadline = NOW + timedelta(minutes=30 if terminal_state == "cancelled" else -1)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        messages = []
        fail_collection_publish = True

        async def publish(envelope, delay):
            nonlocal fail_collection_publish
            assert set(envelope) == {"schema_version", "work_id", "generation"}
            async with sessions() as session:
                work = await session.get(Work, (scope["tenant_id"], envelope["work_id"]))
            if work.kind == "collect" and fail_collection_publish:
                fail_collection_publish = False
                raise ConnectionError("synthetic collection queue acknowledgment loss")
            messages.append((envelope, delay))

        async with sessions() as session:
            session.add(Run(tenant_id=scope["tenant_id"], run_id="run-example",
                            product_id=scope["product_id"], project_id=scope["project_id"],
                            service_id="finops", configuration_id="configuration-example",
                            configuration_revision=1, binding_id="binding-example", trigger_id="manual-example",
                            binding_generation=1, input={"question": question, "query": {}},
                            admission={"scope": scope, "monitoring_policy": {
                                "poll_interval_seconds": 600, "poll_grace_seconds": 300},
                                "limits": {"max_polls": 4}},
                            state="running",
                            provider_state="running", native_job_ref=native_ref, deadline_at=deadline))
            session.add(Work(tenant_id=scope["tenant_id"], work_id="work-example", run_id="run-example",
                             kind="poll", generation=1, step=1, due_at=NOW))
            await session.commit()
        if terminal_state == "cancelled":
            async with sessions() as session:
                backend = GatewayConfigurationBackend(session, SimpleNamespace(
                    org_id=scope["tenant_id"], user_id="owner-example", account_type="human"),
                    publish=publish)

                async def authorize_product_scope(product_id, project_id):
                    assert (product_id, project_id) == (scope["product_id"], scope["project_id"])

                async def relationship(product_id, project_id):
                    assert (product_id, project_id) == (scope["product_id"], scope["project_id"])
                    return SimpleNamespace(relationship_revision=1)

                async def source_rights(project_id, runs):
                    assert project_id == scope["project_id"]
                    assert all(run.tenant_id == scope["tenant_id"] for run in runs)
                    return runs

                backend._authorize_product_scope = authorize_product_scope
                backend.relationship = relationship
                backend.run_source_rights = source_rights
                app = FastAPI()
                app.include_router(create_router(lambda: backend))
                path = "/v1/products/product-example/projects/project-example/services/finops/runs/run-example/cancel"
                headers = {"Idempotency-Key": "cancel-local-example-01"}
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://local.test") as client:
                    cancelled = await client.post(path, json={"reason": "Stop local inquiry"}, headers=headers)
                    again = await client.post(path, json={"reason": "Stop local inquiry"}, headers=headers)
                assert cancelled.status_code == again.status_code == 202
                assert cancelled.json() == again.json()
                assert cancelled.json()["state"] == "cancelled"
                assert cancelled.json()["provider_state"] == "running"
            async with sessions() as session:
                works = (await session.scalars(select(Work))).all()
                assert sum(item.completed_at is None for item in works) == 1
                pending = next(item for item in works if item.completed_at is None)
                receipts = (await session.scalars(select(RunReceipt))).all()
                assert len(receipts) == 1 and receipts[0].operation == "cancel"
        else:
            pending = SimpleNamespace(work_id="work-example")

        operations = []

        class ReadOnlyFixture:
            async def submit(self, *args):
                raise AssertionError("recovery must not submit")

            async def reconcile_submit(self, *args):
                raise AssertionError("persisted native ID must not be resubmitted")

            async def cancel(self, *args):
                raise AssertionError("unsupported native stop must not be claimed")

            async def observe(self, run, native_job_ref, claim, provider_session):
                operations.append("observe")
                assert native_job_ref == native_ref
                return AdapterOutcome("completed", observation={
                    "native_job_ref": native_ref, "outcome": "completed", "observed_at": NOW.isoformat()})

            async def collect(self, run, native_job_ref, claim, provider_session):
                operations.append("collect")
                assert native_job_ref == native_ref
                return AdapterOutcome("completed", result=answer_ref)

        async def authorize(run, claim, operation):
            assert operation in {"observe", "collect"}
            return object()

        fixture = ReadOnlyFixture()
        worker = execution.ExecutionService(sessions, None, None, publish, authorize,
                                            clock=lambda: NOW, finops_adapter=fixture)
        assert await repair_once(worker, now=NOW) == 1
        assert messages[0][0] == {"schema_version": "1.0", "work_id": pending.work_id,
                                  "generation": 1}
        envelope = messages.pop(0)[0]
        if terminal_state == "expired":
            assert await worker.execute(envelope)
            async with sessions() as session:
                run = await session.get(Run, (scope["tenant_id"], "run-example"))
                assert run.state == "expired" and run.provider_state == "running"
                assert run.monitoring["state"] == "recovery_pending"
                pending = (await session.scalars(select(Work).where(
                    Work.kind == "poll", Work.completed_at.is_(None)))).one()
                assert pending.due_at.replace(tzinfo=UTC) == NOW
            assert messages[0][0]["work_id"] == pending.work_id
            envelope = messages.pop(0)[0]
        assert await worker.execute(envelope)
        async with sessions() as session:
            run = await session.get(Run, (scope["tenant_id"], "run-example"))
            collection = (await session.scalars(select(Work).where(Work.kind == "collect"))).one()
            assert run.state == terminal_state and run.provider_state == "completed"
            assert collection.last_error == "publication_failed" and collection.last_enqueued_at is None
        assert not messages
        assert await repair_once(worker, now=NOW) == 1
        collection_envelope = {"schema_version": "1.0", "work_id": collection.work_id, "generation": 1}
        assert messages == [(collection_envelope, 0)]

        original_finish = execution.finish_work
        failed = False

        async def lose_answer_ack(sessions, claim, **kwargs):
            nonlocal failed
            if claim.kind == "collect" and not failed:
                failed = True
                raise ConnectionError("synthetic answer storage acknowledgment loss")
            return await original_finish(sessions, claim, **kwargs)

        monkeypatch.setattr(execution, "finish_work", lose_answer_ack)
        with pytest.raises(ConnectionError):
            await worker.execute(messages.pop(0)[0])
        restarted = execution.ExecutionService(sessions, None, None, publish, authorize,
                                               clock=lambda: NOW + timedelta(seconds=61), finops_adapter=fixture)
        assert await restarted.execute(collection_envelope)
        async with sessions() as session:
            saved = await session.get(Run, (scope["tenant_id"], "run-example"))
            assert saved.state == terminal_state and saved.provider_state == "completed"
            assert saved.native_job_ref == native_ref and saved.provider_readback_ref == answer_ref
            assert saved.monitoring["poll_attempts"] == 3
            assert saved.admission["limits"]["max_polls"] == 4
            assert (await session.scalars(select(AssessmentResult))).all() == []
            assert all(work.completed_at is not None for work in (await session.scalars(select(Work))).all())
        assert not await restarted.execute(collection_envelope)
        assert not messages
        assert operations == ["observe", "collect", "collect"]
    finally:
        await engine.dispose()


async def test_cancel_uncertain_submit_only_reconciles_existing_attempt(tmp_path):
    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.run_access import RunStore
    from l5_services.workers.execution import ExecutionService

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel-uncertain.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example", "owner_id": "owner-example",
             "relationship_revision": "1", "visibility": "only_me"}
    question = "What was spent?"
    agent_ref = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                 "record_type": "finops_agent", "record_id": "agent-example"}
    manifest = {"scope": scope, "run_id": "run-example", "query": {},
                "question_hash": sha256(question.encode()).hexdigest(),
                "configuration_revision": 1, "binding_id": "binding-example", "binding_generation": 1,
                "native_agent_ref": agent_ref, "billing_scope": {"scope_generation": 1}}
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Run(tenant_id=scope["tenant_id"], run_id="run-example",
                            product_id=scope["product_id"], project_id=scope["project_id"],
                            service_id="finops", configuration_id="configuration-example",
                            configuration_revision=1, binding_id="binding-example", trigger_id="manual-example",
                            binding_generation=1, input={"question": question, "query": {}}, manifest=manifest,
                            admission={"scope": scope, "native_agent_ref": agent_ref,
                                       "billing_scope": {"scope_generation": 1},
                                       "monitoring_policy": {"poll_grace_seconds": 300,
                                                             "poll_interval_seconds": 600},
                                       "limits": {"max_polls": 1}},
                            state="queued", deadline_at=NOW + timedelta(hours=1)))
            session.add(Work(tenant_id=scope["tenant_id"], work_id="work-example", run_id="run-example",
                             kind="submit", generation=1, step=1, due_at=NOW))
            session.add(WorkerAttempt(tenant_id=scope["tenant_id"], attempt_id="attempt-example",
                                      run_id="run-example", operation="submit", first_work_id="work-example",
                                      first_fence=1, idempotency_token="a" * 64,
                                      input_hash=sha256(json.dumps(
                                          manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                                      question_hash=manifest["question_hash"], started_at=NOW,
                                      status="prepared", reconcile_checks=0))
            await session.commit()
        async with sessions() as session:
            store = RunStore(session, scope["tenant_id"], scope["product_id"],
                             scope["project_id"], "finops", "owner-example", "1")
            reply = await store.cancel_queued("run-example", "Cancel local effects",
                                              "cancel-uncertain-01", now=NOW)
            assert reply["state"] == "cancelled" and reply["provider_state"] == "not_started"
        async with sessions() as session:
            pending = (await session.scalars(select(Work).where(Work.kind == "reconcile"))).one()
            assert (await session.get(Work, (scope["tenant_id"], "work-example"))).completed_at.replace(tzinfo=UTC) == NOW

        class ReadOnlyFixture:
            async def submit(self, *args):
                raise AssertionError("cancelled inquiry must not submit")

            async def reconcile_submit(self, run, attempt, provider_session):
                assert attempt.attempt_id == "attempt-example"
                return AdapterOutcome("unknown")

            async def cancel(self, *args):
                raise AssertionError("unsupported native stop must not be requested")

        async def authorize(run, claim, operation):
            assert operation == "reconcile"
            return object()

        worker = ExecutionService(sessions, None, None, None, authorize,
                                  clock=lambda: NOW, finops_adapter=ReadOnlyFixture())
        assert await worker.execute({"schema_version": "1.0", "work_id": pending.work_id,
                                     "generation": 1})
        async with sessions() as session:
            saved = await session.get(Run, (scope["tenant_id"], "run-example"))
            attempt = await session.get(WorkerAttempt, (scope["tenant_id"], "attempt-example"))
            assert saved.state == "cancelled" and saved.provider_state == "unknown"
            assert saved.native_job_ref is None and attempt.reconcile_checks == 1
            assert (await session.scalars(select(Work).where(Work.kind == "submit",
                                                      Work.completed_at.is_(None)))).all() == []
            owed = (await session.scalars(select(Work).where(
                Work.kind == "reconcile", Work.completed_at.is_(None)))).one()
        exhausted = ExecutionService(sessions, None, None, None, authorize,
                                     clock=lambda: NOW + timedelta(seconds=600),
                                     finops_adapter=ReadOnlyFixture())
        assert await exhausted.execute({"schema_version": "1.0", "work_id": owed.work_id,
                                        "generation": 1})
        async with sessions() as session:
            saved = await session.get(Run, (scope["tenant_id"], "run-example"))
            assert saved.state == "cancelled" and saved.native_job_ref is None
            assert (await session.scalars(select(Work).where(
                Work.kind == "reconcile", Work.completed_at.is_(None)))).all() == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize("lose_turn_id", [False, True])
@pytest.mark.parametrize("observe_case", [None, "refreshed", "revoked", "ambiguous"])
async def test_submit_recovery(tmp_path, monkeypatch, caplog, lose_turn_id, observe_case):
    await _submit_recovery_scenario(tmp_path, monkeypatch, caplog, lose_turn_id, observe_case)


@pytest.mark.parametrize("observe_case", ["refreshed", "revoked"], ids=["session-expired", "delegation-revoked"])
async def test_session_recovery(tmp_path, monkeypatch, caplog, observe_case):
    await _submit_recovery_scenario(tmp_path, monkeypatch, caplog, False, observe_case, via_host=True)


async def _submit_recovery_scenario(tmp_path, monkeypatch, caplog, lose_turn_id, observe_case, *, via_host=False):
    monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("BG_TOKEN_SECRET_KEY", "fictional-test-key-0123456789abcdef")
    from l5_services import admission
    from l5_integration import GatewayConfigurationBackend
    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.api.router import create_router
    from l5_services.run_access import RunStore
    from l5_services import run_access
    from l5_services.workers import execution

    clock_now = [NOW]

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock_now[0]

    monkeypatch.setattr(admission, "datetime", FixedClock)
    if via_host:
        monkeypatch.setattr(run_access, "datetime", FixedClock)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'submission.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example", "owner_id": "owner-example",
             "relationship_revision": "1", "visibility": "only_me"}
    agent_ref = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                 "record_type": "finops_agent", "record_id": "agent-example"}
    question = "What was spent in January?"
    request = {"service_id": "finops", "capability": "cost_inquiry", "configuration_revision": 1,
               "binding_id": "binding-example", "question": question,
               "query": {"start": "2026-01-01", "end": "2026-02-01", "metric": "UnblendedCost",
                         "granularity": "MONTHLY", "group_by": ["SERVICE"], "currency": "USD"}}
    connection_ref = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                      "source_connection_id": "connection-example", "record_type": "connection",
                      "record_id": "connection-example", "record_revision": "version-one"}
    agent_ref["source_connection_id"] = connection_ref["source_connection_id"]
    verified = {"scope": scope, "service_id": "finops", "actor_id": "owner-example", "actor_type": "human",
                "configuration_id": "configuration-example", "configuration_revision": 1,
                "configuration_hash": "f" * 64, "configuration_enabled": True,
                "binding_id": "binding-example", "binding_generation": 3, "binding_state": "ready",
                "connection_ref": connection_ref, "binding_connection_ref": connection_ref,
                "delegation_id": "delegation-example", "delegation_version": 1,
                "delegation_active": True, "native_qualified": True, "native_agent_ref": agent_ref,
                "provider_identity": {"account_id": "000000000000", "region": "us-east-1",
                                      "role_arn": "arn:aws:iam::000000000000:role/Example"},
                "billing_scope": {"scope_generation": 3, "scope_digest": "a" * 64},
                "profile_ref": {**connection_ref, "record_type": "product", "record_id": "product-example"},
                "trigger": {"trigger_id": "manual-example", "kind": "manual", "enabled": True,
                            "capability": "cost_inquiry", "binding_id": "binding-example"},
                "adapter_revision": "fixture-only", "visibility": "only_me", "classification": "internal",
                "causal_origin": "manual", "exclusions": [], "max_bytes": 0, "max_files": 0,
                "monitoring_policy": {"poll_interval_seconds": 600, "poll_grace_seconds": 300},
                "limits": {"deadline_seconds": 3600, "max_provider_attempts": 1, "max_polls": 4,
                           "concurrency": 1, "max_cost": "1.00", "currency": "USD",
                           "policy_ref": {"record_type": "policy", "tenant_id": scope["tenant_id"],
                                          "project_id": scope["project_id"], "record_id": "budget-example"}}}
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with sessions() as session:
            session.add(Configuration(tenant_id=scope["tenant_id"], configuration_id="configuration-example",
                                      product_id=scope["product_id"], project_id=scope["project_id"],
                                      service_id="finops", relationship_revision="1", owner_id="owner-example",
                                      current_revision=1, handoff_state="linked"))
            session.add(ConfigurationVersion(tenant_id=scope["tenant_id"],
                                             configuration_id="configuration-example", revision=1,
                                             settings={"enabled": True}, inherited_fields=[], gaps=[],
                                             state="ready", hash="f" * 64))
            await session.commit()

        messages = []
        lose_queue_ack = True

        async def publish(envelope, delay):
            if lose_queue_ack:
                raise ConnectionError("synthetic queue acknowledgment loss")
            messages.append((envelope, delay))

        async def current(*args):
            return (SimpleNamespace(owner_id="owner-example", handoff_state="linked"),
                    SimpleNamespace(state="ready", gaps=[], settings={"enabled": True}))

        async def authority(*args):
            return verified, None

        async def budget_guard(session, limits, selected_scope, now, replay):
            assert selected_scope == scope and limits["max_provider_attempts"] == 1
            return await replay()

        from fastapi import HTTPException
        from l5_services.admission import admit_run
        async with sessions() as session:
            with pytest.raises(HTTPException) as failure:
                await admit_run(session, None, request,
                                {**verified, "provider_session": "must-not-be-persisted"},
                                "rejected-session-example", now=NOW, budget_guard=budget_guard)
            assert failure.value.detail == {"code": "native_unqualified"}

        async with sessions() as session:
            backend = GatewayConfigurationBackend(session, SimpleNamespace(
                org_id=scope["tenant_id"], user_id="owner-example", account_type="human"),
                publish=publish, finops_authority=authority)
            backend.current = current
            backend._require_devops_budget = budget_guard
            app = FastAPI()
            app.include_router(create_router(lambda: backend))
            path = "/v1/products/product-example/projects/project-example/services/finops/runs"
            headers = {"Idempotency-Key": "same-question-key-example"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
                first = await client.post(path, json=request, headers=headers)
                replay = await client.post(path, json=request, headers=headers)
                conflict = await client.post(path, json={**request, "question": "Other question"}, headers=headers)
            assert first.status_code == replay.status_code == 202
            assert first.json()["run_id"] == replay.json()["run_id"]
            assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
            from l5_services import runtime, runtime_finops
            from unittest.mock import AsyncMock

            admitted = await session.get(Run, (scope["tenant_id"], first.json()["run_id"]))
            assert admitted.admission["provider_identity"] == verified["provider_identity"]
            claim = SimpleNamespace(work_id="current-work", owner="worker-example", fence=1, generation=1, kind="submit")
            broker_reply = {"operation_id": admitted.run_id, "attempt_id": claim.work_id,
                            "delegation_revision": verified["delegation_version"],
                            "provider_identity": verified["provider_identity"],
                            "expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat()}
            transport = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(
                status_code=200, json=lambda: broker_reply, raise_for_status=lambda: None)))
            monkeypatch.setenv("L5_SERVICE_ID", "finops")
            monkeypatch.setattr(runtime_finops, "_resources", lambda: SimpleNamespace(
                gateway_transport=lambda: transport, sessions=lambda: sessions,
                archive_store=lambda service: None, queue_publisher=lambda service: publish,
                ScopedSources=runtime._ScopedSources))
            monkeypatch.setattr(runtime_finops, "_private_executor", lambda: SimpleNamespace(**{
                method: AsyncMock() for method in ("submit", "read_submit", "observe", "read_answer", "read_native_cost", "stop")}))
            monkeypatch.setattr(runtime_finops, "_independent_billing", lambda: SimpleNamespace(
                collect_billing_page=AsyncMock(), read_billing_page=AsyncMock()))
            factory = runtime_finops.factory()
            assert (await factory.authorize(admitted, claim, "submit"))["provider_identity"] == verified["provider_identity"]
            broker_reply["provider_identity"] = {**verified["provider_identity"], "account_id": "000000000001"}
            with pytest.raises(execution.AuthorityRevoked, match="another work or provider identity"):
                await factory.authorize(admitted, claim, "submit")
        lose_queue_ack = False

        async with sessions() as session:
            receipt = (await session.scalars(select(RunReceipt))).all()
            assert len(receipt) == 1 and receipt[0].run_id == first.json()["run_id"]
            work = (await session.scalars(select(Work))).one()
            assert work.kind == "execute" and work.last_enqueued_at is None
            assert work.last_error == "publication_failed" and work.attempts == 2

        from l5_services.workers.relay import repair_once

        repair_service = execution.ExecutionService(sessions, None, None, publish, None,
                                                    clock=lambda: NOW)
        assert await repair_once(repair_service, now=NOW) == 1
        execute_envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": 1}
        assert messages == [(execute_envelope, 0)]
        assert await repair_once(repair_service, now=NOW) == 0
        async with sessions() as session:
            repaired = await session.get(Work, (scope["tenant_id"], work.work_id))
            assert repaired.last_error is None and repaired.last_enqueued_at is not None
            assert repaired.attempts == 3

        submits = []
        readbacks = []
        observations = []
        sessions_issued = []
        submit_session = []
        access = {"revoked": observe_case == "revoked"}

        class QualifiedFixture:
            async def submit(self, run, manifest, attempt, provider_session):
                submits.append(attempt.attempt_id)
                assert manifest["native_agent_ref"] == agent_ref
                native = {
                    "tenant_id": run.tenant_id, "product_id": run.product_id,
                    "project_id": run.project_id,
                    "agent_id": agent_ref["record_id"], "conversation_id": "conversation-example",
                    "turn_id": "turn-example", "question_hash": attempt.question_hash}
                if lose_turn_id:
                    native.pop("turn_id")
                return AdapterOutcome("accepted", native_job_ref=native)

            async def reconcile_submit(self, run, attempt, provider_session):
                readbacks.append(attempt.attempt_id)
                return AdapterOutcome("accepted", native_job_ref={
                    "tenant_id": run.tenant_id, "product_id": run.product_id,
                    "project_id": run.project_id,
                    "agent_id": agent_ref["record_id"], "conversation_id": "conversation-example",
                    "turn_id": "turn-example", "question_hash": attempt.question_hash})

            async def observe(self, run, native_job_ref, claim, provider_session):
                observations.append((native_job_ref, provider_session))
                if via_host:
                    assert provider_session.expires_at > NOW + timedelta(seconds=662)
                    assert provider_session is not submit_session[0]
                report = {"native_job_ref": native_job_ref,
                          "outcome": "unknown" if observe_case == "ambiguous" else "running",
                          "observed_at": (NOW + timedelta(seconds=662)).isoformat()}
                if observe_case == "ambiguous":
                    report["provider_session"] = "must-not-be-persisted"
                return AdapterOutcome(report["outcome"], observation=report)

        async def authorize(run, claim, operation):
            assert run.tenant_id == scope["tenant_id"]
            if operation == "observe" and access["revoked"]:
                from fastapi import HTTPException
                raise HTTPException(403, detail={"code": "delegation_revoked"})
            provider_session = (SimpleNamespace(expires_at=NOW + timedelta(
                seconds=60 if operation == "submit" else 3600)) if via_host else object())
            if operation == "submit":
                submit_session.append(provider_session)
            sessions_issued.append(provider_session)
            return provider_session

        fixture = QualifiedFixture()
        worker = execution.ExecutionService(sessions, None, None, publish, authorize,
                                            clock=lambda: NOW, finops_adapter=fixture)
        assert await worker.execute(messages.pop(0)[0])
        async with sessions() as session:
            pending = (await session.scalars(select(Work).where(Work.kind == "submit"))).one()
        submit_envelope = {"schema_version": "1.0", "work_id": pending.work_id, "generation": 1}
        assert messages == [(submit_envelope, 0)]

        original_finish = execution.finish_work
        failed = False

        async def lose_native_ack(sessions, claim, **kwargs):
            nonlocal failed
            if claim.kind == "submit" and not failed:
                failed = True
                raise ConnectionError("synthetic answer-storage acknowledgment loss")
            return await original_finish(sessions, claim, **kwargs)

        monkeypatch.setattr(execution, "finish_work", lose_native_ack)
        with pytest.raises(ConnectionError):
            await worker.execute(messages.pop(0)[0])
        restarted = execution.ExecutionService(sessions, None, None, publish, authorize,
                                               clock=lambda: NOW + timedelta(seconds=61),
                                               finops_adapter=fixture)
        assert await restarted.execute(submit_envelope)
        assert len(submits) == len(readbacks) == 1 and submits == readbacks
        async with sessions() as session:
            attempts = (await session.scalars(select(WorkerAttempt))).all()
            stored = await session.get(Run, (scope["tenant_id"], first.json()["run_id"]))
            assert len(attempts) == 1 and attempts[0].status == "accepted"
            assert stored.native_job_ref["turn_id"] == "turn-example" and stored.state == "running"
        if observe_case is not None:
            async with sessions() as session:
                pending_poll = (await session.scalars(select(Work).where(Work.kind == "poll"))).one()
            if via_host:
                assert submit_session[0].expires_at < NOW + timedelta(seconds=662)
                assert messages[0][0] == {"schema_version": "1.0", "work_id": pending_poll.work_id,
                                          "generation": 1}
                poll_envelope = messages.pop(0)[0]
            else:
                poll_envelope = {"schema_version": "1.0", "work_id": pending_poll.work_id,
                                 "generation": 1}
            observing = execution.ExecutionService(sessions, None, None, publish if via_host else None, authorize,
                                                   clock=lambda: NOW + timedelta(seconds=662),
                                                   finops_adapter=fixture)
            assert await observing.execute(poll_envelope)
            async with sessions() as session:
                stored = await session.get(Run, (scope["tenant_id"], first.json()["run_id"]))
                assert stored.native_job_ref["turn_id"] == "turn-example"
                assert stored.monitoring["poll_attempts"] == 1
                assert stored.admission["limits"]["max_polls"] == 4
                assert "must-not-be-persisted" not in str(stored.monitoring)
                assert "must-not-be-persisted" not in str(stored.native_job_ref)
                assert "must-not-be-persisted" not in str(stored.admission)
                assert "must-not-be-persisted" not in caplog.text
                assert "must-not-be-persisted" not in str((await session.scalars(
                    select(ProviderObservation))).all())
                if observe_case == "revoked":
                    assert stored.state == "outcome_unknown" and not observations
                if observe_case == "ambiguous":
                    assert stored.provider_state == "unknown"
            if observe_case == "revoked":
                access["revoked"] = False
                if via_host:
                    clock_now[0] = NOW + timedelta(seconds=663)
                    async with sessions() as session:
                        backend = GatewayConfigurationBackend(session, SimpleNamespace(
                            org_id=scope["tenant_id"], user_id="owner-example", account_type="human"),
                            publish=publish, finops_authority=authority)

                        async def authorize_product_scope(product_id, project_id):
                            assert (product_id, project_id) == (scope["product_id"], scope["project_id"])

                        async def relationship(product_id, project_id):
                            assert (product_id, project_id) == (scope["product_id"], scope["project_id"])
                            return SimpleNamespace(relationship_revision=1)

                        async def source_rights(project_id, runs):
                            assert project_id == scope["project_id"]
                            assert all(run.tenant_id == scope["tenant_id"] for run in runs)
                            return runs

                        backend._authorize_product_scope = authorize_product_scope
                        backend.relationship = relationship
                        backend.run_source_rights = source_rights
                        app = FastAPI()
                        app.include_router(create_router(lambda: backend))
                        recovery_path = f"{path}/{first.json()['run_id']}/reconcile"
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                     base_url="http://local.test") as client:
                            response = await client.post(recovery_path, json={"reason": "Restore authority"},
                                                         headers={"Idempotency-Key": "reauthorized-read-example-01"})
                            duplicate = await client.post(recovery_path, json={"reason": "Restore authority"},
                                                          headers={"Idempotency-Key": "reauthorized-read-example-01"})
                        assert response.status_code == duplicate.status_code == 202
                        assert response.json() == duplicate.json()
                else:
                    async with sessions() as session:
                        store = RunStore(session, scope["tenant_id"], scope["product_id"],
                                         scope["project_id"], "finops", "owner-example", "1")
                        await store.reconcile(first.json()["run_id"], "Refresh after restored authority",
                                              "reauthorized-read-example-01", now=NOW + timedelta(seconds=663))
                async with sessions() as session:
                    recovery = (await session.scalars(select(Work).where(Work.kind == "reconcile"))).one()
                if via_host:
                    assert messages[-1][0] == {"schema_version": "1.0", "work_id": recovery.work_id,
                                               "generation": 1}
                    recovery_envelope = messages.pop()[0]
                else:
                    recovery_envelope = {"schema_version": "1.0", "work_id": recovery.work_id,
                                         "generation": 1}
                refreshed = execution.ExecutionService(sessions, None, None, publish if via_host else None, authorize,
                                                       clock=lambda: NOW + timedelta(seconds=663),
                                                       finops_adapter=fixture)
                assert await refreshed.execute(recovery_envelope)
                async with sessions() as session:
                    stored = await session.get(Run, (scope["tenant_id"], first.json()["run_id"]))
                    assert stored.monitoring["poll_attempts"] == 2
                    assert stored.state == "running"
                    assert stored.admission["limits"]["max_polls"] == 4
            assert len(submits) == 1
            if observe_case == "refreshed":
                assert observations[0][1] is sessions_issued[-1]
                assert observations[0][1] is not sessions_issued[0]
            if via_host:
                async with sessions() as session:
                    receipts = (await session.scalars(select(RunReceipt))).all()
                    assert sorted(receipt.operation for receipt in receipts) == (
                        ["reconcile", "submit"] if observe_case == "revoked" else ["submit"])
                    assert all(receipt.run_id == first.json()["run_id"] for receipt in receipts)
                    assert len((await session.scalars(select(WorkerAttempt))).all()) == 1
                    stored = await session.get(Run, (scope["tenant_id"], first.json()["run_id"]))
                    assert stored.native_job_ref["turn_id"] == "turn-example"
                    assert stored.admission["limits"]["max_polls"] == 4
                    assert stored.monitoring["poll_attempts"] == (2 if observe_case == "revoked" else 1)
    finally:
        await engine.dispose()
