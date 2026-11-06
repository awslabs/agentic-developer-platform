"""The installed assembly requires live Product and Profile scope before Start."""

from datetime import timedelta
from hashlib import sha256
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from botocore.hooks import HierarchicalEmitter
from sqlalchemy import select

from l5_services.adapters.well_architected.execution import build_execution
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.run import Run
from l5_services.models.work import Work
from l5_services.source_manifest import package_architecture_sources
from l5_services.workers.execution import AuthorityRevoked
from test_wa04 import NOW, PROFILE, SCOPE, _database
from test_wa04_repairs import CollectionProvider


async def assembled_worker(tmp_path):
    engine, sessions = await _database(tmp_path)
    provider = CollectionProvider([NOW])
    native_start = provider.start_agent_recommendation_generation

    def observed_start(**request):
        provider.meta.events.emit("before-send.wellarchitected.StartAgentRecommendationGeneration", request=None)
        response = native_start(**request)
        provider.meta.events.emit("after-call.wellarchitected.StartAgentRecommendationGeneration",
                                  http_response=SimpleNamespace(status_code=200),
                                  parsed={**response, "ResponseMetadata": {"RequestId": "fixture-native-request"}})
        return response

    provider.start_agent_recommendation_generation = observed_start
    region = "us-east-1"
    account = "000000000000"
    role = f"arn:aws:iam::{account}:role/broker-example"
    execution_role = f"arn:aws:iam::{account}:role/reader-example"
    native = {"arn": PROFILE["record_id"], "aggregationConfiguration": [],
              "executionRoleArn": execution_role, "pillars": ["SECURITY"],
              "eligibleForArchitectureGeneration": True,
              "eligibleForScheduledGeneration": False, "fieldErrors": []}
    provider.get_agent_profile = lambda **kwargs: native
    provider.list_agent_contexts = lambda **kwargs: {"items": []}
    provider.list_agent_recommendation_generations = lambda **kwargs: {"items": []}
    evidence = {"arn": native["arn"], "aggregation": [], "role": execution_role,
                "pillars": ["SECURITY"], "contexts": [], "eligible": True,
                "scheduled": False, "field_errors": []}
    profile_hash = sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    profile = {**PROFILE, "record_revision": profile_hash, "source_connection_id": "connection-example"}
    source_ref = {**SCOPE, "record_type": "document", "record_id": "architecture-example",
                  "record_revision": "version-example", "source_connection_id": "connection-example"}
    snapshot = {"snapshot_id": "snapshot-example", "source_ref": source_ref}
    source_content = b"Architecture goal: source encryption"
    archive_bytes, architecture = package_architecture_sources([{
        "source_ref": source_ref, "snapshot_id": snapshot["snapshot_id"],
        "revision": source_ref["record_revision"], "selected_paths": ["architecture.txt"],
        "selected_hashes": {"architecture.txt": sha256(source_content).hexdigest()},
        "files": [{"path": "architecture.txt", "content": source_content, "component": "service",
                   "exclusion": None, "entry_point": True}], "unresolved": [],
    }], scope=SCOPE, max_files=1, max_bytes=8192)
    archive_ref = {"artifact_id": "l5/well_architected/work/run-example/archive.zip", "version": "version-example",
                   "sha256": sha256(archive_bytes).hexdigest(), "bytes": len(archive_bytes)}
    connection = {"record_id": "connection-example", "record_type": "credential"}
    scope = SCOPE
    settings = {"enabled": True, "source_refs": [source_ref], "connection_ref": connection,
                "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"],
                                     "execution_role_ref": {"record_id": execution_role}}}
    async with sessions() as session:
        run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
        run.input = {**run.input, "sources": [source_ref]}
        run.admission = {**run.admission, "scope": scope, "native_profile_ref": profile,
                         "profile_hash": profile_hash, "configuration_hash": "a" * 64,
                         "connection_ref": connection, "delegation_id": "delegation-example",
                         "delegation_version": 1, "source_snapshots": [snapshot]}
        run.manifest = {**run.manifest, "archive": archive_ref, "architecture": architecture,
            "source_snapshot_ids": [snapshot["snapshot_id"]],
            "provider_source_uri": f"s3://fictional-wa-bucket/{archive_ref['artifact_id']}",
            "archive_readback": {
            "artifact": archive_ref, "sha256": archive_ref["sha256"], "bytes": archive_ref["bytes"]},
            "provider_coverage": "provider_coverage_unverified"}
        session.add(Configuration(tenant_id=SCOPE["tenant_id"], configuration_id=run.configuration_id,
                                  product_id=run.product_id, project_id=run.project_id,
                                  service_id=run.service_id, relationship_revision="1",
                                  owner_id="actor-example", current_revision=1, handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id=SCOPE["tenant_id"], configuration_id=run.configuration_id,
                                         revision=1, settings=settings, inherited_fields=[], gaps=[],
                                         state="ready", hash="a" * 64))
        session.add(Binding(tenant_id=SCOPE["tenant_id"], binding_id=run.binding_id,
                            configuration_id=run.configuration_id, product_id=run.product_id,
                            project_id=run.project_id, service_id=run.service_id,
                            scope=scope, revision=1, generation=1, state="ready",
                            ownership="customer_linked", connection_ref=connection,
                            connection_key="connection-example", native_resource_ref=profile,
                            native_key="profile-example", provider_identity={
                                "account_id": account, "role_arn": role, "region": region},
                            readiness=[{"snapshot_hash": profile_hash}], gaps=[]))
        await session.commit()

    class DelegatedSession:
        account_id = account
        role_arn = role
        region = "us-east-1"

        def client(self, service, *, config=None):
            assert service == "wellarchitected"
            provider.meta = SimpleNamespace(config=config, events=HierarchicalEmitter())
            return provider

    async def authorize(run, claim, operation):
        assert run.service_id == "well_architected" and operation in {"submit", "reconcile"}
        return DelegatedSession()

    source = SimpleNamespace(read_source=AsyncMock(return_value=snapshot),
                             read_source_manifest=AsyncMock(), stream_source=AsyncMock())
    archive = SimpleNamespace(bucket="fictional-wa-bucket", client=SimpleNamespace(
        meta=SimpleNamespace(region_name="us-east-1")),
        put_archive=AsyncMock(), get_archive=AsyncMock(return_value=archive_bytes))
    consumer = SimpleNamespace(publish=AsyncMock(), read_receipt=AsyncMock(), invalidate=AsyncMock())
    worker = build_execution(sessions, source, archive, AsyncMock(), authorize, AsyncMock(),
                             lambda *args: {"status": "not_verifiable", "citations": []},
                             context_client=consumer, insights_client=consumer, clock=lambda: NOW)
    return engine, sessions, worker, provider, native, source, archive


