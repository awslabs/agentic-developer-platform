"""Native recommendation-generation execution and recovery regressions."""

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.adapters.well_architected.generation import (
    GenerationAdapter, PreparedGeneration, cancel_generation, reconcile_generation,
)
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.base import Base
from l5_services.models.evidence import AssessmentResult
from l5_services.models.generation_collection import GenerationCollection
from l5_services.models.run import Run
from l5_services.models.source_retention import SourceRetention
from l5_services.models.work import Work
from l5_services.work_store import (checkpoint_generation_page, claim_work, finish_work,
                                    generation_collection, prepare_submit_attempt)
from l5_services.source_retention import (RetentionError, pin_source_hold, release_source_hold,
                                          source_cleanup_allowed)
from l5_services.dispatcher import dispatch_work
from l5_services.workers.execution import ExecutionService


NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
SCOPE = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
PROFILE = {**SCOPE, "record_type": "agent_profile", "record_id": "arn:aws:wellarchitected:us-east-1:000000000000:agent-profile/example", "record_revision": "a" * 64}


async def _database(tmp_path, *, profile=None, admission_scope=None):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'generation.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    admission = {"scope": admission_scope or SCOPE, "native_profile_ref": profile or PROFILE, "profile_hash": "a" * 64,
                 "generation_scope": {"pillars": ["SECURITY"]},
                 "generation_context": {"source": "archive-example"},
                 "generation_model_hash": "b" * 64, "generation_creator": "actor-example",
                 "monitoring_policy": {"poll_interval_seconds": 5, "poll_grace_seconds": 300},
                 "limits": {"max_polls": 4}}
    archive = {"artifact_id": "archive-example", "version": "version-example",
               "sha256": "a" * 64, "bytes": 16}
    manifest = {"scope": SCOPE, "run_id": "run-example", "archive": archive,
                "provider_source_uri": "s3://fictional-wa-bucket/archive-example",
                "archive_readback": {"artifact": archive, "sha256": archive["sha256"],
                                     "bytes": archive["bytes"]},
                "provider_coverage": "provider_coverage_unverified"}
    async with sessions() as session:
        session.add(Run(tenant_id=SCOPE["tenant_id"], run_id="run-example", product_id=SCOPE["product_id"],
                        project_id=SCOPE["project_id"], service_id="well_architected",
                        configuration_id="configuration-example", configuration_revision=1,
                        binding_id="binding-example", trigger_id="manual", binding_generation=1,
                        input={"capability": "architecture_review"}, admission=admission,
                        state="queued", manifest=manifest, deadline_at=NOW + timedelta(hours=1)))
        session.add(Work(tenant_id=SCOPE["tenant_id"], work_id="work-example", run_id="run-example",
                         kind="submit", generation=1, step=1, due_at=NOW))
        await session.commit()
    return engine, sessions


async def test_generation_collection_rejects_wrong_pages_and_duplicate_result_commits(tmp_path):
    engine, sessions = await _database(tmp_path)
    native = {"tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"],
              "profile_arn": PROFILE["record_id"], "generation_id": "generation-example",
              "name": "adp-wa-example"}
    first_page = {"profileArn": native["profile_arn"], "generationId": native["generation_id"],
                  "recommendationArn": "arn:aws:wellarchitected:us-east-1:000000000000:agent-recommendation/2956558f-8f72-5bd1-b5c6-529500f00f1c"}
    second_page = {**first_page, "recommendationArn": first_page["recommendationArn"].rsplit("/", 1)[0]
                   + "/00000000-0000-0000-0000-000000000002"}
    try:
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.native_job_ref = native
            run.state = "running"
            work = await session.get(Work, (SCOPE["tenant_id"], "work-example"))
            work.kind = "collect"
            await session.commit()
        claim = await claim_work(sessions, "work-example", 1, now=NOW, lease_seconds=5)
        with pytest.raises(ValueError, match="another generation"):
            await checkpoint_generation_page(sessions, claim, None, "next", [
                {**first_page, "generationId": "other-generation"}], now=NOW)
        assert await generation_collection(sessions, claim) is None
        with pytest.raises(ValueError, match="completed generation collection"):
            await finish_work(sessions, claim, state="succeeded", result={
                "native_job_ref": native, "recommendations": [first_page]}, now=NOW)
        saved = await checkpoint_generation_page(sessions, claim, None, "next", [first_page], now=NOW)
        assert saved["next_token"] == "next" and saved["page_count"] == 1
        with pytest.raises(ValueError, match="completed generation collection"):
            await finish_work(sessions, claim, state="succeeded", result={
                "native_job_ref": native, "recommendations": [first_page]}, now=NOW)
        with pytest.raises(ValueError, match="cursor"):
            await checkpoint_generation_page(sessions, claim, None, None, [second_page], now=NOW)
        with pytest.raises(ValueError, match="duplicate"):
            await checkpoint_generation_page(sessions, claim, "next", None, [first_page], now=NOW)
        assert (await generation_collection(sessions, claim))["next_token"] == "next"
        with pytest.raises(ValueError, match="stale"):
            await checkpoint_generation_page(sessions, claim, "next", None, [second_page],
                                             now=NOW + timedelta(seconds=5))
        recovered = await claim_work(sessions, "work-example", 1, now=NOW + timedelta(seconds=5))
        with pytest.raises(ValueError, match="stale"):
            await checkpoint_generation_page(sessions, claim, "next", None, [second_page], now=NOW)
        saved = await checkpoint_generation_page(sessions, recovered, "next", None, [second_page],
                                                 now=NOW + timedelta(seconds=5))
        assert saved["page_count"] == 2 and saved["recommendations"] == [first_page, second_page]
        with pytest.raises(ValueError, match="completed generation collection"):
            await finish_work(sessions, recovered, state="succeeded", result={
                "native_job_ref": native, "recommendations": [first_page]}, now=NOW + timedelta(seconds=6))
        with pytest.raises(ValueError, match="original native generation"):
            await finish_work(sessions, recovered, state="succeeded", result={
                "native_job_ref": {**native, "generation_id": "other-generation"},
                "recommendations": [first_page, second_page]}, now=NOW + timedelta(seconds=6))
        result = {"native_job_ref": native, "recommendations": [first_page, second_page]}
        assert await finish_work(sessions, recovered, state="succeeded", result=result,
                                 now=NOW + timedelta(seconds=6)) == "completed"
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            session.add(Work(tenant_id=SCOPE["tenant_id"], work_id="collect-replay", run_id=run.run_id,
                             kind="collect", generation=1, step=2, due_at=NOW + timedelta(seconds=7)))
            await session.commit()
        replay = await claim_work(sessions, "collect-replay", 1, now=NOW + timedelta(seconds=7))
        with pytest.raises(ValueError, match="already committed"):
            await finish_work(sessions, replay, state="succeeded", result=result,
                              now=NOW + timedelta(seconds=8))
        async with sessions() as session:
            collection = await session.get(GenerationCollection, (SCOPE["tenant_id"], "run-example"))
            results = (await session.scalars(select(AssessmentResult))).all()
            assert collection.native_job_ref == native and collection.result_id == results[0].result_id
            assert len(results) == 1 and results[0].version == 1
            assert results[0].payload["recommendations"] == [first_page, second_page]
    finally:
        await engine.dispose()


