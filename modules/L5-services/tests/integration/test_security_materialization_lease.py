"""Long repository transfers retain one fenced worker without losing completion."""

from datetime import timedelta

from l5_services.models import Run, Work
from l5_services.work_store import claim_work
from l5_services.workers.execution import ExecutionService
from test_lambda_execution import NOW, work_db  # noqa: F401


async def test_repository_materialization_can_finish_after_the_old_lease_window(work_db, monkeypatch):  # noqa: F811
    source = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "connection",
              "record_type": "repository_commit", "record_id": "repository", "record_revision": "a" * 40}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.input = {"capability": "code_review", "sources": [source]}
        await session.commit()
    clock = {"now": NOW}
    published = []

    async def transfer(run, claim, *_args, **_kwargs):
        clock["now"] += timedelta(seconds=120)
        assert await claim_work(work_db, claim.work_id, claim.generation, now=clock["now"]) is None
        return {"sources": [source], "coverage_mode": "full_snapshot"}, []

    async def authorize(*_):
        return object()

    async def publish(envelope, delay):
        published.append(envelope)

    monkeypatch.setattr("l5_services.source_manifest.materialize_inputs", transfer)
    worker = ExecutionService(work_db, None, None, publish, authorize, clock=lambda: clock["now"], service_id="security")
    assert await worker.execute({"schema_version": "1.0", "work_id": "work-one", "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        work = await session.get(Work, ("tenant-one", "work-one"))
        assert run.state == "queued" and run.provider_state == "not_started"
        assert work.completed_at is not None and run.manifest["coverage_mode"] == "full_snapshot"
        assert len(published) == 1
