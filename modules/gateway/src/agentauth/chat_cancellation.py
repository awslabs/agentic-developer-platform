"""Authenticated owner cancellation records intent without claiming sandbox teardown."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_authority import ChatSessionLease, current_chat_member
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatLaunch, ChatLaunchStore, Identifier
from src.agentauth.chat_data_routes import contract_errors, enabled
from src.agentauth.chat_delivery import ChatDelivery, delivery_lookup_key, load_delivery, load_registered_delivery, verify_delivery_session
from src.agentauth.chat_pending_cancellation import pending_cancellation_fence
from src.agentauth.chat_pre_admission_terminal import load_pre_admission_terminal
from src.agentauth.chat_queued_terminal import load_queued_terminal
from src.agentauth.execution import ExecutionStatus
from src.agentauth.external_roots import root_store
from src.agentauth.store import AbortIntentConflictError, AuthorityStoreError
from src.auth.dependencies import get_current_user
from src.orchestration.chat_data_migration import _owns_context_row
from src.orchestration.intake_wiring import _get_context_table, _get_sessions_table
from src.shared.database import get_db
from src.shared.models.organization import User

router = APIRouter()


class CancelTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: Identifier
    task_id: Identifier


@dataclass(frozen=True)
class CancellationTarget:
    delivery: ChatDelivery
    attempt: int
    status: ExecutionStatus
    sandbox_uid: str | None
    launch: ChatLaunch | None
    buffered: bool = False


class ChatCancellation:
    def __init__(self, store, context_table, sessions):
        self.store = store
        self.context_table = context_table
        self.sessions = sessions

    def _admitted(self, body, tenant_id, user_id):
        now = int(time.time())
        row = self.context_table.get_item(Key={"PK": f"session#{body.session_id}", "SK": "header"}, ConsistentRead=True).get("Item", {})
        if not row or not _owns_context_row(row, (tenant_id, row.get("teamId", ""), user_id)):
            raise ChatAuthorizationRefusedError("chat cancellation refused")
        lease = ChatSessionLease.model_validate(row.get("chatLease"))
        launch = ChatLaunchStore(self.store).load(lease.run_id)
        if (
            launch.tenant_id != tenant_id
            or launch.user_id != user_id
            or launch.session_id != body.session_id
            or not _owns_context_row(row, (tenant_id, launch.team_id, user_id))
            or row.get("status") != "active"
            or row.get("ttl", 0) <= now
            or lease.sandbox_uid != launch.sandbox_uid
            or lease.generation != launch.lease_generation
            or lease.expires_at <= now
            or launch.expires_at <= now
        ):
            raise ChatAuthorizationRefusedError("chat cancellation refused")
        delivery = load_delivery(self, launch)
        if delivery.task_id != body.task_id:
            raise ChatAuthorizationRefusedError("chat cancellation refused")
        verify_delivery_session(delivery, self.sessions)
        return launch

    def resolve(self, body, tenant_id, user_id):
        key = delivery_lookup_key(tenant_id, body.session_id, body.task_id)
        pointer = self.store._read(key["pk"]["S"], key["sk"]["S"])
        launch = None
        buffered = False
        if pointer is None:
            launch = self._admitted(body, tenant_id, user_id)
            delivery = load_delivery(self, launch)
        else:
            delivery = load_registered_delivery(self, pointer.get("run_id", {}).get("S", ""), tenant_id)
            if (delivery.user_id, delivery.session_id, delivery.task_id) != (user_id, body.session_id, body.task_id):
                raise ChatAuthorizationRefusedError("chat cancellation refused")
            buffered = pending_cancellation_fence(self, delivery) is not None
            if self.store._read(f"CHAT-LAUNCH#{delivery.run_id}", "LAUNCH") is not None:
                launch = self._admitted(body, tenant_id, user_id)
                if launch.run_id != delivery.run_id:
                    raise ChatAuthorizationRefusedError("chat cancellation changed")
            else:
                row = self.context_table.get_item(Key={"PK": f"session#{body.session_id}", "SK": "header"}, ConsistentRead=True).get("Item")
                if row is not None and (
                    not _owns_context_row(row, (tenant_id, delivery.team_id, user_id))
                    or row.get("status") != "active"
                    or row.get("ttl", 0) <= int(time.time())
                ):
                    raise ChatAuthorizationRefusedError("chat cancellation refused")
        execution = self.store.authority.load_execution(invocation_id=delivery.run_id, tenant_id=tenant_id)
        if (
            execution is None
            or execution.status not in {ExecutionStatus.PENDING, ExecutionStatus.ACTIVE, ExecutionStatus.CANCELLED}
            or execution.repo != f"chat/{delivery.session_id}"
            or (execution.status == ExecutionStatus.PENDING and execution.workload_binding is not None)
            or (execution.status == ExecutionStatus.ACTIVE and not execution.workload_binding)
            or (buffered and (launch is not None or execution.workload_binding is not None or execution.status == ExecutionStatus.ACTIVE))
            or (launch is not None and (execution.current_attempt != launch.attempt or execution.workload_binding != launch.sandbox_uid))
        ):
            raise ChatAuthorizationRefusedError("chat cancellation changed")
        if execution.status == ExecutionStatus.CANCELLED:
            if launch is not None:
                raise ChatAuthorizationRefusedError("chat cancellation changed")
            metadata = self.store._read(f"TENANT#{tenant_id}", f"EXEC#{delivery.run_id}") or {}
            if execution.workload_binding is not None:
                if "chat_pre_admission_terminal" not in metadata:
                    raise ChatAuthorizationRefusedError("chat cancellation changed")
                terminal = load_pre_admission_terminal(metadata, delivery)
                if (
                    terminal["outcome"] != "cancelled"
                    or terminal["attempt"] != execution.current_attempt
                    or terminal["sandbox_uid"] != execution.workload_binding
                ):
                    raise ChatAuthorizationRefusedError("chat cancellation changed")
                return CancellationTarget(delivery, execution.current_attempt, execution.status, execution.workload_binding, None)
            if "chat_queued_terminal" not in metadata:
                raise ChatAuthorizationRefusedError("chat cancellation changed")
            load_queued_terminal(metadata, delivery)
            return CancellationTarget(delivery, execution.current_attempt, execution.status, None, None, buffered)
        grant = self.store.live_grant(invocation_id=delivery.run_id, tenant_id=tenant_id, attempt=execution.current_attempt, now=datetime.now(UTC))
        if grant.authority.kind != "chat_event" or grant.authority.human_id != user_id or execution.repo not in grant.repo_scope:
            raise ChatAuthorizationRefusedError("chat cancellation refused")
        return CancellationTarget(delivery, execution.current_attempt, execution.status, execution.workload_binding, launch, buffered)

    def cancel(self, body, target):
        delivery = target.delivery
        if self.resolve(body, delivery.tenant_id, delivery.user_id) != target:
            raise ChatAuthorizationRefusedError("chat cancellation changed")
        digest = envelope_digest(
            {
                "operation": "chat.cancel",
                "run_id": delivery.run_id,
                "attempt": target.attempt,
                "tenant_id": delivery.tenant_id,
                "user_id": delivery.user_id,
                **body.model_dump(),
            }
        )
        if target.status == ExecutionStatus.CANCELLED and target.sandbox_uid is not None:
            return
        if target.status in {ExecutionStatus.PENDING, ExecutionStatus.CANCELLED}:
            self._cancel_pending(target, digest)
            return
        self.store.authority.record_abort_intent(
            invocation_id=delivery.run_id,
            tenant_id=delivery.tenant_id,
            attempt=target.attempt,
            command_id="chat-cancel-" + digest,
            body_digest=digest,
            now=datetime.now(UTC),
        )

    def _cancel_pending(self, target, digest):
        delivery = target.delivery
        command = "chat-cancel-" + digest
        try:
            update = dict(
                TableName=self.store.table,
                Key=_encoded({"pk": f"TENANT#{delivery.tenant_id}", "sk": f"EXEC#{delivery.run_id}"}),
                UpdateExpression=(
                    "SET abort_command_id = :command, abort_body_digest = :digest, abort_requested_at = :now, abort_requested_attempt = :attempt"
                ),
                ConditionExpression=(
                    "#status = :pending AND current_attempt = :attempt AND attribute_not_exists(workload_binding) "
                    "AND attribute_not_exists(abort_command_id) AND chat_delivery = :delivery"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=_encoded(
                    {
                        ":pending": "pending",
                        ":attempt": target.attempt,
                        ":command": command,
                        ":digest": digest,
                        ":now": datetime.now(UTC).isoformat(),
                        ":delivery": delivery.model_dump_json(),
                    }
                ),
            )
            if target.buffered:
                fence = pending_cancellation_fence(self, delivery)
                if fence is None:
                    raise AbortIntentConflictError(delivery.run_id)
                unchanged = fence["execution"]
                update["ConditionExpression"] += " AND " + unchanged["ConditionExpression"]
                update["ExpressionAttributeNames"].update(unchanged["ExpressionAttributeNames"])
                update["ExpressionAttributeValues"].update(unchanged["ExpressionAttributeValues"])
                self.store.client.transact_write_items(TransactItems=[{"Update": update}, *fence["checks"]])
            else:
                self.store.client.update_item(**update)
            return
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {"ConditionalCheckFailedException", "TransactionCanceledException"}:
                raise AuthorityStoreError("chat cancellation unavailable") from None
        except BotoCoreError:
            raise AuthorityStoreError("chat cancellation unavailable") from None
        execution = self.store.authority.load_execution(invocation_id=delivery.run_id, tenant_id=delivery.tenant_id)
        marker = self.store.authority.abort_intent(invocation_id=delivery.run_id, tenant_id=delivery.tenant_id)
        if (
            execution is None
            or execution.status not in {ExecutionStatus.PENDING, ExecutionStatus.CANCELLED}
            or execution.current_attempt != target.attempt
            or execution.workload_binding is not None
            or marker is None
            or marker["command_id"] != command
            or marker["body_digest"] != digest
            or marker["attempt"] != str(target.attempt)
        ):
            raise AbortIntentConflictError(delivery.run_id)


def cancellation_service():
    enabled()
    context = _get_context_table()
    sessions = _get_sessions_table()
    if context is None or sessions is None:
        raise HTTPException(503, detail={"error": "chat_cancellation_unavailable"}, headers={"Cache-Control": "no-store"})
    return ChatCancellation(root_store(), context, sessions)


@router.post("/v1/chat/turns/cancel")
@contract_errors
async def cancel_turn(body: CancelTurn, user=Depends(get_current_user), db=Depends(get_db), service=Depends(cancellation_service)):
    if user.account_type != "human" or user.auth_source != "jwt":
        raise ChatAuthorizationRefusedError("chat cancellation refused")
    users = (
        await db.scalars(
            select(User.id).where(
                User.org_id == user.org_id, User.user_kind == "human", or_(User.id == user.user_id, User.cognito_sub == user.user_id)
            )
        )
    ).all()
    if len(users) != 1:
        raise ChatAuthorizationRefusedError("chat cancellation refused")
    target = await run_in_threadpool(service.resolve, body, user.org_id, users[0])
    delivery = target.delivery
    if not await current_chat_member(db, delivery.tenant_id, delivery.user_id, delivery.team_id):
        raise ChatAuthorizationRefusedError("chat cancellation refused")
    try:
        await run_in_threadpool(service.cancel, body, target)
    except AbortIntentConflictError:
        raise HTTPException(409, detail={"error": "chat_cancellation_changed"}, headers={"Cache-Control": "no-store"}) from None
    return JSONResponse({"status": "cancellation_requested", **body.model_dump()}, status_code=202, headers={"Cache-Control": "no-store"})