async def _run_paginated_collection_fault(tmp_path, monkeypatch, fault, *, scenario=False):
    engine, sessions = await _database(tmp_path)
    clock = [NOW]
    published = []
    page_requests = []
    detail_requests = []
    authorizations = []
    start_calls = []
    native = {"tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"],
              "profile_arn": PROFILE["record_id"], "generation_id": "generation-example",
              "name": "adp-wa-example"}
    recommendations = [
        {"profileArn": native["profile_arn"], "generationId": native["generation_id"],
         "recommendationArn": f"arn:aws:wellarchitected:us-east-1:000000000000:agent-recommendation/00000000-0000-0000-0000-{index:012d}",
         "title": None, "description": None, "pillar": None}
        for index in (1, 2)
    ]
    deny_page = [fault == "provider_page"]
    wrong_generation = [scenario]

    class Provider:
        def start_agent_recommendation_generation(self, **request):
            start_calls.append(request)
            pytest.fail("terminal collection must not resubmit native Start")

        def list_agent_recommendations(self, **request):
            page_requests.append(request)
            assert request["profileArn"] == native["profile_arn"]
            if request.get("nextToken") is None:
                return {"items": [recommendations[0]], "nextToken": "page-two"}
            assert request["nextToken"] == "page-two"
            if deny_page[0]:
                deny_page[0] = False
                raise TimeoutError("synthetic lost page response")
            return {"items": [recommendations[1]]}

        def get_agent_recommendation(self, **request):
            detail_requests.append(request)
            detail = next(item for item in recommendations if item["recommendationArn"] == request["recommendationArn"])
            if detail == recommendations[1] and wrong_generation[0]:
                wrong_generation[0] = False
                return {**detail, "generationId": "other-generation"}
            return detail

    class DelegatedSession:
        def client(self, service, *, config=None):
            assert service == "wellarchitected"
            return Provider()

    async def authorize(run, claim, operation):
        authorizations.append(operation)
        assert operation == "collect"
        return DelegatedSession()

    async def publish(envelope, delay):
        published.append((envelope, delay))

    worker = ExecutionService(sessions, None, None, publish, authorize,
                              well_architected_adapter=GenerationAdapter(None), clock=lambda: clock[0])
    try:
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.state = "collecting"
            run.provider_state = "completed"
            run.native_job_ref = native
            run.monitoring = {"state": "current", "latest_observation": {
                "native_job_ref": native, "outcome": "completed", "observed_at": NOW.isoformat(),
                "native_status": "COMPLETED"}}
            work = await session.get(Work, (SCOPE["tenant_id"], "work-example"))
            work.kind = "collect"
            await session.commit()
        initial = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
        assert await worker.execute(initial)
        assert not detail_requests
        assert await worker.execute(published.pop(0)[0])
        async with sessions() as session:
            checkpoint = await session.get(GenerationCollection, (SCOPE["tenant_id"], "run-example"))
            assert checkpoint.next_token == "page-two" and checkpoint.page_count == 1
            assert checkpoint.recommendations == [recommendations[0]]
            assert not (await session.scalars(select(AssessmentResult))).all()
        next_page = published.pop(0)[0]
        assert next_page["work_id"] != initial["work_id"]
        if fault == "provider_page":
            assert await worker.execute(next_page)
            assert published[0][1] == 5
            clock[0] += timedelta(seconds=5)
            next_page = published.pop(0)[0]
            async with sessions() as session:
                checkpoint = await session.get(GenerationCollection, (SCOPE["tenant_id"], "run-example"))
                assert checkpoint.next_token == "page-two" and checkpoint.page_count == 1
        assert await worker.execute(next_page)
        next_detail = published.pop(0)[0]
        if scenario:
            with pytest.raises(ValueError, match="recommendation detail belongs to another generation"):
                await worker.execute(next_detail)
            async with sessions() as session:
                checkpoint = await session.get(GenerationCollection, (SCOPE["tenant_id"], "run-example"))
                assert checkpoint.next_token is None and checkpoint.page_count == 2
                assert checkpoint.recommendations == [recommendations[0]]
                assert len(checkpoint.pending_recommendations) == 1
                assert not (await session.scalars(select(AssessmentResult))).all()
            clock[0] += timedelta(seconds=61)
        if fault == "final_sql_write":
            from l5_services.workers import execution

            real_finish = execution.finish_work
            crash = [True]

            async def crash_before_final_write(*args, **kwargs):
                if crash[0] and kwargs.get("result") is not None:
                    crash[0] = False
                    raise RuntimeError("synthetic SQL write boundary")
                return await real_finish(*args, **kwargs)

            monkeypatch.setattr(execution, "finish_work", crash_before_final_write)
            with pytest.raises(RuntimeError, match="SQL write boundary"):
                await worker.execute(next_detail)
            async with sessions() as session:
                checkpoint = await session.get(GenerationCollection, (SCOPE["tenant_id"], "run-example"))
                assert checkpoint.next_token is None and checkpoint.page_count == 2
                assert checkpoint.result_id is None and not (await session.scalars(select(AssessmentResult))).all()
            clock[0] += timedelta(seconds=61)
        assert await worker.execute(next_detail)
        assert len(page_requests) == (3 if fault == "provider_page" else 2)
        assert [request.get("nextToken") for request in page_requests] == (
            [None, "page-two", "page-two"] if fault == "provider_page" else [None, "page-two"])
        assert len(detail_requests) == 2 + int(scenario)
        assert len(authorizations) == (5 if fault == "provider_page" else 4) + int(scenario)
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            checkpoint = await session.get(GenerationCollection, (SCOPE["tenant_id"], "run-example"))
            results = (await session.scalars(select(AssessmentResult))).all()
            assert run.state == "succeeded" and run.native_job_ref == native
            assert checkpoint.next_token is None and checkpoint.page_count == 2
            assert len(results) == 1 and results[0].version == 1
            assert results[0].payload["recommendations"] == recommendations
            assert checkpoint.result_id == results[0].result_id
            trace = {"generation_id": checkpoint.native_job_ref["generation_id"],
                     "page_count": checkpoint.page_count, "result_count": len(results),
                     "version": results[0].version, "result_id": checkpoint.result_id,
                     "start_count": len(start_calls), "rejected_pages": int(scenario),
                     "page_tokens": [request.get("nextToken") for request in page_requests]}
        assert not published
        return trace
    finally:
        await engine.dispose()


@pytest.mark.parametrize("fault", ["provider_page", "final_sql_write"])
async def test_native_worker_resumes_paginated_collection_after_fault(tmp_path, monkeypatch, fault):
    await _run_paginated_collection_fault(tmp_path, monkeypatch, fault)


@pytest.mark.parametrize("fault", ["provider_page", "final_sql_write"])
async def test_ac_04(tmp_path, monkeypatch, fault):
    trace = await _run_paginated_collection_fault(tmp_path, monkeypatch, fault, scenario=True)
    assert trace["generation_id"] == "generation-example"
    assert trace["page_count"] == 2 and trace["rejected_pages"] == 1
    assert trace["result_count"] == 1 and trace["version"] == 1
    assert isinstance(trace["result_id"], str) and trace["result_id"]
    assert trace["start_count"] == 0
    assert trace["page_tokens"] == (
        [None, "page-two", "page-two"] if fault == "provider_page"
        else [None, "page-two"])


async def test_pinned_request_is_committed_before_start_and_survives_lease_recovery(tmp_path):
    engine, sessions = await _database(tmp_path)
    try:
        claim = await claim_work(sessions, "work-example", 1, now=NOW, lease_seconds=1)
        first, created = await prepare_submit_attempt(sessions, claim, now=NOW)
        assert created and first.prepared_request is not None
        prepared = PreparedGeneration.from_record(first.prepared_request)
        assert prepared.profile_arn == PROFILE["record_id"]
        assert prepared.scope == {"pillars": ["SECURITY"]}
        assert prepared.additional_context == {"s3Uri": "s3://fictional-wa-bucket/archive-example",
                                               "s3VersionId": "version-example",
                                               "lensArns": ["arn:aws:wellarchitected::aws:lens/wellarchitected"]}
        assert prepared.name.startswith("adp-wa-")
        async with sessions() as session:
            attempts = (await session.scalars(select(WorkerAttempt))).all()
            assert len(attempts) == 1
            assert attempts[0].prepared_request == first.prepared_request
            assert attempts[0].status == "prepared"
        next_claim = await claim_work(sessions, "work-example", 1, now=NOW + timedelta(seconds=2))
        repeated, created = await prepare_submit_attempt(sessions, next_claim, now=NOW + timedelta(seconds=2))
        assert not created and repeated.attempt_id == first.attempt_id
        assert repeated.prepared_request == first.prepared_request
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.admission = {**run.admission, "generation_scope": {"pillars": ["COST_OPTIMIZATION"]}}
            await session.commit()
        with pytest.raises(ValueError, match="pinned generation request changed"):
            await prepare_submit_attempt(sessions, next_claim, now=NOW + timedelta(seconds=2))
    finally:
        await engine.dispose()


@pytest.mark.parametrize("profile,admission_scope", [
    ({**PROFILE, "project_id": "other-project"}, None),
    ({**PROFILE, "record_revision": "c" * 64}, None),
    (None, {**SCOPE, "project_id": "other-project"}),
])
async def test_mismatched_native_profile_cannot_create_a_paid_attempt(tmp_path, profile, admission_scope):
    engine, sessions = await _database(tmp_path, profile=profile, admission_scope=admission_scope)
    try:
        claim = await claim_work(sessions, "work-example", 1, now=NOW)
        with pytest.raises(ValueError, match="pinned generation profile or scope changed"):
            await prepare_submit_attempt(sessions, claim, now=NOW)
        async with sessions() as session:
            assert (await session.scalars(select(WorkerAttempt))).all() == []
    finally:
        await engine.dispose()


async def test_native_worker_submits_after_dispatch_and_reads_original_terminal_status(tmp_path):
    engine, sessions = await _database(tmp_path)
    clock = [NOW]
    messages = []
    native_calls = []

    async def publish(envelope, delay):
        messages.append((envelope, delay))

    class Provider:
        def __init__(self, config):
            self.meta = SimpleNamespace(config=config)

        def start_agent_recommendation_generation(self, **request):
            native_calls.append(request)
            return {"id": "generation-example", "profileArn": request["profileArn"],
                    "name": request["name"], "createdBy": "actor-example", "createdAt": NOW}

        def get_agent_recommendation_generation(self, *, profileArn, generationId):
            assert (profileArn, generationId) == (PROFILE["record_id"], "generation-example")
            return {"id": generationId, "profileArn": profileArn, "name": native_calls[0]["name"],
                    "scope": native_calls[0]["scope"],
                    "additionalContext": native_calls[0]["additionalContext"], "status": "COMPLETED"}

    class DelegatedSession:
        def client(self, service, *, config=None):
            assert service == "wellarchitected"
            return Provider(config or SimpleNamespace(retries={}))

    async def authorize(run, claim, operation):
        assert run.tenant_id == SCOPE["tenant_id"]
        assert operation in {"submit", "observe"}
        return DelegatedSession()

    worker = ExecutionService(sessions, None, None, publish, authorize,
                              well_architected_adapter=GenerationAdapter(None), clock=lambda: clock[0])
    try:
        assert await dispatch_work(sessions, SCOPE["tenant_id"], "work-example", publish, now=clock[0])
        assert len(messages) == 1 and messages[0][1] == 0
        submitted = messages.pop(0)[0]
        assert await worker.execute(submitted)
        assert len(native_calls) == 1
        assert native_calls[0]["profileArn"] == PROFILE["record_id"]
        assert native_calls[0]["scope"] == {"pillars": ["SECURITY"]}
        assert len(messages) == 1
        clock[0] += timedelta(seconds=5)
        assert await worker.execute(messages.pop(0)[0])
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            assert run.state == "collecting" and run.provider_state == "completed"
            assert run.native_job_ref["generation_id"] == "generation-example"
            assert run.monitoring["latest_observation"]["native_status"] == "COMPLETED"
            assert len((await session.scalars(select(WorkerAttempt))).all()) == 1
        assert len(native_calls) == 1
    finally:
        await engine.dispose()


