"""SCN-02: durable recovery after an ambiguous native acknowledgment."""

import json
from datetime import timedelta
from hashlib import sha256

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.admission import cancel_and_dispatch
from l5_services.dispatcher import dispatch_work, repair_owed_work
from l5_services.models import Run, Work
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.evidence import AssessmentResult, ProviderObservation
from l5_services.models.receipt import RunReceipt
from l5_services.run_access import RunStore
from l5_services.work_store import claim_work, finish_work, prepare_submit_attempt
from l5_services.workers.execution import ExecutionService

from test_tf03_attempts import NOW, LostAckBridge, ObservationBridge, accepted_poll, work_db


class LostAcknowledgment(LostAckBridge):
    async def read_provider(self, *, intent, authority):
        self.reads += 1
        if self.reads == 1:
            raise HTTPException(404, detail="session not yet readable")
        return self.receipt


@pytest.mark.asyncio
async def test_lost_ack_recovery(work_db):
    bridge = LostAcknowledgment(work_db)
    original = await claim_work(work_db, "submit-a", 1, now=NOW)
    attempt, created = await prepare_submit_attempt(work_db, original, now=NOW)
    assert created
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        pinned_hash = sha256(json.dumps(run.manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert attempt.input_hash == pinned_hash
        with pytest.raises(TimeoutError):
            await TransformAdapter(bridge).submit(run, run.manifest, attempt, attempt)
    assert bridge.launches == 1

    async def lost_queue_ack(envelope, delay):
        raise ConnectionError("queue acknowledgment lost")

    now = NOW + timedelta(seconds=61)
    assert not await dispatch_work(work_db, "tenant-a", "submit-a", lost_queue_ack, now=now)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        work = await session.get(Work, ("tenant-a", "submit-a"))
        saved = await session.get(WorkerAttempt, ("tenant-a", attempt.attempt_id))
        assert run.state == "queued" and run.native_job_ref is None
        assert work.completed_at is None and work.last_error == "publication_failed"
        assert saved.status == "prepared" and saved.input_hash == pinned_hash

    sent = []

    async def publish(envelope, delay):
        assert set(envelope) == {"schema_version", "work_id", "generation"}
        sent.append((envelope, delay))

    assert await repair_owed_work(work_db, publish, now=now) == 1
    message, _ = sent.pop(0)
    assert message == {"schema_version": "1.0", "work_id": "submit-a", "generation": 1}
    operations = []

    async def authorize(run, claim, operation):
        operations.append(operation)
        assert operation == "reconcile"
        return object()

    first = ExecutionService(work_db, None, object(), publish, authorize,
                             transform_adapter=TransformAdapter(bridge), clock=lambda: now)
    assert await first.execute(message)
    assert bridge.launches == 1 and bridge.reads == 1
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        saved = await session.get(WorkerAttempt, ("tenant-a", attempt.attempt_id))
        assert run.state == "outcome_unknown" and run.provider_state == "unknown"
        assert run.native_job_ref is None and saved.attempt_id == attempt.attempt_id
        assert saved.reconcile_checks == 1
    retry, delay = sent.pop(0)
    assert delay == 60
    assert not await first.execute(retry)

    restarted = ExecutionService(work_db, None, object(), publish, authorize,
                                 transform_adapter=TransformAdapter(bridge),
                                 clock=lambda: now + timedelta(seconds=60))
    assert await restarted.execute(retry)
    assert bridge.launches == 1 and bridge.reads == 2
    assert not await restarted.execute(message)
    assert await finish_work(work_db, original, state="failed", now=now + timedelta(seconds=60)) is None
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        attempts = (await session.scalars(select(WorkerAttempt).where(
            WorkerAttempt.tenant_id == "tenant-a", WorkerAttempt.run_id == "run-a"))).all()
        works = (await session.scalars(select(Work).where(
            Work.tenant_id == "tenant-a", Work.run_id == "run-a"))).all()
        observations = (await session.scalars(select(ProviderObservation).where(
            ProviderObservation.tenant_id == "tenant-a", ProviderObservation.run_id == "run-a"))).all()
        observations = [observation for observation in observations
                        if observation.payload.get("kind") != "transform_pre_submit_baseline"]
        results = (await session.scalars(select(AssessmentResult).where(
            AssessmentResult.tenant_id == "tenant-a", AssessmentResult.run_id == "run-a"))).all()
        assert (run.state, run.provider_state) == ("running", "running")
        assert len(attempts) == 1 and attempts[0].attempt_id == attempt.attempt_id
        assert attempts[0].input_hash == run.native_job_ref["input_hash"] == pinned_hash
        assert attempts[0].native_job_ref == run.native_job_ref
        assert run.native_job_ref["native_id"] == bridge.receipt["readback"]["native_id"]
        assert run.native_job_ref["executor_uid"] == bridge.receipt["executor_uid"]
        assert run.native_job_ref["attempt_token"] == attempt.idempotency_token
        assert [work.kind for work in works].count("submit") == 1
        assert [work.kind for work in works].count("reconcile") == 1
        assert [work.kind for work in works].count("poll") == 1
        assert sum(work.completed_at is None for work in works) == 1
        assert not observations and not results
    assert operations == ["reconcile", "reconcile"]
    assert sent[0][0]["work_id"] == next(work.work_id for work in works if work.kind == "poll")


class LateCompletion(ObservationBridge):
    async def read_provider(self, *, intent, authority):
        receipt = await super().read_provider(intent=intent, authority=authority)
        if self.reads == 1:
            receipt["readback"]["state"] = "running"
        return receipt


@pytest.mark.asyncio
async def test_truthful_cancellation(work_db):
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        admission = dict(run.admission)
        admission["scope"] = {**admission["scope"], "owner_id": "owner-a", "relationship_revision": "1"}
        admission["limits"] = {**admission["limits"], "max_polls": 3}
        run.admission = admission
        run.manifest = {**run.manifest, "scope": admission["scope"]}
        await session.commit()
    native, abandoned_poll_id = await accepted_poll(work_db)
    bridge = LateCompletion(native)
    clock = {"now": NOW + timedelta(seconds=1)}
    sent = []

    async def publish(envelope, delay):
        assert set(envelope) == {"schema_version", "work_id", "generation"}
        async with work_db() as session:
            run = await session.get(Run, ("tenant-a", "run-a"))
            work = await session.get(Work, ("tenant-a", envelope["work_id"]))
            assert work is not None and work.completed_at is None
            assert run.state in {"cancel_requested", "cancelled"}
        sent.append((envelope, delay))

    async with work_db() as session:
        store = RunStore(session, "tenant-a", "product-a", "project-a", "transform", "owner-a", "1")
        response = await cancel_and_dispatch(store, work_db, publish, "run-a", "Stop locally",
                                             "cancel-transform-001", now=clock["now"])
        replay = await cancel_and_dispatch(store, work_db, publish, "run-a", "Stop locally",
                                           "cancel-transform-001", now=clock["now"])
    assert replay == response
    assert response["state"] == "cancel_requested" and response["provider_state"] == "running"
    assert response["native_job_ref"] == native
    assert len(sent) == 1 and sent[0][1] == 0
    async with work_db() as session:
        receipts = (await session.scalars(select(RunReceipt).where(
            RunReceipt.tenant_id == "tenant-a", RunReceipt.run_id == "run-a"))).all()
        abandoned = await session.get(Work, ("tenant-a", abandoned_poll_id))
        assert len(receipts) == 1 and receipts[0].operation == "cancel"
        assert abandoned.completed_at is not None

    operations = []

    async def authorize(run, claim, operation):
        operations.append(operation)
        assert operation in {"recover_observe", "recover_collect"}
        return object()

    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge), clock=lambda: clock["now"])
    cancel, _ = sent.pop(0)
    assert await worker.execute(cancel)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        assert run.state == "cancelled" and run.provider_state == "running"
        assert run.native_job_ref == native
        assert run.deadline_at.replace(tzinfo=NOW.tzinfo) == NOW + timedelta(hours=1)
    poll, _ = sent.pop(0)
    assert await worker.execute(poll)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        assert run.state == "cancelled" and run.provider_state == "running"
        assert run.monitoring["poll_attempts"] == 1
    next_poll, delay = sent.pop(0)
    assert delay == 60
    assert not await worker.execute(next_poll)
    clock["now"] += timedelta(seconds=60)
    assert await worker.execute(next_poll)
    collect, delay = sent.pop(0)
    assert delay == 0
    assert await worker.execute(collect)
    assert not sent
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        observations = (await session.scalars(select(ProviderObservation).where(
            ProviderObservation.tenant_id == "tenant-a", ProviderObservation.run_id == "run-a"
        ).order_by(ProviderObservation.observed_at))).all()
        observations = [observation for observation in observations
                        if observation.payload.get("kind") != "transform_pre_submit_baseline"]
        results = (await session.scalars(select(AssessmentResult).where(
            AssessmentResult.tenant_id == "tenant-a", AssessmentResult.run_id == "run-a"))).all()
        works = (await session.scalars(select(Work).where(
            Work.tenant_id == "tenant-a", Work.run_id == "run-a"))).all()
        assert run.state == "cancelled" and run.provider_state == "completed"
        assert run.native_job_ref == native and run.cancel_reason == "Stop locally"
        assert run.monitoring["poll_attempts"] == 3
        assert [observation.payload["outcome"] for observation in observations] == [
            "running", "completed", "completed"]
        assert sum(observation.payload.get("artifact_refs") == {"patch_ref": "private-artifact-a"}
                   for observation in observations) == 1
        assert not results and all(work.completed_at is not None for work in works)
        assert [work.kind for work in works].count("submit") == 1
    assert operations == ["recover_observe", "recover_observe", "recover_collect"]
    assert bridge.reads == 2 and bridge.collections == 1


