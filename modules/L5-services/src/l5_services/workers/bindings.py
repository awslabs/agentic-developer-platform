"""Consume setup envelopes without trusting queue data as provider authority."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import or_, select, update

from l5_services.api.bindings import BindingStore
from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork
from l5_services.workers.execution import AuthorityRevoked


@dataclass(frozen=True)
class SetupClaim:
    tenant_id: str
    operation_id: str
    work_id: str
    generation: int
    owner: str
    fence: int
    lease_until: datetime


async def block_claim(session, claim, reason, now):
    await session.execute(update(SetupWork).where(
        SetupWork.tenant_id == claim.tenant_id, SetupWork.work_id == claim.work_id,
        SetupWork.generation == claim.generation, SetupWork.lease_owner == claim.owner,
        SetupWork.fence == claim.fence, SetupWork.lease_until > now,
        SetupWork.completed_at.is_(None), SetupWork.blocked_at.is_(None),
    ).values(blocked_at=now, last_error=reason),
        execution_options={"synchronize_session": False})
    await session.commit()


async def execute_setup_work(sessions, envelope, authorize, *, now=None, clock=None, knowledge_execute=None):
    now = now or datetime.now(UTC)
    clock = clock or (lambda: now)
    if (not isinstance(envelope, dict) or set(envelope) != {"schema_version", "work_id", "generation"}
            or envelope["schema_version"] != "1.0" or not isinstance(envelope["work_id"], str)
            or not isinstance(envelope["generation"], int)):
        return False
    async with sessions() as session:
        candidates = (await session.scalars(select(SetupWork).where(
            SetupWork.work_id == envelope["work_id"]).limit(2))).all()
        if len(candidates) != 1:
            return False
        work = candidates[0]
        if (work.generation != envelope["generation"]
                or work.due_at.replace(tzinfo=work.due_at.tzinfo or UTC) > now):
            return False
        owner = str(uuid4())
        claimed = await session.execute(update(SetupWork).where(
            SetupWork.tenant_id == work.tenant_id, SetupWork.work_id == work.work_id,
            SetupWork.generation == envelope["generation"],
            SetupWork.completed_at.is_(None), SetupWork.blocked_at.is_(None),
            SetupWork.due_at <= now,
            or_(SetupWork.lease_until.is_(None), SetupWork.lease_until <= now),
        ).values(lease_owner=owner, lease_until=now + timedelta(minutes=5),
                 fence=SetupWork.fence + 1),
            execution_options={"synchronize_session": False})
        await session.commit()
        if claimed.rowcount != 1:
            return False
        claim = SetupClaim(work.tenant_id, work.operation_id, work.work_id, work.generation,
                           owner, work.fence + 1, now + timedelta(minutes=5))
        intent = await session.get(SetupIntent, (work.tenant_id, work.operation_id))
        store = BindingStore(session, intent.tenant_id, intent.product_id, intent.project_id, intent.service_id)
        await store.operation(intent.operation_id)
        if intent.service_id == "devops" and intent.request.get("action") == "sync_knowledge":
            if knowledge_execute is not None:
                # The installed callback owns separate source/native authority;
                # generic setup authorization must never handle this action.
                await session.commit()
                try:
                    await knowledge_execute(intent, claim)
                except Exception as error:
                    await session.rollback()
                    changed = await session.execute(update(SetupWork).where(
                        SetupWork.tenant_id == claim.tenant_id, SetupWork.work_id == claim.work_id,
                        SetupWork.generation == claim.generation, SetupWork.lease_owner == claim.owner,
                        SetupWork.fence == claim.fence, SetupWork.lease_until > clock(),
                        SetupWork.completed_at.is_(None), SetupWork.blocked_at.is_(None),
                    ).values(blocked_at=clock(), lease_owner=None, lease_until=None), execution_options={"synchronize_session": False})
                    if changed.rowcount == 1:
                        operation = await session.get(BindingOperation, (claim.tenant_id, claim.operation_id),
                                                      with_for_update=True, populate_existing=True)
                        from l5_services.devops.knowledge import KnowledgeAccessDenied

                        code = str(error) if isinstance(error, (AuthorityRevoked, KnowledgeAccessDenied)) else "knowledge_publication_failed"
                        if isinstance(error, HTTPException) and isinstance(error.detail, dict):
                            code = error.detail.get("code", code)
                        if operation.state == "outcome_unknown":
                            code = "knowledge_native_outcome_unknown"
                        if not isinstance(code, str) or not code.startswith("knowledge_") or len(code) > 100 or not code.replace("_", "").isalnum():
                            code = "knowledge_publication_failed"
                        operation.state, operation.gaps = "blocked", [code]
                        operation.revision += 1
                        await session.execute(update(SetupWork).where(SetupWork.tenant_id == claim.tenant_id,
                            SetupWork.work_id == claim.work_id).values(last_error=code))
                        await session.commit()
                    else:
                        await session.rollback()
                    return False
                return True
            # An envelope or repair cannot activate an uninstalled publication
            # worker. This guard precedes authorization/provider construction.
            from l5_services.devops.knowledge_admission import WORKER_GAP

            operation = await store.operation(intent.operation_id)
            operation.state = "blocked"
            operation.gaps = [WORKER_GAP]
            await block_claim(session, claim, WORKER_GAP, clock())
            return False
        if intent.service_id == "finops" and intent.request.get("action") != "link":
            operation = await store.operation(intent.operation_id)
            operation.state = "blocked"
            operation.gaps = ["native_unqualified"]
            work.blocked_at = now
            work.last_error = "native_unqualified"
            await session.commit()
            return False
        try:
            verified, provider = await authorize(intent, claim)
        except AuthorityRevoked:
            await block_claim(session, claim, "setup_authority_denied", clock())
            return False
        except HTTPException as error:
            if error.status_code in {401, 403, 404, 409, 410}:
                await block_claim(session, claim, "setup_authority_denied", clock())
                return False
            raise
        await session.refresh(work)
        current = clock()
        if (work.completed_at is not None or work.blocked_at is not None
                or work.lease_owner != claim.owner or work.fence != claim.fence
                or work.lease_until is None
                or work.lease_until.replace(tzinfo=work.lease_until.tzinfo or UTC) <= current):
            return False
        if intent.request["action"] == "create":
            await store.finish_create(intent.operation_id, verified, provider)
        elif intent.request["action"] == "link":
            try:
                result = await store.finish_link(intent.operation_id, verified, provider)
            except HTTPException as error:
                if intent.service_id != "well_architected" or error.status_code != 404:
                    raise
                operation = await store.operation(intent.operation_id)
                if operation.state != "blocked":
                    operation.state = "blocked"
                    operation.gaps = ["native_profile_claim_denied"]
                    operation.revision += 1
                work.blocked_at = now
                work.last_error = operation.gaps[0]
                await session.commit()
                return False
            if intent.service_id == "well_architected" and result.state == "blocked":
                work.blocked_at = now
                work.last_error = result.gaps[0]
                await session.commit()
                return False
        else:
            try:
                await store.finish_control(intent.operation_id, verified, provider)
            except HTTPException as error:
                if error.status_code != 409:
                    raise
                await block_claim(session, claim, "stale_binding_generation", clock())
                return False
        return True