async def test_unverified_source_cannot_prepare_native_start(tmp_path):
    engine, sessions = await _database(tmp_path)
    try:
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.manifest = {**run.manifest, "archive_readback": None}
            await session.commit()
        claim = await claim_work(sessions, "work-example", 1, now=NOW)
        with pytest.raises(ValueError, match="verified immutable source archive required"):
            await prepare_submit_attempt(sessions, claim, now=NOW)
        async with sessions() as session:
            assert (await session.scalars(select(WorkerAttempt))).all() == []
    finally:
        await engine.dispose()


async def test_gateway_requires_matching_wa_authority_after_profile_readback(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fictional")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fictional")
    monkeypatch.setenv("BG_TOKEN_SECRET_KEY", "fictional-test-key-0123456789abcdef")
    from fastapi import HTTPException
    from src.l5_integration import GatewayConfigurationBackend

    engine, sessions = await _database(tmp_path)
    source = {"tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"],
              "source_connection_id": "connection-example", "record_type": "document",
              "record_id": "source-example", "record_revision": "revision-example"}
    from l5_services.admission import _exact_sources
    assert _exact_sources({"service_id": "well_architected", "capability": "architecture_review",
                           "sources": [source]}, SCOPE) == [source]
    verified = {"native_profile_ref": PROFILE, "profile_hash": "a" * 64,
                "scope": SCOPE, "source_refs": [source],
                "generation_scope": {"pillars": ["SECURITY"]}, "native_creator": "actor-example"}
    version = SimpleNamespace(settings={"well_architected": {"pillars": ["SECURITY"]}})

    class SourceClient:
        async def pin_source(self, *args, **kwargs):
            return None

    async def publish(envelope, delay):
        pass

    async def authority(configuration, saved, run_input, actor):
        return verified, SourceClient()

    async def current(product_id, project_id, service_id):
        return SimpleNamespace(), version

    async def readback(configuration, saved, run_input):
        return PROFILE, {"readiness": [{"snapshot_hash": "a" * 64}]}

    try:
        async with sessions() as session:
            backend = GatewayConfigurationBackend(session, SimpleNamespace(org_id=SCOPE["tenant_id"],
                user_id="actor-example"), publish=publish, wa_authority=authority)
            backend.current = current
            backend._wa_admission_readback = readback
            selected, client = await backend.admission_authority(
                SCOPE["product_id"], SCOPE["project_id"], "well_architected", {"sources": [source]})
            assert selected == verified and isinstance(client, SourceClient)
            verified = {**verified, "generation_scope": {"pillars": ["COST_OPTIMIZATION"]}}
            with pytest.raises(HTTPException) as rejected:
                await backend.admission_authority(
                    SCOPE["product_id"], SCOPE["project_id"], "well_architected", {"sources": [source]})
            assert rejected.value.status_code == 409
    finally:
        await engine.dispose()


