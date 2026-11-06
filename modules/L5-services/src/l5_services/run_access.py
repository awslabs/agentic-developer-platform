"""Tenant-scoped run reads and fenced queued cancellation."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run, view_run
from l5_services.models.work import Work
from l5_services.run_input import NoEffectProof


class RunStore:
    def __init__(self, session, tenant_id, product_id, project_id, service_id, actor_id, relationship_revision=None):
        self.session = session
        self.tenant_id = tenant_id
        self.product_id = product_id
        self.project_id = project_id
        self.service_id = service_id
        self.actor_id = actor_id
        self.relationship_revision = relationship_revision

    def _visible(self, run):
        scope = run.scope or (run.admission or {}).get("scope") or {}
        return ((self.relationship_revision is None or scope.get("relationship_revision") == self.relationship_revision)
                and (scope.get("owner_id") == self.actor_id or scope.get("visibility") == "product_team"))

    async def get(self, run_id, *, for_update=False):
        run = await self.session.get(Run, (self.tenant_id, run_id), with_for_update=for_update,
                                     populate_existing=for_update)
        if (run is None or run.product_id != self.product_id or run.project_id != self.project_id
                or run.service_id != self.service_id or not self._visible(run)):
            raise HTTPException(404, detail={"code": "not_found"})
        return run

    async def list(self, *, limit=None, cursor=None):
        query = select(Run).where(
            Run.tenant_id == self.tenant_id, Run.product_id == self.product_id,
            Run.project_id == self.project_id, Run.service_id == self.service_id,
        )
        if cursor is not None:
            query = query.where(Run.run_id > cursor)
        runs = (await self.session.scalars(query.order_by(Run.run_id))).all()
        visible = [run for run in runs if self._visible(run)]
        return visible[:limit] if limit is not None else visible

    async def owned(self, run_id):
        run = await self.get(run_id, for_update=True)
        if run.admission["scope"]["owner_id"] != self.actor_id:
            raise HTTPException(404, detail={"code": "not_found"})
        return run

    def receipt_key(self, operation, idempotency_key):
        return (self.tenant_id, self.product_id, self.project_id, self.service_id,
                self.actor_id, operation, idempotency_key)

    async def replay(self, operation, idempotency_key, request_hash):
        receipt = await self.session.get(RunReceipt, self.receipt_key(operation, idempotency_key))
        if receipt is not None:
            if receipt.request_hash != request_hash:
                raise HTTPException(409, detail={"code": "idempotency_conflict"})
            return receipt.response
        return None

    async def _commit_mutation(self, operation, idempotency_key, request, request_hash, run):
        response = view_run(run)
        self.session.add(RunReceipt(
            tenant_id=self.tenant_id, product_id=self.product_id, project_id=self.project_id,
            service_id=self.service_id, actor_id=self.actor_id, operation=operation,
            idempotency_key=idempotency_key, request_hash=request_hash, request=request,
            response=response, run_id=run.run_id, expires_at=run.deadline_at,
        ))
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            previous = await self.replay(operation, idempotency_key, request_hash)
            if previous is None:
                raise
            return previous
        except Exception:
            await self.session.rollback()
            raise
        return response

    async def cancel_queued(self, run_id, reason, idempotency_key, *, now=None):
        if self.service_id == "finops":
            return await self.cancel_finops(run_id, reason, idempotency_key, now=now)
        run = await self.owned(run_id)
        request = {"run_id": run_id, "reason": reason}
        request_hash = sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        previous = await self.replay("cancel", idempotency_key, request_hash)
        if previous is not None:
            return previous
        if run.state in {"cancelled", "cancel_requested"}:
            return await self._commit_mutation("cancel", idempotency_key, request, request_hash, run)
        uncertain_submit = False
        if self.service_id in {"transform", "well_architected"} and run.state in {"admitted", "queued"}:
            from l5_services.models.attempt import WorkerAttempt
            from l5_services.work_store import TRANSFORM_SUBMIT_OPERATION

            operation = TRANSFORM_SUBMIT_OPERATION if self.service_id == "transform" else "submit"
            uncertain_submit = await self.session.scalar(select(WorkerAttempt.attempt_id).where(
                WorkerAttempt.tenant_id == self.tenant_id, WorkerAttempt.run_id == run_id,
                WorkerAttempt.operation == operation)) is not None
        if run.state in {"running", "collecting", "outcome_unknown"} or uncertain_submit:
            now = now or datetime.now(UTC)
            run.state = "cancel_requested"
            if uncertain_submit:
                run.provider_state = "unknown"
            run.cancel_reason = reason
            run.revision += 1
            await self.session.execute(update(Work).where(
                Work.tenant_id == self.tenant_id, Work.run_id == run_id,
                Work.completed_at.is_(None),
                or_(Work.lease_until.is_(None), Work.lease_until <= now),
            ).values(completed_at=now, fence=Work.fence + 1, lease_owner=None,
                     lease_until=None), execution_options={"synchronize_session": False})
            self.session.add(Work(tenant_id=self.tenant_id, work_id=str(uuid4()),
                                  run_id=run_id, kind="cancel", generation=1,
                                  step=run.revision, due_at=now))
            return await self._commit_mutation("cancel", idempotency_key, request, request_hash, run)
        if run.state not in {"admitted", "queued"} or run.provider_state != "not_started":
            raise HTTPException(409, detail={"code": "not_queued"})
        now = now or datetime.now(UTC)
        active = await self.session.scalar(select(Work.work_id).where(
            Work.tenant_id == self.tenant_id, Work.run_id == run_id,
            Work.completed_at.is_(None), Work.lease_until > now,
        ).limit(1))
        if active is not None:
            raise HTTPException(409, detail={"code": "not_queued"})
        applied = await self.session.execute(update(Run).where(
            Run.tenant_id == self.tenant_id, Run.run_id == run_id, Run.revision == run.revision,
            Run.state.in_(("admitted", "queued")), Run.provider_state == "not_started",
        ).values(state="cancelled", revision=Run.revision + 1, cancel_reason=reason), execution_options={"synchronize_session": False})
        if applied.rowcount != 1:
            await self.session.rollback()
            raise HTTPException(409, detail={"code": "stale_revision"})
        await self.session.execute(update(Work).where(
            Work.tenant_id == self.tenant_id, Work.run_id == run_id, Work.completed_at.is_(None),
        ).values(completed_at=now, fence=Work.fence + 1, dispatch_owner=None, dispatch_until=None,
                 lease_owner=None, lease_until=None), execution_options={"synchronize_session": False})
        await self.session.refresh(run)
        return await self._commit_mutation("cancel", idempotency_key, request, request_hash, run)

    async def cancel_finops(self, run_id, reason, idempotency_key, *, now=None):
        run = await self.owned(run_id)
        request = {"run_id": run_id, "reason": reason}
        request_hash = sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        previous = await self.replay("cancel", idempotency_key, request_hash)
        if previous is not None:
            return previous
        if run.state in {"succeeded", "partial", "failed", "expired"}:
            raise HTTPException(409, detail={"code": "not_cancellable"})
        if run.state != "cancelled":
            from l5_services.models.attempt import WorkerAttempt

            now = now or datetime.now(UTC)
            existing = await self.session.scalar(select(WorkerAttempt.attempt_id).where(
                WorkerAttempt.tenant_id == self.tenant_id, WorkerAttempt.run_id == run_id,
                WorkerAttempt.operation == "submit"))
            run.state = "cancelled"
            run.cancel_reason = reason
            run.revision += 1
            await self.session.execute(update(Work).where(
                Work.tenant_id == self.tenant_id, Work.run_id == run_id,
                Work.completed_at.is_(None),
            ).values(completed_at=now, fence=Work.fence + 1, lease_owner=None,
                     lease_until=None), execution_options={"synchronize_session": False})
            if run.native_job_ref is not None or existing is not None:
                self.session.add(Work(tenant_id=self.tenant_id, work_id=str(uuid4()),
                                      run_id=run_id, kind="poll" if run.native_job_ref else "reconcile",
                                      generation=1, step=run.revision, due_at=now))
        return await self._commit_mutation("cancel", idempotency_key, request, request_hash, run)

    async def cancel_native(self, run_id, reason, idempotency_key, *, now=None):
        run = await self.get(run_id, for_update=True)
        scope = run.scope or {}
        if run.origin != "native" or scope.get("owner_id") != self.actor_id:
            raise HTTPException(404, detail={"code": "not_found"})
        request = {"run_id": run_id, "reason": reason}
        request_hash = sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        previous = await self.replay("cancel", idempotency_key, request_hash)
        if previous is not None:
            return previous
        if run.state in {"succeeded", "failed", "cancelled", "partial"}:
            raise HTTPException(409, detail={"code": "not_cancellable"})
        if run.state != "cancel_requested":
            now = now or datetime.now(UTC)
            monitoring = dict(run.monitoring or {})
            monitoring["next_poll_at"] = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
            run.state = "cancel_requested"
            run.cancel_reason = reason
            run.next_check_at = now
            run.monitoring = monitoring
            run.revision += 1
        return await self._commit_mutation("cancel", idempotency_key, request, request_hash, run)

    async def reconcile(self, run_id, reason, idempotency_key, *, now=None,
                        recovery_authorization=None, recovery_request=None):
        run = await self.owned(run_id)
        operation = "report_recovery" if recovery_request is not None else "reconcile"
        request = {"run_id": run_id, **(recovery_request or {"reason": reason})}
        request_hash = sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        previous = await self.replay(operation, idempotency_key, request_hash)
        if previous is not None:
            return previous
        work_kind = "reconcile"
        observation_recovery = (self.service_id == "security" or self.service_id == "devops"
                                and run.input.get("capability") == "investigation" and run.origin == "adp_submitted")
        if (observation_recovery and run.native_job_ref is not None
                and run.state in {"running", "collecting", "outcome_unknown", "expired"}):
            instant = now or datetime.now(UTC)
            active = await self.session.scalar(select(Work.work_id).where(
                Work.tenant_id == self.tenant_id, Work.run_id == run_id,
                Work.completed_at.is_(None), Work.lease_until > instant).limit(1))
            if active is not None:
                raise HTTPException(409, detail={"code": "work_pending"})
            # Take over undelivered observation work, never submit another job.
            await self.session.execute(update(Work).where(
                Work.tenant_id == self.tenant_id, Work.run_id == run_id,
                Work.completed_at.is_(None), Work.kind.in_(("poll", "collect", "reconcile")),
            ).values(completed_at=instant, fence=Work.fence + 1,
                     lease_owner=None, lease_until=None, dispatch_owner=None, dispatch_until=None),
                execution_options={"synchronize_session": False})
            work_kind = "collect" if run.provider_state == "completed" else "poll"
            if run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC) <= instant:
                if self.service_id == "devops":
                    from l5_services.devops.recovery import request_recovery
                else:
                    from l5_services.security_recovery import request_recovery

                run.monitoring = request_recovery(run, instant)
                if recovery_authorization is not None:
                    run.monitoring["devops_recovery"]["authorization"] = recovery_authorization
        if self.service_id == "security" and run.state == "blocked" and run.native_job_ref is None:
            from l5_services.models.attempt import WorkerAttempt

            # Native effects require a durable provider attempt first. Only a
            # run that never reached that boundary can repeat materialization.
            attempted = await self.session.scalar(select(WorkerAttempt.attempt_id).where(
                WorkerAttempt.tenant_id == self.tenant_id, WorkerAttempt.run_id == run_id).limit(1))
            pending = await self.session.scalar(select(Work.work_id).where(
                Work.tenant_id == self.tenant_id, Work.run_id == run_id,
                Work.completed_at.is_(None), Work.blocked_at.is_(None)).limit(1))
            if pending is not None:
                raise HTTPException(409, detail={"code": "work_pending"})
            instant = now or datetime.now(UTC)
            if attempted is None and run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC) > instant:
                run.state, run.provider_state = "admitted", "not_started"
                work_kind = "execute"
        if self.service_id == "well_architected":
            recoverable_state = (run.state in {"cancelled", "expired"}
                                 or run.state == "collecting" and run.provider_state == "completed")
            has_identity = run.native_job_ref is not None
            if not has_identity and run.state in {"cancelled", "expired"}:
                from l5_services.models.attempt import WorkerAttempt

                has_identity = await self.session.scalar(select(WorkerAttempt.attempt_id).where(
                    WorkerAttempt.tenant_id == self.tenant_id, WorkerAttempt.run_id == run_id,
                    WorkerAttempt.operation == "submit")) is not None
            if (not recoverable_state or not has_identity
                    or (run.monitoring or {}).get("state") != "recovery_blocked"):
                raise HTTPException(409, detail={"code": "recovery_not_blocked"})
            pending = await self.session.scalar(select(Work.work_id).where(
                Work.tenant_id == self.tenant_id, Work.run_id == run_id,
                Work.completed_at.is_(None), Work.blocked_at.is_(None)).limit(1))
            if pending is not None:
                raise HTTPException(409, detail={"code": "work_pending"})
        run.revision += 1
        self.session.add(Work(
            tenant_id=self.tenant_id, work_id=str(uuid4()), run_id=run_id,
            kind=work_kind, generation=1, step=run.revision, due_at=now or datetime.now(UTC),
        ))
        return await self._commit_mutation(operation, idempotency_key, request, request_hash, run)

    async def retryable(self, run_id):
        run = await self.owned(run_id)
        try:
            proof = NoEffectProof.model_validate(run.no_effect_proof)
        except ValidationError:
            raise HTTPException(409, detail={"code": "no_effect_proof_required"}) from None
        if (run.state != "failed" or run.provider_state not in {"not_started", "failed"}
                or proof.evidence_ref.tenant_id != self.tenant_id
                or proof.evidence_ref.project_id != self.project_id):
            raise HTTPException(409, detail={"code": "no_effect_proof_required"})
        active = await self.session.scalar(select(Work.work_id).where(
            Work.tenant_id == self.tenant_id, Work.run_id == run_id,
            Work.completed_at.is_(None),
        ).limit(1))
        if active is not None:
            raise HTTPException(409, detail={"code": "work_pending"})
        return run
