"""Durable FinOps worker attempts survive private browser acknowledgment loss."""

import io
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.adapters.finops.console_executor import ConsoleFinOpsExecutor, FinOpsExecutorEvidence
from l5_services.adapters.finops.executor_host import verify_executor_work
from l5_services.adapters.finops.inquiry import FinOpsInquiryAdapter
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.base import Base
from l5_services.models.run import Run
from l5_services.models.work import Work
from l5_services.work_store import claim_work, finish_work, prepare_submit_attempt
from l5_services.workers.execution import ExecutionService


SURFACE = {"agent_label": "Example Agent", "agent_identity_selector": "[data-agent-id]",
           "conversation_selector": "[data-conversation-id]", "turn_selector": "[data-turn-receipt]",
           "question_selector": "[data-question]", "turn_receipt_attribute": "data-turn-receipt",
           "answer_selector": "[data-answer]", "turn_status_selector": "[data-state]",
           "answer_classification_selector": "[data-outcome]", "history_complete_selector": "[data-complete]",
           "surface_revision": "example-reviewed-surface"}


class VersionedObjects:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, Metadata, IfNoneMatch):
        assert IfNoneMatch == "*" and Bucket == "example-private-bucket"
        if Key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[Key] = (Body, Metadata)
        return {"VersionId": "version-one"}

    def get_object(self, *, Bucket, Key):
        assert Bucket == "example-private-bucket"
        content, metadata = self.objects[Key]
        return {"VersionId": "version-one", "ContentLength": len(content),
                "Metadata": metadata, "Body": io.BytesIO(content)}


class IsolatedBrowser:
    def __init__(self):
        self.sent = []
        self.visible = False

    @asynccontextmanager
    async def session(self, authority, surface):
        assert surface is SURFACE
        assert authority["access_key_id"] == "fictional-key"
        yield self

    async def find_turn(self, intent):
        if not self.visible:
            return None
        assert self.sent == [intent]
        return {"attempt_token": intent["attempt_token"], "agent_id": intent["agent_id"],
                "conversation_id": "conversation-example", "turn_locator": "receipt-example",
                "question_hash": intent["question_hash"]}

    async def send_once(self, intent):
        self.sent.append(intent)

    async def read_turn(self, reference, intent):
        assert reference["product_id"] == intent["product_id"]
        assert reference["turn_locator"] == "receipt-example"
        assert intent == self.sent[0]
        return {"state": "completed", "answer_status": "answered", "answer": "Native cost answer"}


def authority(run, claim, identity, connection):
    return {"operation_id": run.run_id, "attempt_id": claim.work_id,
            "tenant_id": run.tenant_id, "product_id": run.product_id, "project_id": run.project_id,
            "lease_fence": claim.fence, "lease_owner": claim.owner, "work_kind": claim.kind,
            "work_generation": claim.generation, "run_revision": run.revision,
            "delegation_id": run.admission["delegation_id"],
            "delegation_revision": run.admission["delegation_version"],
            "connection_ref": connection, "provider_identity": identity}