async def accepted_review_journey(tmp_path, monkeypatch):
    import importlib.util
    import io
    import json
    import zipfile
    from pathlib import Path
    from hashlib import sha256

    import httpx
    from fastapi import FastAPI

    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fictional")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fictional")
    monkeypatch.setenv("BG_TOKEN_SECRET_KEY", "fictional-test-key-0123456789abcdef")
    from src.l5_integration import GatewayConfigurationBackend
    from src.internal import tools_l5
    from l5_services.api.router import create_router

    monkeypatch.setattr(tools_l5, "files", lambda package: Path(__file__).resolve().parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    from l5_services.models.binding import Binding
    from l5_services.models.configuration import Configuration
    from l5_services.models.evidence import AssessmentResult, ProviderObservation
    from l5_services.models.receipt import RunReceipt
    from l5_services.models.result import ResultDetail
    from l5_services.models.source_retention import SourceRetention

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'accepted-review.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    now = datetime.now(UTC)
    source = {"tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"],
              "source_connection_id": "connection-example", "record_type": "document",
              "record_id": "architecture-example", "record_revision": "revision-example"}
    content = b"Architecture goal: resilient network."
    artifact = lambda name, data: {"artifact_id": name, "version": "revision-one",
                                   "sha256": sha256(data).hexdigest(), "bytes": len(data),
                                   "media_type": "application/octet-stream"}
    file_receipt = {**artifact("source-content", content), "source_ref": source, "path": "goals.txt"}
    file_manifest = json.dumps({"source_ref": source, "exclusions": [],
                                "files": [file_receipt]}, sort_keys=True).encode()
    snapshot = {"source_ref": source, "snapshot_id": "source-snapshot", "exclusions": [],
                "file_count": 1, "content": artifact("source-content", content),
                "file_manifest": artifact("source-manifest", file_manifest),
                "expires_at": (now + timedelta(days=3)).isoformat()}
    archive = SimpleNamespace(content=None, reference=None)
    events = []
    published = []
    clock = [now]

    class SourceClient:
        async def pin_source(self, product_id, project_id, request, *, idempotency_key):
            assert request["source_ref"] == source
            events.append("source_pin")
            return snapshot

        async def read_source(self, product_id, project_id, snapshot_id, request):
            assert snapshot_id == snapshot["snapshot_id"] and request["delegation_id"] == "delegation-example"
            return snapshot

        async def read_source_manifest(self, product_id, project_id, snapshot_id, request):
            return file_manifest

        async def stream_source(self, product_id, project_id, snapshot_id, request):
            yield content

    class ArchiveStore:
        bucket = "fictional-wa-bucket"

        async def put_archive(self, run_id, data):
            archive.content = data
            archive.reference = artifact(f"l5/well_architected/work/{run_id}/sealed-archive", data)
            return archive.reference

        async def get_archive(self, run_id, reference):
            assert reference == archive.reference
            return archive.content

    native = SimpleNamespace(request=None, starts=0, reads=0, recommendation_reads=0,
                             started_at=None, read_at=None, collected_at=None)
    recommendation_arn = "arn:aws:wellarchitected:us-east-1:000000000000:agent-recommendation/f8b72323-0e40-5a08-bd7f-039dfb85a9b2"

    class Provider:
        def __init__(self, config):
            self.meta = SimpleNamespace(config=config)

        def start_agent_recommendation_generation(self, **request):
            assert archive.content is not None and archive.reference is not None
            native.starts += 1
            native.request = request
            native.started_at = clock[0]
            events.append("native_start")
            return {"id": "generation-example", "profileArn": request["profileArn"],
                    "name": request["name"], "createdBy": "provider-actor-example", "createdAt": clock[0]}

        def get_agent_recommendation_generation(self, *, profileArn, generationId):
            assert (profileArn, generationId) == (PROFILE["record_id"], "generation-example")
            native.reads += 1
            native.read_at = clock[0]
            return {"id": generationId, "profileArn": profileArn,
                    "name": native.request["name"], "scope": native.request["scope"],
                    "additionalContext": native.request["additionalContext"],
                    "status": "COMPLETED"}

        def list_agent_recommendations(self, *, profileArn, maxResults):
            assert (profileArn, maxResults) == (PROFILE["record_id"], 1000)
            native.recommendation_reads += 1
            native.collected_at = clock[0]
            return {"items": [{"profileArn": profileArn, "generationId": "generation-example",
                               "recommendationArn": recommendation_arn}]}

        def get_agent_recommendation(self, *, recommendationArn):
            assert recommendationArn == recommendation_arn
            return {"recommendationArn": recommendationArn, "profileArn": PROFILE["record_id"],
                    "generationId": "generation-example", "title": "Network recovery",
                    "description": "Improve recovery", "pillar": "RESILIENCE"}

    class DelegatedSession:
        def client(self, service, *, config=None):
            assert service == "wellarchitected"
            return Provider(config or SimpleNamespace(retries={}))

    async def authorize(run, claim, operation):
        assert (run.tenant_id, run.project_id) == (SCOPE["tenant_id"], SCOPE["project_id"])
        assert claim.run_id == run.run_id and operation in {"materialize", "submit", "observe", "collect"}
        return DelegatedSession()

    async def publish(envelope, delay):
        published.append((envelope, delay))

    scope = {**SCOPE, "relationship_revision": "1"}
    verified = {"service_id": "well_architected", "scope": scope,
                "configuration_id": "configuration-example", "configuration_revision": 1,
                "configuration_enabled": True, "binding_id": "binding-example",
                "binding_generation": 1, "binding_state": "ready",
                "connection_ref": {"record_id": "connection-example"},
                "binding_connection_ref": {"record_id": "connection-example"},
                "delegation_id": "delegation-example", "delegation_version": "1",
                "delegation_active": True, "actor_id": "actor-example", "actor_type": "human",
                "trigger": {"kind": "manual", "enabled": True, "trigger_id": "manual-example",
                            "capability": "architecture_review", "binding_id": "binding-example"},
                "configuration_hash": "c" * 64, "adapter_revision": "wa04-pinned-model",
                "limits": {"deadline_seconds": 300, "max_polls": 4},
                "visibility": "only_me", "classification": "private",
                "causal_origin": "manual", "exclusions": [], "max_files": 1, "max_bytes": 8192,
                "monitoring_policy": {"poll_interval_seconds": 5, "poll_grace_seconds": 300},
                "profile_ref": {"record_id": "profile-example"},
                "native_profile_ref": PROFILE, "profile_hash": PROFILE["record_revision"],
                "native_creator": "provider-actor-example", "source_refs": [source],
                "source_retention": {"required_until": (now + timedelta(days=1)).isoformat(),
                                     "policy_expires_at": (now + timedelta(days=4)).isoformat()},
                "architecture_coverage": {"source-snapshot": {"files": {
                    "goals.txt": {"component": "platform", "exclusion": None, "entry_point": True}},
                    "unresolved": []}},
                "generation_scope": {"pillars": ["RESILIENCE"]},
                "generation_context": {"goal": "resilient network"},
                "generation_model_hash": "b" * 64}

    async def authority(configuration, version, run_input, actor):
        return verified, SourceClient()

    async def budget(session, limits, selected_scope, observed_at, replay):
        assert selected_scope == scope
        return None

    async def current(product_id, project_id, service_id):
        return SimpleNamespace(), SimpleNamespace(settings={
            "well_architected": {"pillars": ["RESILIENCE"]}})

    async def readback(configuration, version, run_input):
        return PROFILE, {"readiness": [{"snapshot_hash": PROFILE["record_revision"]}]}

    try:
        async with sessions() as session:
            session.add(Configuration(tenant_id=SCOPE["tenant_id"], configuration_id="configuration-example",
                                      product_id=SCOPE["product_id"], project_id=SCOPE["project_id"],
                                      service_id="well_architected", relationship_revision="1",
                                      owner_id="actor-example", current_revision=1, handoff_state="linked"))
            session.add(Binding(tenant_id=SCOPE["tenant_id"], binding_id="binding-example",
                                configuration_id="configuration-example", product_id=SCOPE["product_id"],
                                project_id=SCOPE["project_id"], service_id="well_architected",
                                scope=scope, revision=1, generation=1, state="ready", ownership="customer_linked",
                                connection_ref=verified["connection_ref"], connection_key="connection-example",
                                native_resource_ref=PROFILE, native_key="profile-example", readiness=[], gaps=[]))
            await session.commit()
        async with sessions() as session:
            backend = GatewayConfigurationBackend(session, SimpleNamespace(
                org_id=SCOPE["tenant_id"], user_id="actor-example", account_type="human"),
                publish=publish, wa_authority=authority)
            backend.current = current
            backend._wa_admission_readback = readback
            backend._require_devops_budget = budget
            app = FastAPI()
            app.include_router(create_router(lambda: backend))
            path = (f"/v1/products/{SCOPE['product_id']}/projects/{SCOPE['project_id']}"
                    "/services/well_architected/runs")
            request = {"service_id": "well_architected", "configuration_revision": 1,
                       "binding_id": "binding-example", "capability": "architecture_review", "sources": [source]}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                response = await client.post(path, json=request, headers={
                    "Idempotency-Key": "example-review-00001"})
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            mcp = await tools_l5.dispatch_l5("l5___submit_run", {
                "product_id": SCOPE["product_id"], "project_id": SCOPE["project_id"],
                "service_id": "well_architected", "input": request,
                "idempotency_key": "example-review-00001"}, backend)
            assert mcp["structuredContent"]["data"]["run_id"] == run_id
            assert mcp["structuredContent"]["http_status"] == 202
            assert len(published) == 1 and published[0][1] == 0 and native.starts == 0
        async with sessions() as session:
            receipt = await session.scalar(select(RunReceipt).where(RunReceipt.run_id == run_id))
            assert receipt is not None
            admitted = await session.get(Run, (SCOPE["tenant_id"], run_id))
            queued = (await session.scalars(select(Work).where(Work.run_id == run_id))).one()
            assert admitted.state == "admitted" and admitted.admission["scope"] == scope
            assert admitted.admission["native_profile_ref"] == PROFILE
            assert (queued.kind, queued.work_id) == ("execute", published[0][0]["work_id"])
            holds = (await session.scalars(select(SourceRetention).where(SourceRetention.run_id == run_id))).all()
            assert len(holds) == 2
            assert len((await session.scalars(select(RunReceipt))).all()) == 1
            assert len((await session.scalars(select(Work))).all()) == 1
        worker = ExecutionService(sessions, SourceClient(), None, publish, authorize,
                                  archive_store=ArchiveStore(),
                                  well_architected_adapter=GenerationAdapter(None),
                                  clock=lambda: clock[0])
        clock[0] = datetime.now(UTC)
        assert await worker.execute(published.pop(0)[0])
        assert len(published) == 1 and published[0][1] == 0 and native.starts == 0
        assert await worker.execute(published.pop(0)[0])
        assert len(published) == 1 and native.starts == 1
        assert native.reads == 0 and native.read_at is None
        async with sessions() as session:
            submitted = await session.get(Run, (SCOPE["tenant_id"], run_id))
            due_poll = (await session.scalars(select(Work).where(
                Work.run_id == run_id, Work.kind == "poll"))).one()
            assert submitted.native_job_ref["generation_id"] == "generation-example"
            assert due_poll.due_at.replace(tzinfo=UTC) > clock[0]
            assert published[0][0]["work_id"] == due_poll.work_id and published[0][1] > 0
        clock[0] += timedelta(seconds=6)
        assert await worker.execute(published.pop(0)[0])
        assert native.reads == 1
        assert await worker.execute(published.pop(0)[0])
        assert native.starts == 1 and native.recommendation_reads == 1
        queued_before_replay = len(published)
        cli_path = Path(__file__).resolve().parents[3] / "gateway/cli/adp-service.py"
        spec = importlib.util.spec_from_file_location("wa_replay_client", cli_path)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        run_input_path = tmp_path / "replayed-review.json"
        run_input_path.write_text(json.dumps(request))
        loop = asyncio.get_running_loop()

        class CliClient:
            def request(self, method, path, body=None, **options):
                assert method == "POST" and path.endswith("/services/well_architected/runs")

                async def send():
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                        return await client.post(path, json=body, headers=options["extra_headers"])

                result = asyncio.run_coroutine_threadsafe(send(), loop).result()
                assert result.status_code == 202
                return result.json()

        cli_args = cli.parser().parse_args([
            "run", "--input", str(run_input_path), "--idempotency-key", "example-review-00001",
            "--product", SCOPE["product_id"], "--project", SCOPE["project_id"],
            "--service", "well_architected", "--json"])
        replayed = await asyncio.to_thread(cli.execute, cli_args, CliClient())
        assert replayed["status"] == "accepted" and replayed["detail"]["run_id"] == run_id
        assert native.starts == 1 and len(published) == queued_before_replay
        assert events.count("native_start") == events.count("source_pin") == 1
        async with sessions() as session:
            assert len((await session.scalars(select(RunReceipt))).all()) == 1
            assert len((await session.scalars(select(Run))).all()) == 1
            assert len((await session.scalars(select(Work).where(Work.kind == "execute"))).all()) == 1
            assert len((await session.scalars(select(WorkerAttempt))).all()) == 1
        assert await worker.execute(published.pop(0)[0])
        assert events.index("source_pin") < events.index("native_start")
        assert native.request["profileArn"] == PROFILE["record_id"]
        assert native.request["additionalContext"] == {
            "s3Uri": f"s3://fictional-wa-bucket/{archive.reference['artifact_id']}",
            "s3VersionId": archive.reference["version"],
            "lensArns": ["arn:aws:wellarchitected::aws:lens/wellarchitected"]}
        assert native.request["scope"] == {"pillars": ["RESILIENCE"]}
        assert native.started_at < native.read_at <= native.collected_at
        assert verified["generation_model_hash"] == "b" * 64
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], run_id))
            result = await session.scalar(select(AssessmentResult).where(AssessmentResult.run_id == run_id))
            detail = await session.get(ResultDetail, (SCOPE["tenant_id"], result.result_id))
            checkpoint = await session.get(GenerationCollection, (SCOPE["tenant_id"], run_id))
            observations = (await session.scalars(select(ProviderObservation).where(
                ProviderObservation.run_id == run_id))).all()
            assert run.state == "succeeded" and run.provider_state == "completed"
            assert run.manifest["archive_readback"]["artifact"] == archive.reference
            assert run.manifest["provider_coverage"] == "provider_coverage_unverified"
            assert run.native_job_ref["generation_id"] == "generation-example"
            assert run.native_job_ref["profile_arn"] == PROFILE["record_id"]
            assert result.payload["native_job_ref"] == run.native_job_ref
            assert checkpoint.result_id == result.result_id and checkpoint.next_token is None
            assert checkpoint.native_job_ref == run.native_job_ref and checkpoint.page_count == 1
            assert result.version == 1 and result.payload["recommendations"][0]["title"] == "Network recovery"
            assert datetime.fromisoformat(result.payload["observed_at"]).tzinfo is not None
            assert detail.payload["recommendation_count"] == 1
            assert observations[0].payload["native_status"] == "COMPLETED"
            assert observations[0].observed_at.replace(tzinfo=UTC) >= native.read_at
            attempts = (await session.scalars(select(WorkerAttempt).where(
                WorkerAttempt.run_id == run_id))).all()
            assert len(attempts) == 1 and attempts[0].started_at is not None
            prepared = PreparedGeneration.from_record(attempts[0].prepared_request)
            assert attempts[0].status == "accepted" and attempts[0].native_job_ref == run.native_job_ref
            assert (prepared.tenant_id, prepared.project_id) == (SCOPE["tenant_id"], SCOPE["project_id"])
            assert prepared.profile_arn == native.request["profileArn"]
            assert prepared.name == native.request["name"] and prepared.creator == "provider-actor-example"
            assert prepared.scope == native.request["scope"]
            assert prepared.additional_context == native.request["additionalContext"]
            assert prepared.additional_context["s3VersionId"] == archive.reference["version"]
            assert prepared.request_hash and "requestHash" not in native.request["additionalContext"]
            assert len((await session.scalars(select(Run))).all()) == 1
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()
        await engine.dispose()


async def test_ac_01(tmp_path, monkeypatch):
    await accepted_review_journey(tmp_path, monkeypatch)