async def test_assembly_executes_one_sql_owned_generation(tmp_path):
    engine, sessions, worker, provider, native, source, archive = await assembled_worker(tmp_path)
    envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
    try:
        assert await worker.execute(envelope) is True
        assert await worker.execute(envelope) is False
        assert await worker.execute({**envelope, "work_id": "foreign-work"}) is False
        assert len(provider.starts) == 1
        assert provider.starts[0]["additionalContext"] == {
            "s3Uri": "s3://fictional-wa-bucket/l5/well_architected/work/run-example/archive.zip",
            "s3VersionId": "version-example",
            "lensArns": ["arn:aws:wellarchitected::aws:lens/wellarchitected"]}
        assert worker.service_id == "well_architected"
        import botocore.session
        import re

        shape = botocore.session.get_session().get_service_model("wellarchitected").operation_model(
            "StartAgentRecommendationGeneration").input_shape
        assert re.fullmatch(shape.members["profileArn"].metadata["pattern"], provider.starts[0]["profileArn"])
        source.read_source.assert_awaited_once()
        archive.get_archive.assert_awaited_once()
        async with sessions() as session:
            attempt = (await session.scalars(select(WorkerAttempt))).one()
            assert attempt.prepared_request["profile_arn"] == provider.starts[0]["profileArn"]
            assert attempt.status == "accepted"
    finally:
        await engine.dispose()


async def test_run_view_exposes_only_durable_selected_source_manifest(tmp_path):
    from l5_services.models.run import view_run

    engine, sessions, _, _, _, _, _ = await assembled_worker(tmp_path)
    try:
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            source = view_run(run)["source_manifest"]
            assert source == {key: run.manifest[key] for key in (
                "archive", "architecture", "provider_source_uri", "provider_coverage")}
            assert source["provider_source_uri"] == "s3://fictional-wa-bucket/" + source["archive"]["artifact_id"]
            run.service_id = "security"
            assert "source_manifest" not in view_run(run)
            run.service_id = "well_architected"
            run.manifest = {"architecture": source["architecture"]}
            assert "source_manifest" not in view_run(run)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("scenario", ["valid", "revoked", "stale", "expired"])
