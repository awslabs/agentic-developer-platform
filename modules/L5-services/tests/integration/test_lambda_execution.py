"""Independent SQL readback of bounded worker transitions."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.models import Configuration, ConfigurationVersion, Run, Work
from l5_services.models.base import Base
from l5_services.work_store import claim_work, finish_work
from l5_services.workers.execution import execute_once


NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def work_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(Configuration(tenant_id="tenant-one", configuration_id="config", product_id="product-one",
                                  project_id="project-one", service_id="security", relationship_revision="1",
                                  owner_id="owner", current_revision=1, handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id="tenant-one", configuration_id="config", revision=1,
                                         settings={}, inherited_fields=[], gaps=[], state="ready", hash="f" * 64))
        session.add(Run(tenant_id="tenant-one", run_id="run-one", product_id="product-one",
                        project_id="project-one", service_id="security", configuration_id="config",
                        configuration_revision=1, binding_id="binding", trigger_id="trigger",
                        binding_generation=1, input={"sources": []}, admission={"configuration_hash": "f" * 64},
                        deadline_at=NOW + timedelta(hours=1)))
        session.add(Work(tenant_id="tenant-one", work_id="work-one", run_id="run-one", kind="execute",
                         generation=1, step=0, due_at=NOW))
        await session.commit()
    yield sessions
    await engine.dispose()


async def test_fenced_claim_and_atomic_successor(work_db):
    claim = await claim_work(work_db, "work-one", 1, now=NOW)
    assert claim is not None
    assert claim.admission["configuration_hash"] == "f" * 64
    assert await claim_work(work_db, "work-one", 1, now=NOW) is None
    assert await finish_work(work_db, claim, state="queued", next_kind="poll",
                             due_at=NOW + timedelta(seconds=600), now=NOW) is not None
    assert await finish_work(work_db, claim, state="failed", now=NOW) is None
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        work = (await session.scalars(select(Work).where(Work.tenant_id == "tenant-one"))).all()
        assert (run.state, run.revision) == ("queued", 2)
        assert len(work) == 2 and work[0].completed_at is not None
        assert work[1].due_at.replace(tzinfo=UTC) == NOW + timedelta(seconds=600)
        assert await claim_work(work_db, work[1].work_id, 1, now=NOW) is None


async def test_stale_fence_and_one_transition_per_invocation(work_db):
    stale = await claim_work(work_db, "work-one", 1, now=NOW)
    assert await claim_work(work_db, "work-one", 2, now=NOW + timedelta(seconds=61)) is None
    current = await claim_work(work_db, "work-one", 1, now=NOW + timedelta(seconds=61))
    assert current.fence > stale.fence
    assert await finish_work(work_db, stale, state="failed", now=NOW + timedelta(seconds=61)) is None
    assert await execute_once(work_db, {"schema_version": "1.0", "work_id": "work-one", "generation": 1},
                              lambda _: None, now=NOW + timedelta(seconds=61)) is False
    assert await finish_work(work_db, current, state="running", provider_state="running",
                             now=NOW + timedelta(seconds=61)) == "completed"
    async with work_db() as session:
        assert (await session.get(Run, ("tenant-one", "run-one"))).state == "running"


async def test_old_security_reconcile_for_existing_job_never_prepares_another_submission(work_db):
    from l5_services.models.attempt import WorkerAttempt
    from l5_services.workers.execution import ExecutionService

    native = {"record_id": "existing-job", "record_revision": "existing-review"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.state, run.provider_state, run.native_job_ref = "running", "running", native
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "reconcile"
        await session.commit()

    async def forbidden(*args):
        raise AssertionError("Accepted-job reconciliation must not call or requeue the provider")

    service = ExecutionService(work_db, None, None, forbidden, forbidden, clock=lambda: NOW)
    assert await service.execute({"schema_version": "1.0", "work_id": "work-one", "generation": 1})
    async with work_db() as session:
        assert (await session.get(Work, ("tenant-one", "work-one"))).completed_at is not None
        assert (await session.get(Run, ("tenant-one", "run-one"))).native_job_ref == native
        assert (await session.scalars(select(WorkerAttempt))).all() == []


async def test_old_devops_reconcile_queues_only_readback_for_accepted_job(work_db):
    from unittest.mock import AsyncMock
    from l5_services.models.attempt import WorkerAttempt
    from l5_services.workers.execution import ExecutionService

    native = {"record_id": "existing-execution", "record_revision": "existing-task"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.service_id, run.state, run.provider_state = "devops", "outcome_unknown", "running"
        run.input, run.native_job_ref = {"capability": "investigation"}, native
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "reconcile"
        await session.commit()
    authorize = AsyncMock(side_effect=AssertionError("reconciliation cannot submit another task"))
    publish = AsyncMock()
    service = ExecutionService(work_db, None, None, publish, authorize, clock=lambda: NOW)
    assert await service.execute({"schema_version": "1.0", "work_id": "work-one", "generation": 1})
    authorize.assert_not_awaited()
    async with work_db() as session:
        pending = (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).one()
        assert pending.kind == "poll"
        assert (await session.get(Run, ("tenant-one", "run-one"))).native_job_ref == native
        assert (await session.scalars(select(WorkerAttempt))).all() == []
    publish.assert_awaited_once()


async def test_bounded_transition_publishes_only_after_commit(work_db):
    delivered = []
    seen = []

    async def transition(claim):
        seen.append((claim.run_id, claim.admission["configuration_hash"], claim.fence))
        return {"state": "running", "provider_state": "running", "next_kind": "poll",
                "due_at": NOW + timedelta(seconds=600)}

    async def publish(envelope, delay):
        async with work_db() as session:
            work = (await session.scalars(select(Work).where(Work.work_id == envelope["work_id"]))).one()
            assert work.kind == "poll" and work.step == 1
            assert (await session.get(Run, ("tenant-one", "run-one"))).state == "running"
        delivered.append((envelope, delay))

    envelope = {"schema_version": "1.0", "work_id": "work-one", "generation": 1}
    assert await execute_once(work_db, envelope, transition, publish, now=NOW)
    assert seen == [("run-one", "f" * 64, 1)]
    assert delivered[0][1] == 600
    assert await execute_once(work_db, envelope, transition, publish, now=NOW) is False
    assert len(seen) == 1


async def test_verified_manifest_and_immutable_native_evidence(work_db):
    import json
    from hashlib import sha256

    from l5_services.models import AssessmentResult, ProviderObservation
    from l5_services.source_manifest import materialize_inputs

    source = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "document", "record_id": "doc", "record_revision": "rev-1"}
    document = b"pinned document"
    entry = {"source_ref": source, "path": "doc.md", "sha256": sha256(document).hexdigest(), "bytes": len(document)}
    source_manifest = json.dumps({"source_ref": source, "files": [entry], "exclusions": []}, sort_keys=True).encode()
    def artifact(content):
        return {"artifact_id": "artifact", "version": "1", "sha256": sha256(content).hexdigest(),
                "media_type": "application/octet-stream", "bytes": len(content)}
    snapshot = {"snapshot_id": "snapshot", "source_ref": source, "content": artifact(document),
                "file_manifest": artifact(source_manifest), "file_count": 1, "exclusions": [],
                "expires_at": (NOW + timedelta(hours=2)).isoformat()}
    admission = {"source_snapshots": [snapshot], "limits": {}, "max_files": 10, "max_bytes": 1024,
                 "delegation_id": "grant", "delegation_version": "rev-1", "exclusions": [],
                 "scope": {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one",
                           "relationship_revision": "1", "owner_id": "owner", "visibility": "only_me"},
                 "configuration_hash": "f" * 64,
                 "profile_ref": source, "adapter_revision": "adapter-1"}

    class SourceClient:
        requests = []

        async def read_source(self, *args):
            self.requests.append(args[-1])
            return snapshot

        async def read_source_manifest(self, *args):
            return source_manifest

        async def stream_source(self, *args):
            yield document

    client = SourceClient()
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.admission, run.input = admission, {"sources": [source]}
        run.capture_limits = {"max_bytes": 1024, "max_files": 10}
        await session.commit()
    claim = await claim_work(work_db, "work-one", 1, now=NOW)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        manifest, content = await materialize_inputs(run, claim, client, now=NOW)
    assert content == [document]
    assert manifest["source_snapshot_ids"] == ["snapshot"]
    assert manifest["files"] == [entry]
    from jsonschema import Draft202012Validator
    from pathlib import Path
    schema = json.loads((Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1/schemas.json").read_text())
    Draft202012Validator({**schema, "$ref": "#/$defs/Manifest"}).validate(manifest)
    assert client.requests == [{"delegation_id": "grant", "delegation_revision": "rev-1",
                                "operation_id": "run-one", "attempt_id": "work-one", "lease_fence": 1,
                                "lease_owner": claim.owner}]
    observation = {"observed_at": NOW.isoformat(), "native_job_ref": source,
                   "outcome": "completed", "progress_coverage": "complete"}
    result = {"run_id": "run-one", "native_job_ref": source, "coverage": "complete"}
    assert await finish_work(work_db, claim, state="succeeded", provider_state="completed",
                             manifest=manifest, observation=observation, result=result, now=NOW)
    async with work_db() as session:
        stored = await session.get(Run, ("tenant-one", "run-one"))
        observations = (await session.scalars(select(ProviderObservation))).all()
        results = (await session.scalars(select(AssessmentResult))).all()
        assert stored.manifest == manifest and stored.monitoring["latest_observation"] == observation
        assert len(observations) == len(results) == 1
        assert observations[0].payload == observation and results[0].payload == result
    assert await finish_work(work_db, claim, state="failed", observation=observation, now=NOW) is None
    async with work_db() as session:
        assert len((await session.scalars(select(ProviderObservation))).all()) == 1


async def test_materializer_rejects_modified_snapshot_and_unsafe_archive(work_db):
    import io
    import json
    import zipfile
    from hashlib import sha256

    from l5_services.source_manifest import SourceIntegrityError, materialize_inputs

    source = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "repository_commit", "record_id": "repository", "record_revision": "commit-1"}
    data = b"source"
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("../secret", data)
    content = archive_bytes.getvalue()
    file_entry = {"source_ref": source, "path": "../secret", "sha256": sha256(data).hexdigest(), "bytes": len(data)}
    manifest_bytes = json.dumps({"source_ref": source, "files": [file_entry], "exclusions": []}).encode()
    def artifact(value):
        return {"artifact_id": "artifact", "version": "1", "sha256": sha256(value).hexdigest(),
                "media_type": "application/octet-stream", "bytes": len(value)}
    snapshot = {"snapshot_id": "snapshot", "source_ref": source, "content": artifact(content),
                "file_manifest": artifact(manifest_bytes), "file_count": 1, "exclusions": [],
                "expires_at": (NOW + timedelta(hours=1)).isoformat()}
    admission = {"source_snapshots": [snapshot], "max_bytes": 1024, "max_files": 10,
                 "delegation_id": "grant", "delegation_version": "1", "scope": {},
                 "configuration_hash": "f" * 64, "profile_ref": source, "adapter_revision": "adapter"}

    class SourceClient:
        async def read_source(self, *args):
            return snapshot

        async def read_source_manifest(self, *args):
            return manifest_bytes

        async def stream_source(self, *args):
            yield content

    claim = await claim_work(work_db, "work-one", 1, now=NOW)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.admission, run.input = admission, {"sources": [source]}
        run.capture_limits = {"max_bytes": 1024, "max_files": 10}
        await session.commit()
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        with pytest.raises(SourceIntegrityError, match="unsafe source path"):
            await materialize_inputs(run, claim, SourceClient(), now=NOW)
    content = b"tampered bytes"
    with pytest.raises(SourceIntegrityError, match="source integrity mismatch"):
        async with work_db() as session:
            run = await session.get(Run, ("tenant-one", "run-one"))
            await materialize_inputs(run, claim, SourceClient(), now=NOW)


async def test_multiple_sources_seal_single_verified_archive(work_db):
    import io
    import json
    import zipfile
    from hashlib import sha256

    from l5_services.source_manifest import SourceIntegrityError, materialize_inputs

    sources = [{"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
                "record_type": "document", "record_id": f"doc-{index}", "record_revision": "rev-1"}
               for index in range(2)]
    payloads = [b"first", b"second"]
    def reference(content):
        return {"artifact_id": "artifact", "version": "1", "sha256": sha256(content).hexdigest(),
                "media_type": "application/octet-stream", "bytes": len(content)}

    snapshots, manifest_bytes = [], []
    for index, (source, content) in enumerate(zip(sources, payloads, strict=True)):
        entry = {"source_ref": source, "path": "same.md", "sha256": sha256(content).hexdigest(),
                 "bytes": len(content)}
        listing = json.dumps({"source_ref": source, "files": [entry], "exclusions": []}).encode()
        manifest_bytes.append(listing)
        snapshots.append({"snapshot_id": f"snapshot-{index}", "source_ref": source,
                          "content": reference(content), "file_manifest": reference(listing),
                          "file_count": 1, "exclusions": [],
                          "expires_at": (NOW + timedelta(hours=1)).isoformat()})

    class SourceClient:
        async def read_source(self, _, __, snapshot_id, request):
            assert request["lease_fence"] == 1
            return snapshots[int(snapshot_id[-1])]

        async def read_source_manifest(self, _, __, snapshot_id, request):
            return manifest_bytes[int(snapshot_id[-1])]

        async def stream_source(self, _, __, snapshot_id, request):
            yield payloads[int(snapshot_id[-1])]

    class ArchiveStore:
        async def put_archive(self, run_id, content):
            assert run_id == "run-one"
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                assert archive.namelist() == ["0/same.md", "1/same.md"]
                assert [archive.read(name) for name in archive.namelist()] == payloads
            return reference(content)

    admission = {"source_snapshots": snapshots, "delegation_id": "grant", "delegation_version": "1",
                 "scope": {}, "configuration_hash": "f" * 64, "profile_ref": sources[0],
                 "adapter_revision": "adapter"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.input, run.admission = {"sources": sources}, admission
        run.capture_limits = {"max_bytes": 1024, "max_files": 2}
        await session.commit()
    claim = await claim_work(work_db, "work-one", 1, now=NOW)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        manifest, content = await materialize_inputs(run, claim, SourceClient(),
                                                     archive_store=ArchiveStore(), now=NOW)
        assert content == payloads
        assert manifest["source_snapshot_ids"] == ["snapshot-0", "snapshot-1"]
        assert len(manifest["files"]) == 2
        with pytest.raises(SourceIntegrityError, match="qualified archive store"):
            await materialize_inputs(run, claim, SourceClient(), now=NOW)


async def test_async_lifecycle(work_db):
    import asyncio
    import json
    from hashlib import sha256

    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.dispatcher import dispatch_work
    from l5_services.models import AssessmentResult, ProviderObservation
    from l5_services.workers.execution import ExecutionService, lambda_handler

    source = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "document", "record_id": "doc", "record_revision": "rev-1"}
    data = b"stable source content"
    entry = {"source_ref": source, "path": "source.md", "sha256": sha256(data).hexdigest(), "bytes": len(data)}
    listing = json.dumps({"source_ref": source, "files": [entry], "exclusions": []}).encode()

    def artifact(content):
        return {"artifact_id": "artifact", "version": "1", "sha256": sha256(content).hexdigest(),
                "media_type": "application/octet-stream", "bytes": len(content)}

    snapshot = {"snapshot_id": "snapshot", "source_ref": source, "content": artifact(data),
                "file_manifest": artifact(listing), "file_count": 1, "exclusions": [],
                "expires_at": (NOW + timedelta(hours=1)).isoformat()}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.input = {"sources": [source]}
        run.admission = {"source_snapshots": [snapshot], "delegation_id": "grant",
                         "delegation_version": "rev-1", "scope": {"tenant_id": "tenant-one",
                         "product_id": "product-one", "project_id": "project-one", "owner_id": "owner",
                         "relationship_revision": "1", "visibility": "only_me"}, "exclusions": [],
                         "configuration_hash": "f" * 64, "profile_ref": source,
                         "adapter_revision": "adapter-v1", "monitoring_policy": {"poll_interval_seconds": 600}}
        run.capture_limits = {"max_files": 10, "max_bytes": 1024}
        await session.commit()

    class SourceClient:
        async def read_source(self, product, project, snapshot_id, request):
            assert (product, project, snapshot_id) == ("product-one", "project-one", "snapshot")
            assert request["operation_id"] == "run-one" and request["lease_fence"] > 0
            return snapshot

        async def read_source_manifest(self, *args):
            return listing

        async def stream_source(self, *args):
            yield data

    calls = []
    clock = {"now": NOW}
    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "job-1", "record_revision": "1"}

    class Adapter:
        async def submit(self, run, manifest, claim, session):
            assert session == "verified-session" and manifest["source_snapshot_ids"] == ["snapshot"]
            assert run.manifest == manifest
            calls.append(("submit", run.run_id, run.configuration_revision, claim.fence))
            return AdapterOutcome("accepted", native_job_ref=native)

        async def observe(self, run, native_job_ref, claim, session):
            assert native_job_ref == native and session == "verified-session"
            calls.append(("observe", run.run_id, run.configuration_revision, claim.fence))
            outcome = "running" if sum(call[0] == "observe" for call in calls) == 1 else "completed"
            observation = {"observed_at": clock["now"].isoformat(), "native_job_ref": native,
                           "outcome": outcome, "progress_coverage": "complete"}
            return AdapterOutcome(outcome, observation=observation)

        async def collect(self, run, native_job_ref, claim, session):
            assert native_job_ref == native and session == "verified-session"
            calls.append(("collect", run.run_id, run.configuration_revision, claim.fence))
            return AdapterOutcome("completed", result={"run_id": run.run_id, "native_job_ref": native},
                                  observation={"observed_at": clock["now"].isoformat(),
                                               "native_job_ref": native, "outcome": "completed",
                                               "progress_coverage": "complete"})

    async def authorize(run, claim, action):
        assert run.admission["delegation_version"] == "rev-1"
        assert claim.run_id == run.run_id and action in {"materialize", "submit", "observe", "collect"}
        return "verified-session"

    messages = []

    async def publish(envelope, delay):
        messages.append((envelope, delay))

    service = ExecutionService(work_db, SourceClient(), Adapter(), publish, authorize,
                               clock=lambda: clock["now"])
    assert await dispatch_work(work_db, "tenant-one", "work-one", publish, now=NOW)
    assert messages[0][1] == 0

    async def deliver(index):
        envelope, _ = messages[index]
        event = {"Records": [{"messageId": f"message-{index}", "body": json.dumps(envelope)}]}
        return await asyncio.to_thread(lambda_handler, event, None, service=service)

    assert await deliver(0) == {"batchItemFailures": []}
    assert calls == [] and messages[1][1] == 0
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        assert run.state == "queued" and run.manifest["source_snapshot_ids"] == ["snapshot"]
    assert await deliver(1) == {"batchItemFailures": []}
    assert [entry[0] for entry in calls] == ["submit"]
    assert messages[2][1] == 600
    assert await deliver(2) == {"batchItemFailures": []}
    assert [entry[0] for entry in calls] == ["submit"]
    clock["now"] = NOW + timedelta(seconds=600)
    assert await deliver(2) == {"batchItemFailures": []}
    assert messages[3][1] == 600
    clock["now"] = NOW + timedelta(seconds=1200)
    assert await deliver(3) == {"batchItemFailures": []}
    assert messages[4][1] == 0
    assert await deliver(4) == {"batchItemFailures": []}
    assert [entry[0] for entry in calls] == ["submit", "observe", "observe", "collect"]
    async with work_db() as session:
        stored = await session.get(Run, ("tenant-one", "run-one"))
        saved_work = (await session.scalars(select(Work).order_by(Work.step))).all()
        observations = (await session.scalars(select(ProviderObservation))).all()
        results = (await session.scalars(select(AssessmentResult))).all()
        assert stored.state == "succeeded" and stored.provider_state == "completed"
        assert stored.revision == 6 and stored.native_job_ref == native
        assert stored.manifest["sources"] == [source] and stored.manifest["configuration_revision"] == 1
        assert stored.monitoring["state"] == "finished"
        assert len(saved_work) == 5 and all(item.completed_at is not None for item in saved_work)
        assert [observation.payload["outcome"] for observation in observations] == [
            "running", "completed", "completed"]
        assert len(results) == 1 and results[0].payload["native_job_ref"] == native


async def test_lambda_partial_batch_preserves_successful_neighbor():
    import asyncio
    import json

    from l5_services.workers.execution import lambda_handler

    class Service:
        completed = []

        async def execute(self, envelope):
            if envelope["work_id"] == "failed":
                raise TimeoutError("temporary worker fault")
            self.completed.append(envelope["work_id"])

    service = Service()
    event = {"Records": [
        {"messageId": "message-failed", "body": json.dumps({"work_id": "failed"})},
        {"messageId": "message-ok", "body": json.dumps({"work_id": "ok"})},
    ]}
    result = await asyncio.to_thread(lambda_handler, event, None, service=service)
    assert result == {"batchItemFailures": [{"itemIdentifier": "message-failed"}]}
    assert service.completed == ["ok"]


async def test_two_tenant_pinned_status_and_unchanged_progress(work_db):
    import asyncio
    import json

    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.dispatcher import dispatch_work
    from l5_services.models import AssessmentResult, ProviderObservation
    from l5_services.workers.execution import ExecutionService, lambda_handler

    def native(suffix):
        return {"tenant_id": f"tenant-{suffix}", "project_id": f"project-{suffix}",
                "source_connection_id": f"conn-{suffix}", "record_type": "native_job",
                "record_id": f"job-{suffix}", "record_revision": "rev-1"}

    async with work_db() as session:
        first = await session.get(Run, ("tenant-one", "run-one"))
        first.state, first.provider_state = "running", "running"
        first.native_job_ref = native("one")
        first.admission = {"scope": {"tenant_id": "tenant-one", "product_id": "product-one",
                                     "project_id": "project-one", "owner_id": "owner-one"},
                           "monitoring_policy": {"poll_interval_seconds": 600},
                           "source_snapshots": [{"snapshot_id": "pin-one"}]}
        first_work = await session.get(Work, ("tenant-one", "work-one"))
        first_work.kind, first_work.step = "poll", 2
        session.add(Configuration(tenant_id="tenant-two", configuration_id="config-two",
                                  product_id="product-two", project_id="project-two", service_id="security",
                                  relationship_revision="1", owner_id="owner-two", current_revision=1,
                                  handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id="tenant-two", configuration_id="config-two",
                                         revision=2, settings={}, inherited_fields=[], gaps=[],
                                         state="ready", hash="e" * 64))
        session.add(Run(tenant_id="tenant-two", run_id="run-two", product_id="product-two",
                        project_id="project-two", service_id="security", configuration_id="config-two",
                        configuration_revision=2, binding_id="binding-two", trigger_id="trigger-two",
                        binding_generation=3, input={"sources": []},
                        admission={"scope": {"tenant_id": "tenant-two", "product_id": "product-two",
                                             "project_id": "project-two", "owner_id": "owner-two"},
                                   "monitoring_policy": {"poll_interval_seconds": 600},
                                   "source_snapshots": [{"snapshot_id": "pin-two"}]},
                        native_job_ref=native("two"), state="running", provider_state="running",
                        deadline_at=NOW + timedelta(hours=1)))
        session.add(Work(tenant_id="tenant-two", work_id="work-two", run_id="run-two", kind="poll",
                         generation=1, step=2, due_at=NOW))
        await session.commit()

    clock = {"now": NOW}
    calls = []

    class Adapter:
        async def observe(self, run, native_job_ref, claim, session):
            assert native_job_ref == native(run.tenant_id.split("-")[-1])
            assert session == f"verified-{run.tenant_id}"
            assert run.admission["source_snapshots"][0]["snapshot_id"] == f"pin-{run.tenant_id[-3:]}"
            calls.append(("observe", run.tenant_id, run.configuration_revision, native_job_ref))
            count = sum(call[0] == "observe" and call[1] == run.tenant_id for call in calls)
            outcome = "completed" if run.tenant_id == "tenant-two" or count == 3 else "running"
            return AdapterOutcome(outcome, observation={
                "observed_at": clock["now"].isoformat(), "native_job_ref": native_job_ref,
                "outcome": outcome, "progress_coverage": "complete",
                "progress_fingerprint": ("b" if outcome == "completed" else "a") * 64,
            })

        async def collect(self, run, native_job_ref, claim, session):
            assert native_job_ref == native(run.tenant_id.split("-")[-1])
            calls.append(("collect", run.tenant_id, run.configuration_revision, native_job_ref))
            return AdapterOutcome("completed", result={"run_id": run.run_id, "native_job_ref": native_job_ref},
                                  observation={"observed_at": clock["now"].isoformat(),
                                               "native_job_ref": native_job_ref, "outcome": "completed",
                                               "progress_coverage": "complete", "progress_fingerprint": "b" * 64})

    async def authorize(run, claim, operation):
        assert run.tenant_id == claim.tenant_id
        assert run.admission["scope"]["tenant_id"] == run.tenant_id
        assert run.product_id == run.admission["scope"]["product_id"]
        return f"verified-{run.tenant_id}"

    messages = []

    async def publish(envelope, delay):
        messages.append((envelope, delay))

    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: clock["now"])
    assert await dispatch_work(work_db, "tenant-one", "work-one", publish, now=NOW)
    assert await dispatch_work(work_db, "tenant-two", "work-two", publish, now=NOW)

    async def deliver(index):
        event = {"Records": [{"messageId": f"m-{index}", "body": json.dumps(messages[index][0])}]}
        return await asyncio.to_thread(lambda_handler, event, None, service=service)

    assert await deliver(0) == {"batchItemFailures": []}
    assert await deliver(1) == {"batchItemFailures": []}
    assert messages[2][1] == 600 and messages[3][1] == 0
    assert await deliver(3) == {"batchItemFailures": []}
    clock["now"] = NOW + timedelta(seconds=600)
    assert await deliver(2) == {"batchItemFailures": []}
    async with work_db() as session:
        first = await session.get(Run, ("tenant-one", "run-one"))
        assert first.monitoring["state"] == "current"
        assert first.monitoring["poll_attempts"] == 2
        assert first.monitoring["progress_baseline_at"] == NOW.isoformat()
        assert first.monitoring["last_successful_poll_at"] == clock["now"].isoformat()
        assert "last_progress_at" not in first.monitoring
        second = await session.get(Run, ("tenant-two", "run-two"))
        assert second.state == "succeeded" and second.configuration_revision == 2
        assert second.native_job_ref == native("two")
    clock["now"] = NOW + timedelta(seconds=1200)
    assert await deliver(4) == {"batchItemFailures": []}
    assert await deliver(5) == {"batchItemFailures": []}
    async with work_db() as session:
        first = await session.get(Run, ("tenant-one", "run-one"))
        second = await session.get(Run, ("tenant-two", "run-two"))
        assert (first.state, first.configuration_revision, first.native_job_ref) == (
            "succeeded", 1, native("one"))
        assert first.monitoring["poll_attempts"] == 3
        assert first.monitoring["last_progress_at"] == clock["now"].isoformat()
        assert first.admission["source_snapshots"][0]["snapshot_id"] == "pin-one"
        assert second.admission["source_snapshots"][0]["snapshot_id"] == "pin-two"
        results = (await session.scalars(select(AssessmentResult))).all()
        observations = (await session.scalars(select(ProviderObservation))).all()
        assert {item.tenant_id for item in results} == {"tenant-one", "tenant-two"}
        assert {item.payload["run_id"] for item in results} == {"run-one", "run-two"}
        assert len(observations) == 6
    assert [(item[0], item[1]) for item in calls] == [
        ("observe", "tenant-one"), ("observe", "tenant-two"), ("collect", "tenant-two"),
        ("observe", "tenant-one"), ("observe", "tenant-one"), ("collect", "tenant-one")]


async def test_status_cannot_replace_pinned_native_job(work_db):
    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.workers.execution import ExecutionService

    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "job-one", "record_revision": "1"}
    other_native = {**native, "tenant_id": "tenant-two", "record_id": "job-two"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.native_job_ref, run.state = native, "running"
        run.admission = {"monitoring_policy": {"poll_interval_seconds": 600}}
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "poll"
        await session.commit()

    class Adapter:
        async def observe(self, run, native_job_ref, claim, session):
            assert native_job_ref == native
            return AdapterOutcome("completed", observation={"observed_at": NOW.isoformat(),
                                                           "native_job_ref": other_native,
                                                           "outcome": "completed",
                                                           "progress_coverage": "complete"})

    async def authorize(run, claim, operation):
        assert operation == "observe" and run.tenant_id == claim.tenant_id
        return "verified-session"

    async def publish(envelope, delay):
        raise AssertionError("bad observation must not publish next work")

    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: NOW)
    with pytest.raises(ValueError, match="pinned job"):
        await service.execute({"schema_version": "1.0", "work_id": "work-one", "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        assert run.native_job_ref == native and run.state == "running" and run.revision == 1
        assert (await session.get(Work, ("tenant-one", "work-one"))).completed_at is None


@pytest.mark.parametrize("failure", ["lost_response", "crash_after_success"])
async def test_submit_attempt_survives_uncertain_effect(work_db, monkeypatch, failure):
    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.models import WorkerAttempt
    from l5_services.workers import execution

    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "job-one", "record_revision": "rev-1"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.state, run.manifest = "queued", {"source_snapshot_ids": ["pin-one"], "run_id": "run-one"}
        run.admission = {"scope": {"tenant_id": "tenant-one", "product_id": "product-one",
                                   "project_id": "project-one"},
                         "monitoring_policy": {"poll_interval_seconds": 600}}
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind, work.step = "submit", 1
        await session.commit()

    paid_calls, reconciliations = [], []

    class Adapter:
        async def submit(self, run, manifest, attempt, session):
            assert manifest["source_snapshot_ids"] == ["pin-one"]
            assert session == "verified-submit"
            async with work_db() as readback:
                stored = (await readback.scalars(select(WorkerAttempt))).one()
                assert stored.status == "prepared"
                assert stored.attempt_id == attempt.attempt_id
                assert stored.idempotency_token == attempt.idempotency_token
            paid_calls.append((attempt.attempt_id, attempt.idempotency_token, attempt.fence))
            if failure == "lost_response":
                raise TimeoutError("response lost after native creation")
            return AdapterOutcome("accepted", native_job_ref=native)

        async def reconcile_submit(self, run, attempt, session):
            assert session == "verified-reconcile"
            reconciliations.append((attempt.attempt_id, attempt.idempotency_token, attempt.fence))
            assert attempt.attempt_id == paid_calls[0][0]
            return AdapterOutcome("accepted", native_job_ref=native)

    async def authorize(run, claim, operation):
        assert run.admission["scope"]["tenant_id"] == claim.tenant_id
        return f"verified-{operation}"

    messages, clock = [], {"now": NOW}

    async def publish(envelope, delay):
        messages.append((envelope, delay))

    service = execution.ExecutionService(work_db, None, Adapter(), publish, authorize,
                                         clock=lambda: clock["now"])
    if failure == "crash_after_success":
        original_finish = execution.finish_work
        crashed = []

        async def fail_after_effect(sessions, claim, **kwargs):
            if kwargs.get("attempt_status") == "accepted" and not crashed:
                crashed.append(True)
                raise RuntimeError("worker lost after provider success")
            return await original_finish(sessions, claim, **kwargs)

        monkeypatch.setattr(execution, "finish_work", fail_after_effect)

    envelope = {"schema_version": "1.0", "work_id": "work-one", "generation": 1}
    if failure == "crash_after_success":
        with pytest.raises(RuntimeError, match="worker lost"):
            await service.execute(envelope)
        async with work_db() as readback:
            attempt = (await readback.scalars(select(WorkerAttempt))).one()
            assert attempt.status == "prepared" and attempt.native_job_ref is None
            assert (await readback.get(Work, ("tenant-one", "work-one"))).completed_at is None
        clock["now"] = NOW + timedelta(seconds=61)
        assert await service.execute(envelope)
    else:
        assert await service.execute(envelope)
        async with work_db() as readback:
            attempt = (await readback.scalars(select(WorkerAttempt))).one()
            assert attempt.status == "outcome_unknown" and attempt.native_job_ref is None
            run = await readback.get(Run, ("tenant-one", "run-one"))
            assert run.state == "outcome_unknown" and run.native_job_ref is None
            assert messages[0][1] == 600
        clock["now"] = NOW + timedelta(seconds=600)
        assert await service.execute(messages[0][0])
    assert await service.execute(envelope) is False
    async with work_db() as readback:
        attempt = (await readback.scalars(select(WorkerAttempt))).one()
        run = await readback.get(Run, ("tenant-one", "run-one"))
        works = (await readback.scalars(select(Work).order_by(Work.step))).all()
        assert (attempt.attempt_id, attempt.idempotency_token) == paid_calls[0][:2]
        assert attempt.first_work_id == "work-one" and attempt.first_fence == 1
        assert attempt.status == "accepted" and attempt.native_job_ref == native
        assert run.native_job_ref == native and run.state == "running"
        assert run.configuration_revision == 1 and run.manifest["source_snapshot_ids"] == ["pin-one"]
        assert len(works) == (3 if failure == "lost_response" else 2)
        assert works[-1].kind == "poll" and works[-1].completed_at is None
    assert len(paid_calls) == len(reconciliations) == 1


async def test_ambiguous_reconciliation_keeps_one_attempt_and_owed_work(work_db):
    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.models import WorkerAttempt
    from l5_services.workers.execution import ExecutionService

    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.state, run.manifest = "queued", {"source_snapshot_ids": ["pin-one"]}
        run.admission = {"monitoring_policy": {"poll_interval_seconds": 600}}
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "submit"
        await session.commit()
    submits, reconciles = [], []

    class Adapter:
        async def submit(self, run, manifest, attempt, session):
            submits.append(attempt.idempotency_token)
            return AdapterOutcome("outcome_unknown")

        async def reconcile_submit(self, run, attempt, session):
            reconciles.append(attempt.idempotency_token)
            return AdapterOutcome("outcome_unknown")

    async def authorize(run, claim, operation):
        return "verified"

    queued, clock = [], {"now": NOW}

    async def publish(envelope, delay):
        queued.append(envelope)

    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: clock["now"])
    envelope = {"schema_version": "1.0", "work_id": "work-one", "generation": 1}
    assert await service.execute(envelope)
    for step in range(2):
        clock["now"] += timedelta(seconds=600)
        assert await service.execute(queued[step])
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        attempts = (await session.scalars(select(WorkerAttempt))).all()
        owed = (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).all()
        assert run.state == "outcome_unknown" and run.native_job_ref is None
        assert len(attempts) == 1 and attempts[0].status == "outcome_unknown"
        assert len(owed) == 1 and owed[0].kind == "reconcile"
    assert submits == [reconciles[0]] and len(reconciles) == 2


async def test_loss_and_recovery(work_db):
    import asyncio

    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.dispatcher import dispatch_work
    from l5_services.models import AssessmentResult, WorkerAttempt
    from l5_services.workers.execution import ExecutionService
    from l5_services.workers.recovery import lambda_handler as repair_handler

    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "native-one", "record_revision": "1"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.state, run.provider_state = "running", "running"
        run.native_job_ref = native
        run.manifest = {"run_id": "run-one", "source_snapshot_ids": ["snapshot-one"]}
        run.admission = {"scope": {"tenant_id": "tenant-one", "product_id": "product-one",
                                   "project_id": "project-one"},
                         "monitoring_policy": {"poll_interval_seconds": 600}}
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind, work.step = "poll", 2
        session.add(WorkerAttempt(tenant_id="tenant-one", attempt_id="attempt-one", run_id="run-one",
                                  operation="submit", first_work_id="work-original", first_fence=1,
                                  idempotency_token="b" * 64, input_hash="a" * 64,
                                  started_at=NOW, status="accepted", native_job_ref=native))
        await session.commit()

    delivered = []
    fail_next = {"value": False}

    async def publish(envelope, delay):
        if fail_next["value"]:
            fail_next["value"] = False
            raise ConnectionError("repair tick transport loss")
        delivered.append((envelope, delay))

    clock = {"now": NOW}
    calls = []

    class Adapter:
        async def submit(self, *args):
            raise AssertionError("recovery must not submit")

        async def observe(self, run, native_job_ref, claim, session):
            assert native_job_ref == native and session == "verified-one"
            calls.append("observe")
            return AdapterOutcome("completed", observation={"observed_at": clock["now"].isoformat(),
                                                           "native_job_ref": native, "outcome": "completed",
                                                           "progress_coverage": "complete"})

        async def collect(self, run, native_job_ref, claim, session):
            calls.append("collect")
            return AdapterOutcome("completed", result={"run_id": run.run_id, "native_job_ref": native},
                                  observation={"observed_at": clock["now"].isoformat(),
                                               "native_job_ref": native, "outcome": "completed",
                                               "progress_coverage": "complete"})

    async def authorize(run, claim, action):
        assert run.tenant_id == claim.tenant_id == "tenant-one"
        return "verified-one"

    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: clock["now"])
    assert await dispatch_work(work_db, "tenant-one", "work-one", publish, now=NOW)
    assert delivered == [({"schema_version": "1.0", "work_id": "work-one", "generation": 1}, 0)]
    assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 0}
    clock["now"] = NOW + timedelta(seconds=900)
    fail_next["value"] = True
    assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 0}
    async with work_db() as session:
        work = await session.get(Work, ("tenant-one", "work-one"))
        assert work.completed_at is None and work.attempts == 2
        assert work.last_error == "publication_failed"
    clock["now"] += timedelta(seconds=60)
    assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 1}
    assert delivered[0] == delivered[1]
    assert await service.execute(delivered[1][0])
    assert delivered[2][1] == 0
    assert await service.execute(delivered[2][0])
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        attempt = (await session.scalars(select(WorkerAttempt))).one()
        result = (await session.scalars(select(AssessmentResult))).one()
        work = await session.get(Work, ("tenant-one", "work-one"))
        assert work.completed_at is not None and run.state == "succeeded"
        assert run.manifest["source_snapshot_ids"] == ["snapshot-one"]
        assert run.native_job_ref == attempt.native_job_ref == result.payload["native_job_ref"] == native
        assert (attempt.attempt_id, attempt.idempotency_token, attempt.status) == (
            "attempt-one", "b" * 64, "accepted")
    assert calls == ["observe", "collect"]


async def test_repair_fairness_expired_leases_and_poison_block(work_db):
    from l5_services.dispatcher import MAX_DISPATCH_ATTEMPTS, repair_owed_work

    async with work_db() as session:
        session.add(Configuration(tenant_id="tenant-two", configuration_id="config-two",
                                  product_id="product-two", project_id="project-two", service_id="security",
                                  relationship_revision="1", owner_id="owner-two", current_revision=1,
                                  handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id="tenant-two", configuration_id="config-two",
                                         revision=1, settings={}, inherited_fields=[], gaps=[],
                                         state="ready", hash="e" * 64))
        session.add(Run(tenant_id="tenant-two", run_id="run-two", product_id="product-two",
                        project_id="project-two", service_id="security", configuration_id="config-two",
                        configuration_revision=1, binding_id="binding-two", trigger_id="trigger-two",
                        binding_generation=1, input={}, admission={}, deadline_at=NOW + timedelta(hours=1)))
        session.add(Work(tenant_id="tenant-two", work_id="work-two", run_id="run-two",
                         kind="execute", generation=1, step=0, due_at=NOW))
        for index in range(1, 4):
            session.add(Work(tenant_id="tenant-one", work_id=f"extra-{index}", run_id="run-one",
                             kind="execute", generation=1, step=index, due_at=NOW))
        session.add(Work(tenant_id="tenant-one", work_id="blocked-work", run_id="run-one",
                         kind="execute", generation=1, step=9, due_at=NOW,
                         attempts=MAX_DISPATCH_ATTEMPTS))
        existing = await session.get(Work, ("tenant-one", "work-one"))
        existing.lease_owner = "abandoned-worker"
        existing.lease_until = NOW + timedelta(seconds=120)
        existing.dispatch_owner = "abandoned-dispatch"
        existing.dispatch_until = NOW + timedelta(seconds=60)
        await session.commit()

    delivered = []

    async def publish(envelope, delay):
        delivered.append((envelope, delay))

    assert await repair_owed_work(work_db, publish, now=NOW, limit=2) == 2
    async with work_db() as session:
        sent_tenants = {(await session.get(Work, (tenant, envelope["work_id"]))).tenant_id
                        for envelope, _ in delivered for tenant in ("tenant-one", "tenant-two")
                        if await session.get(Work, (tenant, envelope["work_id"])) is not None}
        assert sent_tenants == {"tenant-one", "tenant-two"}
    assert await repair_owed_work(work_db, publish, now=NOW + timedelta(seconds=61), limit=2) == 2
    assert all(envelope["work_id"] != "work-one" for envelope, _ in delivered)
    assert await repair_owed_work(work_db, publish, now=NOW + timedelta(seconds=121), limit=2) == 1
    assert delivered[-1][0]["work_id"] == "work-one"
    assert await repair_owed_work(work_db, publish, now=NOW + timedelta(seconds=122), limit=2) == 0
    async with work_db() as session:
        blocked = await session.get(Work, ("tenant-one", "blocked-work"))
        assert blocked.blocked_at is not None and blocked.last_error == "dispatch_attempt_limit"
        assert blocked.completed_at is None
        assert (await session.get(Work, ("tenant-one", "work-one"))).lease_owner == "abandoned-worker"
    assert await claim_work(work_db, "blocked-work", 1, now=NOW + timedelta(seconds=122)) is None


async def test_failed_repair_tick_does_not_block_other_tenant(work_db):
    import asyncio
    from types import SimpleNamespace

    from l5_services.workers.recovery import lambda_handler as repair_handler

    async with work_db() as session:
        session.add(Configuration(tenant_id="tenant-two", configuration_id="config-two",
                                  product_id="product-two", project_id="project-two", service_id="security",
                                  relationship_revision="1", owner_id="owner-two", current_revision=1,
                                  handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id="tenant-two", configuration_id="config-two",
                                         revision=2, settings={}, inherited_fields=[], gaps=[],
                                         state="ready", hash="e" * 64))
        session.add(Run(tenant_id="tenant-two", run_id="run-two", product_id="product-two",
                        project_id="project-two", service_id="security", configuration_id="config-two",
                        configuration_revision=2, binding_id="binding-two", trigger_id="trigger-two",
                        binding_generation=1, input={}, admission={}, deadline_at=NOW + timedelta(hours=1)))
        session.add(Work(tenant_id="tenant-two", work_id="work-two", run_id="run-two",
                         kind="execute", generation=1, step=0, due_at=NOW))
        await session.commit()

    queued = []
    failures = {"work-one": 1}

    async def publish(envelope, delay):
        work_id = envelope["work_id"]
        if failures.get(work_id, 0):
            failures[work_id] -= 1
            raise ConnectionError("one tenant's queue send failed")
        queued.append(envelope)

    clock = {"now": NOW}
    service = SimpleNamespace(sessions=work_db, publish=publish, clock=lambda: clock["now"])
    assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 1}
    assert [message["work_id"] for message in queued] == ["work-two"]
    async with work_db() as readback:
        first = await readback.get(Work, ("tenant-one", "work-one"))
        second = await readback.get(Work, ("tenant-two", "work-two"))
        assert first.completed_at is None and first.last_error == "publication_failed"
        assert second.completed_at is None and second.last_enqueued_at is not None
        assert (await readback.get(Run, ("tenant-two", "run-two"))).configuration_revision == 2
    clock["now"] += timedelta(seconds=60)
    assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 1}
    assert [message["work_id"] for message in queued] == ["work-two", "work-one"]
    async with work_db() as readback:
        first = await readback.get(Work, ("tenant-one", "work-one"))
        second = await readback.get(Work, ("tenant-two", "work-two"))
        assert first.last_error is None and first.last_enqueued_at is not None
        assert second.attempts == 1 and second.completed_at is None


async def test_restart_recovers_committed_next_work(work_db, monkeypatch):
    import asyncio
    import json

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services import dispatcher
    from l5_services.models import AssessmentResult, WorkerAttempt
    from l5_services.workers.execution import ExecutionService, lambda_handler
    from l5_services.workers.recovery import lambda_handler as repair_handler

    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "job-one", "record_revision": "rev-1"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.state, run.provider_state = "running", "running"
        run.native_job_ref = native
        run.manifest = {"run_id": "run-one", "source_snapshot_ids": ["source-pin-1"]}
        run.admission = {"scope": {"tenant_id": "tenant-one", "product_id": "product-one",
                                   "project_id": "project-one"},
                         "monitoring_policy": {"poll_interval_seconds": 600}}
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind, work.step = "poll", 2
        session.add(WorkerAttempt(tenant_id="tenant-one", attempt_id="attempt-one", run_id="run-one",
                                  operation="submit", first_work_id="submit-work", first_fence=1,
                                  idempotency_token="c" * 64, input_hash="d" * 64,
                                  started_at=NOW, status="accepted", native_job_ref=native))
        await session.commit()

    calls = []

    class Adapter:
        async def submit(self, *args):
            raise AssertionError("restart must never submit a new provider assessment")

        async def observe(self, run, native_job_ref, claim, session):
            assert native_job_ref == native and run.configuration_revision == 1
            calls.append("observe")
            return AdapterOutcome("completed", observation={"observed_at": NOW.isoformat(),
                                                           "native_job_ref": native, "outcome": "completed",
                                                           "progress_coverage": "complete"})

        async def collect(self, run, native_job_ref, claim, session):
            assert native_job_ref == native and session == "current-grant"
            calls.append("collect")
            return AdapterOutcome("completed", result={"run_id": run.run_id, "native_job_ref": native},
                                  observation={"observed_at": NOW.isoformat(),
                                               "native_job_ref": native, "outcome": "completed",
                                               "progress_coverage": "complete"})

    async def authorize(run, claim, action):
        assert run.tenant_id == claim.tenant_id == "tenant-one"
        return "current-grant"

    delivered = []

    async def publish(envelope, delay):
        delivered.append((envelope, delay))

    original_dispatch = dispatcher.dispatch_work

    async def lose_dispatch_response(*args, **kwargs):
        raise ConnectionError("DB transport failed after commit")

    monkeypatch.setattr(dispatcher, "dispatch_work", lose_dispatch_response)
    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: NOW)
    original_message = {"schema_version": "1.0", "work_id": "work-one", "generation": 1}
    event = {"Records": [{"messageId": "message-one", "body": json.dumps(original_message)}]}
    assert await asyncio.to_thread(lambda_handler, event, None, service=service) == {"batchItemFailures": []}
    assert calls == ["observe"] and delivered == []
    async with work_db() as readback:
        old = await readback.get(Work, ("tenant-one", "work-one"))
        owed = (await readback.scalars(select(Work).where(Work.kind == "collect"))).one()
        assert old.completed_at is not None
        assert owed.completed_at is None and owed.last_enqueued_at is None
        assert (await readback.get(Run, ("tenant-one", "run-one"))).state == "collecting"

    monkeypatch.setattr(dispatcher, "dispatch_work", original_dispatch)
    restarted_sessions = async_sessionmaker(work_db.kw["bind"], expire_on_commit=False)
    restarted = ExecutionService(restarted_sessions, None, Adapter(), publish, authorize,
                                 clock=lambda: NOW + timedelta(seconds=60))
    assert await asyncio.to_thread(repair_handler, {}, None, service=restarted) == {"enqueued": 1}
    assert delivered[0][1] == 0 and delivered[0][0]["work_id"] == owed.work_id
    recovered = {"Records": [{"messageId": "message-two", "body": json.dumps(delivered[0][0])}]}
    assert await asyncio.to_thread(lambda_handler, recovered, None, service=restarted) == {
        "batchItemFailures": []}
    assert await restarted.execute(original_message) is False
    async with restarted_sessions() as readback:
        run = await readback.get(Run, ("tenant-one", "run-one"))
        attempt = (await readback.scalars(select(WorkerAttempt))).one()
        result = (await readback.scalars(select(AssessmentResult))).one()
        assert run.state == "succeeded" and run.revision == 3
        assert run.native_job_ref == attempt.native_job_ref == result.payload["native_job_ref"] == native
        assert run.manifest["source_snapshot_ids"] == ["source-pin-1"]
        assert attempt.attempt_id == "attempt-one" and attempt.idempotency_token == "c" * 64
        assert (await readback.get(Work, ("tenant-one", owed.work_id))).completed_at is not None
    assert calls == ["observe", "collect"]


@pytest.mark.parametrize("intent", ["cancel", "cancel_unknown", "deadline_stopped", "deadline_running", "revoked"])
async def test_revoke_deadline_cancel(work_db, intent):
    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.admission import cancel_and_dispatch
    from l5_services.run_access import RunStore
    from l5_services.workers.execution import AuthorityRevoked, ExecutionService

    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "job-one", "record_revision": "1"}
    clock = {"now": NOW}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.native_job_ref = native
        run.state, run.provider_state = "running", "running"
        run.admission = {"scope": {"tenant_id": "tenant-one", "product_id": "product-one",
                                   "project_id": "project-one", "owner_id": "owner", "visibility": "only_me"},
                         "limits": {"max_provider_attempts": 1, "max_polls": 2},
                         "monitoring_policy": {"poll_interval_seconds": 600}}
        if intent.startswith("deadline"):
            run.deadline_at = NOW
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind, work.step = "poll", 2
        await session.commit()

    messages, calls = [], []

    async def publish(envelope, delay):
        messages.append((envelope, delay))

    class Adapter:
        async def submit(self, *args):
            raise AssertionError("fenced work must never submit")

        async def cancel(self, run, native_job_ref, claim, session):
            assert native_job_ref == native and session == "current-grant"
            calls.append("cancel")
            state = "running" if intent in {"cancel", "deadline_running"} else "unknown" if intent == "cancel_unknown" else "stopped"
            return AdapterOutcome(state, observation={"observed_at": clock["now"].isoformat(),
                                                      "native_job_ref": native, "outcome": state,
                                                      "progress_coverage": "unavailable"})

        async def observe(self, run, native_job_ref, claim, session):
            assert native_job_ref == native and session == "current-grant"
            calls.append("observe")
            return AdapterOutcome("stopped", observation={"observed_at": clock["now"].isoformat(),
                                                         "native_job_ref": native, "outcome": "stopped",
                                                         "progress_coverage": "unavailable"})

    async def authorize(run, claim, operation):
        if intent == "revoked":
            raise AuthorityRevoked("delegation revoked")
        assert run.tenant_id == claim.tenant_id and operation in {"cancel", "observe"}
        return "current-grant"

    if intent in {"cancel", "cancel_unknown"}:
        async with work_db() as session:
            store = RunStore(session, "tenant-one", "product-one", "project-one", "security", "owner")
            response = await cancel_and_dispatch(store, work_db, publish, "run-one", "requested stop",
                                                 "cancel-key-000000001", now=NOW)
        assert response["state"] == "cancel_requested"
        async with work_db() as session:
            old = await session.get(Work, ("tenant-one", "work-one"))
            assert old.completed_at is not None
        envelope = messages[0][0]
    else:
        envelope = {"schema_version": "1.0", "work_id": "work-one", "generation": 1}

    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: clock["now"])
    assert await service.execute(envelope)
    if intent == "cancel":
        assert calls == ["cancel"] and messages[1][1] == 600
        async with work_db() as session:
            run = await session.get(Run, ("tenant-one", "run-one"))
            assert (run.state, run.provider_state) == ("cancel_requested", "running")
        clock["now"] += timedelta(seconds=600)
        assert await service.execute(messages[1][0])
        assert calls == ["cancel", "observe"]
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        pending = (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).all()
        assert pending == []
        assert run.native_job_ref == native
        if intent == "cancel":
            assert (run.state, run.provider_state, run.cancel_reason) == (
                "cancelled", "stopped", "requested stop")
        elif intent == "cancel_unknown":
            assert (run.state, run.provider_state, run.cancel_reason) == (
                "outcome_unknown", "unknown", "requested stop")
            assert calls == ["cancel"]
        elif intent == "deadline_stopped":
            assert (run.state, run.provider_state) == ("expired", "stopped")
            assert calls == ["cancel"]
        elif intent == "deadline_running":
            assert (run.state, run.provider_state) == ("expired", "running")
            assert calls == ["cancel"]
        else:
            assert (run.state, run.provider_state) == ("outcome_unknown", "running")
            assert calls == []


async def test_revocation_before_submit_fences_provider_effect(work_db):
    from l5_services.models import WorkerAttempt
    from l5_services.workers.execution import AuthorityRevoked, ExecutionService

    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.state, run.manifest = "queued", {"run_id": "run-one", "source_snapshot_ids": ["snapshot-one"]}
        run.admission = {"limits": {"max_provider_attempts": 1, "max_polls": 1},
                         "monitoring_policy": {"poll_interval_seconds": 600}}
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "submit"
        await session.commit()

    class Adapter:
        async def submit(self, *args):
            raise AssertionError("revoked delegation must not start provider work")

    async def authorize(run, claim, action):
        raise AuthorityRevoked("grant revoked")

    async def publish(envelope, delay):
        raise AssertionError("blocked run has no new effect work")

    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: NOW)
    assert await service.execute({"schema_version": "1.0", "work_id": "work-one", "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        attempts = (await session.scalars(select(WorkerAttempt))).all()
        assert (run.state, run.provider_state, run.native_job_ref) == (
            "blocked", "unknown", None)
        assert len(attempts) == 1 and attempts[0].status == "prepared"
        assert (await session.get(Work, ("tenant-one", "work-one"))).completed_at is not None


@pytest.mark.parametrize("work_kind", ["poll", "submit"])
async def test_total_poll_and_reconcile_bounds(work_db, work_kind):
    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.models import WorkerAttempt
    from l5_services.workers.execution import ExecutionService

    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "job-one", "record_revision": "1"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.admission = {"limits": {"max_provider_attempts": 1, "max_polls": 1},
                         "monitoring_policy": {"poll_interval_seconds": 600}}
        if work_kind == "poll":
            run.state, run.provider_state, run.native_job_ref = "running", "in_progress", native
        else:
            run.state, run.manifest = "queued", {"source_snapshot_ids": ["snapshot-one"]}
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind, work.step = work_kind, 1
        await session.commit()

    clock = {"now": NOW}
    calls = []

    class Adapter:
        async def submit(self, run, manifest, attempt, session):
            calls.append("submit")
            return AdapterOutcome("outcome_unknown")

        async def reconcile_submit(self, run, attempt, session):
            calls.append("reconcile")
            return AdapterOutcome("outcome_unknown")

        async def observe(self, run, native_job_ref, claim, session):
            calls.append("observe")
            return AdapterOutcome("running", observation={"observed_at": clock["now"].isoformat(),
                                                          "native_job_ref": native,
                                                          "outcome": "running", "progress_coverage": "partial"})

    async def authorize(run, claim, action):
        assert run.tenant_id == claim.tenant_id
        return "verified"

    queued = []

    async def publish(envelope, delay):
        queued.append(envelope)

    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: clock["now"])
    assert await service.execute({"schema_version": "1.0", "work_id": "work-one", "generation": 1})
    clock["now"] = NOW + timedelta(seconds=600)
    assert await service.execute(queued[0])
    clock["now"] = NOW + timedelta(seconds=1200)
    if work_kind == "submit":
        assert await service.execute(queued[1])
        assert calls == ["submit", "reconcile"]
    else:
        assert calls == ["observe"]
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        owed = (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).all()
        assert owed == [] and run.state == ("expired" if work_kind == "poll" else "outcome_unknown")
        if work_kind == "submit":
            attempts = (await session.scalars(select(WorkerAttempt))).all()
            assert len(attempts) == 1 and attempts[0].reconcile_checks == 1
            assert attempts[0].status == "outcome_unknown" and run.native_job_ref is None
        else:
            assert run.monitoring["poll_attempts"] == 1
            assert run.provider_state == "running" and run.native_job_ref == native


async def test_cancel_during_uncertain_submit_reconciles_original_job(work_db):
    import json
    from hashlib import sha256

    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.admission import cancel_and_dispatch
    from l5_services.models import WorkerAttempt
    from l5_services.run_access import RunStore
    from l5_services.workers.execution import ExecutionService

    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "job-one", "record_revision": "1"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.state, run.provider_state = "outcome_unknown", "unknown"
        run.manifest = {"source_snapshot_ids": ["source-pin-one"]}
        run.admission = {"scope": {"tenant_id": "tenant-one", "product_id": "product-one",
                                   "project_id": "project-one", "owner_id": "owner", "visibility": "only_me"},
                         "limits": {"max_provider_attempts": 1, "max_polls": 2},
                         "monitoring_policy": {"poll_interval_seconds": 600}}
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "reconcile"
        session.add(WorkerAttempt(tenant_id="tenant-one", attempt_id="attempt-one", run_id="run-one",
                                  operation="submit", first_work_id="submit-work", first_fence=1,
                                  idempotency_token="c" * 64, input_hash=sha256(json.dumps(
                                      run.manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                                  started_at=NOW, status="prepared"))
        await session.commit()

    delivered = []

    async def publish(envelope, delay):
        delivered.append((envelope, delay))

    async with work_db() as session:
        store = RunStore(session, "tenant-one", "product-one", "project-one", "security", "owner")
        response = await cancel_and_dispatch(store, work_db, publish, "run-one", "user stop",
                                             "cancel-unknown-0001", now=NOW)
        assert response["state"] == "cancel_requested"
    async with work_db() as session:
        assert (await session.get(Work, ("tenant-one", "work-one"))).completed_at is not None
    calls = []

    class Adapter:
        async def submit(self, *args):
            raise AssertionError("cancel cannot submit another paid job")

        async def reconcile_submit(self, run, attempt, session):
            calls.append("reconcile")
            assert attempt.attempt_id == "attempt-one" and attempt.idempotency_token == "c" * 64
            return AdapterOutcome("accepted", native_job_ref=native)

        async def cancel(self, run, native_job_ref, claim, session):
            calls.append("cancel")
            assert native_job_ref == native and session == "verified"
            return AdapterOutcome("stopped", observation={"observed_at": NOW.isoformat(),
                                                         "native_job_ref": native, "outcome": "stopped",
                                                         "progress_coverage": "unavailable"})

    async def authorize(run, claim, operation):
        assert operation in {"reconcile", "cancel"} and run.tenant_id == claim.tenant_id
        return "verified"

    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: NOW)
    assert await service.execute(delivered[0][0])
    assert delivered[1][1] == 0
    assert await service.execute(delivered[1][0])
    assert calls == ["reconcile", "cancel"]
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        attempt = (await session.scalars(select(WorkerAttempt))).one()
        assert (run.state, run.provider_state, run.cancel_reason) == (
            "cancelled", "stopped", "user stop")
        assert run.native_job_ref == attempt.native_job_ref == native
        assert run.manifest["source_snapshot_ids"] == ["source-pin-one"]
        assert attempt.reconcile_checks == 1 and attempt.status == "accepted"
        assert not (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).all()


@pytest.mark.parametrize("status", [403, 404])
async def test_revoked_broker_response_is_a_terminal_authority_fence(work_db, status):
    import httpx

    from l5_services.workers.execution import ExecutionService

    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.state, run.manifest = "queued", {"source_snapshot_ids": ["pin-one"]}
        run.admission = {"limits": {"max_provider_attempts": 1, "max_polls": 1}}
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "submit"
        await session.commit()

    class Adapter:
        async def submit(self, *args):
            raise AssertionError("denied broker must not start provider work")

    async def authorize(run, claim, action):
        request = httpx.Request("POST", "http://internal.test/delegation")
        response = httpx.Response(status, request=request, json={"detail": {"code": "delegation_denied", "message": "private provider text"}})
        raise httpx.HTTPStatusError("revoked", request=request, response=response)

    async def publish(envelope, delay):
        raise AssertionError("denied provider work has no successor")

    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: NOW)
    assert await service.execute({"schema_version": "1.0", "work_id": "work-one", "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        assert (run.state, run.provider_state) == ("blocked", "unknown")
        assert run.monitoring["last_poll_error"]["stage"] == "submit"
        assert run.monitoring["last_poll_error"]["cause"] == "delegation_denied"
        assert "Reconcile" in run.monitoring["last_poll_error"]["action"]
        assert "private provider text" not in str(run.monitoring)
        assert (await session.get(Work, ("tenant-one", "work-one"))).completed_at is not None


async def test_cancel_response_loss_reconciles_status_without_second_cancel(work_db, monkeypatch):
    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.workers import execution

    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "job-one", "record_revision": "1"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.native_job_ref, run.state, run.provider_state = native, "cancel_requested", "running"
        run.admission = {"limits": {"max_polls": 2}, "monitoring_policy": {"poll_interval_seconds": 600}}
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "cancel"
        await session.commit()

    calls = []
    observation = {"observed_at": NOW.isoformat(), "native_job_ref": native,
                   "outcome": "stopped", "progress_coverage": "unavailable"}

    class Adapter:
        async def cancel(self, run, native_job_ref, claim, session):
            calls.append("cancel")
            return AdapterOutcome("stopped", observation=observation)

        async def observe(self, run, native_job_ref, claim, session):
            calls.append("observe")
            return AdapterOutcome("stopped", observation=observation)

    async def authorize(run, claim, action):
        assert action in {"cancel", "observe"}
        return "verified"

    async def publish(envelope, delay):
        raise AssertionError("confirmed cancellation schedules no further work")

    original_finish = execution.finish_work
    crashed = []

    async def crash_after_remote_cancel(sessions, claim, **kwargs):
        if kwargs["state"] == "cancelled" and not crashed:
            crashed.append(True)
            raise RuntimeError("worker crashed before commit")
        return await original_finish(sessions, claim, **kwargs)

    monkeypatch.setattr(execution, "finish_work", crash_after_remote_cancel)
    clock = {"now": NOW}
    service = execution.ExecutionService(work_db, None, Adapter(), publish, authorize,
                                         clock=lambda: clock["now"])
    envelope = {"schema_version": "1.0", "work_id": "work-one", "generation": 1}
    with pytest.raises(RuntimeError, match="worker crashed"):
        await service.execute(envelope)
    async with work_db() as session:
        assert (await session.get(Work, ("tenant-one", "work-one"))).completed_at is None
        assert (await session.get(Run, ("tenant-one", "run-one"))).state == "cancel_requested"
    clock["now"] = NOW + timedelta(seconds=61)
    assert await service.execute(envelope)
    assert calls == ["cancel", "observe"]
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        assert (run.state, run.provider_state) == ("cancelled", "stopped")
        assert (await session.get(Work, ("tenant-one", "work-one"))).fence == 2


@pytest.mark.parametrize("work_kind", ["submit", "poll"])
async def test_deadline_wakeup_precedes_next_poll(work_db, work_kind):
    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.workers.execution import ExecutionService

    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "job-one", "record_revision": "1"}
    deadline = NOW + timedelta(seconds=120)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.deadline_at = deadline
        run.admission = {"limits": {"max_polls": 3}, "monitoring_policy": {"poll_interval_seconds": 600}}
        if work_kind == "submit":
            run.state, run.manifest = "queued", {"source_snapshot_ids": ["snapshot-one"]}
        else:
            run.state, run.provider_state, run.native_job_ref = "running", "running", native
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = work_kind
        await session.commit()

    calls = []

    class Adapter:
        async def submit(self, run, manifest, attempt, session):
            calls.append("submit")
            return AdapterOutcome("accepted", native_job_ref=native)

        async def observe(self, run, native_job_ref, claim, session):
            calls.append("observe")
            return AdapterOutcome("running", observation={"native_job_ref": native,
                                                          "observed_at": NOW.isoformat(),
                                                          "outcome": "running", "progress_coverage": "partial"})

        async def cancel(self, run, native_job_ref, claim, session):
            calls.append("cancel")
            return AdapterOutcome("running")

    async def authorize(run, claim, operation):
        assert run.run_id == claim.run_id
        return "current-grant"

    messages = []

    async def publish(envelope, delay):
        messages.append((envelope, delay))

    clock = {"now": NOW}
    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: clock["now"])
    assert await service.execute({"schema_version": "1.0", "work_id": "work-one", "generation": 1})
    assert len(messages) == 1 and messages[0][1] == 120
    async with work_db() as session:
        owed = (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).one()
        assert owed.due_at.replace(tzinfo=UTC) == deadline
    clock["now"] = deadline
    assert await service.execute(messages[0][0])
    assert calls == [work_kind if work_kind == "submit" else "observe", "cancel"]
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        assert (run.state, run.provider_state) == ("expired", "running")
        assert not (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).all()


async def test_two_tenant_monitoring_faults_and_native_outcomes(work_db):
    import asyncio
    import json

    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.models import ProviderObservation, WorkerAttempt
    from l5_services.workers.execution import ExecutionService, lambda_handler

    def native(suffix):
        return {"tenant_id": f"tenant-{suffix}", "project_id": f"project-{suffix}",
                "source_connection_id": f"conn-{suffix}", "record_type": "native_job",
                "record_id": f"job-{suffix}", "record_revision": "rev-1"}

    async with work_db() as session:
        first = await session.get(Run, ("tenant-one", "run-one"))
        first.state, first.provider_state, first.native_job_ref = "running", "running", native("one")
        first.admission = {"scope": {"tenant_id": "tenant-one", "product_id": "product-one"},
                           "configuration_hash": "a" * 64, "source_snapshots": [{"snapshot_id": "pin-one"}],
                           "limits": {"max_polls": 6},
                           "monitoring_policy": {"poll_interval_seconds": 600, "poll_grace_seconds": 300,
                                                 "stall_after_seconds": 1200}}
        first_work = await session.get(Work, ("tenant-one", "work-one"))
        first_work.kind, first_work.step = "poll", 2
        session.add(Configuration(tenant_id="tenant-two", configuration_id="config-two",
                                  product_id="product-two", project_id="project-two", service_id="security",
                                  relationship_revision="1", owner_id="owner-two", current_revision=2,
                                  handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id="tenant-two", configuration_id="config-two",
                                         revision=2, settings={}, inherited_fields=[], gaps=[], state="ready",
                                         hash="b" * 64))
        session.add(Run(tenant_id="tenant-two", run_id="run-two", product_id="product-two",
                        project_id="project-two", service_id="security", configuration_id="config-two",
                        configuration_revision=2, binding_id="binding-two", trigger_id="trigger-two",
                        binding_generation=1, input={"sources": []},
                        admission={"scope": {"tenant_id": "tenant-two", "product_id": "product-two"},
                                   "configuration_hash": "b" * 64,
                                   "source_snapshots": [{"snapshot_id": "pin-two"}],
                                   "limits": {"max_polls": 3},
                                   "monitoring_policy": {"poll_interval_seconds": 600}},
                        native_job_ref=native("two"), state="running", provider_state="running",
                        deadline_at=NOW + timedelta(hours=2)))
        session.add(Work(tenant_id="tenant-two", work_id="work-two", run_id="run-two",
                         kind="poll", generation=1, step=2, due_at=NOW))
        for suffix in ("one", "two"):
            session.add(WorkerAttempt(tenant_id=f"tenant-{suffix}", attempt_id=f"attempt-{suffix}",
                                      run_id=f"run-{suffix}", operation="submit", first_work_id=f"start-{suffix}",
                                      first_fence=1, idempotency_token=suffix[0] * 64,
                                      input_hash="c" * 64, started_at=NOW, status="accepted",
                                      native_job_ref=native(suffix)))
        await session.commit()

    clock = {"now": NOW}
    reads = {"one": 0, "two": 0}

    class Adapter:
        async def observe(self, run, native_job_ref, claim, provider_session):
            suffix = run.tenant_id.split("-")[-1]
            assert provider_session == f"current-{suffix}"
            assert native_job_ref == native(suffix) and claim.native_job_ref == native_job_ref
            assert run.admission["source_snapshots"][0]["snapshot_id"] == f"pin-{suffix}"
            reads[suffix] += 1
            count = reads[suffix]
            if suffix == "one" and count == 4:
                raise TimeoutError("private provider error: must not persist")
            classification = ("failed" if suffix == "two" else
                              "unknown" if count == 5 else "stopped" if count == 6 else "running")
            observation = {"observed_at": clock["now"].isoformat(), "native_job_ref": native_job_ref,
                           "outcome": classification, "progress_coverage": "complete" if classification == "running" else "unavailable"}
            if classification == "running":
                observation["progress_fingerprint"] = "a" * 64
            if classification == "failed":
                observation["error"] = {"code": "native_failure"}
            return AdapterOutcome(classification, observation=observation)

    async def authorize(run, claim, operation):
        assert operation == "observe" and run.tenant_id == claim.tenant_id
        assert run.product_id == run.admission["scope"]["product_id"]
        return f"current-{run.tenant_id.split('-')[-1]}"

    messages = []

    async def publish(envelope, delay):
        messages.append((envelope, delay))

    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: clock["now"])

    async def deliver(work_id):
        event = {"Records": [{"messageId": work_id, "body": json.dumps(
            {"schema_version": "1.0", "work_id": work_id, "generation": 1})}]}
        assert await asyncio.to_thread(lambda_handler, event, None, service=service) == {"batchItemFailures": []}

    await deliver("work-one")
    await deliver("work-two")
    async with work_db() as readback:
        first = await readback.get(Run, ("tenant-one", "run-one"))
        second = await readback.get(Run, ("tenant-two", "run-two"))
        assert (first.state, first.provider_state, first.revision) == ("running", "running", 2)
        assert first.monitoring["progress_state"] == "observed"
        assert (second.state, second.provider_state, second.revision) == ("failed", "failed", 2)
        assert second.monitoring["state"] == "finished"
        assert second.monitoring["latest_observation"]["error"]["code"] == "native_failure"
        assert (await readback.get(WorkerAttempt, ("tenant-two", "attempt-two"))).native_job_ref == native("two")
    assert messages[0][1] == 600 and len(messages) == 1

    for minute in (10, 20):
        clock["now"] = NOW + timedelta(minutes=minute)
        async with work_db() as readback:
            work = (await readback.scalars(select(Work).where(Work.tenant_id == "tenant-one",
                                                      Work.completed_at.is_(None)))).one()
            next_id = work.work_id
        await deliver(next_id)
    async with work_db() as readback:
        first = await readback.get(Run, ("tenant-one", "run-one"))
        assert (first.state, first.provider_state, first.revision) == ("running", "running", 4)
        assert first.monitoring["progress_state"] == "suspected_stalled"
        assert first.monitoring["last_successful_poll_at"] == clock["now"].isoformat()
        assert "last_progress_at" not in first.monitoring
        assert first.monitoring["poll_attempts"] == 3

    clock["now"] = NOW + timedelta(minutes=30)
    async with work_db() as readback:
        next_id = (await readback.scalars(select(Work).where(
            Work.tenant_id == "tenant-one", Work.completed_at.is_(None)))).one().work_id
    await deliver(next_id)
    async with work_db() as readback:
        first = await readback.get(Run, ("tenant-one", "run-one"))
        assert (first.state, first.provider_state) == ("running", "unknown")
        assert first.monitoring["state"] == "degraded" and first.monitoring["consecutive_poll_errors"] == 1
        assert first.monitoring["last_poll_error"] == {"code": "status_read_failed"}
        assert first.monitoring["progress_state"] == "unknown"
        assert first.monitoring["latest_observation"]["outcome"] == "running"
        assert first.monitoring["last_successful_poll_at"] == (NOW + timedelta(minutes=20)).isoformat()
        assert "private provider error" not in str(first.monitoring)

    clock["now"] = NOW + timedelta(minutes=40)
    async with work_db() as readback:
        next_id = (await readback.scalars(select(Work).where(
            Work.tenant_id == "tenant-one", Work.completed_at.is_(None)))).one().work_id
    await deliver(next_id)
    async with work_db() as readback:
        first = await readback.get(Run, ("tenant-one", "run-one"))
        assert (first.state, first.provider_state) == ("running", "unknown")
        assert first.monitoring["consecutive_poll_errors"] == 2
        assert first.monitoring["last_poll_error"] == {"code": "status_unknown"}
        assert first.monitoring["latest_observation"]["outcome"] == "running"
        assert first.monitoring["last_successful_poll_at"] == (NOW + timedelta(minutes=20)).isoformat()

    clock["now"] = NOW + timedelta(minutes=50)
    async with work_db() as readback:
        next_id = (await readback.scalars(select(Work).where(
            Work.tenant_id == "tenant-one", Work.completed_at.is_(None)))).one().work_id
    await deliver(next_id)
    async with work_db() as readback:
        first = await readback.get(Run, ("tenant-one", "run-one"))
        second = await readback.get(Run, ("tenant-two", "run-two"))
        attempts = (await readback.scalars(select(WorkerAttempt).order_by(WorkerAttempt.tenant_id))).all()
        observations = (await readback.scalars(select(ProviderObservation).order_by(ProviderObservation.observed_at))).all()
        owed = (await readback.scalars(select(Work).where(Work.completed_at.is_(None)))).all()
        assert (first.state, first.provider_state, first.revision) == ("failed", "stopped", 7)
        assert (second.state, second.provider_state, second.revision) == ("failed", "failed", 2)
        assert first.monitoring["state"] == "finished" and first.monitoring["consecutive_poll_errors"] == 0
        assert first.monitoring["latest_observation"]["outcome"] == "stopped"
        assert first.monitoring["poll_attempts"] == 6 and second.monitoring["poll_attempts"] == 1
        assert [(attempt.tenant_id, attempt.status, attempt.native_job_ref) for attempt in attempts] == [
            ("tenant-one", "accepted", native("one")), ("tenant-two", "accepted", native("two"))]
        assert [item.payload["outcome"] for item in observations if item.tenant_id == "tenant-one"] == [
            "running", "running", "running", "unknown", "stopped"]
        assert [item.payload["outcome"] for item in observations if item.tenant_id == "tenant-two"] == ["failed"]
        assert owed == [] and reads == {"one": 6, "two": 1}


async def test_overdue_check_and_partial_progress_cannot_claim_stall(work_db):
    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.workers.execution import ExecutionService

    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "job-one", "record_revision": "rev-1"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.native_job_ref, run.state, run.provider_state = native, "running", "running"
        run.admission = {"limits": {"max_polls": 6},
                         "monitoring_policy": {"poll_interval_seconds": 600, "poll_grace_seconds": 300,
                                               "stall_after_seconds": 1200}}
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "poll"
        await session.commit()

    clock = {"now": NOW + timedelta(seconds=301)}
    reads = []

    class Adapter:
        async def observe(self, run, native_job_ref, claim, provider_session):
            async with work_db() as independent:
                monitoring = (await independent.get(Run, ("tenant-one", "run-one"))).monitoring
                if not reads:
                    assert monitoring["state"] == "overdue"
                assert monitoring["poll_attempts"] == len(reads) + 1
            reads.append(clock["now"])
            coverage = "partial" if len(reads) == 2 else "complete"
            fingerprint = "b" * 64 if len(reads) == 5 else "a" * 64
            observation = {"native_job_ref": native, "observed_at": clock["now"].isoformat(),
                           "outcome": "running", "progress_coverage": coverage}
            if coverage == "complete":
                observation["progress_fingerprint"] = fingerprint
            return AdapterOutcome("running", observation=observation)

    async def authorize(run, claim, operation):
        assert operation == "observe" and claim.native_job_ref == native
        return "current-grant"

    published = []

    async def publish(envelope, delay):
        published.append((envelope, delay))

    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: clock["now"])
    await service.execute({"schema_version": "1.0", "work_id": "work-one", "generation": 1})
    async with work_db() as independent:
        monitoring = (await independent.get(Run, ("tenant-one", "run-one"))).monitoring
        assert monitoring["state"] == "current"
        assert monitoring["progress_baseline_at"] == clock["now"].isoformat()
    for attempt in range(2, 6):
        clock["now"] += timedelta(seconds=600)
        assert await service.execute(published[-1][0])
        async with work_db() as independent:
            monitoring = (await independent.get(Run, ("tenant-one", "run-one"))).monitoring
            assert monitoring["poll_attempts"] == attempt
            assert monitoring["progress_state"] == ("unknown" if attempt == 2 else "observed")
            if attempt == 2:
                assert "progress_baseline_at" not in monitoring
            if attempt == 3:
                assert monitoring["progress_baseline_at"] == clock["now"].isoformat()
            if attempt == 5:
                assert monitoring["last_progress_at"] == clock["now"].isoformat()
    assert len(reads) == 5


async def test_conflicting_native_failure_evidence_cannot_finish_run(work_db):
    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.workers.execution import ExecutionService

    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "job-one", "record_revision": "rev-1"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.native_job_ref, run.state, run.provider_state = native, "running", "running"
        run.admission = {"limits": {"max_polls": 2},
                         "monitoring_policy": {"poll_interval_seconds": 600}}
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "poll"
        await session.commit()

    class Adapter:
        async def observe(self, run, native_job_ref, claim, session):
            return AdapterOutcome("failed", observation={"native_job_ref": native,
                                                          "observed_at": NOW.isoformat(),
                                                          "outcome": "running", "progress_coverage": "unavailable"})

    async def authorize(run, claim, operation):
        return "current"

    async def publish(envelope, delay):
        raise AssertionError("conflicting status must not schedule more work")

    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: NOW)
    with pytest.raises(ValueError, match="classification does not match"):
        await service.execute({"schema_version": "1.0", "work_id": "work-one", "generation": 1})
    async with work_db() as readback:
        run = await readback.get(Run, ("tenant-one", "run-one"))
        work = await readback.get(Work, ("tenant-one", "work-one"))
        assert run.state == "running" and run.provider_state == "running"
        assert run.monitoring["poll_attempts"] == 1 and work.completed_at is None


async def test_expired_poll_lease_does_not_spend_an_attempt(work_db):
    from l5_services.work_store import record_poll_attempt

    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.admission = {"limits": {"max_polls": 1}, "monitoring_policy": {"poll_grace_seconds": 300}}
        await session.commit()
    claim = await claim_work(work_db, "work-one", 1, now=NOW)
    assert claim is not None
    assert await record_poll_attempt(work_db, claim, now=NOW + timedelta(seconds=61)) is False
    async with work_db() as readback:
        run = await readback.get(Run, ("tenant-one", "run-one"))
        work = await readback.get(Work, ("tenant-one", "work-one"))
        assert run.monitoring is None and work.completed_at is None
    replacement = await claim_work(work_db, "work-one", 1, now=NOW + timedelta(seconds=61))
    assert replacement is not None and replacement.fence > claim.fence
    assert await record_poll_attempt(work_db, replacement, now=NOW + timedelta(seconds=61))
    async with work_db() as readback:
        assert (await readback.get(Run, ("tenant-one", "run-one"))).monitoring["poll_attempts"] == 1


@pytest.mark.parametrize("elapsed", [30, 61])
async def test_provider_reply_uses_completion_time_for_lease_and_dispatch(work_db, elapsed):
    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.models import ProviderObservation
    from l5_services.workers.execution import ExecutionService

    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "job-one", "record_revision": "rev-1"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.native_job_ref, run.state, run.provider_state = native, "running", "running"
        run.admission = {"limits": {"max_polls": 3},
                         "monitoring_policy": {"poll_interval_seconds": 600, "poll_grace_seconds": 300}}
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "poll"
        await session.commit()

    clock = {"now": NOW}
    replies, sent = [], []

    class Adapter:
        async def observe(self, run, native_job_ref, claim, provider_session):
            # Advance a real service clock while the provider request is outstanding.
            # No replacement worker is needed for the original lease to expire.
            if not replies:
                clock["now"] += timedelta(seconds=elapsed)
            replies.append(claim.fence)
            return AdapterOutcome("running", observation={
                "native_job_ref": native, "observed_at": clock["now"].isoformat(),
                "outcome": "running", "progress_coverage": "unavailable"})

    async def authorize(run, claim, operation):
        return "current-grant"

    async def publish(envelope, delay):
        sent.append((envelope, delay))

    service = ExecutionService(work_db, None, Adapter(), publish, authorize, clock=lambda: clock["now"])
    envelope = {"schema_version": "1.0", "work_id": "work-one", "generation": 1}
    assert await service.execute(envelope) is (elapsed < 60)
    async with work_db() as readback:
        original = await readback.get(Work, ("tenant-one", "work-one"))
        run = await readback.get(Run, ("tenant-one", "run-one"))
        observations = (await readback.scalars(select(ProviderObservation))).all()
        if elapsed < 60:
            assert original.completed_at.replace(tzinfo=UTC) == clock["now"]
            assert run.revision == 2 and len(observations) == 1
            # Queue delay is relative to completion, not the old invocation start.
            assert sent[0][1] == 600 - elapsed
        else:
            assert original.completed_at is None and run.revision == 1
            assert observations == [] and sent == []
    if elapsed >= 60:
        assert await service.execute(envelope)
        assert replies == [1, 2] and len(sent) == 1
        async with work_db() as readback:
            assert (await readback.get(Run, ("tenant-one", "run-one"))).revision == 2
            assert len((await readback.scalars(select(ProviderObservation))).all()) == 1


async def test_delayed_revocation_cannot_commit_after_its_work_lease(work_db):
    from l5_services.workers.execution import AuthorityRevoked, ExecutionService

    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "job-one", "record_revision": "rev-1"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.native_job_ref, run.state, run.provider_state = native, "running", "running"
        run.admission = {"limits": {"max_polls": 3}, "monitoring_policy": {"poll_interval_seconds": 600}}
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "poll"
        await session.commit()

    clock = {"now": NOW}

    async def authorize(run, claim, operation):
        clock["now"] += timedelta(seconds=61)
        raise AuthorityRevoked("delegation revoked while checking authority")

    service = ExecutionService(work_db, None, None, None, authorize, clock=lambda: clock["now"])
    assert await service.execute({"schema_version": "1.0", "work_id": "work-one", "generation": 1}) is False
    async with work_db() as readback:
        run = await readback.get(Run, ("tenant-one", "run-one"))
        work = await readback.get(Work, ("tenant-one", "work-one"))
        assert (run.state, run.provider_state, run.revision) == ("running", "running", 1)
        assert work.completed_at is None


async def test_minute_repair_recovers_lost_send_without_early_poll_or_repeat_effect(work_db):
    import asyncio
    from types import SimpleNamespace

    from l5_services.workers.recovery import lambda_handler as repair_handler

    async with work_db() as session:
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "poll"
        work.due_at = NOW + timedelta(minutes=10)
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.state = "running"
        await session.commit()

    sent = []
    failures = 1

    async def publish(envelope, delay):
        nonlocal failures
        if failures:
            failures -= 1
            raise ConnectionError("send failed after repair started")
        sent.append((envelope, delay))

    clock = {"now": NOW}
    service = SimpleNamespace(sessions=work_db, publish=publish, clock=lambda: clock["now"])
    assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 0}
    async with work_db() as session:
        owed = await session.get(Work, ("tenant-one", "work-one"))
        assert owed.completed_at is None and owed.last_error == "publication_failed"
    clock["now"] += timedelta(minutes=1)
    assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 1}
    assert sent == [({"schema_version": "1.0", "work_id": "work-one", "generation": 1}, 540)]
    assert await asyncio.to_thread(repair_handler, {}, None, service=service) == {"enqueued": 0}

    effects = []

    async def poll(claim):
        effects.append(claim.fence)
        return {"state": "running", "provider_state": "running"}

    envelope = sent[0][0]
    assert await execute_once(work_db, envelope, poll, now=clock["now"]) is False
    assert effects == []
    clock["now"] += timedelta(minutes=9)
    assert await execute_once(work_db, envelope, poll, now=clock["now"]) is True
    assert await execute_once(work_db, envelope, poll, now=clock["now"]) is False
    assert len(effects) == 1
    async with work_db() as session:
        completed = await session.get(Work, ("tenant-one", "work-one"))
        run = await session.get(Run, ("tenant-one", "run-one"))
        assert completed.completed_at is not None and completed.last_error is None
        assert run.state == "running" and run.revision == 2


async def test_service_repair_and_claim_leave_foreign_work_untouched(work_db):
    from unittest.mock import AsyncMock
    from l5_services.dispatcher import repair_owed_work

    publish = AsyncMock()
    assert await repair_owed_work(work_db, publish, now=NOW, service_id="devops") == 0
    assert await claim_work(work_db, "work-one", 1, now=NOW, service_id="devops") is None
    async with work_db() as session:
        work = await session.get(Work, ("tenant-one", "work-one"))
        assert work.attempts == 0 and work.fence == 0 and work.lease_owner is None
    publish.assert_not_awaited()
    assert await repair_owed_work(work_db, publish, now=NOW, service_id="security") == 1
    assert await claim_work(work_db, "work-one", 1, now=NOW, service_id="security") is not None


async def test_service_dispatch_rechecks_durable_ownership_before_queue_send(work_db):
    from unittest.mock import AsyncMock
    from l5_services.dispatcher import dispatch_work

    publish = AsyncMock()
    assert await dispatch_work(work_db, "tenant-one", "work-one", publish, now=NOW, service_id="devops") is False
    async with work_db() as session:
        work = await session.get(Work, ("tenant-one", "work-one"))
        assert work.attempts == 0 and work.dispatch_owner is None and work.last_enqueued_at is None
    publish.assert_not_awaited()
    assert await dispatch_work(work_db, "tenant-one", "work-one", publish, now=NOW, service_id="security") is True
    publish.assert_awaited_once_with({"schema_version": "1.0", "work_id": "work-one", "generation": 1}, 0)


@pytest.mark.parametrize("capability", ["code_review", "threat_model"])
async def test_expired_security_collects_same_job_once(work_db, capability):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from l5_services.models import AssessmentResult
    from l5_services.models.attempt import WorkerAttempt
    from l5_services.security_recovery import request_recovery
    from l5_services.workers.execution import ExecutionService

    native = {"record_id": "retained-job", "record_revision": "retained-review"}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.state, run.provider_state, run.native_job_ref = "expired", "running", native
        run.deadline_at = NOW - timedelta(hours=2)
        run.input = {"capability": capability, "sources": []}
        run.admission = {**run.admission, "monitoring_policy": {"poll_interval_seconds": 60, "poll_grace_seconds": 300},
                         "scope": {"tenant_id": run.tenant_id, "product_id": run.product_id, "project_id": run.project_id}}
        run.monitoring = request_recovery(run, NOW)
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "poll"
        await session.commit()
    observation = {"native_job_ref": native, "outcome": "completed", "observed_at": NOW.isoformat()}
    result = {"native_job_ref": native, "findings": [], "finding_count": 0, "coverage": "complete"}
    adapter = SimpleNamespace(observe=AsyncMock(return_value=SimpleNamespace(classification="completed", observation=observation)),
        collect=AsyncMock(return_value=SimpleNamespace(classification="completed", result=result,
            observation=observation if capability == "code_review" else None)))
    authority, publish = AsyncMock(return_value=object()), AsyncMock()
    service = ExecutionService(work_db, None, adapter, publish, authority, security_threat_adapter=adapter, clock=lambda: NOW)
    envelope = {"schema_version": "1.0", "work_id": "work-one", "generation": 1}
    assert await service.execute(envelope)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        assert run.state == "collecting" and run.native_job_ref == native
        successor = (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).one()
        assert successor.kind == "collect"
        envelope = {"schema_version": "1.0", "work_id": successor.work_id, "generation": 1}
    assert await service.execute(envelope)
    assert not await service.execute(envelope)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        assert run.state == "succeeded" and run.provider_state == "completed"
        assert run.deadline_at.replace(tzinfo=UTC) == NOW - timedelta(hours=2)
        assert len((await session.scalars(select(AssessmentResult))).all()) == 1
        assert not (await session.scalars(select(WorkerAttempt))).all()
    assert [call.args[2] for call in authority.await_args_list] == ["observe", "collect"]


@pytest.mark.parametrize("service_id", ["security", "devops"])
async def test_expired_job_without_recovery_window_never_contacts_provider(work_db, service_id):
    from unittest.mock import AsyncMock
    from l5_services.workers.execution import ExecutionService
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.service_id = service_id
        run.input = {"capability": "investigation" if service_id == "devops" else "code_review"}
        run.state, run.native_job_ref = "expired", {"record_id": "original"}
        run.deadline_at = NOW - timedelta(hours=2)
        work = await session.get(Work, ("tenant-one", "work-one"))
        work.kind = "poll"
        await session.commit()
    authority = AsyncMock(side_effect=AssertionError("expired authority"))
    service = ExecutionService(work_db, None, None, AsyncMock(), authority, clock=lambda: NOW)
    assert await service.execute({"schema_version": "1.0", "work_id": "work-one", "generation": 1})
    authority.assert_not_awaited()
    async with work_db() as session:
        run = await session.get(Run, ("tenant-one", "run-one"))
        assert run.state == "expired"
        assert run.monitoring["last_poll_error"]["code"] == service_id + "_observation_window_expired"
        assert "Reconcile" in run.monitoring["last_poll_error"]["action"]