async def test_reconciled_request_recovers_only_the_sql_prepared_generation(tmp_path):
    engine, sessions = await _database(tmp_path)
    try:
        claim = await claim_work(sessions, "work-example", 1, now=NOW)
        attempt, created = await prepare_submit_attempt(sessions, claim, now=NOW)
        assert created
        prepared = PreparedGeneration.from_record(attempt.prepared_request)
        calls = {"list": 0, "get": 0, "start": 0}

        class ReadOnlyClient:
            def list_agent_recommendation_generations(self, **request):
                calls["list"] += 1
                assert request == {"profileArn": prepared.profile_arn,
                                   "recommendationType": "ARCHITECTURE"}
                return {"items": [{"id": "original-generation", "profileArn": prepared.profile_arn,
                                   "name": prepared.name}]}

            def get_agent_recommendation_generation(self, **request):
                calls["get"] += 1
                assert request == {"profileArn": prepared.profile_arn,
                                   "generationId": "original-generation"}
                return {"id": "original-generation", "profileArn": prepared.profile_arn,
                        "name": prepared.name, "createdBy": prepared.creator,
                        "createdAt": NOW, "scope": prepared.scope,
                        "additionalContext": prepared.additional_context}

            def start_agent_recommendation_generation(self, **request):
                calls["start"] += 1
                raise AssertionError("reconciliation cannot Start")

        class DelegatedSession:
            def client(self, service, **options):
                assert service == "wellarchitected" and options == {}
                return ReadOnlyClient()

        async with sessions() as session:
            original = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            recovered = await GenerationAdapter(None).reconcile_submit(original, attempt, DelegatedSession())
        assert recovered.classification == "accepted"
        assert recovered.native_job_ref == {"tenant_id": SCOPE["tenant_id"],
                                            "project_id": SCOPE["project_id"],
                                            "profile_arn": prepared.profile_arn,
                                            "generation_id": "original-generation", "name": prepared.name}
        assert calls == {"list": 1, "get": 1, "start": 0}
    finally:
        await engine.dispose()


@pytest.mark.parametrize("failure", [
    "zero", "multiple", "incomplete_page", "malformed_page", "foreign_profile",
    "foreign_scope", "foreign_context", "wrong_request_hash", "wrong_creator",
    "outside_window", "wrong_generation_id", "malformed_candidate",
])
def test_reconcile_rejects_ambiguous_or_unattributable_native_generation(failure):
    prepared = PreparedGeneration(SCOPE["tenant_id"], SCOPE["project_id"], PROFILE["record_id"],
                                  "opaque-generation", "creator-example", "b" * 64, "c" * 64,
                                  NOW, NOW + timedelta(minutes=15), {"pillars": ["SECURITY"]},
                                  {"requestHash": "c" * 64, "sourceArchive": "archive-example"})
    candidate = {"id": "original-generation", "profileArn": prepared.profile_arn,
                 "name": prepared.name}
    detail = {**candidate, "createdBy": prepared.creator, "createdAt": NOW,
              "scope": prepared.scope, "additionalContext": prepared.additional_context}
    if failure == "foreign_profile":
        detail["profileArn"] = "foreign-profile"
    if failure == "foreign_scope":
        detail["scope"] = {"pillars": ["RESILIENCE"]}
    if failure == "foreign_context":
        detail["additionalContext"] = {"sourceArchive": "another-archive"}
    if failure == "wrong_request_hash":
        detail["additionalContext"] = {**prepared.additional_context, "requestHash": "d" * 64}
    if failure == "wrong_creator":
        detail["createdBy"] = "another-creator"
    if failure == "outside_window":
        detail["createdAt"] = NOW - timedelta(seconds=1)
    if failure == "wrong_generation_id":
        detail["id"] = "another-generation"

    class ReadOnlyClient:
        def __init__(self):
            self.list_calls = 0
            self.get_calls = 0

        def list_agent_recommendation_generations(self, **request):
            self.list_calls += 1
            if failure == "zero":
                return {"items": []}
            if failure == "multiple":
                return {"items": [candidate, {**candidate, "id": "second-generation"}]}
            if failure == "malformed_page":
                return {"items": "not-a-list"}
            if failure == "malformed_candidate":
                return {"items": [None]}
            if failure == "incomplete_page":
                return {"items": [candidate], "nextToken": "looping-token"}
            return {"items": [candidate]}

        def get_agent_recommendation_generation(self, **request):
            self.get_calls += 1
            return detail

        def start_agent_recommendation_generation(self, **request):
            raise AssertionError("readback must never Start")

    client = ReadOnlyClient()
    outcome = reconcile_generation(client, prepared, max_pages=2)
    assert outcome.classification == "outcome_unknown" and outcome.native_job_ref is None
    assert client.get_calls == (1 if failure in {"foreign_profile", "foreign_scope", "foreign_context",
                                               "wrong_request_hash", "wrong_creator", "outside_window",
                                               "wrong_generation_id"} else 0)


async def _run_uncertain_worker_fault(tmp_path, monkeypatch, fault, *, scenario=False):
    import json
    from botocore.exceptions import EndpointConnectionError
    from l5_services.workers import execution
    from l5_services.workers.execution import process_batch

    engine, sessions = await _database(tmp_path)
    clock = [NOW]
    messages = []
    native_calls = []
    native_request = [None]
    list_calls = [0]
    crashed = [False]
    crashed_claim = [None]
    unknown_list_calls = []

    class Provider:
        def __init__(self, config):
            self.meta = SimpleNamespace(config=config)

        def start_agent_recommendation_generation(self, **request):
            assert self.meta.config.retries["total_max_attempts"] == 1
            native_calls.append(request)
            native_request[0] = request
            if fault == "lost_start_response":
                raise EndpointConnectionError(endpoint_url="https://example.invalid")
            return {"id": "original-generation", "profileArn": request["profileArn"],
                    "name": request["name"], "createdBy": "actor-example", "createdAt": NOW}

        def list_agent_recommendation_generations(self, **request):
            list_calls[0] += 1
            assert request == {"profileArn": PROFILE["record_id"],
                               "recommendationType": "ARCHITECTURE"}
            if fault == "lost_start_response" and list_calls[0] == 1:
                return {"items": []}
            if scenario and fault == "lost_start_response" and list_calls[0] == 2:
                return {"items": [{"id": "original-generation", "profileArn": PROFILE["record_id"],
                                   "name": native_request[0]["name"]},
                                  {"id": "second-generation", "profileArn": PROFILE["record_id"],
                                   "name": native_request[0]["name"]}]}
            return {"items": [{"id": "original-generation", "profileArn": PROFILE["record_id"],
                               "name": native_request[0]["name"]}]}

        def get_agent_recommendation_generation(self, **request):
            assert request["generationId"] == "original-generation"
            return {"id": "original-generation", "profileArn": PROFILE["record_id"],
                    "name": native_request[0]["name"], "createdBy": "actor-example",
                    "createdAt": NOW, "scope": native_request[0]["scope"],
                    "additionalContext": native_request[0]["additionalContext"]}

    class DelegatedSession:
        def client(self, service, *, config=None):
            assert service == "wellarchitected"
            return Provider(config or SimpleNamespace(retries={}))

    async def authorize(run, claim, operation):
        assert operation in {"submit", "reconcile"}
        return DelegatedSession()

    async def publish(envelope, delay):
        messages.append((envelope, delay))

    worker = ExecutionService(sessions, None, None, publish, authorize,
                              well_architected_adapter=GenerationAdapter(None), clock=lambda: clock[0])
    envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
    if fault == "crash_before_sql_receipt":
        real_finish = execution.finish_work

        async def crash_after_start(database, claim, **kwargs):
            if kwargs.get("native_job_ref") is not None and not crashed[0]:
                crashed[0] = True
                crashed_claim[0] = claim
                raise RuntimeError("worker died before committing Start receipt")
            return await real_finish(database, claim, **kwargs)

        monkeypatch.setattr(execution, "finish_work", crash_after_start)
    try:
        if fault == "crash_before_sql_receipt":
            batch = {"Records": [{"messageId": "crashed-message", "body": json.dumps(envelope)}]}
            assert await process_batch(batch, worker) == {
                "batchItemFailures": [{"itemIdentifier": "crashed-message"}]}
            assert not messages
            clock[0] += timedelta(seconds=61)
        else:
            assert await worker.execute(envelope)
            async with sessions() as session:
                unknown = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
                assert unknown.state == "outcome_unknown" and unknown.native_job_ref is None
            assert messages[0][1] == 600
            for expected_reads in range(1, 3 if scenario else 2):
                clock[0] += timedelta(seconds=600)
                assert await worker.execute(messages.pop(0)[0])
                assert list_calls[0] == expected_reads and len(native_calls) == 1
                async with sessions() as session:
                    unknown = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
                    assert unknown.state == "outcome_unknown" and unknown.native_job_ref is None
                unknown_list_calls.append(expected_reads)
            clock[0] += timedelta(seconds=600)
        if fault == "crash_before_sql_receipt":
            assert await process_batch(batch, worker) == {"batchItemFailures": []}
        else:
            assert await worker.execute(messages.pop(0)[0])
        if scenario:
            assert await process_batch({"Records": [{"messageId": "replayed-ack",
                                                      "body": json.dumps(envelope)}]}, worker) == {
                "batchItemFailures": []}
        assert not await worker.execute(envelope)
        assert len(native_calls) == 1
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            attempts = (await session.scalars(select(WorkerAttempt))).all()
            work = await session.get(Work, (SCOPE["tenant_id"], "work-example"))
            assert run.state == "running" and run.native_job_ref["generation_id"] == "original-generation"
            assert len(attempts) == 1 and attempts[0].status == "accepted"
            assert attempts[0].first_fence == 1 and attempts[0].reconcile_checks == (
                1 if fault == "crash_before_sql_receipt" else 3 if scenario else 2)
            assert work.completed_at is not None and work.fence == (
                2 if fault == "crash_before_sql_receipt" else 1)
            assert attempts[0].prepared_request["profile_arn"] == native_calls[0]["profileArn"]
            assert attempts[0].prepared_request["additional_context"] == native_calls[0]["additionalContext"]
            assert attempts[0].native_job_ref == run.native_job_ref
            trace = {"start_count": len(native_calls), "list_count": list_calls[0],
                     "unknown_list_calls": unknown_list_calls, "final_fence": work.fence,
                     "generation_id": run.native_job_ref["generation_id"],
                     "source_archive": native_calls[0]["additionalContext"]["s3Uri"]}
        if scenario and crashed_claim[0] is not None:
            assert await finish_work(sessions, crashed_claim[0], state="running", now=clock[0]) is None
        return trace
    finally:
        await engine.dispose()