async def test_selected_service_boundary_rejects_foreign_and_duplicate_deliveries(tmp_path, scenario):
    engine, sessions, worker, provider, _, source, archive = await assembled_worker(tmp_path)
    envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
    try:
        assert await worker.execute({**envelope, "work_id": "foreign-work"}) is False
        assert await worker.execute({**envelope, "generation": 2}) is False
        assert not provider.starts
        assert source.read_source.await_count == 0
        assert archive.get_archive.await_count == 0
        async with sessions() as session:
            pending = await session.get(Work, (SCOPE["tenant_id"], "work-example"))
            assert pending.completed_at is None
            assert (await session.scalars(select(WorkerAttempt))).all() == []
            if scenario == "revoked":
                binding = await session.get(Binding, (SCOPE["tenant_id"], "binding-example"))
                binding.state = "blocked"
            elif scenario == "stale":
                configuration = await session.get(Configuration, (SCOPE["tenant_id"], "configuration-example"))
                configuration.current_revision = 2
            elif scenario == "expired":
                run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
                run.deadline_at = NOW - timedelta(seconds=1)
            await session.commit()

        assert await worker.execute(envelope) is True
        assert await worker.execute(envelope) is False
        assert not provider.details
        assert len(provider.starts) == (1 if scenario == "valid" else 0)
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            work = await session.get(Work, (SCOPE["tenant_id"], "work-example"))
            attempts = (await session.scalars(select(WorkerAttempt))).all()
            assert work.completed_at is not None
            if scenario == "valid":
                assert run.state == "running" and run.native_job_ref is not None
                assert len(attempts) == 1 and attempts[0].status == "accepted"
            else:
                assert run.native_job_ref is None and run.provider_state == "not_started"
                assert len(attempts) <= 1
                if scenario in {"revoked", "stale"}:
                    assert len(attempts) == 1
                if attempts:
                    assert attempts[0].status == "definitive_failure"
    finally:
        await engine.dispose()


@pytest.mark.parametrize("change", ["disabled", "revoked", "source_changed", "profile_ineligible",
                                   "profile_drift", "archive_changed", "wrong_role", "quota_exhausted", "wrong_region",
                                   "missing_manifest", "stale", "expired", "source_denied", "wrong_source_uri"])
async def test_preflight_denies_changed_scope_before_native_start(tmp_path, change):
    engine, sessions, worker, provider, native, source, archive = await assembled_worker(tmp_path)
    try:
        if change in {"disabled", "revoked"}:
            async with sessions() as session:
                if change == "disabled":
                    version = await session.get(ConfigurationVersion, (SCOPE["tenant_id"], "configuration-example", 1))
                    version.settings = {**version.settings, "enabled": False}
                else:
                    binding = await session.get(Binding, (SCOPE["tenant_id"], "binding-example"))
                    binding.state = "blocked"
                await session.commit()
        elif change == "stale":
            async with sessions() as session:
                config = await session.get(Configuration, (SCOPE["tenant_id"], "configuration-example"))
                config.current_revision = 2
                await session.commit()
        elif change == "expired":
            from datetime import timedelta

            async with sessions() as session:
                run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
                run.deadline_at = NOW - timedelta(seconds=1)
                await session.commit()
        elif change == "source_denied":
            source.read_source.side_effect = PermissionError("source revoked")
        elif change == "source_changed":
            source.read_source.return_value = {"snapshot_id": "stale"}
        elif change == "profile_ineligible":
            native["eligibleForArchitectureGeneration"] = None
        elif change == "profile_drift":
            native["executionRoleArn"] = "arn:aws:iam::000000000000:role/foreign-example"
        elif change == "quota_exhausted":
            provider.list_agent_recommendation_generations = lambda **kwargs: {"items": [
                {"profileArn": native["arn"], "createdAt": NOW} for _ in range(5)]}
        elif change == "wrong_region":
            archive.client.meta.region_name = "eu-west-1"
        elif change == "wrong_source_uri":
            async with sessions() as session:
                run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
                run.manifest = {**run.manifest, "provider_source_uri": "s3://other-example-bucket/archive.zip"}
                await session.commit()
        elif change == "missing_manifest":
            async with sessions() as session:
                run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
                run.manifest = {**run.manifest, "architecture": {"sources": [], "files": []}}
                await session.commit()
        elif change == "archive_changed":
            archive.get_archive.return_value = b"changed"
        else:
            worker.authorize = AsyncMock(return_value=SimpleNamespace(
                account_id="000000000000", role_arn="foreign-role", region="us-east-1"))
        result = await worker.execute({"schema_version": "1.0", "work_id": "work-example", "generation": 1})
        assert result is True
        assert not provider.starts
    finally:
        await engine.dispose()


