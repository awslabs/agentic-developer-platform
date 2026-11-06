"""Assemble durable L5 control, Product source readback and private artifacts."""

import base64
import json
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from l5_services.adapters.transform.control_host import TransformControlHost
from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.adapters.transform.provenance import verify_original_patch, capture_original_patch, compare_source_trees
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.run import Run
from l5_services.models.work import Work
from l5_services.work_store import TRANSFORM_SUBMIT_OPERATION, transform_baseline
from l5_services.workers.execution import AuthorityRevoked


class TransformExecutor:
    nonblocking = True

    def __init__(self, sessions, transport, pods, checks, source, artifacts, *, clock=None):
        self.sessions, self.pods, self.checks = sessions, pods, checks
        self.source, self.artifacts = source, artifacts
        self.clock = clock or (lambda: datetime.now(UTC))
        self.host = TransformControlHost(sessions, transport, pods, clock=self.clock)

    async def _current(self, authority):
        authority.assert_current()
        request, scope = authority.source_request, authority.scope
        async with self.sessions() as session:
            run = await session.get(Run, (scope["tenant_id"], request["operation_id"]))
            work = await session.get(Work, (scope["tenant_id"], request["attempt_id"]))
            if (run is None or work is None or run.service_id != "transform" or run.admission["scope"] != scope
                    or work.run_id != run.run_id or work.completed_at is not None or work.blocked_at is not None
                    or work.lease_owner != request["lease_owner"] or work.fence != request["lease_fence"]
                    or work.lease_until is None or work.lease_until.replace(tzinfo=work.lease_until.tzinfo or UTC) <= self.clock()
                    or run.admission["delegation_id"] != request["delegation_id"]
                    or run.admission["delegation_version"] != request["delegation_revision"]):
                raise AuthorityRevoked("Transform executor work lease or scope changed")
            return run, work

    async def _call(self, operation, intent, authority, *, manifest=None, native_job_ref=None):
        await self._current(authority)
        event = {"schema_version": "1.0", "kind": "l5-transform-control-v1", "operation": operation,
                 "request_id": str(uuid4()), "intent": intent, "scope": authority.scope,
                 "work": authority.source_request, "expires_at": authority.expires_at.isoformat(),
                 "native_job_ref": native_job_ref}
        if manifest is not None:
            event.update(manifest_sha256=sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                         snapshot_ids=manifest["source_snapshot_ids"])
        return (await self.host(event))["result"]

    async def launch_provider(self, *, intent, manifest, authority):
        return await self._call("launch", intent, authority, manifest=manifest)

    async def read_provider(self, *, intent, authority):
        return await self._call("read", intent, authority)

    async def collect_provider(self, *, intent, authority):
        return await self._call("collect", intent, authority)

    async def request_stop(self, *, intent, native_job_ref, authority):
        return await self._call("stop", intent, authority, native_job_ref=native_job_ref)

    async def read_sealed_source(self, *, manifest, authority):
        run, _ = await self._current(authority)
        if manifest != run.manifest:
            raise AuthorityRevoked("Transform source differs from the durable manifest")
        return await self.source.read_sealed_source(manifest=manifest, authority=authority)

    async def read_original_patch(self, *, reference, authority):
        await self._current(authority)
        return await self.artifacts.read(reference=reference, authority=authority)

    async def read_test_baseline(self, *, manifest, authority):
        run, _ = await self._current(authority)
        async with self.sessions() as session:
            attempt = await session.scalar(select(WorkerAttempt).where(
                WorkerAttempt.tenant_id == run.tenant_id, WorkerAttempt.run_id == run.run_id,
                WorkerAttempt.operation == TRANSFORM_SUBMIT_OPERATION))
        if attempt is None:
            return None
        receipt = await transform_baseline(self.sessions, SimpleNamespace(
            tenant_id=run.tenant_id, run_id=run.run_id, work_id=attempt.first_work_id))
        return [entry["receipt"] for entry in receipt["checks"]] if receipt and receipt["status"] == "passed" else None

    async def read_verified_trees(self, *, manifest, native_job_ref, original_patch, authority):
        run, _ = await self._current(authority)
        if native_job_ref != run.native_job_ref or manifest != run.manifest:
            raise AuthorityRevoked("Transform result differs from the durable provider identity")
        async with self.sessions() as session:
            saved = await session.scalar(select(WorkerAttempt).where(
                WorkerAttempt.tenant_id == run.tenant_id, WorkerAttempt.run_id == run.run_id,
                WorkerAttempt.operation == TRANSFORM_SUBMIT_OPERATION))
        intent = TransformAdapter(self)._intent(run, saved)
        receipt = await self.pods.completed_observation(intent=intent, authority=authority)
        if receipt is None or receipt["executor_uid"] != native_job_ref["executor_uid"]:
            raise AuthorityRevoked("Transform candidate Pod identity changed")
        if (receipt is None or receipt["readback"]["state"] != "completed"
                or receipt["readback"]["native_id"] != native_job_ref["native_id"]):
            raise ValueError("Transform candidate native observation is incomplete")
        base = await self.read_sealed_source(manifest=manifest, authority=authority)
        candidate = {path: base64.b64decode(content, validate=True) for path, content in receipt["candidate_files"].items()}
        modes = {entry["path"]: entry["mode"] for entry in manifest["files"]}
        patch = await self.read_original_patch(reference=original_patch, authority=authority)
        original = capture_original_patch(patch, native_job_ref, run.admission["transform_qualification"], manifest)
        trees = compare_source_trees(base, candidate, base_modes=modes, candidate_modes=receipt["candidate_modes"])
        verify_original_patch(base, trees["candidate_tree"], original, modes)
        return {"base_files": base, "candidate_files": candidate, "base_modes": modes,
                "candidate_modes": receipt["candidate_modes"]}

    async def run_check(self, *, authority, **request):
        run, work = await self._current(authority)
        if (request["stage"] == "before" and work.kind != "submit"
                or request["stage"] == "after" and work.kind != "collect"):
            raise AuthorityRevoked("Transform check stage differs from current work")
        result = await self.checks.run_check(policy=run.admission["transform_test_policy"], authority=authority, **request)
        await self._current(authority)
        return result

    async def read_log(self, *, reference, authority):
        await self._current(authority)
        scoped = {**reference, "tenant_id": authority.scope["tenant_id"], "project_id": authority.scope["project_id"]}
        return await self.artifacts.read(reference=scoped, authority=authority)