@pytest.mark.parametrize("fault", ["lost_start_response", "crash_before_sql_receipt"])
async def test_uncertain_worker_start_reuses_original_attempt(tmp_path, monkeypatch, fault):
    await _run_uncertain_worker_fault(tmp_path, monkeypatch, fault)


@pytest.mark.parametrize("fault", ["lost_start_response", "crash_before_sql_receipt"])
async def test_ac_02(tmp_path, monkeypatch, fault):
    trace = await _run_uncertain_worker_fault(tmp_path, monkeypatch, fault, scenario=True)
    assert trace["start_count"] == 1
    assert trace["generation_id"] == "original-generation"
    assert trace["source_archive"] == "s3://fictional-wa-bucket/archive-example"
    if fault == "lost_start_response":
        assert trace["unknown_list_calls"] == [1, 2]
        assert trace["list_count"] == 3 and trace["final_fence"] == 1
    else:
        assert trace["unknown_list_calls"] == []
        assert trace["list_count"] == 1 and trace["final_fence"] == 2


async def test_native_start_rechecks_fence_after_authorization(tmp_path):
    engine, sessions = await _database(tmp_path)
    clock = [NOW]
    old_claim = []
    replacement = []
    native_calls = []

    class Provider:
        def __init__(self, config):
            self.meta = SimpleNamespace(config=config)

        def start_agent_recommendation_generation(self, **request):
            native_calls.append(request)
            raise AssertionError("expired lease cannot Start")

    class DelegatedSession:
        def client(self, service, *, config=None):
            assert service == "wellarchitected"
            return Provider(config)

    async def authorize(run, claim, operation):
        assert operation == "submit"
        old_claim.append(claim)
        clock[0] += timedelta(seconds=61)
        replacement.append(await claim_work(sessions, claim.work_id, claim.generation, now=clock[0]))
        return DelegatedSession()

    worker = ExecutionService(sessions, None, None, None, authorize,
                              well_architected_adapter=GenerationAdapter(None), clock=lambda: clock[0])
    envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
    try:
        assert not await worker.execute(envelope)
        assert replacement[0] is not None and replacement[0].fence > old_claim[0].fence
        assert not native_calls
        assert await finish_work(sessions, old_claim[0], state="running", now=clock[0]) is None
        with pytest.raises(ValueError, match="stale provider attempt"):
            await prepare_submit_attempt(sessions, old_claim[0], now=clock[0])
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            attempts = (await session.scalars(select(WorkerAttempt))).all()
            assert run.state == "queued" and run.native_job_ref is None
            assert len(attempts) == 1 and attempts[0].status == "prepared"
    finally:
        await engine.dispose()


async def test_expired_native_start_receipt_cannot_overwrite_reconciled_generation(tmp_path):
    engine, sessions = await _database(tmp_path)
    clock = [NOW]
    entered = Event()
    release = Event()
    native_calls = []
    published = []

    class Provider:
        def __init__(self, config):
            self.meta = SimpleNamespace(config=config)

        def start_agent_recommendation_generation(self, **request):
            assert self.meta.config.retries["total_max_attempts"] == 1
            native_calls.append(request)
            entered.set()
            assert release.wait(10)
            return {"id": "original-generation", "profileArn": request["profileArn"],
                    "name": request["name"], "createdBy": "actor-example", "createdAt": NOW}

        def list_agent_recommendation_generations(self, **request):
            assert request["profileArn"] == PROFILE["record_id"]
            return {"items": [{"id": "original-generation", "profileArn": PROFILE["record_id"],
                               "name": native_calls[0]["name"]}]}

        def get_agent_recommendation_generation(self, **request):
            assert request["generationId"] == "original-generation"
            return {"id": "original-generation", "profileArn": PROFILE["record_id"],
                    "name": native_calls[0]["name"], "createdBy": "actor-example", "createdAt": NOW,
                    "scope": native_calls[0]["scope"],
                    "additionalContext": native_calls[0]["additionalContext"]}

    class DelegatedSession:
        def client(self, service, *, config=None):
            assert service == "wellarchitected"
            return Provider(config or SimpleNamespace(retries={}))

    async def authorize(run, claim, operation):
        assert operation in {"submit", "reconcile"}
        return DelegatedSession()

    async def publish(envelope, delay):
        published.append((envelope, delay))

    worker = ExecutionService(sessions, None, None, publish, authorize,
                              well_architected_adapter=GenerationAdapter(None), clock=lambda: clock[0])
    envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
    first = asyncio.create_task(worker.execute(envelope))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 10), timeout=11)
        clock[0] += timedelta(seconds=61)
        assert await worker.execute(envelope)
        release.set()
        assert not await first
        assert len(native_calls) == 1 and len(published) == 1
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            attempt = (await session.scalars(select(WorkerAttempt))).one()
            work = await session.get(Work, (SCOPE["tenant_id"], "work-example"))
            assert run.state == "running" and run.native_job_ref["generation_id"] == "original-generation"
            assert attempt.status == "accepted" and attempt.first_fence == 1
            assert attempt.reconcile_checks == 1 and work.fence == 2
        assert not await worker.execute(envelope)
        assert len(native_calls) == 1
    finally:
        release.set()
        await first
        await engine.dispose()


async def test_unsupported_native_cancellation_keeps_last_observation_and_source_hold(tmp_path):
    engine, sessions = await _database(tmp_path)
    archive = {"artifact_id": "archive-example", "version": "revision-example", "sha256": "a" * 64}
    native = {"tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"],
              "profile_arn": PROFILE["record_id"], "generation_id": "generation-example",
              "name": "adp-wa-example"}
    observed = {"native_job_ref": native, "native_status": "IN_PROGRESS", "outcome": "running",
                "observed_at": NOW.isoformat(), "progress_coverage": "complete",
                "progress_fingerprint": "observed-progress"}
    try:
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            await pin_source_hold(session, run, archive, required_until=NOW + timedelta(days=1),
                                  policy_expires_at=NOW + timedelta(days=4), now=NOW)
            await session.commit()
        first = await claim_work(sessions, "work-example", 1, now=NOW)
        polling = await finish_work(sessions, first, state="running", provider_state="running",
                                    native_job_ref=native, observation=observed,
                                    next_kind="poll", due_at=NOW + timedelta(seconds=5), now=NOW)
        assert cancel_generation().classification == "cancel_unsupported"
        second = await claim_work(sessions, polling, 1, now=NOW + timedelta(seconds=5))
        pending = await finish_work(sessions, second, state="cancelled", provider_state="unknown",
                                    poll_error={"code": "cancel_unsupported"}, next_kind="poll",
                                    due_at=NOW + timedelta(seconds=10), now=NOW + timedelta(seconds=5))
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            assert run.state == "cancelled" and run.provider_state == "running"
            assert run.monitoring["state"] == "recovery_pending"
            assert run.monitoring["latest_observation"] == observed
            assert run.monitoring["last_successful_poll_at"] == NOW.isoformat()
            assert run.monitoring["progress_state"] == "observed"
            assert run.monitoring["last_poll_error"] == {"code": "cancel_unsupported"}
        third = await claim_work(sessions, pending, 1, now=NOW + timedelta(seconds=10))
        assert await finish_work(sessions, third, state="cancelled", provider_state="unknown",
                                 poll_error={"code": "poll_limit_reached"},
                                 now=NOW + timedelta(seconds=10)) == "completed"
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            hold = await session.get(SourceRetention, (SCOPE["tenant_id"], archive["artifact_id"], run.run_id))
            assert run.provider_state == "running" and run.native_job_ref == native
            assert run.monitoring["state"] == "recovery_blocked"
            assert run.monitoring["latest_observation"] == observed
            assert run.monitoring["last_successful_poll_at"] == NOW.isoformat()
            assert run.monitoring["last_poll_error"] == {"code": "poll_limit_reached"}
            assert hold.released_at is None and hold.artifact_ref == archive
            with pytest.raises(RetentionError, match="matching remote terminal proof required"):
                release_source_hold(hold, run, None, now=NOW + timedelta(days=2))
            assert not await source_cleanup_allowed(session, SCOPE["tenant_id"], archive,
                                                    now=NOW + timedelta(days=5))
    finally:
        await engine.dispose()


