"""A Project's shared execution budget and the Product's cost rollup."""

import base64
import time
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.budget.enforcement_service import budget_enforcement_service
from src.budget.project_budget import prepare_project_budget, project_target, reconcile_project_budget
from src.crew.host_routes import host_run_route
from src.crew.host_tools import current_host_target
from src.orchestration.models import OrchestrationFlow
from src.products.models import Product, ProductProjectBinding, Project
from src.products.routes import current_owner
from src.products.work_scope import resolve_project
from src.proxy.routes import set_agent_run_id_from_header
from src.shared.database import get_db
from src.shared.identity.product_scope import product_visibility_scope
from src.shared.models.audit import AuditLog
from src.shared.models.budget import BudgetConfig
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/v1", tags=["products"])


async def accessible_project(db, actor, project_id, *, write=False):
    member = await current_owner(db, actor)
    row = (
        await db.execute(
            select(Product, Project, ProductProjectBinding.is_base)
            .join(
                ProductProjectBinding,
                (ProductProjectBinding.tenant_id == Product.tenant_id)
                & (ProductProjectBinding.product_id == Product.id)
                & ProductProjectBinding.valid_to.is_(None),
            )
            .join(Project, (Project.tenant_id == ProductProjectBinding.tenant_id) & (Project.id == ProductProjectBinding.project_id))
            .where(
                Project.id == project_id,
                Project.archived_at.is_(None),
                Product.archived_at.is_(None),
                product_visibility_scope(member, actor.org_id, is_platform_admin=actor.is_admin),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(404, detail={"code": "not_found"})
    product, project, is_base = row
    can_manage = actor.is_admin or product.owner_user_id == member.id
    if write and not can_manage:
        raise HTTPException(
            403, detail={"code": "project_budget_forbidden", "message": "The Product owner or a platform administrator can manage this budget."}
        )
    return product, project, is_base, can_manage


async def budget_view(db, actor, project_id):
    product, project, is_base, can_manage = await accessible_project(db, actor, project_id)
    config = await db.scalar(
        select(BudgetConfig).where(
            BudgetConfig.org_id == actor.org_id,
            BudgetConfig.entity_type == "project",
            BudgetConfig.entity_id == project_id,
            BudgetConfig.period_type == "run",
        )
    )
    amount = config.budget_amount_usd if config else None
    status, spent, reserved, available = "current", Decimal(0), Decimal(0), amount
    if project.budget_meter_initialized:
        store = budget_enforcement_service._get_reservations()
        target = project_target(actor.org_id, project_id)
        try:
            await reconcile_project_budget(db, store, target)
            snapshot = await store.snapshot(target, include_requests=True)
            if snapshot is None:
                raise ValueError("Project accounting unavailable")
            spent = (snapshot.request_amounts or {}).get("__settled__", Decimal(0))
            reserved = snapshot.total_usd - spent
            available = max(Decimal(0), amount - snapshot.total_usd) if amount is not None else None
            status = "pending" if snapshot.has_pending or reserved > 0 else "current"
        except Exception:
            status, spent, reserved, available = "unavailable", None, None, None
    rows = (
        await db.execute(
            select(
                UsageLog.flow_id,
                func.max(OrchestrationFlow.title),
                func.sum(UsageLog.cost_usd),
                func.count(func.distinct(UsageLog.agent_run_id)),
            )
            .outerjoin(OrchestrationFlow, (OrchestrationFlow.org_id == UsageLog.org_id) & (OrchestrationFlow.id == UsageLog.flow_id))
            .where(UsageLog.org_id == actor.org_id, UsageLog.project_id == project_id)
            .group_by(UsageLog.flow_id)
        )
    ).all()
    return {
        "product_id": product.id,
        "project_id": project_id,
        "project_name": project.name,
        "is_base": is_base,
        "period": "total",
        "currency": "USD",
        "budget_usd": amount,
        "spent_usd": spent,
        "reserved_usd": reserved,
        "available_usd": available,
        "accounting_status": status,
        "can_manage": can_manage,
        "flows": [{"flow_id": flow, "flow_name": name, "spent_usd": cost, "run_count": count} for flow, name, cost, count in rows],
        "cost_scope": "metered_ai_usage",
        "historical_attribution": "captured_at_execution",
    }


class ProjectBudgetWrite(BaseModel):
    budget_usd: Decimal = Field(gt=0, le=Decimal("99999999.99"), decimal_places=2)


host_run_route("/v1/projects/{project_id}/budget")


async def budget_read_context(request: Request, actor: TokenContext = Depends(get_current_user)):
    request.state.token_context = actor
    return actor


@router.get("/projects/{project_id}/budget")
async def get_project_budget(
    project_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(budget_read_context),
    run_id: str | None = Depends(set_agent_run_id_from_header),
):
    if actor.account_type != "human":
        owner, _ = await current_host_target(actor, run_id, now=int(time.time()))
        actor = TokenContext(
            user_id=owner.user_id,
            org_id=owner.tenant_id,
            team_id="",
            department_id="",
            account_type="human",
            expires_at=actor.expires_at,
        )
    response.headers["Cache-Control"] = "no-store"
    return await budget_view(db, actor, project_id)


@router.put("/projects/{project_id}/budget")
async def set_project_budget(
    project_id: str,
    body: ProjectBudgetWrite,
    response: Response,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
    reason: str | None = Header(default=None, alias="X-ADP-Budget-Reason", max_length=500),
    encoded_reason: str | None = Header(default=None, alias="X-ADP-Budget-Reason-Base64", max_length=2668),
):
    if encoded_reason is not None:
        if reason is not None:
            raise HTTPException(422, detail={"code": "validation_error", "message": "Supply only one budget change reason header."})
        try:
            reason = base64.b64decode(encoded_reason, validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            raise HTTPException(422, detail={"code": "validation_error", "message": "Budget change reason must be base64-encoded UTF-8."}) from None
    if reason is not None and len(reason) > 500:
        raise HTTPException(422, detail={"code": "validation_error", "message": "Budget change reason must be at most 500 characters."})
    if reason is not None and not reason.strip():
        raise HTTPException(422, detail={"code": "validation_error", "message": "Budget change reason must not be empty."})
    product, _, _, _ = await accessible_project(db, actor, project_id, write=True)
    # Same Project lock as admission, so two writes cannot create two budgets.
    await db.scalar(select(Project).where(Project.tenant_id == actor.org_id, Project.id == project_id).with_for_update())
    config = await db.scalar(
        select(BudgetConfig).where(
            BudgetConfig.org_id == actor.org_id,
            BudgetConfig.entity_type == "project",
            BudgetConfig.entity_id == project_id,
            BudgetConfig.period_type == "run",
        )
    )
    previous = str(config.budget_amount_usd) if config is not None else None
    if config is None:
        config = BudgetConfig(
            org_id=actor.org_id,
            entity_type="project",
            entity_id=project_id,
            period_type="run",
            enforcement_mode="hard",
            budget_amount_usd=body.budget_usd,
        )
        db.add(config)
    else:
        config.budget_amount_usd = body.budget_usd
        config.enforcement_mode = "hard"
    await db.flush()
    scope = await resolve_project(db, tenant_id=actor.org_id, product_id=product.id, project_id=project_id)
    try:
        await prepare_project_budget(db, budget_enforcement_service._get_reservations(), scope)
    except Exception:
        await db.rollback()
        raise HTTPException(
            503, detail={"code": "project_budget_unavailable", "message": "The Project budget could not be saved. Try again."}
        ) from None
    db.add(
        AuditLog(
            org_id=actor.org_id,
            actor_id=actor.user_id,
            event_type="project_budget_changed",
            details={
                "product_id": product.id,
                "project_id": project_id,
                "previous_budget_usd": previous,
                "budget_usd": str(body.budget_usd),
                **({"reason": reason.strip()} if reason is not None else {}),
            },
        )
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return await budget_view(db, actor, project_id)


@router.get("/products/{product_id}/spend")
async def get_product_spend(product_id: str, response: Response, db: AsyncSession = Depends(get_db), actor: TokenContext = Depends(get_current_user)):
    member = await current_owner(db, actor)
    if not await db.scalar(
        select(Product.id).where(Product.id == product_id, product_visibility_scope(member, actor.org_id, is_platform_admin=actor.is_admin))
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    rows = (
        await db.execute(
            select(UsageLog.project_id, func.sum(UsageLog.cost_usd))
            .where(
                UsageLog.org_id == actor.org_id,
                UsageLog.product_id == product_id,
            )
            .group_by(UsageLog.project_id)
        )
    ).all()
    response.headers["Cache-Control"] = "no-store"
    return {
        "product_id": product_id,
        "spent_usd": sum((cost for _, cost in rows), Decimal(0)),
        "projects": [{"project_id": project_id, "spent_usd": cost} for project_id, cost in rows],
        "cost_scope": "metered_ai_usage",
    }