@pytest.mark.asyncio
async def test_cancelled_queued_attempt_reconciles_ambiguous_native_effect(work_db):
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        admission = dict(run.admission)
        admission["scope"] = {**admission["scope"], "owner_id": "owner-a"}
        run.admission = admission
        run.manifest = {**run.manifest, "scope": admission["scope"]}
        from l5_services.adapters.transform.baseline import manifest_digest

        baseline = await session.get(ProviderObservation, ("tenant-a", "baseline-fixture"))
        baseline.payload = {**baseline.payload, "manifest_sha256": manifest_digest(run.manifest)}
        await session.commit()
    original = await claim_work(work_db, "submit-a", 1, now=NOW)
    attempt, created = await prepare_submit_attempt(work_db, original, now=NOW)
    assert created
    bridge = LostAcknowledgment(work_db)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        with pytest.raises(TimeoutError):
            await TransformAdapter(bridge).submit(run, run.manifest, attempt, attempt)
    assert bridge.launches == 1
    sent = []

    async def publish(envelope, delay):
        assert set(envelope) == {"schema_version", "work_id", "generation"}
        sent.append((envelope, delay))

    now = NOW + timedelta(seconds=61)
    async with work_db() as session:
        store = RunStore(session, "tenant-a", "product-a", "project-a", "transform", "owner-a")
        response = await cancel_and_dispatch(store, work_db, publish, "run-a", "Stop locally",
                                             "cancel-ambiguous-001", now=now)
    assert response["state"] == "cancel_requested"
    assert response["provider_state"] == "unknown" and "native_job_ref" not in response
    async with work_db() as session:
        work = await session.get(Work, ("tenant-a", "submit-a"))
        saved = await session.get(WorkerAttempt, ("tenant-a", attempt.attempt_id))
        assert work.completed_at is not None
        assert saved.status == "prepared" and saved.native_job_ref is None
    operations = []

    async def authorize(run, claim, operation):
        assert operation in {"recover_reconcile", "recover_observe"}
        operations.append(operation)
        return object()

    clock = {"now": now}
    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge), clock=lambda: clock["now"])
    cancel, _ = sent.pop(0)
    assert await worker.execute(cancel)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        assert run.state == "cancelled" and run.provider_state == "unknown"
        assert run.native_job_ref is None and run.monitoring["state"] == "recovery_pending"
    reconcile, _ = sent.pop(0)
    assert await worker.execute(reconcile)
    assert bridge.launches == 1 and bridge.reads == 1
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        assert run.state == "cancelled" and run.provider_state == "unknown"
        assert run.monitoring["last_poll_error"] == {"code": "native_readback_unavailable"}
    retry, delay = sent.pop(0)
    assert delay == 60 and not await worker.execute(retry)
    clock["now"] += timedelta(seconds=60)
    assert await worker.execute(retry)
    poll, delay = sent.pop(0)
    assert delay == 0
    assert await worker.execute(poll)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        saved = await session.get(WorkerAttempt, ("tenant-a", attempt.attempt_id))
        observations = (await session.scalars(select(ProviderObservation).where(
            ProviderObservation.tenant_id == "tenant-a", ProviderObservation.run_id == "run-a"))).all()
        observations = [observation for observation in observations
                        if observation.payload.get("kind") != "transform_pre_submit_baseline"]
        assert run.state == "cancelled" and run.provider_state == "running"
        assert run.native_job_ref == saved.native_job_ref
        assert run.native_job_ref["native_id"] == bridge.receipt["readback"]["native_id"]
        assert run.native_job_ref["attempt_token"] == attempt.idempotency_token
        assert saved.reconcile_checks == 2 and run.monitoring["poll_attempts"] == 1
        assert run.deadline_at.replace(tzinfo=NOW.tzinfo) > clock["now"]
        assert len(observations) == 1 and observations[0].payload["outcome"] == "running"
    assert bridge.launches == 1 and bridge.reads == 3
    assert operations == ["recover_reconcile", "recover_reconcile", "recover_observe"]


