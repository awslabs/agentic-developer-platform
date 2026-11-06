"""Pre-submit denial and single-launch recovery against a detached baseline."""

from datetime import timedelta
from hashlib import sha256

import pytest
from sqlalchemy import select

from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.adapters.transform.baseline import pinned_test_policy
from l5_services.adapters.transform.provenance import compare_source_trees
from l5_services.models import Run
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.evidence import ProviderObservation
from l5_services.workers.execution import ExecutionService
from test_tf03_attempts import NOW, TEST_POLICY, LostAckBridge, work_db


FILES = {"src/app.py": b"original\n", "tests/test_required.py": b"assert 1\n"}
TREE = compare_source_trees(FILES, FILES)["base_tree"]


def test_selected_executor_rejects_unqualified_long_checks():
    admission = {"transform_settings": {"test_policy_ref": TEST_POLICY["test_policy_ref"]},
                 "transform_test_policy": {**TEST_POLICY, "commands": [
                     {**TEST_POLICY["commands"][0], "timeout_seconds": 120}]}}
    assert pinned_test_policy(admission)[0]["commands"][0]["timeout_seconds"] == 120
    admission["transform_test_policy"]["commands"][0]["timeout_seconds"] = 121
    with pytest.raises(ValueError, match="120 seconds"):
        pinned_test_policy(admission)


class SealedBridge(LostAckBridge):
    def __init__(self, sessions):
        super().__init__(sessions)
        self.sealed_reads = 0
        self.files = FILES

    async def read_sealed_source(self, *, manifest, authority):
        self.sealed_reads += 1
        return self.files

    async def launch_provider(self, *, intent, manifest, authority):
        async with self.sessions() as session:
            attempt = (await session.scalars(select(WorkerAttempt))).one()
            assert attempt.status == "prepared" and attempt.idempotency_token == intent["attempt_token"]
        from types import SimpleNamespace

        return await super().launch_provider(intent=intent, manifest=manifest,
                                             authority=SimpleNamespace(attempt_id=attempt.attempt_id))


class BaselineRunner:
    def __init__(self, *, failure=None):
        self.calls = 0
        self.failure = failure

    async def run_check(self, *, stage, files, source_tree, command, image_digest,
                        bundle_sha256, oracle_version, authority, file_modes):
        self.calls += 1
        assert stage == "before" and files == FILES and source_tree == TREE
        assert file_modes == {path: "100644" for path in FILES}
        return {"stage": stage, "argv": command["argv"], "cwd": command["cwd"],
                "environment": command["environment"], "image_digest": image_digest,
                "source_tree": source_tree, "bundle_sha256": bundle_sha256,
                "lock_sha256": TEST_POLICY["lock_sha256"], "oracle_version": oracle_version,
                "started_at": "2025-12-31T23:59:58Z",
                "ended_at": "2026-01-01T00:00:04Z" if self.failure == "late" else "2025-12-31T23:59:59Z",
                "timed_out": False, "exit_code": 1 if self.failure == "failed" else 0,
                "test_count": 1,
                "stdout": {"artifact_id": "stdout", "version": "1",
                           "sha256": sha256(b"").hexdigest(), "bytes": 0},
                "stderr": {"artifact_id": "stderr", "version": "1",
                           "sha256": sha256(b"").hexdigest(), "bytes": 0}}

    async def read_log(self, *, reference, authority):
        return b""