@pytest.mark.parametrize("local_stop", ["cancelled", "expired"])
async def test_native_worker_collects_after_local_stop_and_denied_readback(tmp_path, local_stop):
    from botocore.exceptions import ClientError

    from l5_services.api.runs import validate_mutation
    from l5_services.models.evidence import AssessmentResult
    from l5_services.run_access import RunStore

    engine, sessions = await _database(tmp_path)
    clock = [NOW]
    messages = []
    native_calls = []
    status_reads = []
    archive = {"artifact_id": "archive-example", "version": "revision-example",
               "sha256": "a" * 64, "bytes": 16}
    recommendation_arn = "arn:aws:wellarchitected:us-east-1:000000000000:agent-recommendation/f8b72323-0e40-5a08-bd7f-039dfb85a9b2"

    class Provider:
        def __init__(self, config):
            self.meta = SimpleNamespace(config=config)

        def start_agent_recommendation_generation(self, **request):
            native_calls.append(request)
            return {"id": "original-generation", "profileArn": request["profileArn"],
                    "name": request["name"], "createdBy": "actor-example", "createdAt": NOW}

        def get_agent_recommendation_generation(self, **request):
            assert request == {"profileArn": PROFILE["record_id"], "generationId": "original-generation"}
            status_reads.append(clock[0])
            if len(status_reads) == 2:
                raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "restricted"}},
                                  "GetAgentRecommendationGeneration")
            return {"id": "original-generation", "profileArn": PROFILE["record_id"],
                    "name": native_calls[0]["name"], "scope": native_calls[0]["scope"],
                    "additionalContext": native_calls[0]["additionalContext"],
                    "status": "IN_PROGRESS" if len(status_reads) == 1 else "COMPLETED"}

        def list_agent_recommendations(self, **request):
            assert request == {"profileArn": PROFILE["record_id"], "maxResults": 1000}
            return {"items": [{"recommendationArn": recommendation_arn,
                               "profileArn": PROFILE["record_id"], "generationId": "original-generation"}]}

        def get_agent_recommendation(self, **request):
            assert request == {"recommendationArn": recommendation_arn}
            return {"recommendationArn": recommendation_arn, "profileArn": PROFILE["record_id"],
                    "generationId": "original-generation", "title": "Review network recovery",
                    "description": "Use an isolated path", "pillar": "RESILIENCE"}

    class DelegatedSession:
        def client(self, service, *, config=None):
            assert service == "wellarchitected"
            return Provider(config or SimpleNamespace(retries={}))

    async def authorize(run, claim, operation):
        assert operation in {"submit", "observe", "collect"}
        return DelegatedSession()

    async def publish(envelope, delay):
        messages.append((envelope, delay))

    worker = ExecutionService(sessions, None, None, publish, authorize,
                              well_architected_adapter=GenerationAdapter(None), clock=lambda: clock[0])
    try:
        assert validate_mutation("well_architected", {"reason": "local-only"},
                                 "example-idempotency-key", allow_wa=True) == "local-only"
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            scope = {**SCOPE, "owner_id": "actor-example"}
            run.scope = scope
            run.admission = {**run.admission, "scope": scope}
            run.manifest = {**run.manifest, "scope": scope, "archive": archive,
                            "archive_readback": {"artifact": archive, "sha256": archive["sha256"],
                                                 "bytes": archive.get("bytes")}}
            await pin_source_hold(session, run, archive, required_until=NOW + timedelta(days=1),
                                  policy_expires_at=NOW + timedelta(days=4), now=NOW)
            await session.commit()
        envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
        assert await worker.execute(envelope)
        assert len(native_calls) == 1
        clock[0] += timedelta(seconds=5)
        assert await worker.execute(messages.pop(0)[0])
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            original = run.monitoring["latest_observation"]
            assert run.provider_state == "running" and original["native_status"] == "IN_PROGRESS"
        if local_stop == "cancelled":
            async with sessions() as session:
                store = RunStore(session, SCOPE["tenant_id"], SCOPE["product_id"], SCOPE["project_id"],
                                 "well_architected", "actor-example")
                response = await store.cancel_queued("run-example", "local-only", "example-idempotency-key", now=clock[0])
                assert response["state"] == "cancel_requested"
            async with sessions() as session:
                stopped = (await session.scalars(select(Work).where(
                    Work.kind == "cancel", Work.completed_at.is_(None)))).one()
            messages.clear()
            assert await worker.execute({"schema_version": "1.0", "work_id": stopped.work_id, "generation": 1})
        else:
            clock[0] += timedelta(hours=1)
            assert await worker.execute(messages.pop(0)[0])
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            assert run.state == local_stop and run.provider_state == "running"
            assert run.monitoring["state"] == "recovery_pending"
            assert run.monitoring["latest_observation"] == original
        assert await worker.execute(messages.pop(0)[0])
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            assert run.state == local_stop and run.provider_state == "running"
            assert run.monitoring["latest_observation"] == original
            assert run.monitoring["last_successful_poll_at"] == original["observed_at"]
            assert run.monitoring["last_poll_error"] == {"code": "status_read_failed"}
        clock[0] += timedelta(seconds=5)
        assert await worker.execute(messages.pop(0)[0])
        assert await worker.execute(messages.pop(0)[0])
        assert await worker.execute(messages.pop(0)[0])
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            result = (await session.scalars(select(AssessmentResult))).one()
            hold = await session.get(SourceRetention, (SCOPE["tenant_id"], archive["artifact_id"], run.run_id))
            assert run.state == local_stop and run.provider_state == "completed"
            assert run.native_job_ref["generation_id"] == "original-generation"
            assert result.payload["native_job_ref"] == run.native_job_ref
            assert result.payload["recommendations"][0]["recommendationArn"] == recommendation_arn
            assert hold.released_at is None
        assert len(status_reads) == 3 and len(native_calls) == 1
    finally:
        await engine.dispose()


async def test_native_recovery_routes_allow_scoped_reads_and_cancel_but_not_retry(tmp_path):
    import httpx
    from fastapi import FastAPI

    from l5_services.api.runs import create_runs_router

    engine, sessions = await _database(tmp_path)
    actions = []

    class Backend:
        async def get_run(self, product_id, project_id, service_id, run_id):
            assert (product_id, project_id, service_id, run_id) == (
                SCOPE["product_id"], SCOPE["project_id"], "well_architected", "run-example")
            async with sessions() as session:
                return await session.get(Run, (SCOPE["tenant_id"], run_id))

        async def list_runs(self, product_id, project_id, service_id, cursor):
            assert cursor is None
            return [await self.get_run(product_id, project_id, service_id, "run-example")]

        async def cancel_run(self, product_id, project_id, service_id, run_id, reason, idempotency_key):
            actions.append(("cancel", reason, idempotency_key))
            return await self.get_run(product_id, project_id, service_id, run_id)

        async def reconcile_run(self, product_id, project_id, service_id, run_id, reason, idempotency_key):
            actions.append(("reconcile", reason, idempotency_key))
            return await self.get_run(product_id, project_id, service_id, run_id)

    backend = Backend()
    app = FastAPI()
    app.include_router(create_runs_router(lambda: backend),
                       prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")
    path = (f"/v1/products/{SCOPE['product_id']}/projects/{SCOPE['project_id']}"
            "/services/well_architected/runs")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            listed = await client.get(path)
            fetched = await client.get(f"{path}/run-example")
            cancelled = await client.post(f"{path}/run-example/cancel", json={"reason": "local-only"},
                                          headers={"Idempotency-Key": "example-idempotency-key"})
            reconciled = await client.post(f"{path}/run-example/reconcile", json={"reason": "readback"},
                                           headers={"Idempotency-Key": "example-idempotency-key"})
            retry = await client.post(f"{path}/run-example/retry", json={"reason": "new Start"},
                                      headers={"Idempotency-Key": "example-idempotency-key"})
        assert listed.status_code == fetched.status_code == 200
        assert listed.json()["items"][0]["run_id"] == fetched.json()["run_id"] == "run-example"
        assert cancelled.status_code == reconciled.status_code == 202
        assert actions == [("cancel", "local-only", "example-idempotency-key"),
                           ("reconcile", "readback", "example-idempotency-key")]
        assert retry.status_code == 404
    finally:
        await engine.dispose()


async def test_uncertain_native_start_recovers_original_generation_after_local_expiry(tmp_path):
    from botocore.exceptions import EndpointConnectionError

    from l5_services.models.evidence import AssessmentResult

    engine, sessions = await _database(tmp_path)
    clock = [NOW]
    messages = []
    native_calls = []
    list_calls = []

    class Provider:
        def __init__(self, config):
            self.meta = SimpleNamespace(config=config)

        def start_agent_recommendation_generation(self, **request):
            assert self.meta.config.retries["total_max_attempts"] == 1
            native_calls.append(request)
            raise EndpointConnectionError(endpoint_url="https://example.invalid")

        def list_agent_recommendation_generations(self, **request):
            list_calls.append(request)
            return {"items": [{"id": "original-generation", "profileArn": PROFILE["record_id"],
                               "name": native_calls[0]["name"]}]}

        def get_agent_recommendation_generation(self, **request):
            assert request == {"profileArn": PROFILE["record_id"], "generationId": "original-generation"}
            return {"id": "original-generation", "profileArn": PROFILE["record_id"],
                    "name": native_calls[0]["name"], "createdBy": "actor-example", "createdAt": NOW,
                    "scope": native_calls[0]["scope"], "additionalContext": native_calls[0]["additionalContext"],
                    "status": "COMPLETED"}

        def list_agent_recommendations(self, **request):
            return {"items": []}

    class DelegatedSession:
        def client(self, service, *, config=None):
            assert service == "wellarchitected"
            return Provider(config or SimpleNamespace(retries={}))

    async def authorize(run, claim, operation):
        assert operation in {"submit", "reconcile", "observe", "collect"}
        return DelegatedSession()

    async def publish(envelope, delay):
        messages.append((envelope, delay))

    worker = ExecutionService(sessions, None, None, publish, authorize,
                              well_architected_adapter=GenerationAdapter(None), clock=lambda: clock[0])
    try:
        assert await worker.execute({"schema_version": "1.0", "work_id": "work-example", "generation": 1})
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            assert run.state == "outcome_unknown" and run.native_job_ref is None
        clock[0] += timedelta(hours=1)
        assert await worker.execute(messages.pop(0)[0])
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            assert run.state == "expired" and run.provider_state == "unknown"
            assert run.monitoring["state"] == "recovery_pending"
        assert await worker.execute(messages.pop(0)[0])
        assert len(native_calls) == len(list_calls) == 1
        assert await worker.execute(messages.pop(0)[0])
        assert await worker.execute(messages.pop(0)[0])
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            result = (await session.scalars(select(AssessmentResult))).one()
            assert run.state == "expired" and run.provider_state == "completed"
            assert run.native_job_ref["generation_id"] == "original-generation"
            assert result.payload["native_job_ref"] == run.native_job_ref
        assert len(native_calls) == 1
    finally:
        await engine.dispose()


async def test_native_cancel_requested_at_poll_limit_remains_local_not_remote_stop(tmp_path):
    engine, sessions = await _database(tmp_path)
    native = {"tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"],
              "profile_arn": PROFILE["record_id"], "generation_id": "generation-example", "name": "adp-wa-example"}
    observed = {"native_job_ref": native, "outcome": "running", "observed_at": NOW.isoformat()}
    try:
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.state = "cancel_requested"
            run.provider_state = "running"
            run.native_job_ref = native
            run.monitoring = {"latest_observation": observed, "last_successful_poll_at": NOW.isoformat()}
            run.admission = {**run.admission, "limits": {"max_polls": 0}}
            work = await session.get(Work, (SCOPE["tenant_id"], "work-example"))
            work.kind = "poll"
            await session.commit()

        async def authorize(run, claim, operation):
            raise AssertionError("poll cap must not call provider")

        worker = ExecutionService(sessions, None, None, None, authorize,
                                  well_architected_adapter=GenerationAdapter(None), clock=lambda: NOW)
        assert await worker.execute({"schema_version": "1.0", "work_id": "work-example", "generation": 1})
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            assert run.state == "cancelled" and run.provider_state == "running"
            assert run.monitoring["state"] == "recovery_blocked"
            assert run.monitoring["latest_observation"] == observed
            assert run.monitoring["last_successful_poll_at"] == NOW.isoformat()
            assert run.monitoring["last_poll_error"] == {"code": "poll_limit_reached"}
    finally:
        await engine.dispose()