async def test_undocumented_review_pillar_is_rejected_before_start(tmp_path):
    engine, sessions, worker, provider, *_ = await assembled_worker(tmp_path)
    try:
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            run.admission = {**run.admission, "generation_scope": {"pillars": ["OPERATIONAL_EXCELLENCE"]}}
            await session.commit()
        with pytest.raises(ValueError, match="documented native source or review scope unavailable"):
            await worker.execute({"schema_version": "1.0", "work_id": "work-example", "generation": 1})
        assert not provider.starts
    finally:
        await engine.dispose()


@pytest.mark.parametrize("denial", ["source", "authorization"])
async def test_pre_start_denial_records_definitive_attempt_without_replay(tmp_path, denial):
    engine, sessions, worker, provider, _, source, _ = await assembled_worker(tmp_path)
    envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
    if denial == "source":
        source.read_source.side_effect = PermissionError("source revoked")
    else:
        worker.authorize = AsyncMock(side_effect=AuthorityRevoked("delegation revoked"))
    try:
        assert await worker.execute(envelope)
        assert not await worker.execute(envelope)
        assert not provider.starts
        assert not provider.details
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            attempt = (await session.scalars(select(WorkerAttempt))).one()
            work = await session.get(Work, (SCOPE["tenant_id"], "work-example"))
            assert run.state == "blocked" and run.provider_state == "not_started"
            assert run.native_job_ref is None
            assert attempt.status == "definitive_failure" and attempt.native_job_ref is None
            assert attempt.prepared_request["additional_context"]["s3Uri"] == (
                "s3://fictional-wa-bucket/l5/well_architected/work/run-example/archive.zip")
            assert work.completed_at is not None
    finally:
        await engine.dispose()


async def test_lost_start_response_recovers_pinned_generation_without_resubmitting(tmp_path):
    from botocore.exceptions import EndpointConnectionError

    engine, sessions, worker, provider, _, _, _ = await assembled_worker(tmp_path)
    clock = [NOW]
    worker.clock = lambda: clock[0]
    published = []

    async def publish(envelope, delay):
        published.append(envelope)

    worker.publish = publish
    start = provider.start_agent_recommendation_generation

    def lost_response(**request):
        start(**request)
        raise EndpointConnectionError(endpoint_url="https://example.invalid")

    provider.start_agent_recommendation_generation = lost_response
    provider.list_agent_recommendation_generations = lambda **kwargs: {"items": [
        {"id": "original-generation", "profileArn": provider.native["profile_arn"],
         "name": provider.native["name"], "createdAt": NOW}] if provider.starts else []}
    envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
    try:
        assert await worker.execute(envelope)
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            attempt = (await session.scalars(select(WorkerAttempt))).one()
            assert run.state == "outcome_unknown" and run.provider_state == "unknown"
            assert attempt.status == "outcome_unknown" and attempt.native_job_ref is None
        assert not await worker.execute(envelope)
        assert len(published) == 1
        clock[0] += timedelta(seconds=600)
        assert await worker.execute(published.pop())
        assert len(provider.starts) == 1
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            attempt = (await session.scalars(select(WorkerAttempt))).one()
            assert run.state == "running" and run.native_job_ref["generation_id"] == "original-generation"
            assert attempt.status == "accepted" and attempt.native_job_ref == run.native_job_ref
            assert attempt.reconcile_checks == 1
    finally:
        await engine.dispose()


async def test_pre_start_fence_loss_never_marks_attempt_definitive_or_calls_provider(tmp_path):
    engine, sessions, worker, provider, _, _, _ = await assembled_worker(tmp_path)
    preflight = worker.well_architected_preflight

    async def lose_fence(run, claim, provider_session):
        await preflight(run, claim, provider_session)
        async with sessions() as session:
            work = await session.get(Work, (SCOPE["tenant_id"], "work-example"))
            work.fence += 1
            await session.commit()

    worker.well_architected_preflight = lose_fence
    try:
        assert not await worker.execute({"schema_version": "1.0", "work_id": "work-example", "generation": 1})
        assert not provider.starts
        async with sessions() as session:
            run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
            attempt = (await session.scalars(select(WorkerAttempt))).one()
            work = await session.get(Work, (SCOPE["tenant_id"], "work-example"))
            assert run.state == "queued" and run.native_job_ref is None
            assert attempt.status == "prepared" and attempt.first_fence == 1
            assert work.fence == 2 and work.completed_at is None
    finally:
        await engine.dispose()
