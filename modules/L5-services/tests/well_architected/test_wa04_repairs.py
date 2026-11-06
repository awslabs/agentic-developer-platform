"""Cancellation and bounded native collection recovery regressions."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from l5_services.adapters.well_architected.generation import GenerationAdapter
from l5_services.admission import cancel_and_dispatch, reconcile_and_dispatch
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.evidence import AssessmentResult
from l5_services.models.generation_collection import GenerationCollection
from l5_services.models.run import Run
from l5_services.models.work import Work
from l5_services.run_access import RunStore
from l5_services.work_store import checkpoint_generation_detail, claim_work, finish_work
from l5_services.workers import execution
from test_wa04 import NOW, PROFILE, SCOPE, _database


class CollectionProvider:
    def __init__(self, clock, *, count=1):
        self.clock = clock
        self.native = {"tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"],
                       "profile_arn": PROFILE["record_id"], "generation_id": "original-generation",
                       "name": "adp-wa-example"}
        self.items = [{"profileArn": PROFILE["record_id"], "generationId": "original-generation",
                       "recommendationArn": f"arn:aws:wellarchitected:us-east-1:000000000000:agent-recommendation/00000000-0000-0000-0000-{index:012d}",
                       "title": None, "description": None, "pillar": None} for index in range(count)]
        self.starts, self.details, self.lists = [], [], []
        self.fail_read = False

    def start_agent_recommendation_generation(self, **request):
        self.starts.append(request)
        self.native["name"] = request["name"]
        return {"id": self.native["generation_id"], "profileArn": PROFILE["record_id"],
                "name": request["name"], "createdBy": "actor-example", "createdAt": NOW}

    def list_agent_recommendation_generations(self, **request):
        return {"items": [{"id": self.native["generation_id"], "profileArn": PROFILE["record_id"],
                           "name": self.native["name"]}]}

    def get_agent_recommendation_generation(self, **request):
        return {"id": self.native["generation_id"], "profileArn": PROFILE["record_id"],
                "name": self.native["name"], "createdBy": "actor-example", "createdAt": NOW,
                "scope": self.starts[0]["scope"], "additionalContext": self.starts[0]["additionalContext"],
                "status": "COMPLETED"}

    def list_agent_recommendations(self, **request):
        self.lists.append(request)
        if self.fail_read:
            raise TimeoutError("synthetic collection timeout")
        return {"items": self.items}

    def get_agent_recommendation(self, *, recommendationArn):
        if self.fail_read:
            raise TimeoutError("synthetic detail timeout")
        self.details.append(recommendationArn)
        self.clock[0] += timedelta(seconds=1)
        return next(item for item in self.items if item["recommendationArn"] == recommendationArn)


def worker_for(sessions, clock, provider, published):
    class DelegatedSession:
        def client(self, service, *, config=None):
            assert service == "wellarchitected"
            if config is not None and config.read_timeout == 15:
                assert config.connect_timeout == 3 and config.retries["total_max_attempts"] == 1
            provider.meta = SimpleNamespace(config=config)
            return provider

    async def authorize(run, claim, operation):
        return DelegatedSession()

    async def publish(envelope, delay):
        published.append(envelope)

    worker = execution.ExecutionService(sessions, None, None, publish, authorize,
                                        well_architected_adapter=GenerationAdapter(None), clock=lambda: clock[0])
    return worker, publish


async def seed_collection(sessions, native):
    async with sessions() as session:
        run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
        run.admission = {**run.admission, "scope": {**SCOPE, "owner_id": "actor-example"}}
        run.state, run.provider_state, run.native_job_ref = "collecting", "completed", native
        run.monitoring = {"state": "current", "latest_observation": {
            "native_job_ref": native, "outcome": "completed", "observed_at": NOW.isoformat()}}
        work = await session.get(Work, (SCOPE["tenant_id"], "work-example"))
        work.kind = "collect"
        await session.commit()


def store_for(session, actor="actor-example"):
    return RunStore(session, SCOPE["tenant_id"], SCOPE["product_id"], SCOPE["project_id"],
                    "well_architected", actor)


@pytest.mark.parametrize("cancel_after", [61, 3901])
async def test_cancel_after_start_receipt_crash_recovers_one_generation(tmp_path, monkeypatch, cancel_after):
    engine, sessions = await _database(tmp_path, admission_scope={**SCOPE, "owner_id": "actor-example"})
    clock, published = [NOW], []
    provider = CollectionProvider(clock, count=0)
    worker, publish = worker_for(sessions, clock, provider, published)
    original_finish = execution.finish_work

    async def crash_before_receipt(*args, **kwargs):
        if kwargs.get("attempt_status") == "accepted":
            raise RuntimeError("crash before native receipt")
        return await original_finish(*args, **kwargs)

    try:
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.manifest = {**run.manifest, "scope": run.admission["scope"]}
            await session.commit()
        monkeypatch.setattr(execution, "finish_work", crash_before_receipt)
        with pytest.raises(RuntimeError, match="crash before native receipt"):
            await worker.execute({"schema_version": "1.0", "work_id": "work-example", "generation": 1})
        monkeypatch.setattr(execution, "finish_work", original_finish)
        clock[0] += timedelta(seconds=cancel_after)
        async with sessions() as session:
            response = await cancel_and_dispatch(store_for(session), sessions, publish, "run-example",
                                                 "cancel uncertain run", "cancel-uncertain-0001", now=clock[0])
        assert response["state"] == "cancel_requested"
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            assert run.provider_state == "unknown" and run.native_job_ref is None
            assert (await session.scalars(select(WorkerAttempt))).one().prepared_request
        for _ in range(5):
            if not published:
                break
            assert await worker.execute(published.pop(0))
        if cancel_after > 3900:
            async with sessions() as session:
                run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
                assert run.provider_state == "unknown" and run.monitoring["state"] == "recovery_blocked"
            for index in range(2):
                async with sessions() as session:
                    await reconcile_and_dispatch(store_for(session), sessions, publish, "run-example",
                                                 "recover original generation", f"late-recovery-000{index}", now=clock[0])
                for _ in range(3):
                    if not published:
                        break
                    assert await worker.execute(published.pop(0))
        assert not published and len(provider.starts) == 1
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            assert run.state == "cancelled" and run.provider_state == "completed"
            assert (await session.scalars(select(AssessmentResult))).one().payload["native_job_ref"] == provider.native
    finally:
        await engine.dispose()


@pytest.mark.parametrize("boundary_offset", [0, 1])
@pytest.mark.parametrize("failed_step", ["list", "detail"])
async def test_collection_timeout_at_recovery_boundary_allows_owner_resume(tmp_path, boundary_offset, failed_step):
    engine, sessions = await _database(tmp_path)
    clock, published = [NOW], []
    provider = CollectionProvider(clock, count=2 if failed_step == "detail" else 0)
    worker, publish = worker_for(sessions, clock, provider, published)
    try:
        await seed_collection(sessions, provider.native)
        envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
        if failed_step == "detail":
            assert await worker.execute(envelope)
            assert await worker.execute(published.pop(0))
            envelope = published.pop(0)
        clock[0] = NOW + timedelta(hours=1, seconds=300 + boundary_offset)
        provider.fail_read = True
        assert await worker.execute(envelope)
        assert not published
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            assert run.state == "collecting" and run.provider_state == "completed"
            assert run.monitoring["state"] == "recovery_blocked"
            assert run.monitoring["last_poll_error"] == {"code": "collection_read_failed"}
            with pytest.raises(HTTPException) as denied:
                await store_for(session, "other-owner").reconcile("run-example", "restore", "other-owner-0001", now=clock[0])
            assert denied.value.status_code == 404
        provider.fail_read = False
        async with sessions() as session:
            await reconcile_and_dispatch(store_for(session), sessions, publish, "run-example",
                                         "restore collection", "restore-collection-0001", now=clock[0])
        for _ in range(3):
            if not published:
                break
            assert await worker.execute(published.pop(0))
        assert not published and not provider.starts
        assert len(provider.lists) == (1 if failed_step == "detail" else 2)
        assert provider.details == [item["recommendationArn"] for item in provider.items]
        async with sessions() as session:
            assert (await session.scalars(select(AssessmentResult))).one().payload["native_job_ref"] == provider.native
    finally:
        await engine.dispose()


async def test_slow_collection_checkpoints_each_detail_and_resumes_after_crash(tmp_path, monkeypatch):
    engine, sessions = await _database(tmp_path)
    clock, published = [NOW], []
    provider = CollectionProvider(clock, count=61)
    worker, publish = worker_for(sessions, clock, provider, published)
    try:
        await seed_collection(sessions, provider.native)
        assert await worker.execute({"schema_version": "1.0", "work_id": "work-example", "generation": 1})
        assert provider.details == []
        for _ in range(30):
            before = len(provider.details)
            assert await worker.execute(published.pop(0))
            assert len(provider.details) == before + 1
        async with sessions() as session:
            saved = await session.get(GenerationCollection, (SCOPE["tenant_id"], "run-example"))
            assert len(saved.recommendations) == 30 and saved.result_id is None
        original_finish = execution.finish_work

        async def crash_after_checkpoint(*args, **kwargs):
            raise RuntimeError("crash after detail checkpoint")

        envelope = published.pop(0)
        monkeypatch.setattr(execution, "finish_work", crash_after_checkpoint)
        with pytest.raises(RuntimeError, match="crash after detail checkpoint"):
            await worker.execute(envelope)
        monkeypatch.setattr(execution, "finish_work", original_finish)
        clock[0] += timedelta(seconds=61)
        assert await worker.execute(envelope)
        for _ in range(61):
            if not published:
                break
            assert await worker.execute(published.pop(0))
        assert not published and not provider.starts and len(provider.lists) == 1
        assert provider.details == [item["recommendationArn"] for item in provider.items]
        assert clock[0] > NOW + timedelta(seconds=60)
        async with sessions() as session:
            result = (await session.scalars(select(AssessmentResult))).one()
            assert result.version == 1 and result.payload["recommendations"] == provider.items
    finally:
        await engine.dispose()


async def test_pending_collection_rejects_stale_fences_wrong_details_and_early_result(tmp_path):
    engine, sessions = await _database(tmp_path)
    clock, published = [NOW], []
    provider = CollectionProvider(clock, count=1)
    worker, publish = worker_for(sessions, clock, provider, published)
    try:
        await seed_collection(sessions, provider.native)
        assert await worker.execute({"schema_version": "1.0", "work_id": "work-example", "generation": 1})
        envelope = published.pop(0)
        claim = await claim_work(sessions, envelope["work_id"], 1, now=NOW, lease_seconds=1)
        with pytest.raises(ValueError, match="completed generation collection"):
            await finish_work(sessions, claim, state="succeeded", provider_state="completed", result={
                "native_job_ref": provider.native, "recommendations": []}, now=NOW)
        clock[0] += timedelta(seconds=2)
        recovered = await claim_work(sessions, envelope["work_id"], 1, now=clock[0])
        with pytest.raises(ValueError, match="stale native collection work"):
            await checkpoint_generation_detail(sessions, claim, provider.items[0], now=clock[0])
        with pytest.raises(ValueError, match="another generation"):
            await checkpoint_generation_detail(sessions, recovered, {
                **provider.items[0], "generationId": "wrong-generation"}, now=clock[0])
        with pytest.raises(ValueError, match="another generation"):
            await checkpoint_generation_detail(sessions, recovered, {
                **provider.items[0], "recommendationArn": "wrong-recommendation"}, now=clock[0])
        saved = await checkpoint_generation_detail(sessions, recovered, provider.items[0], now=clock[0])
        assert not saved["pending_recommendations"] and saved["recommendations"] == provider.items
        with pytest.raises(ValueError, match="cursor or generation changed"):
            await checkpoint_generation_detail(sessions, recovered, provider.items[0], now=clock[0])
        assert await finish_work(sessions, recovered, state="succeeded", provider_state="completed", result={
            "native_job_ref": provider.native, "recommendations": provider.items}, now=clock[0]) == "completed"
    finally:
        await engine.dispose()
