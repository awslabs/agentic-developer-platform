"""A new recovery decision can confirm an unchanged, immutable planning cap."""

import json
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.models.operation_reauthorization import OperationReauthorization
from app.models.workspace import Workspace
from app.services.operation_approvals import ApprovalDenied


async def confirmation_terms(session, binding):
    rows = (
        await session.scalars(
            select(OperationReauthorization).where(
                OperationReauthorization.operation_id == binding["operation_id"],
                OperationReauthorization.scope == "planning-budget",
                OperationReauthorization.result == "allowed-once",
            )
        )
    ).all()
    if not rows:
        return None
    if len(rows) != 1:
        raise ApprovalDenied("ambiguous original planning budget")
    budget = rows[0]
    if (
        budget.revoked
        or budget.decided_at is None
        or any(
            getattr(budget, field) != binding[field]
            for field in (
                "org_id",
                "workspace_id",
                "requester",
                "approved_by",
                "plan_digest",
            )
        )
        or budget.original_approval_id != binding["original_approval_id"]
    ):
        raise ApprovalDenied("original planning budget was withdrawn or changed")
    terms = json.loads(budget.amendment_json)
    workspace = await session.scalar(
        select(Workspace).where(
            Workspace.id == uuid.UUID(binding["workspace_id"]),
            Workspace.org_id == uuid.UUID(binding["org_id"]),
        )
    )
    if (
        workspace is None
        or workspace.status != "Provisioning"
        or workspace.cluster_id is not None
        or workspace.provisioning_operation_id != binding["operation_id"]
        or Decimal(workspace.budget_max_daily_usd or 0) * 1000000
        < terms["daily_micros"]
    ):
        raise ApprovalDenied("original planning budget projection changed")
    return {
        "version": 1,
        "budget_reauthorization_id": budget.reauthorization_id,
        "daily_micros": terms["daily_micros"],
    }


async def confirm_decision(service, session, row, result):
    if row.amendment_json is None or result != "allowed-once":
        return
    principal = await service.authority.resolve(
        org_id=row.org_id,
        workspace_id=row.workspace_id,
        permission="workspace:administer",
    )
    if principal is None or principal.subject != row.approved_by:
        raise ApprovalDenied("current workspace administration authority required")
    binding = {
        field: getattr(row, field)
        for field in (
            "operation_id",
            "org_id",
            "workspace_id",
            "original_approval_id",
            "requester",
            "approved_by",
            "plan_digest",
        )
    }
    expected = await confirmation_terms(session, binding)
    if expected is None or json.loads(row.amendment_json) != expected:
        raise ApprovalDenied("reviewed planning budget confirmation changed")
    async with service.composition.operation_connect() as db:
        operation = await db.fetchrow(
            "SELECT version,state,plan_digest FROM harness_operations "
            "WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            row.operation_id,
            row.org_id,
            row.workspace_id,
        )
    if (
        operation is None
        or operation["version"] != row.expected_version
        or operation["state"] != "running"
        or operation["plan_digest"] != row.plan_digest
    ):
        raise ApprovalDenied("reviewed planning recovery version changed")
