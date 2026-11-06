"""Persist human approval without admitting work or reserving budget."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.adapters.operation_authority_source import (
    PROVISION_PERMISSION,
    GrantBackedAuthority,
    acting_principal,
)
from app.config import settings
from app.models.operation_approval import OperationApproval


class ApprovalDenied(PermissionError):
    pass


def record_from_row(row):
    from harness_jobs.approval import (
        ApprovalBinding,
        ApprovalRecord,
        ApprovalResult,
        SpendEnvelope,
    )
    from harness_jobs.identity import decode_payload, payload_digest

    if payload_digest(decode_payload(row.request_payload)) != row.plan_digest:
        raise ValueError("approval request digest mismatch")

    if row.result is None:
        return None
    return ApprovalRecord(
        approval_id=row.approval_id,
        binding=ApprovalBinding(
            row.org_id, row.workspace_id, row.plan_digest, row.requester
        ),
        envelope=SpendEnvelope(
            row.max_resource_units, row.max_runtime_seconds, row.max_cost_micros
        ),
        result=ApprovalResult(row.result),
        approvers=frozenset(json.loads(row.approvers_json)),
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        expires_at=row.expires_at,
        revoked=row.revoked,
        requires_distinct_approver=row.requires_distinct_approver,
    )


def can_decide_ticket(row, subject, statuses):
    status = statuses.get(subject)
    return bool(
        (not row.requires_distinct_approver or subject != row.requester)
        and subject in json.loads(row.approvers_json)
        and status
        and status.may_approve
        and not row.revoked
        and row.result is None
        and row.expires_at > datetime.now(UTC)
    )


def public_ticket(row, *, can_decide=False):
    return {
        "approval_id": row.approval_id,
        "workspace_id": row.workspace_id,
        "requester": row.requester,
        "request": json.loads(row.request_payload),
        "plan_digest": row.plan_digest,
        "envelope": {
            "max_resource_units": row.max_resource_units,
            "max_runtime_seconds": row.max_runtime_seconds,
            "max_cost_micros": row.max_cost_micros,
        },
        "approvers": json.loads(row.approvers_json),
        "result": row.result or "pending",
        "decided_by": row.decided_by,
        "decided_at": row.decided_at,
        "expires_at": row.expires_at,
        "revoked": row.revoked,
        "can_decide": can_decide,
        "requires_distinct_approver": row.requires_distinct_approver,
    }


class ApprovalService:
    def __init__(self, session_factory):
        self.sessions = session_factory
        self.authority = GrantBackedAuthority(session_factory)

    async def _principal(self, workspace_id, *, organization_scope=False):
        caller = acting_principal()
        if caller is None or caller.account_type != "human":
            raise ApprovalDenied("a verified human principal is required")
        if organization_scope:
            from harness_jobs.identity import ResolvedPrincipal

            from app.models.organization_grant import (
                ORGANIZATION_ADMINISTER,
                OrganizationGrantRecord,
            )

            async with self.sessions() as session:
                grant = (
                    await session.execute(
                        select(OrganizationGrantRecord).where(
                            OrganizationGrantRecord.org_id == uuid.UUID(caller.org_id),
                            OrganizationGrantRecord.principal == caller.subject,
                            OrganizationGrantRecord.principal_type == "human",
                            OrganizationGrantRecord.revoked_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if (
                    grant is None
                    or ORGANIZATION_ADMINISTER not in (grant.permissions or "").split()
                ):
                    raise ApprovalDenied(
                        "current organization approval authority refused"
                    )
            return ResolvedPrincipal(
                caller.org_id,
                workspace_id,
                caller.subject,
                frozenset({PROVISION_PERMISSION}),
            )
        principal = await self.authority.resolve(
            org_id=caller.org_id,
            workspace_id=workspace_id,
            permission=PROVISION_PERMISSION,
        )
        if principal is None:
            raise ApprovalDenied("operation authority refused")
        return principal

    async def issue(self, *, workspace_id, request):
        from harness_jobs.approval import ApprovalBinding
        from harness_jobs.identity import encode_payload

        from app.adapters.operation_authority_source import _requested_envelope

        principal = await self._principal(workspace_id)
        organization_scope = not await self.authority._workspace_exists(workspace_id)
        binding = ApprovalBinding.for_request(principal, request)
        envelope = _requested_envelope(request)
        statuses = await self.authority._approver_statuses(principal)
        require_distinct = settings.superplane_require_distinct_approver
        approvers = sorted(
            subject
            for subject, status in statuses.items()
            if (not require_distinct or subject != principal.subject)
            and status.may_approve
        )
        query = select(OperationApproval).where(
            OperationApproval.org_id == principal.org_id,
            OperationApproval.workspace_id == principal.workspace_id,
            OperationApproval.requester == principal.subject,
            OperationApproval.plan_digest == binding.plan_digest,
        )
        async with self.sessions() as session:
            existing = (await session.execute(query)).scalar_one_or_none()
            if existing is not None:
                return public_ticket(
                    existing,
                    can_decide=can_decide_ticket(existing, principal.subject, statuses),
                )
            if not approvers:
                raise ApprovalDenied(
                    "no distinct current human approver is available"
                    if require_distinct
                    else "no current human administrator is available to approve"
                )
            row = OperationApproval(
                approval_id=str(uuid.uuid4()),
                org_id=principal.org_id,
                workspace_id=principal.workspace_id,
                requester=principal.subject,
                plan_digest=binding.plan_digest,
                request_payload=encode_payload(request),
                approvers_json=json.dumps(approvers),
                max_resource_units=envelope.max_resource_units,
                max_runtime_seconds=envelope.max_runtime_seconds,
                max_cost_micros=envelope.max_cost_micros,
                # STS requires a 900-second session inside the recorded authority.
                # A 15-minute ticket is already too short when execution starts.
                # New native tickets cover their approved runtime plus dispatch
                # headroom; the existing-ticket branch above never renews expiry.
                expires_at=datetime.now(UTC)
                + timedelta(
                    seconds=max(3600, envelope.max_runtime_seconds + 900)
                    if request.parameters.get("lifecycle_request")
                    else 900
                ),
                revoked=False,
                organization_scope=organization_scope,
                requires_distinct_approver=require_distinct,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = (await session.execute(query)).scalar_one_or_none()
                if existing is None:
                    raise
                return public_ticket(
                    existing,
                    can_decide=can_decide_ticket(existing, principal.subject, statuses),
                )
            await session.refresh(row)
            return public_ticket(
                row, can_decide=can_decide_ticket(row, principal.subject, statuses)
            )

    async def read(self, approval_id):
        caller = acting_principal()
        if caller is None or caller.account_type != "human":
            raise ApprovalDenied("a verified human principal is required")
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(OperationApproval).where(
                        OperationApproval.approval_id == approval_id,
                        OperationApproval.org_id == caller.org_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ApprovalDenied("approval unavailable to this principal")
            principal = await self._principal(
                row.workspace_id, organization_scope=row.organization_scope
            )
            statuses = await self.authority._approver_statuses(
                principal, organization_scope=row.organization_scope
            )
            status = statuses.get(caller.subject)
            if caller.subject != row.requester and (
                caller.subject not in json.loads(row.approvers_json)
                or status is None
                or not status.may_approve
            ):
                raise ApprovalDenied("approval unavailable to this principal")
            return public_ticket(
                row,
                can_decide=can_decide_ticket(row, caller.subject, statuses),
            )

    async def decide(self, approval_id, result):
        from harness_jobs.approval import ApprovalResult

        if result not in {
            ApprovalResult.ALLOWED_ONCE.value,
            ApprovalResult.REJECTED.value,
        }:
            raise ApprovalDenied("unsupported approval decision")
        caller = acting_principal()
        if caller is None or caller.account_type != "human":
            raise ApprovalDenied("a verified human principal is required")
        async with self.sessions() as session, session.begin():
            row = (
                await session.execute(
                    select(OperationApproval)
                    .where(
                        OperationApproval.approval_id == approval_id,
                        OperationApproval.org_id == caller.org_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or (
                row.requires_distinct_approver and caller.subject == row.requester
            ):
                raise ApprovalDenied("a distinct selected approver is required")
            principal = await self._principal(
                row.workspace_id, organization_scope=row.organization_scope
            )
            statuses = await self.authority._approver_statuses(
                principal, organization_scope=row.organization_scope
            )
            status = statuses.get(caller.subject)
            if (
                caller.subject not in json.loads(row.approvers_json)
                or status is None
                or not status.may_approve
            ):
                raise ApprovalDenied("current approval authority refused")
            if row.revoked or row.expires_at <= datetime.now(UTC):
                raise ApprovalDenied("approval is expired or revoked")
            if row.result is not None:
                if row.result != result or row.decided_by != caller.subject:
                    raise ApprovalDenied("approval already has an immutable decision")
                return public_ticket(row)
            row.result = result
            row.decided_by = caller.subject
            row.decided_at = datetime.now(UTC)
            await session.flush()
            return public_ticket(row)