class LedgerBridge(ObservationBridge):
    def __init__(self, native, ledger, sessions):
        super().__init__(native)
        self.ledger = ledger
        self.sessions = sessions

    async def _check_authority(self, intent, authority, operation):
        tenant, project, work_id, fence, owner, scope = authority
        assert (tenant, project, scope) == (intent["tenant_id"], intent["project_id"], operation)
        async with self.sessions() as session:
            work = await session.get(Work, (tenant, work_id))
            assert work is not None and (work.fence, work.lease_owner) == (fence, owner)

    async def read_provider(self, *, intent, authority):
        await self._check_authority(intent, authority, "recover_observe")
        self.ledger["read_effects"] += 1
        return await super().read_provider(intent=intent, authority=authority)

    async def collect_provider(self, *, intent, authority):
        await self._check_authority(intent, authority, "recover_collect")
        self.ledger["collect_effects"] += 1
        return await super().collect_provider(intent=intent, authority=authority)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["revoked", "exhausted", "stale_fence", "collect_revoked"])
async def test_authority_budget_fences(work_db, case):
    native, poll_id = await accepted_poll(work_db)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        run.state = "cancelled"
        await session.commit()
    ledger = {"remaining_allowance": 0 if case == "exhausted" else 1,
              "new_allowances": 0, "paid_effects": 0, "cost_status": "unknown",
              "read_effects": 0, "collect_effects": 0}
    original_allowance = ledger["remaining_allowance"]
    bridge = LedgerBridge(native, ledger, work_db)
    operations = []
    published = []

    async def publish(envelope, delay):
        assert set(envelope) == {"schema_version", "work_id", "generation"}
        published.append(envelope)

    async def authorize(run, claim, operation):
        operations.append(operation)
        assert operation in {"recover_observe", "recover_collect"}
        assert run.tenant_id == claim.tenant_id == "tenant-a"
        assert run.project_id == "project-a" and claim.native_job_ref == native
        if case == "stale_fence":
            async with work_db() as session:
                work = await session.get(Work, ("tenant-a", claim.work_id))
                work.fence += 1
                work.lease_owner = "successor-owner"
                await session.commit()
        if case in {"revoked", "collect_revoked"} and (case != "collect_revoked" or operation == "recover_collect"):
            raise HTTPException(403, detail="delegation_revoked")
        if ledger["remaining_allowance"] == 0:
            raise HTTPException(403, detail="allowance_exhausted")
        return (run.tenant_id, run.project_id, claim.work_id, claim.fence, claim.owner, operation)

    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge),
                              clock=lambda: NOW + timedelta(seconds=1))
    first_result = await worker.execute({"schema_version": "1.0", "work_id": poll_id, "generation": 1})
    assert first_result is (case != "stale_fence")
    if case == "collect_revoked":
        assert len(published) == 1
        assert await worker.execute(published.pop())
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        attempts = (await session.scalars(select(WorkerAttempt).where(
            WorkerAttempt.tenant_id == "tenant-a", WorkerAttempt.run_id == "run-a"))).all()
        work = await session.get(Work, ("tenant-a", poll_id))
        observations = (await session.scalars(select(ProviderObservation).where(
            ProviderObservation.tenant_id == "tenant-a", ProviderObservation.run_id == "run-a"))).all()
        observations = [observation for observation in observations
                        if observation.payload.get("kind") != "transform_pre_submit_baseline"]
        results = (await session.scalars(select(AssessmentResult).where(
            AssessmentResult.tenant_id == "tenant-a", AssessmentResult.run_id == "run-a"))).all()
        assert run.state == "cancelled" and run.native_job_ref == native
        assert len(attempts) == 1 and attempts[0].native_job_ref == native
        assert not results and not published
        assert run.monitoring["poll_attempts"] == (2 if case == "collect_revoked" else 1)
        if case == "stale_fence":
            assert work.lease_owner == "successor-owner" and work.completed_at is None
            assert run.provider_state == "running" and not observations
        else:
            assert work.completed_at is not None
            assert run.monitoring["last_poll_error"] == {"code": "recovery_authority_unavailable"}
            if case == "collect_revoked":
                assert run.provider_state == "completed"
                assert len(observations) == 1 and observations[0].payload["outcome"] == "completed"
                assert "artifact_refs" not in observations[0].payload
            else:
                assert run.provider_state == "running" and not observations
    assert ledger["remaining_allowance"] == original_allowance
    assert ledger["new_allowances"] == ledger["paid_effects"] == 0
    assert ledger["cost_status"] == "unknown" and ledger["collect_effects"] == 0
    assert ledger["read_effects"] == (1 if case == "collect_revoked" else 0)
    assert bridge.reads == ledger["read_effects"] and bridge.collections == 0
    assert operations == (["recover_observe", "recover_collect"] if case == "collect_revoked"
                          else ["recover_observe"])