async def sealed_run(sessions, *, denial=None):
    async with sessions() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        admission = dict(run.admission)
        if denial == "missing_policy":
            admission.pop("transform_test_policy")
        elif denial == "mismatched_policy":
            admission["transform_test_policy"] = {**TEST_POLICY, "test_policy_ref": {"record_id": "other"}}
        elif denial == "unsupported_timeout":
            admission["transform_test_policy"] = {**TEST_POLICY, "commands": [
                {**TEST_POLICY["commands"][0], "timeout_seconds": 121}]}
        run.admission = admission
        manifest = {**run.manifest, "files": [{"path": path, "sha256": sha256(content).hexdigest(),
                                                "bytes": len(content)} for path, content in FILES.items()]}
        if denial == "unsealed":
            manifest.pop("files")
        run.manifest = manifest
        baseline = await session.get(ProviderObservation, ("tenant-a", "baseline-fixture"))
        await session.delete(baseline)
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["missing_policy", "mismatched_policy", "unsupported_timeout", "missing_runner",
                                    "missing_bridge", "unsealed", "substituted_source", "failed", "late"])
async def test_baseline_denial_prevents_paid_attempt(work_db, denial):
    await sealed_run(work_db, denial=denial)
    bridge = SealedBridge(work_db)
    if denial == "missing_bridge":
        bridge.read_sealed_source = None
    if denial == "substituted_source":
        bridge.files = {**FILES, "src/app.py": b"tampered"}
    runner = BaselineRunner(failure=denial)
    operations = []

    async def authorize(run, claim, operation):
        operations.append(operation)
        return object()

    async def publish(envelope, delay):
        raise AssertionError("denial cannot enqueue provider work")

    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge, detached_runner=None
                                                                 if denial == "missing_runner" else runner),
                              clock=lambda: NOW + timedelta(seconds=2))
    assert await worker.execute({"schema_version": "1.0", "work_id": "submit-a", "generation": 1})
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        assert run.state == "blocked" and run.provider_state == "not_started"
        assert not (await session.scalars(select(WorkerAttempt))).all()
    assert bridge.launches == bridge.reads == 0
    assert "submit" not in operations
    assert runner.calls == (1 if denial in {"failed", "late"} else 0)


@pytest.mark.asyncio
async def test_passing_baseline_committed_before_single_provider_launch_and_reused(work_db):
    await sealed_run(work_db)
    bridge = SealedBridge(work_db)
    runner = BaselineRunner()
    published = []
    operations = []

    async def authorize(run, claim, operation):
        operations.append(operation)
        return claim

    async def publish(envelope, delay):
        published.append(envelope)

    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge, detached_runner=runner),
                              clock=lambda: NOW + timedelta(seconds=2))
    assert await worker.execute({"schema_version": "1.0", "work_id": "submit-a", "generation": 1})
    async with work_db() as session:
        baseline = (await session.scalars(select(ProviderObservation))).one()
        attempt = (await session.scalars(select(WorkerAttempt))).one()
        assert baseline.payload["status"] == "passed"
        assert baseline.payload["source_tree"] == TREE
        assert baseline.payload["checks"][0]["receipt"]["source_tree"] == TREE
        assert attempt.first_work_id == baseline.work_id == "submit-a"
        assert attempt.status == "outcome_unknown"
    assert bridge.launches == runner.calls == bridge.sealed_reads == 1
    assert operations == ["verify_baseline", "submit"]
    worker.clock = lambda: NOW + timedelta(seconds=63)
    assert await worker.execute(published.pop())
    assert bridge.launches == runner.calls == bridge.sealed_reads == 1
    assert bridge.reads == 1 and operations[-1] == "reconcile"


