"""Financial approvals serialize against other approvals and graph changes."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.models import OrchestrationDecision, OrchestrationFlow
from src.orchestration.shared_budget import BudgetIncreaseError, BudgetIncreaseRequest, accept_budget_increase, preview_budget_increase
from tests.orchestration.test_shared_amendment_postgres import (  # noqa: F401
    appendable,
    approval,
    pg_server,
    pg_url,
    session,
)


@pytest.mark.parametrize("competing", ["approval", "graph"])
async def test_accept_rechecks_after_waiting_for_current_flow_lock(session, appendable, monkeypatch, competing):  # noqa: F811
    ctx = appendable
    actor = replace(ctx.actor, actor_role="platform_admin")
    monkeypatch.setattr("src.orchestration.shared_policy.read_flow_meter", AsyncMock(return_value=SimpleNamespace(total_usd=Decimal("12.34"))))
    request = BudgetIncreaseRequest(
        expected_plan_version=ctx.plan.version,
        expected_plan_hash=ctx.plan.plan_hash,
        limits={"max_spend_usd": "1000", "max_run_spend_usd": "100", "max_chain_spend_usd": "1000"},
        reason="Platform owner approves this flow's financial increase.",
    )
    preview = await preview_budget_increase(session, flow_id=ctx.flow.id, actor=actor, request=request)
    request = request.model_copy(update={"expected_snapshot": preview["snapshot"]})
    await session.commit()
    async with session.info["factory"]() as writer:
        await writer.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).with_for_update())
        if competing == "approval":
            smaller = request.model_copy(update={"limits": request.limits.model_copy(update={"max_spend_usd": Decimal(500)})})
            # Keep run <= chain <= flow in the separate valid approval.
            smaller.limits.max_chain_spend_usd = Decimal(500)
            preview = await preview_budget_increase(writer, flow_id=ctx.flow.id, actor=actor, request=smaller)
            smaller = smaller.model_copy(update={"expected_snapshot": preview["snapshot"]})
            await accept_budget_increase(writer, flow_id=ctx.flow.id, actor=actor, request=smaller)
        else:
            plan = await writer.get(type(ctx.plan), ctx.plan.id)
            plan.version += 1
            await writer.flush()
        task = asyncio.create_task(accept_budget_increase(session, flow_id=ctx.flow.id, actor=actor, request=request))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            await writer.commit()
            with pytest.raises(BudgetIncreaseError, match="budget_preview_changed" if competing == "approval" else "accepted_plan_changed"):
                await asyncio.wait_for(task, timeout=3)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    receipts = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "budget_increased")))
    assert len(receipts) == (1 if competing == "approval" else 0)
