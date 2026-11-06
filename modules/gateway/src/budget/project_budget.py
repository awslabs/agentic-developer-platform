"""Project allowances use the shared ledger, reservation store and receipts."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from src.budget.reservations import ReservationTarget
from src.products.models import Project
from src.shared.models.budget import BudgetConfig, BudgetSettlementReceipt, BudgetUsage

PROJECT_EPOCH = date(1970, 1, 1)


def project_target(tenant_id, project_id, headroom=Decimal("1000000000000")):
    return ReservationTarget(
        org_id=tenant_id,
        entity_type="project",
        entity_id=project_id,
        period_type="run",
        period_start=PROJECT_EPOCH.isoformat(),
        headroom_usd=headroom,
        ttl_seconds=1000000000000,
        require_initialization=True,
        retain_until_settlement=True,
    )


async def prepare_project_budget(db, store, scope, *, enforce=True):
    """Meter every scoped call, including projects with no spending limit.

    A durable initialization flag prevents Redis loss from resetting an allowance.
    The same live accumulator holds actual plus reserved cost, so settlement and
    concurrent admission never race a stale SQL headroom read.
    """
    if not store.enabled:
        raise ValueError("Project budget reservation store is unavailable.")
    project = await db.scalar(
        select(Project)
        .where(
            Project.tenant_id == scope.tenant_id,
            Project.id == scope.project_id,
        )
        .with_for_update()
    )
    if project is None:
        raise ValueError("Project is unavailable.")
    target = project_target(scope.tenant_id, scope.project_id)
    if not project.budget_meter_initialized:
        balance = await db.scalar(
            select(BudgetUsage.total_cost_usd).where(
                BudgetUsage.org_id == scope.tenant_id,
                BudgetUsage.entity_type == "project",
                BudgetUsage.entity_id == scope.project_id,
                BudgetUsage.period_type == "run",
                BudgetUsage.period_start == PROJECT_EPOCH,
            )
        ) or Decimal(0)
        await store.initialize_project_meter(target, balance)
        project.budget_meter_initialized = True
        await db.flush()
    config = await db.scalar(
        select(BudgetConfig).where(
            BudgetConfig.org_id == scope.tenant_id,
            BudgetConfig.entity_type == "project",
            BudgetConfig.entity_id == scope.project_id,
            BudgetConfig.period_type == "run",
        )
    )
    if enforce and config is not None and config.enforcement_mode == "hard":
        target = project_target(scope.tenant_id, scope.project_id, config.budget_amount_usd)
    await reconcile_project_budget(db, store, target)
    if await store.snapshot(target) is None:
        raise ValueError("Project spend cannot be confirmed.")
    return target


async def reconcile_project_budget(db, store, target):
    """Remove a hold only when its committed, scoped settlement receipt exists.

    Retained markers survive deadlines. A finalizer interruption is recoverable
    on the next check; a missing receipt never restores spending headroom.
    """
    pending = await store.live_request_amounts(target) or {}
    ids = [key for key in pending if not key.startswith("__")]
    for offset in range(0, len(ids), 200):
        receipts = await db.scalars(
            select(BudgetSettlementReceipt).where(
                BudgetSettlementReceipt.org_id == target.org_id,
                BudgetSettlementReceipt.request_id.in_(ids[offset : offset + 200]),
            )
        )
        for receipt in receipts:
            if target.key() in (receipt.reservation_scope_keys or []):
                await store.release_project_receipt(receipt.request_id, receipt.cost_usd, target)

    # The durable transport records unsent cancellation before releasing a hold.
    # Expiry or a missing provider response alone is never cancellation proof.
    from src.shared.models.metering import MeteringAttempt

    for offset in range(0, len(ids), 200):
        attempts = await db.scalars(
            select(MeteringAttempt).where(
                MeteringAttempt.tenant_id == target.org_id,
                MeteringAttempt.request_id.in_(ids[offset : offset + 200]),
                MeteringAttempt.state == "cancelled_unsubmitted",
            )
        )
        for attempt in attempts:
            scope = attempt.attribution_snapshot.get("project_attribution") or {}
            if scope.get("tenant_id") == target.org_id and scope.get("project_id") == target.entity_id:
                await store.release_project_receipt(attempt.request_id, Decimal(0), target)