async def _run_late_native_readback(tmp_path, *, scenario=False):
    from fastapi import HTTPException

    from l5_services.adapters.well_architected.generation import prepare_generation
    from l5_services.admission import reconcile_and_dispatch
    from l5_services.models.evidence import AssessmentResult
    from l5_services.run_access import RunStore

    engine, sessions = await _database(tmp_path)
    clock = [NOW + timedelta(hours=1)]
    published = []
    read_calls = []
    start_calls = []
    access = ["denied"]
    archive = {"artifact_id": "archive-example", "version": "revision-example",
               "sha256": "a" * 64, "bytes": 16}
    native = None
    prepared = None
    last_observation = None

    class Provider:
        def __init__(self, config):
            self.meta = SimpleNamespace(config=config)

        def start_agent_recommendation_generation(self, **request):
            start_calls.append(request)
            raise AssertionError("late readback must not submit a new native generation")

        def get_agent_recommendation_generation(self, **request):
            assert request == {"profileArn": PROFILE["record_id"], "generationId": "original-generation"}
            read_calls.append(request)
            return {"id": "original-generation", "profileArn": PROFILE["record_id"],
                    "name": prepared.name, "scope": prepared.scope,
                    "additionalContext": prepared.additional_context, "status": "COMPLETED"}

        def list_agent_recommendations(self, **request):
            assert request == {"profileArn": PROFILE["record_id"], "maxResults": 1000}
            return {"items": []}

    class DelegatedSession:
        def client(self, service, *, config=None):
            assert service == "wellarchitected"
            return Provider(config or SimpleNamespace(retries={}))

    async def authorize(run, claim, operation):
        assert operation in {"observe", "collect"}
        if operation == "observe" and access[0] == "denied":
            raise HTTPException(403, detail="readback restricted")
        return DelegatedSession()

    async def publish(envelope, delay):
        published.append((envelope, delay))

    worker = ExecutionService(sessions, None, None, publish, authorize,
                              well_architected_adapter=GenerationAdapter(None), clock=lambda: clock[0])
    try:
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            scope = {**SCOPE, "owner_id": "actor-example"}
            run.scope = scope
            run.admission = {**run.admission, "scope": scope, "limits": {"max_polls": 2}}
            run.manifest = {**run.manifest, "scope": scope, "archive": archive,
                            "archive_readback": {"artifact": archive, "sha256": archive["sha256"],
                                                 "bytes": archive.get("bytes")}}
            prepared = prepare_generation(run, "attempt-example", now=NOW)
            native = {"tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"],
                      "profile_arn": prepared.profile_arn, "generation_id": "original-generation",
                      "name": prepared.name}
            last_observation = {"native_job_ref": native, "native_status": "IN_PROGRESS",
                                "outcome": "running", "observed_at": NOW.isoformat(),
                                "progress_coverage": "complete", "progress_fingerprint": "pinned-progress"}
            run.state = "running" if scenario else "expired"
            run.provider_state = "running"
            run.native_job_ref = native
            run.monitoring = {"state": "recovery_pending", "poll_attempts": 1,
                              "latest_observation": last_observation,
                              "last_successful_poll_at": NOW.isoformat(), "progress_state": "observed",
                              "progress_baseline_at": NOW.isoformat()}
            work = await session.get(Work, (SCOPE["tenant_id"], "work-example"))
            work.kind = "poll"
            work.due_at = clock[0]
            await pin_source_hold(session, run, archive, required_until=NOW + timedelta(days=1),
                                  policy_expires_at=NOW + timedelta(days=4), now=NOW)
            await session.commit()
        first_envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
        if scenario:
            assert await worker.execute(first_envelope)
            assert len(published) == 1 and published[0][1] == 0 and not read_calls
            first_envelope = published.pop(0)[0]
            async with sessions() as session:
                run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
                assert run.state == "expired" and run.provider_state == "running"
                assert run.monitoring["latest_observation"] == last_observation
        assert await worker.execute(first_envelope)
        assert len(published) == 1 and not read_calls
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            assert (run.state, run.provider_state) == ("expired", "running")
            assert run.monitoring["latest_observation"] == last_observation
            assert run.monitoring["last_successful_poll_at"] == NOW.isoformat()
            assert run.monitoring["progress_state"] == "observed"
            assert run.monitoring["last_poll_error"] == {"code": "status_read_denied"}
            assert run.monitoring["poll_attempts"] == 2
        clock[0] += timedelta(seconds=5)
        assert await worker.execute(published.pop(0)[0])
        assert not published and not read_calls
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            assert run.monitoring["state"] == "recovery_blocked"
            assert run.monitoring["last_poll_error"] == {"code": "poll_limit_reached"}
            assert run.monitoring["last_successful_poll_at"] == NOW.isoformat()
            assert run.monitoring["latest_observation"] == last_observation
            hold = await session.get(SourceRetention, (SCOPE["tenant_id"], archive["artifact_id"], run.run_id))
            assert hold.released_at is None
            assert not await source_cleanup_allowed(session, SCOPE["tenant_id"], archive,
                                                    now=NOW + timedelta(days=5))
        clock[0] += timedelta(seconds=360)

        async def request_read(key):
            async with sessions() as session:
                store = RunStore(session, SCOPE["tenant_id"], SCOPE["product_id"], SCOPE["project_id"],
                                 "well_architected", "actor-example")
                return await reconcile_and_dispatch(store, sessions, publish, "run-example", "restored readback",
                                                    key, now=clock[0])

        first = await request_read("manual-recovery-0001")
        assert first["state"] == "expired" and published[0][1] == 0
        with pytest.raises(HTTPException) as pending:
            await request_read("manual-recovery-0002")
        assert pending.value.status_code == 409
        assert await worker.execute(published.pop(0)[0])
        assert not published and not read_calls
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            assert run.monitoring["state"] == "recovery_blocked"
            assert run.monitoring["last_poll_error"] == {"code": "status_read_denied"}
            assert run.monitoring["last_successful_poll_at"] == NOW.isoformat()
            assert run.monitoring["poll_attempts"] == 3 and run.monitoring["manual_recovery_attempts"] == 1
        assert (await request_read("manual-recovery-0001"))["revision"] == first["revision"]
        assert not published
        access[0] = "restored"
        clock[0] += timedelta(seconds=1)
        assert (await request_read("manual-recovery-0002"))["state"] == "expired"
        assert await worker.execute(published.pop(0)[0])
        assert len(read_calls) == 1 and published[0][1] == 0
        assert await worker.execute(published.pop(0)[0])
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            results = (await session.scalars(select(AssessmentResult))).all()
            checkpoint = await session.get(GenerationCollection, (SCOPE["tenant_id"], run.run_id))
            hold = await session.get(SourceRetention, (SCOPE["tenant_id"], archive["artifact_id"], run.run_id))
            assert run.state == "expired" and run.provider_state == "completed"
            assert run.native_job_ref == native and len(results) == 1
            assert results[0].payload["native_job_ref"] == native
            assert run.monitoring["state"] == "finished"
            assert run.monitoring["poll_attempts"] == 4 and run.monitoring["manual_recovery_attempts"] == 2
            assert run.monitoring["latest_observation"]["native_status"] == "COMPLETED"
            assert hold.released_at is None
            assert checkpoint.native_job_ref == native and checkpoint.result_id == results[0].result_id
            trace = {"state": run.state, "provider_state": run.provider_state,
                     "read_count": len(read_calls), "start_count": len(start_calls),
                     "manual_attempts": run.monitoring["manual_recovery_attempts"],
                     "result_count": len(results), "retained": hold.released_at is None,
                     "generation_id": run.native_job_ref["generation_id"]}
        assert not published
        return trace
    finally:
        await engine.dispose()


async def test_denied_native_readback_and_exhausted_polling_allow_one_authorized_late_read(tmp_path):
    await _run_late_native_readback(tmp_path)


async def test_ac_03(tmp_path):
    trace = await _run_late_native_readback(tmp_path, scenario=True)
    assert trace == {"state": "expired", "provider_state": "completed", "read_count": 1,
                     "start_count": 0, "manual_attempts": 2, "result_count": 1,
                     "retained": True, "generation_id": "original-generation"}
