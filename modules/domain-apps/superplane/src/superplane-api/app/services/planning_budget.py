"""A separate human decision changes a future allocation cap, not original admission."""

import json
import uuid
from decimal import Decimal
from datetime import UTC, datetime
from sqlalchemy import select
from harness_jobs.identity import decode_payload
from app.models.workspace import Workspace
from app.models.operation_reauthorization import OperationReauthorization
from app.services.provider_recovery import ProviderRecoveryService
from app.services.operation_approvals import ApprovalDenied


class PlanningBudgetService(ProviderRecoveryService):
    recovery_scope = "planning-budget"
    recovery_description = (
        "Amend only the daily infrastructure budget for this original initial plan. "
        "Original request, approvals, history, runtime and attempts are retained. "
        "A separate recovery decision and exact saved-plan approval remain required."
    )

    async def request(
        self, workspace_id, *, expected_version, idempotency_key, daily_usd
    ):
        principal, operation = await self.original(workspace_id)
        request = decode_payload(operation["request_payload"])
        previous = int(request.parameters["lifecycle_allocation_max_cost_micros"])
        daily = Decimal(str(daily_usd))
        if (
            not daily.is_finite()
            or daily <= 0
            or daily > Decimal("99999999.99")
            or daily != daily.quantize(Decimal("0.01"))
        ):
            raise ApprovalDenied("an exact positive daily USD limit is required")
        proposed = int(daily * 1000000)
        if proposed <= previous:
            raise ApprovalDenied(
                "planning amendment must raise the original allocation limit"
            )
        async with self.sessions() as session:
            workspace = await session.scalar(
                select(Workspace).where(
                    Workspace.id == uuid.UUID(workspace_id),
                    Workspace.org_id == uuid.UUID(principal.org_id),
                )
            )
            if workspace is None:
                raise ApprovalDenied("original workspace unavailable")
            existing = await session.scalar(
                select(OperationReauthorization).where(
                    OperationReauthorization.org_id == principal.org_id,
                    OperationReauthorization.idempotency_key == idempotency_key,
                )
            )
            prior_budget = int(Decimal(workspace.budget_max_daily_usd or 0) * 1000000)
            approved = await session.scalar(
                select(OperationReauthorization.reauthorization_id).where(
                    OperationReauthorization.operation_id == operation["operation_id"],
                    OperationReauthorization.scope == self.recovery_scope,
                    OperationReauthorization.result == "allowed-once",
                )
            )
            if approved and (
                existing is None or approved != existing.reauthorization_id
            ):
                raise ApprovalDenied(
                    "original plan already has an approved budget amendment"
                )
            if existing is not None:
                if existing.scope != self.recovery_scope:
                    raise ApprovalDenied("budget review key names another scope")
                prior_budget = json.loads(existing.amendment_json)[
                    "previous_workspace_daily_micros"
                ]
        return await super().request(
            workspace_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            review_terms={
                "version": 1,
                "previous_daily_micros": previous,
                "daily_micros": proposed,
                "previous_workspace_daily_micros": prior_budget,
            },
        )

    @classmethod
    def ticket(cls, row, original, subject):
        ticket = super().ticket(row, original, subject)
        terms = json.loads(row.amendment_json)
        ticket["budget_amendment"] = {
            **terms,
            "daily_usd": str(Decimal(terms["daily_micros"]) / 1000000),
            "original_plan_digest": row.plan_digest,
            "operation_id": row.operation_id,
        }
        return ticket

    async def before_decision(self, session, row, result):
        if row.expires_at <= datetime.now(UTC):
            raise ApprovalDenied("budget amendment expired")
        if result != "allowed-once":
            return
        workspace = await session.scalar(
            select(Workspace)
            .where(
                Workspace.id == uuid.UUID(row.workspace_id),
                Workspace.org_id == uuid.UUID(row.org_id),
            )
            .with_for_update()
        )
        terms = json.loads(row.amendment_json)
        async with self.composition.operation_connect() as connection:
            operation = await connection.fetchrow(
                "SELECT * FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
                row.operation_id,
                row.org_id,
                row.workspace_id,
            )
        if (
            workspace is None
            or workspace.status != "Provisioning"
            or workspace.cluster_id is not None
            or workspace.provisioning_operation_id != row.operation_id
            or operation is None
            or operation["state"] != "running"
            or operation["version"] != row.expected_version
            or operation["plan_digest"] != row.plan_digest
            or int(Decimal(workspace.budget_max_daily_usd or 0) * 1000000)
            != terms["previous_workspace_daily_micros"]
        ):
            raise ApprovalDenied("reviewed planning budget scope changed")
        workspace.budget_max_daily_usd = Decimal(terms["daily_micros"]) / 1000000

    async def resume(self, *args, **kwargs):
        raise ApprovalDenied("budget decisions do not recover or dispatch operations")
