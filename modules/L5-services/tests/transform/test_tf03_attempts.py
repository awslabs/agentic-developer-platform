"""SQL-owned Transform attempt identity under synthetic lost acknowledgements."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.dispatcher import dispatch_work, repair_owed_work
from l5_services.models import Configuration, ConfigurationVersion, Run, Work
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.base import Base
from l5_services.models.evidence import AssessmentResult, ProviderObservation
from l5_services.workers.execution import ExecutionService
from l5_services.work_store import (
    TRANSFORM_SUBMIT_OPERATION, claim_work, finish_work, prepare_submit_attempt,
)


NOW = datetime(2026, 1, 1, tzinfo=UTC)
SOURCE = {"tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": "repo",
          "record_type": "repository_commit", "record_id": "example/widget", "record_revision": "a" * 40}
SCOPE = {"tenant_id": "tenant-a", "project_id": "project-a", "product_id": "product-a"}
TEST_POLICY_REF = {"record_id": "policy-a"}
TEST_POLICY = {"test_policy_ref": TEST_POLICY_REF, "image_digest": "sha256:" + "b" * 64,
               "bundle_sha256": "c" * 64, "lock_sha256": "d" * 64, "oracle_version": "oracle-1",
               "commands": [{"argv": ["/oracle/check"], "cwd": ".", "environment": {},
                             "timeout_seconds": 10, "minimum_test_count": 1}]}
QUALIFIED = {"schema_version": "1.0", "interface": "synthetic", "cli_version": "synthetic",
             "binary_sha256": "a" * 64, "image_digest": "sha256:" + "b" * 64,
             "definition_revision": "synthetic", "region_qualification": "qualified",
             "executor_qualification": "qualified",
             **{name: {"status": "supported", "reason": "synthetic receipt", "receipt_sha256": "c" * 64}
                for name in ("tool_isolation", "submit", "read", "resume")},
             "cancel": {"status": "unsupported", "reason": "no remote stop"}}


@pytest.fixture
async def work_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'transform.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(Configuration(tenant_id="tenant-a", configuration_id="config-a", product_id="product-a",
                                  project_id="project-a", service_id="transform", relationship_revision="1",
                                  owner_id="owner-a", current_revision=1, handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=1,
                                         settings={}, inherited_fields=[], gaps=[], state="ready", hash="f" * 64))
        session.add(Run(tenant_id="tenant-a", run_id="run-a", product_id="product-a", project_id="project-a",
                        service_id="transform", configuration_id="config-a", configuration_revision=1,
                        binding_id="binding-a", trigger_id="manual-a", binding_generation=1,
                        input={"sources": [SOURCE], "transform_input": {"goal": {
                            "kind": "refactor", "summary": "Simplify"}}},
                        admission={"scope": SCOPE, "source_snapshots": [{"snapshot_id": "seal-a"}],
                                   "transform_settings": {"executor_binding_ref": SOURCE, "read_paths": ["src"],
                                                          "write_paths": ["src/app.py"], "excluded_paths": [".git"],
                                                          "test_policy_ref": TEST_POLICY_REF},
                                   "transform_test_policy": TEST_POLICY,
                                   "transform_qualification": QUALIFIED, "limits": {"max_polls": 2},
                                   "monitoring_policy": {"poll_interval_seconds": 60,
                                                         "poll_grace_seconds": 60}},
                        manifest={"scope": SCOPE, "sources": [SOURCE], "run_id": "run-a",
                                  "source_snapshot_ids": ["seal-a"]},
                        state="queued", deadline_at=NOW + timedelta(hours=1)))
        session.add(Work(tenant_id="tenant-a", work_id="submit-a", run_id="run-a", kind="submit",
                         generation=1, step=1, due_at=NOW))
        from l5_services.adapters.transform.baseline import manifest_digest, pinned_test_policy

        run = next(row for row in session.new if isinstance(row, Run))
        _, policy_sha256 = pinned_test_policy(run.admission)
        session.add(ProviderObservation(tenant_id="tenant-a", observation_id="baseline-fixture",
                                        run_id="run-a", work_id="submit-a", observed_at=NOW,
                                        payload={"kind": "transform_pre_submit_baseline", "schema_version": "1.0",
                                                 "manifest_sha256": manifest_digest(run.manifest),
                                                 "policy_sha256": policy_sha256, "source_tree": "fixture-tree",
                                                 "status": "passed", "checks": [{"stage": "before",
                                                                                 "status": "passed", "receipt": {}}]}))
        await session.commit()
    yield sessions
    await engine.dispose()


class LostAckBridge:
    def __init__(self, sessions):
        self.sessions = sessions
        self.launches = 0
        self.reads = 0
        self.receipt = None

    async def launch_provider(self, *, intent, manifest, authority):
        async with self.sessions() as session:
            saved = await session.get(WorkerAttempt, ("tenant-a", authority.attempt_id))
            assert saved.status == "prepared" and saved.idempotency_token == intent["attempt_token"]
        self.launches += 1
        self.receipt = {"executor_name": intent["executor_name"], "executor_uid": "uid-a",
                        "readback": {"native_id": "session-a", "attempt_token": intent["attempt_token"],
                                     "input_hash": intent["input_hash"], "state": "running"}}
        raise TimeoutError("synthetic acknowledgment lost after effect")

    async def read_provider(self, *, intent, authority):
        self.reads += 1
        return self.receipt


@pytest.mark.asyncio
async def test_same_sql_attempt_reconciles_without_second_provider_launch(work_db):
    bridge = LostAckBridge(work_db)
    adapter = TransformAdapter(bridge)
    original = await claim_work(work_db, "submit-a", 1, now=NOW)
    first, created = await prepare_submit_attempt(work_db, original, now=NOW)
    assert created
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        with pytest.raises(TimeoutError):
            await adapter.submit(run, run.manifest, first, first)
    assert bridge.launches == 1
    next_id = await finish_work(work_db, original, state="outcome_unknown", provider_state="unknown",
                                attempt_id=first.attempt_id, attempt_status="outcome_unknown",
                                next_kind="reconcile", now=NOW)
    assert next_id not in {None, "completed"}
    redelivery = await claim_work(work_db, next_id, 1, now=NOW)
    second, created = await prepare_submit_attempt(work_db, redelivery, now=NOW)
    assert not created
    assert (second.attempt_id, second.idempotency_token, second.input_hash) == (
        first.attempt_id, first.idempotency_token, first.input_hash)
    assert second.work_id == next_id and second.fence == redelivery.fence
    async with work_db() as session:
        saved = await session.get(WorkerAttempt, ("tenant-a", first.attempt_id))
        assert saved.operation == TRANSFORM_SUBMIT_OPERATION
        assert saved.reconcile_checks == 1 and saved.first_work_id == "submit-a"
        run = await session.get(Run, ("tenant-a", "run-a"))
        observed = await adapter.reconcile_submit(run, second, second)
    assert observed.classification == "accepted" and observed.native_job_ref["native_id"] == "session-a"
    assert bridge.launches == 1 and bridge.reads == 1
    assert await finish_work(work_db, original, state="failed", now=NOW) is None
    assert await finish_work(work_db, redelivery, state="running", provider_state="running",
                             native_job_ref=observed.native_job_ref, attempt_id=first.attempt_id,
                             attempt_status="accepted", now=NOW) == "completed"
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        saved = await session.get(WorkerAttempt, ("tenant-a", first.attempt_id))
        assert run.native_job_ref == saved.native_job_ref == observed.native_job_ref


@pytest.mark.asyncio
async def test_missing_readback_never_grants_replacement_submit(work_db):
    bridge = LostAckBridge(work_db)
    adapter = TransformAdapter(bridge)
    original = await claim_work(work_db, "submit-a", 1, now=NOW)
    first, created = await prepare_submit_attempt(work_db, original, now=NOW)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        with pytest.raises(TimeoutError):
            await adapter.submit(run, run.manifest, first, first)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        bridge.receipt = None
        assert (await adapter.reconcile_submit(run, first, first)).classification == "unknown"
        async def missing(*, intent, authority):
            raise HTTPException(404, detail="missing pod")
        bridge.read_provider = missing
        assert (await adapter.reconcile_submit(run, first, first)).classification == "unknown"
    second, created = await prepare_submit_attempt(work_db, original, now=NOW)
    assert not created and second.can_reconcile and second.attempt_id == first.attempt_id
    third, created = await prepare_submit_attempt(work_db, original, now=NOW)
    assert not created and third.can_reconcile and third.attempt_id == first.attempt_id
    exhausted, created = await prepare_submit_attempt(work_db, original, now=NOW)
    assert not created and not exhausted.can_reconcile
    assert exhausted.idempotency_token == first.idempotency_token and bridge.launches == 1


@pytest.mark.asyncio
async def test_worker_redelivery_reads_original_attempt_without_new_submit(work_db):
    bridge = LostAckBridge(work_db)
    adapter = TransformAdapter(bridge)
    original = await claim_work(work_db, "submit-a", 1, now=NOW)
    attempt, created = await prepare_submit_attempt(work_db, original, now=NOW)
    assert created
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        with pytest.raises(TimeoutError):
            await adapter.submit(run, run.manifest, attempt, attempt)
    published = []

    async def publish(envelope, delay):
        published.append(envelope)

    async def authorize(run, claim, operation):
        assert operation == "reconcile"
        return object()

    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=adapter, clock=lambda: NOW + timedelta(seconds=61))
    assert await worker.execute({"schema_version": "1.0", "work_id": "submit-a", "generation": 1})
    assert bridge.launches == 1 and bridge.reads == 1
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        saved = await session.get(WorkerAttempt, ("tenant-a", attempt.attempt_id))
        owed = (await session.scalars(select(Work).where(
            Work.tenant_id == "tenant-a", Work.run_id == "run-a", Work.kind == "poll"))).one()
        assert (run.state, run.provider_state) == ("running", "running")
        assert run.native_job_ref == saved.native_job_ref
        assert saved.attempt_id == attempt.attempt_id and saved.reconcile_checks == 1
        assert published == [{"schema_version": "1.0", "work_id": owed.work_id, "generation": 1}]
    assert await finish_work(work_db, original, state="failed", now=NOW + timedelta(seconds=61)) is None


@pytest.mark.asyncio
async def test_recovery_republishes_owed_original_work_after_send_failure(work_db):
    bridge = LostAckBridge(work_db)
    adapter = TransformAdapter(bridge)
    original = await claim_work(work_db, "submit-a", 1, now=NOW)
    attempt, _ = await prepare_submit_attempt(work_db, original, now=NOW)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        with pytest.raises(TimeoutError):
            await adapter.submit(run, run.manifest, attempt, attempt)

    async def fail_publish(envelope, delay):
        raise ConnectionError("synthetic send failed")

    recovery_at = NOW + timedelta(seconds=61)
    assert not await dispatch_work(work_db, "tenant-a", "submit-a", fail_publish, now=recovery_at)
    async with work_db() as session:
        work = await session.get(Work, ("tenant-a", "submit-a"))
        assert work.completed_at is None and work.last_error == "publication_failed"
    published = []

    async def publish(envelope, delay):
        published.append(envelope)

    assert await repair_owed_work(work_db, publish, now=recovery_at) == 1
    assert published == [{"schema_version": "1.0", "work_id": "submit-a", "generation": 1}]
    async def authorize(run, claim, operation):
        assert operation == "reconcile"
        return object()

    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=adapter, clock=lambda: recovery_at)
    assert await worker.execute(published[0])
    assert bridge.launches == 1 and bridge.reads == 1
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        assert run.native_job_ref["native_id"] == "session-a"


@pytest.mark.asyncio
async def test_unavailable_bridge_leaves_committed_attempt_owed(work_db):
    original = await claim_work(work_db, "submit-a", 1, now=NOW)
    attempt, created = await prepare_submit_attempt(work_db, original, now=NOW)
    assert created

    async def publish(envelope, delay):
        raise AssertionError("no successor can be published without recovery")

    async def authorize(run, claim, operation):
        raise AssertionError("no authority may be requested without a qualified bridge")

    recovery_at = NOW + timedelta(seconds=61)
    worker = ExecutionService(work_db, None, object(), publish, authorize, clock=lambda: recovery_at)
    with pytest.raises(RuntimeError, match="recovery bridge is unavailable"):
        await worker.execute({"schema_version": "1.0", "work_id": "submit-a", "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        saved = await session.get(WorkerAttempt, ("tenant-a", attempt.attempt_id))
        work = await session.get(Work, ("tenant-a", "submit-a"))
        assert run.state == "queued" and run.native_job_ref is None
        assert saved.status == "prepared" and work.completed_at is None

@pytest.mark.asyncio
async def test_cancel_is_local_and_does_not_claim_native_stop(work_db):
    original = await claim_work(work_db, "submit-a", 1, now=NOW)
    attempt, _ = await prepare_submit_attempt(work_db, original, now=NOW)
    native = {"tenant_id": "tenant-a", "project_id": "project-a", "native_id": "session-a",
              "executor_name": "tf-example", "executor_uid": "uid-a",
              "attempt_token": attempt.idempotency_token, "input_hash": attempt.input_hash}
    cancel_id = await finish_work(work_db, original, state="cancel_requested", provider_state="running",
                                  native_job_ref=native, attempt_id=attempt.attempt_id,
                                  attempt_status="accepted", next_kind="cancel", now=NOW)
    assert cancel_id not in {None, "completed"}
    published = []

    async def publish(envelope, delay):
        published.append(envelope)

    async def authorize(run, claim, operation):
        raise AssertionError("unsupported remote cancel must not request provider authority")

    worker = ExecutionService(work_db, None, object(), publish, authorize, clock=lambda: NOW)
    assert await worker.execute({"schema_version": "1.0", "work_id": cancel_id, "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        owed = (await session.scalars(select(Work).where(
            Work.tenant_id == "tenant-a", Work.run_id == "run-a", Work.kind == "poll"))).one()
        assert run.state == "cancelled" and run.provider_state == "running"
        assert run.native_job_ref == native and run.cancel_reason is None
        assert owed.completed_at is None
        assert published == [{"schema_version": "1.0", "work_id": owed.work_id, "generation": 1}]


@pytest.mark.asyncio
async def test_expiry_keeps_unknown_native_state_and_same_recovery_attempt(work_db):
    original = await claim_work(work_db, "submit-a", 1, now=NOW)
    attempt, _ = await prepare_submit_attempt(work_db, original, now=NOW)
    expiry = NOW + timedelta(hours=1, seconds=1)
    reconcile_id = await finish_work(work_db, original, state="outcome_unknown", provider_state="unknown",
                                     attempt_id=attempt.attempt_id, attempt_status="outcome_unknown",
                                     next_kind="reconcile", due_at=expiry, now=NOW)
    published = []

    async def publish(envelope, delay):
        published.append(envelope)

    async def authorize(run, claim, operation):
        raise AssertionError("local expiry must not imply a provider operation")

    worker = ExecutionService(work_db, None, object(), publish, authorize, clock=lambda: expiry)
    assert await worker.execute({"schema_version": "1.0", "work_id": reconcile_id, "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        saved = await session.get(WorkerAttempt, ("tenant-a", attempt.attempt_id))
        owed = (await session.scalars(select(Work).where(
            Work.tenant_id == "tenant-a", Work.run_id == "run-a", Work.kind == "reconcile",
            Work.completed_at.is_(None)))).one()
        assert run.state == "expired" and run.provider_state == "unknown"
        assert run.native_job_ref is None and saved.attempt_id == attempt.attempt_id
        assert saved.reconcile_checks == 0 and owed.completed_at is None
        assert published == [{"schema_version": "1.0", "work_id": owed.work_id, "generation": 1}]


async def accepted_poll(work_db):
    from l5_services.adapters.transform.baseline import manifest_digest, pinned_test_policy

    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        baseline = await session.get(ProviderObservation, ("tenant-a", "baseline-fixture"))
        _, policy_sha256 = pinned_test_policy(run.admission)
        baseline.payload = {**baseline.payload, "manifest_sha256": manifest_digest(run.manifest),
                            "policy_sha256": policy_sha256}
        await session.commit()
    original = await claim_work(work_db, "submit-a", 1, now=NOW)
    attempt, _ = await prepare_submit_attempt(work_db, original, now=NOW)
    native = {"tenant_id": "tenant-a", "project_id": "project-a", "native_id": "session-a",
              "executor_name": "tf-" + sha256(
                  f"tenant-a:run-a:{attempt.idempotency_token}".encode()).hexdigest()[:40],
              "executor_uid": "uid-a",
              "attempt_token": attempt.idempotency_token, "input_hash": attempt.input_hash}
    poll_id = await finish_work(work_db, original, state="running", provider_state="running",
                                native_job_ref=native, attempt_id=attempt.attempt_id,
                                attempt_status="accepted", next_kind="poll", now=NOW)
    return native, poll_id


class ObservationBridge:
    def __init__(self, native):
        self.native = native
        self.reads = 0
        self.collections = 0

    async def launch_provider(self, **kwargs):
        raise AssertionError("recovery must not submit")

    async def request_stop(self, **kwargs):
        raise AssertionError("local cancellation must not claim native stop")

    async def read_provider(self, *, intent, authority):
        self.reads += 1
        assert intent["attempt_token"] == self.native["attempt_token"]
        return {"executor_name": intent["executor_name"], "executor_uid": self.native["executor_uid"],
                "readback": {"native_id": self.native["native_id"],
                             "attempt_token": intent["attempt_token"],
                             "input_hash": intent["input_hash"], "state": "completed"}}

    async def collect_provider(self, *, intent, authority):
        self.collections += 1
        return {"native_job_ref": self.native, "state": "completed",
                "artifacts": {"patch_ref": "private-artifact-a"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", ["running", "unknown"])
async def test_exhausted_polls_retain_deadline_work_without_provider_reads(work_db, readback):
    native, poll_id = await accepted_poll(work_db)
    bridge = ObservationBridge(native)
    original_read = bridge.read_provider

    async def read_provider(*, intent, authority):
        receipt = await original_read(intent=intent, authority=authority)
        receipt["readback"]["state"] = readback
        return receipt

    bridge.read_provider = read_provider
    published = []
    operations = []

    async def authorize(run, claim, operation):
        operations.append(operation)
        return object()

    async def publish(envelope, delay):
        published.append(envelope)

    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        admission = run.admission
    deadline = NOW + timedelta(hours=1)
    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge), clock=lambda: NOW + timedelta(seconds=1))
    assert await worker.execute({"schema_version": "1.0", "work_id": poll_id, "generation": 1})
    worker.clock = lambda: NOW + timedelta(seconds=62)
    assert await worker.execute(published.pop())
    worker.clock = lambda: NOW + timedelta(seconds=123)
    assert await worker.execute(published.pop())
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        owed = (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).one()
        assert owed.kind == "poll" and owed.due_at.replace(tzinfo=UTC) == deadline
        assert owed.last_enqueued_at is None
        assert (run.state, run.provider_state) == ("running", "running")
        assert run.native_job_ref == native
        assert run.monitoring["poll_attempts"] == 2
        assert run.monitoring["last_poll_error"] == {"code": "poll_limit_reached"}
    assert bridge.reads == 2 and operations == ["poll", "poll"]
    recovery_at = deadline + timedelta(seconds=1)
    assert await repair_owed_work(work_db, publish, now=recovery_at) == 1
    worker.clock = lambda: recovery_at
    assert await worker.execute(published.pop())
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        assert (run.state, run.provider_state) == ("expired", "running")
        assert run.monitoring["poll_attempts"] == 2
    assert await worker.execute(published.pop())
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        attempt = (await session.scalars(select(WorkerAttempt))).one()
        assert (run.state, run.provider_state) == ("expired", "running")
        assert run.native_job_ref == attempt.native_job_ref == native
        assert run.deadline_at.replace(tzinfo=UTC) == deadline and run.admission == admission
        assert run.monitoring["poll_attempts"] == admission["limits"]["max_polls"] == 2
        assert run.monitoring["last_poll_error"] == {"code": "poll_limit_reached"}
        assert not (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).all()
    assert bridge.reads == 2 and bridge.collections == 0
    assert operations == ["poll", "poll"] and not published
    assert await repair_owed_work(work_db, publish, now=deadline + timedelta(seconds=61)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", ["running", "unknown"])
@pytest.mark.parametrize("seconds_before_deadline", [60, 10])
async def test_poll_at_deadline_retains_expiry_and_sweep_recovery(
        work_db, readback, seconds_before_deadline):
    native, poll_id = await accepted_poll(work_db)
    bridge = ObservationBridge(native)
    original_read = bridge.read_provider

    async def read_provider(*, intent, authority):
        receipt = await original_read(intent=intent, authority=authority)
        if bridge.reads == 1:
            receipt["readback"]["state"] = readback
        return receipt

    bridge.read_provider = read_provider
    published = []
    operations = []

    async def authorize(run, claim, operation):
        operations.append(operation)
        return object()

    async def fail_publish(envelope, delay):
        raise ConnectionError("synthetic send failed")

    async def publish(envelope, delay):
        published.append(envelope)

    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        admission = {**run.admission, "limits": {"max_polls": 3}}
        run.admission = admission
        await session.commit()
    deadline = NOW + timedelta(hours=1)
    worker = ExecutionService(work_db, None, object(), fail_publish, authorize,
                              transform_adapter=TransformAdapter(bridge),
                              clock=lambda: deadline - timedelta(seconds=seconds_before_deadline))
    assert await worker.execute({"schema_version": "1.0", "work_id": poll_id, "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        owed = (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).one()
        assert owed.kind == "poll" and owed.due_at.replace(tzinfo=UTC) == deadline
        assert owed.last_error == "publication_failed"
        assert (run.state, run.provider_state) == ("running", "running")
        assert run.monitoring["poll_attempts"] == 1
    recovery_at = deadline + timedelta(seconds=1)
    assert await repair_owed_work(work_db, publish, now=recovery_at) == 1
    worker.publish = publish
    worker.clock = lambda: recovery_at
    assert await worker.execute(published.pop())
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        assert (run.state, run.provider_state) == ("expired", "running")
        assert run.monitoring["state"] == "recovery_pending"
        assert run.monitoring["poll_attempts"] == 1
    assert operations == ["poll"] and bridge.reads == 1
    assert await worker.execute(published.pop())
    worker.clock = lambda: recovery_at + timedelta(seconds=1)
    assert await worker.execute(published.pop())
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        observations = (await session.scalars(select(ProviderObservation))).all()
        attempt = (await session.scalars(select(WorkerAttempt))).one()
        assert (run.state, run.provider_state) == ("expired", "completed")
        assert run.native_job_ref == attempt.native_job_ref == native
        assert run.deadline_at.replace(tzinfo=UTC) == deadline and run.admission == admission
        assert run.monitoring["poll_attempts"] == 3
        assert any(observation.payload.get("artifact_refs") == {"patch_ref": "private-artifact-a"}
                   for observation in observations)
        assert not (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).all()
    assert operations == ["poll", "recover_observe", "recover_collect"]
    assert bridge.reads == 2 and bridge.collections == 1 and not published


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "connection", "transport", "unknown"])
@pytest.mark.parametrize("retry_window", ["active", "cancelled", "deadline"])
async def test_failed_collection_retries_same_result_without_poll(work_db, failure, retry_window):
    native, poll_id = await accepted_poll(work_db)
    bridge = ObservationBridge(native)
    original_collect = bridge.collect_provider

    async def collect_provider(*, intent, authority):
        assert intent["attempt_token"] == native["attempt_token"]
        assert intent["input_hash"] == native["input_hash"]
        if bridge.collections == 0:
            bridge.collections += 1
            if failure == "timeout":
                raise TimeoutError("synthetic lost collection response")
            if failure == "connection":
                raise ConnectionError("synthetic collection connection failure")
            if failure == "transport":
                raise httpx.ConnectError("synthetic collection transport failure")
            return {"native_job_ref": native, "state": "unknown"}
        return await original_collect(intent=intent, authority=authority)

    bridge.collect_provider = collect_provider
    published = []
    operations = []

    async def authorize(run, claim, operation):
        operations.append(operation)
        return object()

    async def publish(envelope, delay):
        published.append(envelope)

    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        admission = {**run.admission, "limits": {"max_polls": 3}}
        run.admission = admission
        if retry_window == "cancelled":
            run.state = "cancelled"
        await session.commit()
    deadline = NOW + timedelta(hours=1)
    start_at = deadline - timedelta(seconds=10) if retry_window == "deadline" else NOW
    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge), clock=lambda: start_at)
    assert await worker.execute({"schema_version": "1.0", "work_id": poll_id, "generation": 1})
    worker.clock = lambda: start_at + timedelta(seconds=1)
    assert await worker.execute(published.pop())
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        owed = (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).one()
        retry_at = owed.due_at.replace(tzinfo=UTC)
        assert owed.kind == "collect"
        assert retry_at == min(start_at + timedelta(seconds=61), deadline)
        assert run.provider_state == "completed" and run.native_job_ref == native
        assert run.monitoring["poll_attempts"] == 2
    worker.clock = lambda: retry_at
    assert await worker.execute(published.pop())
    if retry_window == "deadline":
        async with work_db() as session:
            run = await session.get(Run, ("tenant-a", "run-a"))
            owed = (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).one()
            assert owed.kind == "collect"
            assert (run.state, run.provider_state) == ("expired", "completed")
            assert run.monitoring["poll_attempts"] == 2
        worker.clock = lambda: retry_at + timedelta(seconds=1)
        assert await worker.execute(published.pop())
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        observations = (await session.scalars(select(ProviderObservation))).all()
        attempt = (await session.scalars(select(WorkerAttempt))).one()
        assert run.state == {"active": "collecting", "cancelled": "cancelled", "deadline": "expired"}[retry_window]
        assert run.provider_state == "completed" and run.monitoring["poll_attempts"] == 3
        assert run.native_job_ref == attempt.native_job_ref == native
        assert run.deadline_at.replace(tzinfo=UTC) == deadline and run.admission == admission
        assert any(observation.payload.get("artifact_refs") == {"patch_ref": "private-artifact-a"}
                   for observation in observations)
        assert not (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).all()
        assert not (await session.scalars(select(AssessmentResult))).all()
    assert operations == {"active": ["poll", "collect", "collect"],
                          "cancelled": ["recover_observe", "recover_collect", "recover_collect"],
                          "deadline": ["poll", "collect", "recover_collect"]}[retry_window]
    assert bridge.reads == 1 and bridge.collections == 2 and not published


@pytest.mark.asyncio
async def test_late_completion_is_observed_and_collected_after_local_cancel(work_db):
    native, poll_id = await accepted_poll(work_db)
    bridge = ObservationBridge(native)
    operations = []
    published = []

    async def authorize(run, claim, operation):
        operations.append(operation)
        return object()

    async def publish(envelope, delay):
        published.append(envelope)

    cancel_at = NOW + timedelta(seconds=1)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        run.state = "cancelled"
        await session.commit()
    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge), clock=lambda: cancel_at)
    assert await worker.execute({"schema_version": "1.0", "work_id": poll_id, "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        collect = (await session.scalars(select(Work).where(
            Work.tenant_id == "tenant-a", Work.run_id == "run-a", Work.kind == "collect"))).one()
        assert run.state == "cancelled" and run.provider_state == "completed"
        assert run.native_job_ref == native and run.monitoring["state"] == "recovery_pending"
        assert published == [{"schema_version": "1.0", "work_id": collect.work_id, "generation": 1}]
    worker.clock = lambda: cancel_at + timedelta(seconds=1)
    assert await worker.execute(published.pop())
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        observations = (await session.scalars(select(ProviderObservation).where(
            ProviderObservation.tenant_id == "tenant-a", ProviderObservation.run_id == "run-a").order_by(ProviderObservation.observed_at))).all()
        results = (await session.scalars(select(AssessmentResult).where(
            AssessmentResult.tenant_id == "tenant-a", AssessmentResult.run_id == "run-a"))).all()
        assert run.state == "cancelled" and run.provider_state == "completed"
        assert run.native_job_ref == native and run.monitoring["poll_attempts"] == 2
        assert [observation.payload["outcome"] for observation in observations
                if observation.payload.get("kind") != "transform_pre_submit_baseline"] == ["completed", "completed"]
        assert observations[-1].payload["artifact_refs"] == {"patch_ref": "private-artifact-a"}
        assert not results and not published
    assert operations == ["recover_observe", "recover_collect"]
    assert bridge.reads == 1 and bridge.collections == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", ["missing", "stale", "transport"])
async def test_missing_native_readback_is_unknown_and_polling_is_bounded(work_db, readback):
    native, poll_id = await accepted_poll(work_db)
    bridge = ObservationBridge(native)

    async def missing(*, intent, authority):
        bridge.reads += 1
        if readback == "transport":
            raise httpx.ConnectError("synthetic native read transport failure")
        if readback == "missing":
            raise HTTPException(404, detail="native session not found")
        return {"executor_name": intent["executor_name"], "executor_uid": native["executor_uid"],
                "readback": {"native_id": "another-session", "attempt_token": intent["attempt_token"],
                             "input_hash": intent["input_hash"], "state": "completed"}}

    bridge.read_provider = missing
    published = []
    operations = []

    async def authorize(run, claim, operation):
        operations.append(operation)
        return object()

    async def publish(envelope, delay):
        published.append(envelope)

    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        run.state = "cancelled"
        await session.commit()
    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge), clock=lambda: NOW + timedelta(seconds=1))
    assert await worker.execute({"schema_version": "1.0", "work_id": poll_id, "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        assert (run.state, run.provider_state) == ("cancelled", "running")
        assert run.monitoring["last_poll_error"] == {"code": "native_readback_unavailable"}
    assert not await worker.execute({"schema_version": "1.0", "work_id": poll_id, "generation": 1})
    worker.clock = lambda: NOW + timedelta(seconds=62)
    assert await worker.execute(published.pop())
    worker.clock = lambda: NOW + timedelta(seconds=123)
    assert await worker.execute(published.pop())
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        assert run.state == "cancelled" and run.provider_state == "running"
        assert run.monitoring["poll_attempts"] == 2
        assert run.monitoring["last_poll_error"] == {"code": "poll_limit_reached"}
    assert bridge.reads == 2 and bridge.collections == 0
    assert operations == ["recover_observe", "recover_observe"] and not published


@pytest.mark.asyncio
async def test_expired_run_reads_late_native_completion_without_resubmitting(work_db):
    native, poll_id = await accepted_poll(work_db)
    bridge = ObservationBridge(native)
    published = []
    operations = []

    async def authorize(run, claim, operation):
        operations.append(operation)
        return object()

    async def publish(envelope, delay):
        published.append(envelope)

    deadline = NOW + timedelta(hours=1)
    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge), clock=lambda: deadline)
    assert await worker.execute({"schema_version": "1.0", "work_id": poll_id, "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        assert run.state == "expired" and run.provider_state == "running"
        assert run.native_job_ref == native and run.monitoring["state"] == "recovery_pending"
    worker.clock = lambda: deadline + timedelta(seconds=1)
    assert await worker.execute(published.pop())
    worker.clock = lambda: deadline + timedelta(seconds=2)
    assert await worker.execute(published.pop())
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        observations = (await session.scalars(select(ProviderObservation).where(
            ProviderObservation.tenant_id == "tenant-a", ProviderObservation.run_id == "run-a"))).all()
        assert run.state == "expired" and run.provider_state == "completed"
        assert run.native_job_ref == native and run.monitoring["poll_attempts"] == 2
        assert any(observation.payload.get("artifact_refs") == {"patch_ref": "private-artifact-a"}
                   for observation in observations)
    assert bridge.reads == 1 and bridge.collections == 1
    assert operations == ["recover_observe", "recover_collect"] and not published


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["delegation_revoked", "allowance_exhausted"])
async def test_recovery_denial_preserves_native_and_local_state(work_db, denial):
    native, poll_id = await accepted_poll(work_db)
    bridge = ObservationBridge(native)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        run.state = "cancelled"
        await session.commit()

    async def deny(run, claim, operation):
        assert operation == "recover_observe"
        raise HTTPException(403, detail=denial)

    async def publish(envelope, delay):
        raise AssertionError("denial cannot publish a successor")

    worker = ExecutionService(work_db, None, object(), publish, deny,
                              transform_adapter=TransformAdapter(bridge),
                              clock=lambda: NOW + timedelta(seconds=1))
    assert await worker.execute({"schema_version": "1.0", "work_id": poll_id, "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        work = await session.get(Work, ("tenant-a", poll_id))
        observations = (await session.scalars(select(ProviderObservation).where(
            ProviderObservation.tenant_id == "tenant-a", ProviderObservation.run_id == "run-a"))).all()
        assert (run.state, run.provider_state, run.native_job_ref) == ("cancelled", "running", native)
        assert run.monitoring["last_poll_error"] == {"code": "recovery_authority_unavailable"}
        assert work.completed_at is not None and not [observation for observation in observations
                                                     if observation.payload.get("kind") != "transform_pre_submit_baseline"]
    assert bridge.reads == bridge.collections == 0


@pytest.mark.asyncio
async def test_stale_lease_after_authorization_cannot_read_or_settle(work_db):
    native, poll_id = await accepted_poll(work_db)
    bridge = ObservationBridge(native)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        run.state = "cancelled"
        await session.commit()

    async def stale_authorize(run, claim, operation):
        assert operation == "recover_observe"
        async with work_db() as session:
            work = await session.get(Work, ("tenant-a", claim.work_id))
            work.fence += 1
            work.lease_owner = "successor-owner"
            await session.commit()
        return object()

    async def publish(envelope, delay):
        raise AssertionError("stale owner cannot publish a successor")

    worker = ExecutionService(work_db, None, object(), publish, stale_authorize,
                              transform_adapter=TransformAdapter(bridge),
                              clock=lambda: NOW + timedelta(seconds=1))
    assert not await worker.execute({"schema_version": "1.0", "work_id": poll_id, "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        work = await session.get(Work, ("tenant-a", poll_id))
        observations = (await session.scalars(select(ProviderObservation).where(
            ProviderObservation.tenant_id == "tenant-a", ProviderObservation.run_id == "run-a"))).all()
        assert (run.state, run.provider_state, run.native_job_ref) == ("cancelled", "running", native)
        assert work.lease_owner == "successor-owner" and work.completed_at is None
        assert run.monitoring["poll_attempts"] == 1 and not [observation for observation in observations
                                                             if observation.payload.get("kind") != "transform_pre_submit_baseline"]
    assert bridge.reads == bridge.collections == 0


@pytest.mark.asyncio
async def test_provider_denial_does_not_erase_native_status(work_db):
    native, poll_id = await accepted_poll(work_db)
    bridge = ObservationBridge(native)

    async def denied_read(*, intent, authority):
        raise HTTPException(403, detail="delegation no longer permits readback")

    bridge.read_provider = denied_read
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        run.state = "expired"
        await session.commit()

    async def authorize(run, claim, operation):
        assert operation == "recover_observe"
        return object()

    async def publish(envelope, delay):
        raise AssertionError("denied readback cannot schedule unscoped work")

    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge),
                              clock=lambda: NOW + timedelta(hours=1, seconds=1))
    assert await worker.execute({"schema_version": "1.0", "work_id": poll_id, "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        observations = (await session.scalars(select(ProviderObservation).where(
            ProviderObservation.tenant_id == "tenant-a", ProviderObservation.run_id == "run-a"))).all()
        assert (run.state, run.provider_state, run.native_job_ref) == ("expired", "running", native)
        assert run.monitoring["last_poll_error"] == {"code": "recovery_authority_unavailable"}
        assert not [observation for observation in observations
                    if observation.payload.get("kind") != "transform_pre_submit_baseline"]
    assert bridge.collections == 0


@pytest.mark.asyncio
async def test_stale_reconciliation_fence_cannot_read_original_attempt(work_db):
    original = await claim_work(work_db, "submit-a", 1, now=NOW)
    attempt, _ = await prepare_submit_attempt(work_db, original, now=NOW)
    next_id = await finish_work(work_db, original, state="outcome_unknown", provider_state="unknown",
                                attempt_id=attempt.attempt_id, attempt_status="outcome_unknown",
                                next_kind="reconcile", now=NOW)
    bridge = LostAckBridge(work_db)

    async def stale_authorize(run, claim, operation):
        assert operation == "reconcile"
        async with work_db() as session:
            work = await session.get(Work, ("tenant-a", claim.work_id))
            work.fence += 1
            work.lease_owner = "successor-owner"
            await session.commit()
        return object()

    async def publish(envelope, delay):
        raise AssertionError("stale attempt cannot schedule work")

    worker = ExecutionService(work_db, None, object(), publish, stale_authorize,
                              transform_adapter=TransformAdapter(bridge), clock=lambda: NOW)
    assert not await worker.execute({"schema_version": "1.0", "work_id": next_id, "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        work = await session.get(Work, ("tenant-a", next_id))
        saved = await session.get(WorkerAttempt, ("tenant-a", attempt.attempt_id))
        assert (run.state, run.provider_state) == ("outcome_unknown", "unknown")
        assert work.lease_owner == "successor-owner" and work.completed_at is None
        assert saved.attempt_id == attempt.attempt_id and saved.native_job_ref is None
    assert bridge.launches == bridge.reads == 0


@pytest.mark.asyncio
async def test_reconcile_readback_denial_does_not_replace_original_attempt(work_db):
    original = await claim_work(work_db, "submit-a", 1, now=NOW)
    attempt, _ = await prepare_submit_attempt(work_db, original, now=NOW)
    next_id = await finish_work(work_db, original, state="outcome_unknown", provider_state="unknown",
                                attempt_id=attempt.attempt_id, attempt_status="outcome_unknown",
                                next_kind="reconcile", now=NOW)
    bridge = LostAckBridge(work_db)

    async def denied_read(*, intent, authority):
        raise HTTPException(403, detail="recovery scope revoked")

    bridge.read_provider = denied_read

    async def authorize(run, claim, operation):
        assert operation == "reconcile"
        return object()

    async def publish(envelope, delay):
        raise AssertionError("denied readback cannot publish")

    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge), clock=lambda: NOW)
    assert await worker.execute({"schema_version": "1.0", "work_id": next_id, "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        saved = await session.get(WorkerAttempt, ("tenant-a", attempt.attempt_id))
        assert run.state == "outcome_unknown" and run.provider_state == "unknown"
        assert run.native_job_ref is None and saved.attempt_id == attempt.attempt_id
        assert run.monitoring["last_poll_error"] == {"code": "recovery_authority_unavailable"}
    assert bridge.launches == 0


@pytest.mark.asyncio
async def test_reconcile_transport_outage_preserves_original_paid_attempt(work_db):
    original = await claim_work(work_db, "submit-a", 1, now=NOW)
    attempt, _ = await prepare_submit_attempt(work_db, original, now=NOW)
    next_id = await finish_work(work_db, original, state="outcome_unknown", provider_state="unknown",
                                attempt_id=attempt.attempt_id, attempt_status="outcome_unknown",
                                next_kind="reconcile", now=NOW)
    bridge = LostAckBridge(work_db)

    async def unavailable(*, intent, authority):
        raise httpx.ConnectError("synthetic native read transport outage")

    bridge.read_provider = unavailable
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        run.state = "cancelled"
        await session.commit()

    async def authorize(run, claim, operation):
        assert operation == "recover_reconcile"
        return object()

    published = []

    async def publish(envelope, delay):
        published.append(envelope)

    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge), clock=lambda: NOW)
    assert await worker.execute({"schema_version": "1.0", "work_id": next_id, "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        saved = await session.get(WorkerAttempt, ("tenant-a", attempt.attempt_id))
        assert run.state == "cancelled" and run.native_job_ref is None
        assert run.monitoring["last_poll_error"] == {"code": "native_readback_unavailable"}
        assert saved.idempotency_token == attempt.idempotency_token
        assert saved.native_job_ref is None
    assert bridge.launches == 0 and len(published) == 1