async def test_lost_ack_recovers_single_native_turn_under_new_sql_fence(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'native.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    run_id = "00000000-0000-0000-0000-000000000001"
    question, query = "What was spent?", {"metric": "UnblendedCost"}
    agent = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
             "record_type": "finops_agent", "record_id": "agent-example"}
    billing = {"scope_generation": 1, "scope_digest": "a" * 64}
    identity = {"account_id": "000000000000", "role_arn": "arn:aws:iam::000000000000:role/Example",
                "region": "us-east-1"}
    connection = {"record_type": "credential", "record_id": "credential-example"}
    admission = {"scope": scope, "native_agent_ref": agent, "billing_scope": billing,
                 "native_agent_display_name": "Example Agent",
                 "provider_identity": identity, "connection_ref": connection,
                 "delegation_id": "delegation-example", "delegation_version": 1}
    manifest = {"scope": scope, "native_agent_ref": agent, "billing_scope": billing,
                "native_agent_display_name": "Example Agent",
                "run_id": run_id, "query": query, "binding_generation": 1,
                "binding_id": "binding-example", "configuration_revision": 1,
                "question_hash": sha256(question.encode()).hexdigest()}
    browser, objects = IsolatedBrowser(), VersionedObjects()
    transport = SimpleNamespace(post=AsyncMock())

    async def verify(selected):
        return await verify_executor_work(selected, sessions, transport)

    executor = ConsoleFinOpsExecutor(browser, verify,
                                     FinOpsExecutorEvidence("example-private-bucket", objects), SURFACE)
    billing_adapter = SimpleNamespace(collect_billing_page=AsyncMock(), read_billing_page=AsyncMock())
    adapter = FinOpsInquiryAdapter(executor, billing_adapter)
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection_db:
            await connection_db.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Run(**scope, run_id=run_id, service_id="finops",
                            configuration_id="configuration-example", configuration_revision=1,
                            binding_id="binding-example", trigger_id="manual-example", binding_generation=1,
                            input={"capability": "cost_inquiry", "question": question, "query": query},
                            admission=admission, state="queued", manifest=manifest,
                            deadline_at=now + timedelta(hours=1)))
            session.add(Work(tenant_id=scope["tenant_id"], work_id="work-example", run_id=run_id,
                             kind="submit", generation=1, step=1, due_at=now - timedelta(seconds=1)))
            await session.commit()

        first = await claim_work(sessions, "work-example", 1, now=now, lease_seconds=30, service_id="finops")
        assert first is not None
        attempt, created = await prepare_submit_attempt(sessions, first, now=now)
        assert created
        async with sessions() as session:
            run = await session.get(Run, (scope["tenant_id"], run_id))

        def broker_response(path, *, json):
            assert path == "/internal/v1/l5/credential-session"
            assert json["delegation_id"] == "delegation-example"
            return SimpleNamespace(status_code=200, json=lambda: {
                "operation_id": run_id, "attempt_id": json["attempt_id"],
                "delegation_revision": 1, "provider_identity": identity,
                "access_key_id": "fictional-key", "secret_access_key": "fictional-secret",
                "session_token": "fictional-session", "expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat()})

        transport.post.side_effect = broker_response
        first_authority = authority(run, first, identity, connection)
        assert await adapter.submit(run, manifest, attempt, first_authority) is None
        assert len(browser.sent) == 1 and len(objects.objects) == 1

        async with sessions() as session:
            work = await session.get(Work, (scope["tenant_id"], first.work_id))
            work.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        second = await claim_work(sessions, first.work_id, 1, now=datetime.now(UTC), service_id="finops")
        assert second is not None and second.fence > first.fence
        second_attempt, created = await prepare_submit_attempt(sessions, second, now=datetime.now(UTC))
        assert not created and second_attempt.idempotency_token == attempt.idempotency_token
        second_authority = authority(run, second, identity, connection)
        with pytest.raises(PermissionError, match="current scope"):
            await executor.read_submit(intent=browser.sent[0], authority=first_authority)
        browser.visible = True
        accepted = await adapter.reconcile_submit(run, second_attempt, second_authority)
        assert accepted.classification == "accepted"
        assert accepted.native_job_ref["turn_locator"] == "receipt-example"
        assert accepted.native_job_ref["product_id"] == scope["product_id"]
        assert len(browser.sent) == 1 and len(objects.objects) == 1
        poll_work_id = await finish_work(sessions, second, state="running", provider_state="running",
                                         native_job_ref=accepted.native_job_ref,
                                         attempt_id=second_attempt.attempt_id, attempt_status="accepted",
                                         next_kind="poll", due_at=datetime.now(UTC), now=datetime.now(UTC))
        assert poll_work_id not in (None, "completed")
        async with sessions() as session:
            attempts = (await session.scalars(select(WorkerAttempt))).all()
            assert len(attempts) == 1 and attempts[0].status == "accepted"
            assert attempts[0].native_job_ref == accepted.native_job_ref
            stored = await session.get(Run, (scope["tenant_id"], run_id))
            assert stored.native_job_ref == accepted.native_job_ref and stored.state == "running"
            assert (await session.get(Work, (scope["tenant_id"], poll_work_id))).kind == "poll"
        with pytest.raises(PermissionError, match="current scope"):
            await executor.read_submit(intent=browser.sent[0], authority=second_authority)

        poll_claim = await claim_work(sessions, poll_work_id, 1, now=datetime.now(UTC), service_id="finops")
        poll_authority = authority(stored, poll_claim, identity, connection)
        observed = await adapter.observe(stored, accepted.native_job_ref, poll_claim, poll_authority)
        assert observed.classification == "completed"
        collect_work_id = await finish_work(sessions, poll_claim, state="collecting", provider_state="completed",
                                            observation=observed.observation, next_kind="collect",
                                            due_at=datetime.now(UTC), now=datetime.now(UTC))
        assert collect_work_id not in (None, "completed")
        collect_claim = await claim_work(sessions, collect_work_id, 1, now=datetime.now(UTC), service_id="finops")
        async with sessions() as session:
            collecting = await session.get(Run, (scope["tenant_id"], run_id))
        collect_authority = authority(collecting, collect_claim, identity, connection)
        answer = await adapter.collect(collecting, accepted.native_job_ref, collect_claim, collect_authority)
        assert answer.result["kind"] == "finops_native_answer"
        assert answer.result["answer_artifact"]["version"] == "version-one"
        assert billing_adapter.collect_billing_page.await_count == 0
        assert await finish_work(sessions, collect_claim, state="partial", provider_state="completed",
                                 provider_readback_ref=answer.result, now=datetime.now(UTC)) == "completed"
        async with sessions() as session:
            retained = await session.get(Run, (scope["tenant_id"], run_id))
        assert retained.provider_readback_ref == answer.result and retained.state == "partial"
        assert len(browser.sent) == 1
        assert transport.post.await_count >= 6
    finally:
        await engine.dispose()

async def test_worker_retains_receipt_only_recovery_without_repeating_effect(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    run_id = "00000000-0000-0000-0000-000000000002"
    question = "What was spent?"
    query = {"metric": "UnblendedCost"}
    agent = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
             "record_type": "finops_agent", "record_id": "agent-example"}
    identity = {"account_id": "000000000000", "role_arn": "arn:aws:iam::000000000000:role/Example",
                "region": "us-east-1"}
    connection = {"record_type": "credential", "record_id": "credential-example"}
    billing = {"scope_generation": 1, "scope_digest": "a" * 64}
    admission = {"scope": scope, "native_agent_ref": agent, "billing_scope": billing,
                 "native_agent_display_name": "Example Agent",
                 "provider_identity": identity, "connection_ref": connection,
                 "delegation_id": "delegation-example", "delegation_version": 1,
                 "monitoring_policy": {"poll_interval_seconds": 10, "poll_grace_seconds": 600,
                                       "max_poll_attempts": 5}}
    manifest = {"scope": scope, "native_agent_ref": agent, "billing_scope": billing,
                "native_agent_display_name": "Example Agent",
                "run_id": run_id, "query": query, "binding_generation": 1,
                "binding_id": "binding-example", "configuration_revision": 1,
                "question_hash": sha256(question.encode()).hexdigest()}
    browser, objects = IsolatedBrowser(), VersionedObjects()
    transport = SimpleNamespace(post=AsyncMock())
    broker_state = {"revoked": False}

    def broker(path, *, json):
        assert path == "/internal/v1/l5/credential-session"
        if broker_state["revoked"]:
            return SimpleNamespace(status_code=403)
        return SimpleNamespace(status_code=200, json=lambda: {
            "operation_id": run_id, "attempt_id": json["attempt_id"],
            "delegation_revision": 1, "provider_identity": identity,
            "access_key_id": "fictional-key", "secret_access_key": "fictional-secret",
            "session_token": "fictional-session", "expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat()})

    transport.post.side_effect = broker

    async def verify(selected):
        return await verify_executor_work(selected, sessions, transport)

    async def authorize(run, claim, operation):
        return {**authority(run, claim, identity, connection), "access_key_id": "fictional-key",
                "secret_access_key": "fictional-secret", "session_token": "fictional-session",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat()}

    executor = ConsoleFinOpsExecutor(browser, verify,
                                     FinOpsExecutorEvidence("example-private-bucket", objects), SURFACE)
    billing_adapter = SimpleNamespace(collect_billing_page=AsyncMock(), read_billing_page=AsyncMock())
    adapter = FinOpsInquiryAdapter(executor, billing_adapter)
    service = ExecutionService(sessions, None, None, None, authorize, finops_adapter=adapter, service_id="finops")
    envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
    try:
        async with engine.begin() as connection_db:
            await connection_db.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Run(**scope, run_id=run_id, service_id="finops", configuration_id="config-example",
                            configuration_revision=1, binding_id="binding-example", trigger_id="manual-example",
                            binding_generation=1, input={"capability": "cost_inquiry", "question": question,
                                                         "query": query}, admission=admission, state="queued",
                            manifest=manifest, deadline_at=now + timedelta(hours=1)))
            session.add(Work(tenant_id=scope["tenant_id"], work_id="work-example", run_id=run_id,
                             kind="submit", generation=1, step=1, due_at=now - timedelta(seconds=1)))
            await session.commit()
        assert await service.execute(envelope)
        assert len(browser.sent) == 1
        async with sessions() as session:
            saved = await session.get(Run, (scope["tenant_id"], run_id))
            assert saved.state == "outcome_unknown" and saved.native_job_ref is None
            attempts = (await session.scalars(select(WorkerAttempt))).all()
            assert len(attempts) == 1 and attempts[0].status == "outcome_unknown"
            recovery = await session.scalar(select(Work).where(Work.kind == "reconcile"))
            assert recovery is not None
            recovery.due_at = datetime.now(UTC) - timedelta(seconds=1)
            recovery_id = recovery.work_id
            await session.commit()
        assert not await service.execute(envelope)
        browser.visible = True
        broker_state["revoked"] = True
        with pytest.raises(PermissionError, match="broker authority"):
            await service.execute({**envelope, "work_id": recovery_id})
        assert len(browser.sent) == 1 and len(objects.objects) == 1
        async with sessions() as session:
            attempts = (await session.scalars(select(WorkerAttempt))).all()
            assert len(attempts) == 1 and attempts[0].status == "outcome_unknown"
            recovery = await session.get(Work, (scope["tenant_id"], recovery_id))
            recovery.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        broker_state["revoked"] = False
        assert await service.execute({**envelope, "work_id": recovery_id})
        async with sessions() as session:
            saved = await session.get(Run, (scope["tenant_id"], run_id))
            assert saved.state == "running"
            assert saved.native_job_ref["turn_locator"] == "receipt-example"
            assert saved.native_job_ref["product_id"] == scope["product_id"]
            attempts = (await session.scalars(select(WorkerAttempt))).all()
            assert len(attempts) == 1 and attempts[0].status == "accepted"
        assert not await service.execute({**envelope, "work_id": recovery_id})
        assert len(browser.sent) == 1 and len(objects.objects) == 1
        billing_adapter.collect_billing_page.assert_not_awaited()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("violation", (
    "foreign_service", "foreign_product", "foreign_project", "revoked_delegation",
    "stale_fence", "expired_broker", "expired_run", "stale_delivery", "duplicate_delivery",
    "foreign_receipt",
))
async def test_worker_denies_unscoped_or_duplicate_native_effects(tmp_path, violation):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'boundary.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    run_id = "00000000-0000-0000-0000-000000000003"
    question, query = "What was spent?", {"metric": "UnblendedCost"}
    agent = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
             "record_type": "finops_agent", "record_id": "agent-example"}
    billing = {"scope_generation": 1, "scope_digest": "a" * 64}
    identity = {"account_id": "000000000000", "role_arn": "arn:aws:iam::000000000000:role/Example",
                "region": "us-east-1"}
    connection = {"record_type": "credential", "record_id": "credential-example"}
    admission = {"scope": scope, "native_agent_ref": agent, "billing_scope": billing,
                 "native_agent_display_name": "Example Agent",
                 "provider_identity": identity, "connection_ref": connection,
                 "delegation_id": "delegation-example", "delegation_version": 1,
                 "monitoring_policy": {"poll_interval_seconds": 10, "poll_grace_seconds": 600,
                                       "max_poll_attempts": 5}}
    manifest = {"scope": scope, "native_agent_ref": agent, "billing_scope": billing,
                "native_agent_display_name": "Example Agent",
                "run_id": run_id, "query": query, "binding_generation": 1,
                "binding_id": "binding-example", "configuration_revision": 1,
                "question_hash": sha256(question.encode()).hexdigest()}
    browser, objects = IsolatedBrowser(), VersionedObjects()
    transport = SimpleNamespace(post=AsyncMock())

    def broker(path, *, json):
        assert path == "/internal/v1/l5/credential-session"
        if violation == "revoked_delegation":
            return SimpleNamespace(status_code=403)
        return SimpleNamespace(status_code=200, json=lambda: {
            "operation_id": run_id, "attempt_id": json["attempt_id"],
            "delegation_revision": 1, "provider_identity": identity,
            "access_key_id": "fictional-key", "secret_access_key": "fictional-secret",
            "session_token": "fictional-session", "expires_at": (
                datetime.now(UTC) + timedelta(minutes=-1 if violation == "expired_broker" else 2)).isoformat()})

    transport.post.side_effect = broker

    async def verify(selected):
        return await verify_executor_work(selected, sessions, transport)

    async def authorize(run, claim, operation):
        selected = authority(run, claim, identity, connection)
        if violation == "foreign_product":
            selected["product_id"] = "other-product"
        elif violation == "foreign_project":
            selected["project_id"] = "other-project"
        elif violation == "stale_fence":
            selected["lease_fence"] += 1
        return {**selected, "access_key_id": "fictional-key", "secret_access_key": "fictional-secret",
                "session_token": "fictional-session",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat()}

    executor = ConsoleFinOpsExecutor(browser, verify,
                                     FinOpsExecutorEvidence("example-private-bucket", objects), SURFACE)
    billing_adapter = SimpleNamespace(collect_billing_page=AsyncMock(), read_billing_page=AsyncMock())
    adapter = FinOpsInquiryAdapter(executor, billing_adapter)
    if violation == "foreign_receipt":
        async def foreign_receipt(*, intent, authority):
            return {"attempt_token": intent["attempt_token"], "native_job_ref": {
                "tenant_id": intent["tenant_id"], "product_id": "other-product",
                "project_id": intent["project_id"], "agent_id": intent["agent_id"],
                "conversation_id": "conversation-example", "turn_locator": "receipt-example",
                "question_hash": intent["question_hash"]}}

        executor.submit = foreign_receipt
    service = ExecutionService(sessions, None, None, None, authorize, finops_adapter=adapter, service_id="finops")
    envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
    try:
        async with engine.begin() as connection_db:
            await connection_db.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Run(**scope, run_id=run_id,
                            service_id="security" if violation == "foreign_service" else "finops",
                            configuration_id="config-example", configuration_revision=1,
                            binding_id="binding-example", trigger_id="manual-example", binding_generation=1,
                            input={"capability": "cost_inquiry", "question": question, "query": query},
                            admission=admission, state="queued", manifest=manifest,
                            deadline_at=now + timedelta(hours=-1 if violation == "expired_run" else 1)))
            session.add(Work(tenant_id=scope["tenant_id"], work_id="work-example", run_id=run_id,
                             kind="submit", generation=1, step=1, due_at=now - timedelta(seconds=1)))
            await session.commit()
        if violation == "stale_delivery":
            envelope["generation"] = 2
        if violation in {"foreign_product", "foreign_project", "revoked_delegation",
                         "stale_fence", "expired_broker"}:
            with pytest.raises(PermissionError):
                await service.execute(envelope)
        else:
            assert await service.execute(envelope) == (violation not in {"foreign_service", "stale_delivery"})
        if violation == "duplicate_delivery":
            assert len(browser.sent) == 1 and len(objects.objects) == 1
            assert not await service.execute(envelope)
        else:
            assert browser.sent == [] and objects.objects == {}
            if violation in {"expired_run", "foreign_receipt"}:
                async with sessions() as session:
                    run = await session.get(Run, (scope["tenant_id"], run_id))
                    assert run.state == ("expired" if violation == "expired_run" else "outcome_unknown")
                    assert run.native_job_ref is None
        billing_adapter.collect_billing_page.assert_not_awaited()
    finally:
        await engine.dispose()