@pytest.mark.asyncio
async def test_existing_paid_attempt_without_baseline_still_recovers(work_db):
    from test_tf03_attempts import ObservationBridge, accepted_poll

    native, poll_id = await accepted_poll(work_db)
    async with work_db() as session:
        baseline = await session.get(ProviderObservation, ("tenant-a", "baseline-fixture"))
        await session.delete(baseline)
        await session.commit()
    bridge = ObservationBridge(native)

    async def authorize(run, claim, operation):
        assert operation == "poll"
        return object()

    async def publish(envelope, delay):
        return None

    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge),
                              clock=lambda: NOW + timedelta(seconds=1))
    assert await worker.execute({"schema_version": "1.0", "work_id": poll_id, "generation": 1})
    assert bridge.reads == 1
    async with work_db() as session:
        attempts = (await session.scalars(select(WorkerAttempt))).all()
        assert len(attempts) == 1
        assert (await session.scalars(select(ProviderObservation))).one().payload["outcome"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(("latency_seconds", "file_mode"), [(1, "100644"), (61, "100755")])
async def test_advancing_clock_baseline_submission_and_collection(work_db, latency_seconds, file_mode):
    from datetime import UTC

    from l5_services.models import AssessmentResult, Work

    await sealed_run(work_db)
    current_time = NOW
    candidate = {**FILES, "src/app.py": b"updated\n"}
    modes = {path: file_mode for path in FILES}
    trees = compare_source_trees(FILES, candidate, base_modes=modes, candidate_modes=modes)
    raw_patch = (b"diff --git a/src/app.py b/src/app.py\n"
                 b"--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-original\n+updated\n")
    reference = {"tenant_id": "tenant-a", "project_id": "project-a", "artifact_id": "patch",
                 "version": "1", "media_type": "text/x-diff", "sha256": sha256(raw_patch).hexdigest(),
                 "bytes": len(raw_patch)}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        run.manifest = {**run.manifest, "files": [{**entry, "mode": file_mode} for entry in run.manifest["files"]]}
        run.admission = {**run.admission, "transform_test_policy": {
            **TEST_POLICY, "commands": [{**TEST_POLICY["commands"][0], "timeout_seconds": 120}]}}
        await session.commit()

    class Bridge(SealedBridge):
        async def launch_provider(self, *, intent, manifest, authority):
            try:
                await super().launch_provider(intent=intent, manifest=manifest, authority=authority)
            except TimeoutError:
                pass
            self.receipt["readback"]["state"] = "completed"
            self.native = {key: intent[key] for key in (
                "tenant_id", "project_id", "executor_name", "attempt_token", "input_hash")}
            self.native.update(executor_uid="uid-a", native_id="session-a")
            return self.receipt

        async def collect_provider(self, *, intent, authority):
            return {"state": "completed", "native_job_ref": self.native, "artifacts": {"original_patch": reference}}

        async def read_original_patch(self, *, reference, authority):
            return raw_patch

        async def read_verified_trees(self, **kwargs):
            return {"base_files": FILES, "candidate_files": candidate,
                    "base_modes": modes, "candidate_modes": modes}

        async def read_test_baseline(self, **kwargs):
            raise AssertionError("collection must reuse the durable baseline")

    class Runner:
        def __init__(self):
            self.calls = []

        async def run_check(self, *, stage, files, file_modes, source_tree, command,
                            image_digest, bundle_sha256, oracle_version, authority):
            nonlocal current_time
            assert files == (FILES if stage == "before" else candidate)
            assert file_modes == modes
            assert source_tree == trees["base_tree" if stage == "before" else "candidate_tree"]
            self.calls.append(stage)
            started_at = current_time
            current_time += timedelta(seconds=latency_seconds)
            log = {"artifact_id": "empty-log", "version": "1", "sha256": sha256(b"").hexdigest(), "bytes": 0}
            return {"stage": stage, "argv": command["argv"], "cwd": command["cwd"],
                    "environment": command["environment"], "image_digest": image_digest,
                    "source_tree": source_tree, "bundle_sha256": bundle_sha256,
                    "lock_sha256": TEST_POLICY["lock_sha256"], "oracle_version": oracle_version,
                    "started_at": started_at.isoformat(), "ended_at": current_time.isoformat(),
                    "exit_code": 0, "test_count": 1, "timed_out": False, "stdout": log, "stderr": log}

        async def read_log(self, **kwargs):
            return b""

    class Store:
        def __init__(self):
            self.objects = {}

        async def put(self, *, tenant_id, project_id, run_id, name, contents, media_type, authority):
            self.objects[name] = contents
            return {"tenant_id": tenant_id, "project_id": project_id, "artifact_id": name,
                    "version": "1", "sha256": sha256(contents).hexdigest(), "bytes": len(contents),
                    "media_type": media_type, "expires_at": (NOW + timedelta(hours=1)).isoformat()}

        async def read(self, *, reference, authority):
            return self.objects[reference["artifact_id"]]

    bridge, runner, store = Bridge(work_db), Runner(), Store()
    published = []

    async def authorize(run, claim, operation):
        return claim

    async def publish(envelope, delay):
        published.append(envelope)

    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge, detached_runner=runner, retention_store=store),
                              clock=lambda: current_time)
    assert await worker.execute({"schema_version": "1.0", "work_id": "submit-a", "generation": 1})
    async with work_db() as session:
        attempt = (await session.scalars(select(WorkerAttempt))).one()
        baseline = (await session.scalars(select(ProviderObservation))).one()
        assert baseline.payload["status"] == "passed"
        assert attempt.started_at.replace(tzinfo=UTC) >= baseline.observed_at.replace(tzinfo=UTC) >= (
            NOW + timedelta(seconds=latency_seconds))
    for _stage in ("poll", "collect"):
        envelope = published.pop(0)
        async with work_db() as session:
            work = await session.get(Work, ("tenant-a", envelope["work_id"]))
            current_time = max(current_time, work.due_at.replace(tzinfo=UTC))
        assert await worker.execute(envelope)
    assert not await worker.execute(envelope)
    async with work_db() as session:
        result = (await session.scalars(select(AssessmentResult))).one()
        assert result.payload["source_trees"] == trees
        assert result.payload["checks_status"] == "passed"
        assert (await session.get(Run, ("tenant-a", "run-a"))).state == "succeeded"
        assert len((await session.scalars(select(WorkerAttempt))).all()) == 1
    assert bridge.launches == 1 and runner.calls == ["before", "after"] and not published


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["expired", "owner", "fence", "generation", "revision", "blocked", "completed"])
async def test_extended_work_lease_cannot_revive_stale_ownership(work_db, invalid):
    from dataclasses import replace

    from l5_services.models import Work
    from l5_services.work_store import claim_work, finish_work, renew_work_lease

    claim = await claim_work(work_db, "submit-a", 1, now=NOW)
    checking_at = NOW + timedelta(seconds=60 if invalid == "expired" else 1)
    if invalid in {"owner", "fence", "generation", "revision"}:
        claim = replace(claim, **{"owner": "foreign"} if invalid == "owner" else {
            "run_revision" if invalid == "revision" else invalid: 999})
    if invalid in {"blocked", "completed"}:
        async with work_db() as session:
            work = await session.get(Work, ("tenant-a", "submit-a"))
            setattr(work, f"{invalid}_at", NOW)
            await session.commit()
    assert not await renew_work_lease(work_db, claim, now=checking_at, lease_seconds=240)
    if invalid != "blocked":
        assert await finish_work(work_db, claim, state="succeeded", now=checking_at) is None


@pytest.mark.asyncio
async def test_extended_work_lease_still_expires_and_fences_replacement(work_db):
    from l5_services.work_store import claim_work, finish_work, renew_work_lease

    original = await claim_work(work_db, "submit-a", 1, now=NOW)
    assert await renew_work_lease(work_db, original, now=NOW + timedelta(seconds=1), lease_seconds=180)
    assert await claim_work(work_db, "submit-a", 1, now=NOW + timedelta(seconds=61)) is None
    after_expiry = NOW + timedelta(seconds=182)
    assert await finish_work(work_db, original, state="succeeded", now=after_expiry) is None
    replacement = await claim_work(work_db, "submit-a", 1, now=after_expiry)
    assert replacement.fence > original.fence and replacement.owner != original.owner
    assert not await renew_work_lease(work_db, original, now=after_expiry, lease_seconds=180)
    assert await finish_work(work_db, replacement, state="blocked", now=after_expiry) == "completed"
