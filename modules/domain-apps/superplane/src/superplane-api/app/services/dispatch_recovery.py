"""Human review, decision and resume for the original undelivered initial phase."""

import uuid
import json
from datetime import UTC, datetime, timedelta

from harness_jobs.identity import OperationRefused, decode_payload
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from workspace_provisioning.artifacts import native_lifecycle_phase

from app.models.operation_approval import OperationApproval
from app.models.operation_reauthorization import OperationReauthorization
from app.services.operation_approvals import (
    ApprovalDenied,
    ApprovalService,
    public_ticket,
)


class DispatchRecoveryService(ApprovalService):
    lifecycle_phase = "prepare-infrastructure"
    recovery_scope = "undispatched"
    recoverable_state = "unknown"
    recovery_endpoint = "/recover-undispatched"
    recovery_description = (
        "One retry of the same initial plan, only if no dispatch or execution occurred"
    )

    def __init__(self, sessions, composition):
        super().__init__(sessions)
        self.composition = composition

    async def original(self, workspace_id):
        principal = await self._principal(workspace_id)
        async with self.composition.domain_connect() as db:
            operation_id = await db.fetchval(
                "SELECT provisioning_operation_id FROM workspaces WHERE id::text=$1 AND org_id::text=$2 "
                "AND status='Provisioning' AND cluster_id IS NULL",
                workspace_id,
                principal.org_id,
            )
        async with self.composition.operation_connect() as db:
            row = await db.fetchrow(
                "SELECT o.*,c.approval_id,c.requester,c.approved_by,c.reservation_state "
                "FROM harness_operations o JOIN harness_approval_consumption c USING(operation_id) "
                "WHERE o.operation_id=$1 AND o.org_id=$2 AND o.workspace_id=$3",
                operation_id,
                principal.org_id,
                workspace_id,
            )
        if row is None or row["requester"] != principal.subject:
            raise ApprovalDenied(
                "only the original requester can recover this admission"
            )
        request = decode_payload(row["request_payload"])
        if (
            request.action != "provision"
            or native_lifecycle_phase(request.parameters) != self.lifecycle_phase
        ):
            raise ApprovalDenied(
                "only an original initial phase can use dispatch recovery"
            )
        return principal, dict(row)

    async def request(
        self, workspace_id, *, expected_version, idempotency_key, review_terms=None
    ):
        principal, operation = await self.original(workspace_id)
        amendment_json = (
            json.dumps(review_terms, sort_keys=True, separators=(",", ":"))
            if review_terms is not None
            else None
        )
        if (
            operation["state"] != self.recoverable_state
            or operation["version"] != expected_version
        ):
            raise ApprovalDenied("original operation version is not recoverable")
        async with self.sessions() as session:
            original = await session.get(OperationApproval, operation["approval_id"])
            if (
                original is None
                or original.revoked
                or original.result != "allowed-once"
                or original.decided_by != operation["approved_by"]
                or original.plan_digest != operation["plan_digest"]
            ):
                raise ApprovalDenied("original approval is unavailable or withdrawn")
            statuses = await self.authority._approver_statuses(
                principal,
                organization_scope=original.organization_scope,
            )
            approver = statuses.get(original.decided_by)
            if approver is None or not approver.may_approve:
                raise ApprovalDenied("original approver no longer has authority")
            query = select(OperationReauthorization).where(
                OperationReauthorization.org_id == principal.org_id,
                OperationReauthorization.idempotency_key == idempotency_key,
            )
            row = (await session.execute(query)).scalar_one_or_none()
            if row is None:
                now = datetime.now(UTC)
                row = OperationReauthorization(
                    reauthorization_id=str(uuid.uuid4()),
                    idempotency_key=idempotency_key,
                    original_approval_id=original.approval_id,
                    operation_id=operation["operation_id"],
                    org_id=principal.org_id,
                    workspace_id=workspace_id,
                    requester=principal.subject,
                    approved_by=original.decided_by,
                    plan_digest=original.plan_digest,
                    expected_version=expected_version,
                    scope=self.recovery_scope,
                    amendment_json=amendment_json,
                    created_at=now,
                    expires_at=now
                    + timedelta(
                        seconds=self.authority_lifetime_seconds(original, review_terms)
                    ),
                    revoked=False,
                )
                session.add(row)
                try:
                    await session.commit()
                except IntegrityError as error:
                    await session.rollback()
                    # Only the request-key uniqueness race can be reconciled by
                    # reading a concurrent ticket. A CHECK/FK failure is a store
                    # incompatibility, not a withdrawal of the human's authority.
                    cause = getattr(error.orig, "__cause__", None)
                    if (
                        getattr(error.orig, "sqlstate", None) != "23505"
                        or getattr(cause, "constraint_name", None)
                        != "operation_reauthorizations_org_id_idempotency_key_key"
                    ):
                        raise
                    row = (await session.execute(query)).scalar_one_or_none()
                    if row is None:
                        raise
                    await session.refresh(original)
            if (
                row is None
                or (
                    row.operation_id,
                    row.workspace_id,
                    row.requester,
                    row.expected_version,
                    row.scope,
                )
                != (
                    operation["operation_id"],
                    workspace_id,
                    principal.subject,
                    expected_version,
                    self.recovery_scope,
                )
                or getattr(row, "amendment_json", None) != amendment_json
            ):
                raise ApprovalDenied("recovery request key names a different admission")
            return self.ticket(row, original, principal.subject)

    @classmethod
    def authority_lifetime_seconds(cls, original, review_terms):
        return min(3600, original.max_runtime_seconds)

    @classmethod
    def ticket(cls, row, original, subject):
        result = public_ticket(original)
        result.update(
            reauthorization_id=row.reauthorization_id,
            original_approval_id=original.approval_id,
            operation_id=row.operation_id,
            expected_version=row.expected_version,
            result=row.result or "pending",
            expires_at=row.expires_at,
            decided_at=row.decided_at,
            decided_by=row.approved_by if row.result else None,
            can_decide=subject == row.approved_by
            and row.result is None
            and not row.revoked
            and row.expires_at > datetime.now(UTC),
            reauthorization_scope=row.scope,
            recovery_scope=cls.recovery_description,
        )
        return result

    async def decide(self, workspace_id, reauthorization_id, result):
        principal = await self._principal(workspace_id)
        if result not in {"allowed-once", "rejected"}:
            raise ApprovalDenied("unsupported decision")
        async with self.sessions() as session, session.begin():
            row = (
                await session.execute(
                    select(OperationReauthorization)
                    .where(
                        OperationReauthorization.reauthorization_id
                        == reauthorization_id,
                        OperationReauthorization.org_id == principal.org_id,
                        OperationReauthorization.workspace_id == workspace_id,
                        OperationReauthorization.scope == self.recovery_scope,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or principal.subject != row.approved_by:
                raise ApprovalDenied("original selected human approver required")
            original = await session.get(OperationApproval, row.original_approval_id)
            if (
                original is None
                or original.revoked
                or original.decided_by != row.approved_by
            ):
                raise ApprovalDenied("original authority withdrawn")
            statuses = await self.authority._approver_statuses(
                principal, organization_scope=original.organization_scope
            )
            approver = statuses.get(principal.subject)
            if (
                approver is None
                or not approver.may_approve
                or row.revoked
                or row.expires_at <= datetime.now(UTC)
                or (
                    original.requires_distinct_approver
                    and principal.subject == row.requester
                )
            ):
                raise ApprovalDenied("current approval authority refused")
            if row.result is not None and row.result != result:
                raise ApprovalDenied("reauthorization decision is immutable")
            if row.result is None:
                await self.before_decision(session, row, result)
                row.result, row.decided_at = result, datetime.now(UTC)
            await session.flush()
            return self.ticket(row, original, principal.subject)

    async def before_decision(self, session, row, result):
        """Scoped decisions may atomically update their own domain projection."""

    async def resume(self, workspace_id, reauthorization_id):
        principal, operation = await self.original(workspace_id)
        async with self.sessions() as session:
            row = await session.get(OperationReauthorization, reauthorization_id)
            if row is None or (
                row.org_id,
                row.workspace_id,
                row.requester,
                row.operation_id,
                row.scope,
            ) != (
                principal.org_id,
                workspace_id,
                principal.subject,
                operation["operation_id"],
                self.recovery_scope,
            ):
                raise ApprovalDenied("reauthorization scope differs")
        dispatcher = self.composition.dispatcher
        if (
            dispatcher is None
            or not dispatcher.enabled
            or not await dispatcher._lifecycle_ready(operation)
            or not await dispatcher._domain_registration(operation)
        ):
            raise OperationRefused("installed initial lifecycle is not ready")
        # Protected producer re-reads the decision, current humans, budget and all
        # dispatch evidence under its database locks. This body is not authority.
        return await dispatcher.transport.post(
            self.recovery_endpoint,
            {
                "domain": "superplane",
                "org_id": principal.org_id,
                "workspace_id": workspace_id,
                "operation_id": operation["operation_id"],
                "requester": principal.subject,
                "reauthorization_id": row.reauthorization_id,
                "expected_version": row.expected_version,
            },
            distinguish_denial=True,
        )
