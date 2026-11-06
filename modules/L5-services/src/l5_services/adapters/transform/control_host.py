"""Private Transform control gate backed by current SQL work and Product authority."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sqlalchemy import select

from l5_services.adapters.transform.authority import TransformWorkAuthorizer
from l5_services.adapters.transform.control_client import _contains_credentials
from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.adapters.transform.provider_job import finite_provider_job
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.run import Run
from l5_services.models.work import Work
from l5_services.work_store import TRANSFORM_SUBMIT_OPERATION, WorkClaim, current_work_fence
from l5_services.workers.execution import AuthorityRevoked


class TransformControlHost:
    def __init__(self, sessions, broker, executor, *, clock=None):
        self.sessions = sessions
        self.clock = clock or (lambda: datetime.now(UTC))
        self.authorizer = TransformWorkAuthorizer(broker, clock=self.clock)
        self.executor = executor

    async def __call__(self, event):
        if (not isinstance(event, dict) or event.get("schema_version") != "1.0"
                or event.get("kind") != "l5-transform-control-v1"
                or event.get("operation") not in {"launch", "read", "stop", "collect"}
                or not isinstance(event.get("request_id"), str) or not event["request_id"]
                or not isinstance(event.get("scope"), dict)
                or not isinstance(event.get("intent"), dict)
                or not isinstance(event.get("work"), dict)
                or _contains_credentials(event)
                or set(event) - {"schema_version", "kind", "operation", "request_id",
                                 "intent", "scope", "work", "expires_at", "native_job_ref",
                                 "manifest_sha256", "snapshot_ids"}):
            raise AuthorityRevoked("Invalid private Transform control request")
        operation = event["operation"]
        scope, intent, request = event["scope"], event["intent"], event["work"]
        if (not isinstance(scope.get("tenant_id"), str)
                or not isinstance(intent.get("run_id"), str)
                or not isinstance(request.get("attempt_id"), str)):
            raise AuthorityRevoked("Transform control identity unavailable")
        now = self.clock()
        try:
            expires_at = datetime.fromisoformat(event["expires_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise AuthorityRevoked("Transform control expiry unavailable") from error
        if (expires_at.tzinfo is None or now >= expires_at
                or not callable(getattr(self.executor, {
                    "launch": "launch_provider", "read": "read_provider",
                    "stop": "request_stop", "collect": "collect_provider"}[operation], None))):
            raise AuthorityRevoked("Transform control host unavailable or expired")
        async with self.sessions() as session:
            work = await session.get(Work, (scope["tenant_id"], request["attempt_id"]))
            run = await session.get(Run, (scope["tenant_id"], intent["run_id"]))
            attempt = await session.scalar(select(WorkerAttempt).where(
                WorkerAttempt.tenant_id == scope["tenant_id"],
                WorkerAttempt.run_id == intent["run_id"],
                WorkerAttempt.operation == TRANSFORM_SUBMIT_OPERATION))
            if (work is None or run is None or attempt is None
                    or run.service_id != "transform" or work.run_id != run.run_id
                    or work.lease_owner != request.get("lease_owner")
                    or work.fence != request.get("lease_fence")
                    or work.lease_until is None or work.lease_until.replace(
                        tzinfo=work.lease_until.tzinfo or UTC) <= now
                    or work.completed_at is not None or work.blocked_at is not None
                    or work.kind not in {"submit", "reconcile", "poll", "collect"}
                    or attempt.status not in {"prepared", "accepted", "outcome_unknown"}
                    or work.tenant_id != scope["tenant_id"]
                    or run.admission.get("scope") != scope
                    or run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC) + timedelta(
                        seconds=(run.admission.get("monitoring_policy") or {}).get("poll_grace_seconds", 0)) < expires_at
                    or request.get("operation_id") != run.run_id):
                raise AuthorityRevoked("Transform control work lease or scope changed")
            claim = WorkClaim(work.tenant_id, work.work_id, work.run_id, work.generation,
                              work.kind, work.step, work.fence, work.lease_owner,
                              run.revision, run.admission, run.input, run.native_job_ref)
            expected = TransformAdapter(object())._intent(run, attempt)
            if operation == "launch":
                if (work.kind != "submit" or attempt.status != "prepared"
                        or run.native_job_ref is not None or attempt.first_work_id != work.work_id
                        or attempt.first_fence != work.fence
                        or not isinstance(run.manifest, dict)
                        or run.manifest.get("scope") != scope
                        or run.manifest.get("run_id") != run.run_id
                        or run.manifest.get("sources") != run.input.get("sources")
                        or run.manifest.get("source_snapshot_ids") != [
                            snapshot["snapshot_id"] for snapshot in run.admission["source_snapshots"]]
                        or sha256(json.dumps(run.manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                        != attempt.input_hash):
                    raise AuthorityRevoked("Transform launch has no committed source or attempt")
                expected["provider_job"] = finite_provider_job(
                    expected, run.manifest, run.admission["transform_qualification"],
                    run.admission.get("limits"))
                if (event.get("manifest_sha256") != attempt.input_hash
                        or event.get("snapshot_ids") != run.manifest.get("source_snapshot_ids")):
                    raise AuthorityRevoked("Transform launch source changed")
            elif event.get("manifest_sha256") is not None or event.get("snapshot_ids") is not None:
                raise AuthorityRevoked("Transform control cannot replace the source")
            if intent != expected:
                raise AuthorityRevoked("Transform control intent differs from durable attempt")
            if operation == "stop" and (not isinstance(event.get("native_job_ref"), dict)
                                        or event["native_job_ref"] != run.native_job_ref):
                raise AuthorityRevoked("Transform stop differs from committed native job")
            if operation != "stop" and event.get("native_job_ref") is not None:
                raise AuthorityRevoked("Transform control cannot replace native job")
            authorize_operation = ("submit" if operation == "launch" else
                                   "reconcile" if work.kind in {"submit", "reconcile"} else
                                   "observe" if work.kind == "poll" else "collect")
            authority = await self.authorizer(run, claim, authorize_operation)
            if authority.source_request != request or authority.scope != scope:
                raise AuthorityRevoked("Transform control broker work differs")
            authority.assert_current()
            if not await current_work_fence(self.sessions, claim, now=self.clock()):
                raise AuthorityRevoked("Transform control work changed before executor effect")
            method = getattr(self.executor, {"launch": "launch_provider", "read": "read_provider",
                                             "stop": "request_stop", "collect": "collect_provider"}[operation])
            args = {"intent": intent, "authority": authority}
            if operation == "launch":
                args["manifest"] = run.manifest
            if operation == "stop":
                args["native_job_ref"] = event["native_job_ref"]
            result = await asyncio.wait_for(method(**args), timeout=5)
            authority.assert_current()
            if not await current_work_fence(self.sessions, claim, now=self.clock()):
                raise ConnectionError("Private Transform control outcome unknown")
            if result is not None and (not isinstance(result, dict) or _contains_credentials(result)):
                raise ConnectionError("Private Transform control result unavailable")
            return {"schema_version": "1.0", "request_id": event["request_id"],
                    "operation": operation, "run_id": run.run_id,
                    "executor_name": intent["executor_name"], "attempt_token": intent["attempt_token"],
                    "work_id": work.work_id, "lease_fence": work.fence, "result": result}
