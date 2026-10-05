"""Real SQL query/serialization regressions for incomplete node-rate estimates."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.cluster import Cluster
from app.models.node import Node
from app.models.node_pool import NodePool
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.schemas.proxy import BudgetStatusResponse, CostResponse, OrgCostResponse
from app.services import cost_reconciler
from app.services.cost import get_workspace_cost
from app.services.node_cost_estimates import estimate
from app.services.cost_reconciler import (
    get_org_cost_summary,
    get_workspace_budget_status,
)
from tests.conftest import async_session_test

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(hours=2)


@pytest.fixture
async def recorded():
    org, other_org, cluster, pool, workspace = (uuid.uuid4() for _ in range(5))
    async with async_session_test() as db:
        for oid in (org, other_org):
            db.add(Organization(id=oid, name=str(oid)))
            await db.flush()
        db.add(Cluster(id=cluster, org_id=org, name="recorded-cluster"))
        await db.flush()
        db.add(NodePool(id=pool, org_id=org, cluster_id=cluster, name="recorded-pool"))
        await db.flush()
        db.add(
            Workspace(
                id=workspace,
                org_id=org,
                cluster_id=cluster,
                name="recorded-workspace",
                isolation_mode="namespace",
                budget_max_daily_usd=Decimal("0"),
            )
        )
        await db.commit()
    return org, other_org, cluster, pool, workspace


async def add_node(recorded, rate, *, org_id=None, **times):
    org, _, cluster, pool, _ = recorded
    async with async_session_test() as db:
        node = Node(
            id=uuid.uuid4(),
            org_id=org_id or org,
            cluster_id=cluster,
            node_pool_id=pool,
            hourly_cost_usd=rate,
            gpu_type="fixture-gpu",
            cloud="aws",
            gpu_count=1,
            status="Running",
            created_at=times.get("created_at", START - timedelta(days=1)),
            terminated_at=times.get("terminated_at"),
        )
        db.add(node)
        await db.commit()
        return node.id


async def read(recorded):
    async with async_session_test() as db:
        result = await get_workspace_cost(recorded[4], recorded[0], db, START, END)
        return CostResponse(**result).model_dump()


async def test_old_node_overlapping_window_is_counted_and_foreign_org_is_not(recorded):
    owned = await add_node(recorded, Decimal("3.50"))
    await add_node(recorded, Decimal("999"), org_id=recorded[1])
    await add_node(recorded, Decimal("999"), terminated_at=START - timedelta(hours=1))
    await add_node(recorded, Decimal("999"), created_at=END + timedelta(hours=1))
    result = await read(recorded)
    assert result["total_cost_usd"] == "7.00"
    assert [node["node_id"] for node in result["nodes"]] == [str(owned)]
    assert result["estimate_status"] == "available"
    assert result["cost_scope"] == "workspace_cluster"
    assert result["cost_basis"] == "recorded_node_rates"
    assert result["observed_cost_usd"] is None
    assert result["cost_reconciliation"] == "unavailable"


async def test_missing_rate_keeps_known_subtotal_separate_from_unknown_total(recorded):
    await add_node(recorded, Decimal("3.50"))
    unknown = await add_node(recorded, None)
    result = await read(recorded)
    assert result["total_cost_usd"] is None
    assert result["known_subtotal_usd"] == "7.00"
    assert result["estimate_status"] == "partial"
    assert result["unestimated_node_count"] == 1
    assert result["breakdown_by_gpu"] == {"fixture-gpu": None}
    detail = next(node for node in result["nodes"] if node["node_id"] == str(unknown))
    assert detail["hourly_cost_usd"] is None and detail["total_cost_usd"] is None


@pytest.mark.parametrize(
    "rate,status,total",
    [
        (None, "unavailable", None),
        (Decimal("0"), "available", "0.00"),
        (Decimal("-1"), "unavailable", None),
    ],
)
async def test_missing_or_invalid_rate_is_distinct_from_explicit_zero(
    recorded, rate, status, total
):
    await add_node(recorded, rate)
    result = await read(recorded)
    assert result["estimate_status"] == status and result["total_cost_usd"] == total
    assert result["observed_cost_usd"] is None


async def test_no_recorded_nodes_and_no_cluster_do_not_establish_free_usage(recorded):
    assert (await read(recorded))["total_cost_usd"] is None
    async with async_session_test() as db:
        ws = await db.get(Workspace, recorded[4])
        ws.cluster_id = None
        await db.commit()
    result = await read(recorded)
    assert (
        result["estimate_status"] == "unavailable" and result["total_cost_usd"] is None
    )


async def test_org_counts_a_shared_cluster_once_and_preserves_zero_budget(recorded):
    await add_node(recorded, Decimal("3.50"))
    async with async_session_test() as db:
        db.add(
            Workspace(
                id=uuid.uuid4(),
                org_id=recorded[0],
                cluster_id=recorded[2],
                name="shared-neighbor",
                isolation_mode="namespace",
            )
        )
        await db.commit()
        result = OrgCostResponse(
            **await get_org_cost_summary(recorded[0], db, START, END)
        ).model_dump()
    assert result["total_cost_usd"] == "7.00"
    assert result["workspace_count"] == 2
    assert all(
        row["cost_scope"] == "workspace_cluster" and row["total_cost_usd"] == "7.00"
        for row in result["workspaces"]
    )
    original = next(
        row for row in result["workspaces"] if row["workspace_id"] == str(recorded[4])
    )
    assert Decimal(original["budget_max_daily_usd"]) == 0


async def test_org_with_unobserved_workspace_has_no_complete_total(recorded):
    await add_node(recorded, Decimal("3.50"))
    async with async_session_test() as db:
        db.add(
            Workspace(
                id=uuid.uuid4(),
                org_id=recorded[0],
                name="unobserved",
                isolation_mode="namespace",
            )
        )
        await db.commit()
        result = OrgCostResponse(
            **await get_org_cost_summary(recorded[0], db, START, END)
        ).model_dump()
    assert result["total_cost_usd"] is None and result["known_subtotal_usd"] == "7.00"
    assert result["estimate_status"] == "partial"


async def test_budget_unknown_usage_has_no_percentage_and_preserves_zero_cap(recorded):
    await add_node(recorded, None)
    async with async_session_test() as db:
        result = BudgetStatusResponse(
            **await get_workspace_budget_status(recorded[4], recorded[0], db)
        ).model_dump()
    budget = result["budget"]
    assert (
        budget["current_daily_cost_usd"] is None
        and budget["daily_budget_used_pct"] is None
    )
    assert (
        budget["estimate_status"] == "unavailable"
        and Decimal(budget["max_daily_usd"]) == 0
    )


async def test_naive_window_is_utc_and_reversed_window_is_refused(recorded):
    await add_node(recorded, Decimal("2"))
    async with async_session_test() as db:
        result = await get_workspace_cost(
            recorded[4],
            recorded[0],
            db,
            START.replace(tzinfo=None),
            END.replace(tzinfo=None),
        )
        assert result["total_cost_usd"] == "4.00"
        with pytest.raises(HTTPException) as error:
            await get_workspace_cost(recorded[4], recorded[0], db, END, START)
        assert error.value.status_code == 422


async def test_wrong_organization_cannot_read_workspace_estimate(recorded):
    await add_node(recorded, Decimal("2"))
    async with async_session_test() as db:
        result = await get_workspace_cost(recorded[4], recorded[1], db, START, END)
        assert result["status_code"] == 404


@pytest.mark.parametrize("end", [None, datetime(2100, 2, 1, tzinfo=timezone.utc)])
async def test_future_only_cost_window_is_refused(recorded, end):
    async with async_session_test() as db:
        with pytest.raises(HTTPException) as error:
            await get_workspace_cost(
                recorded[4],
                recorded[0],
                db,
                datetime(2100, 1, 1, tzinfo=timezone.utc),
                end,
            )
    assert error.value.status_code == 422


async def test_invalid_recorded_interval_remains_unknown(recorded):
    await add_node(
        recorded,
        Decimal("2"),
        created_at=START - timedelta(hours=1),
        terminated_at=START - timedelta(hours=2),
    )
    result = await read(recorded)
    assert result["node_count"] == 1
    assert result["total_cost_usd"] is None
    assert result["nodes"][0]["hours_running"] is None


@pytest.mark.parametrize("rate", [Decimal("NaN"), Decimal("Infinity")])
def test_nonfinite_recorded_rate_is_unavailable(rate):
    node = Node(
        id=uuid.uuid4(), created_at=START, hourly_cost_usd=rate, status="Running"
    )
    result = estimate([node], START, END).values
    assert result["estimate_status"] == "unavailable"
    assert result["nodes"][0]["hourly_cost_usd"] is None
    assert result["total_cost_usd"] is None


async def test_budget_percentage_uses_unrounded_estimate(recorded, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return END

    monkeypatch.setattr(cost_reconciler, "datetime", FixedDatetime)
    await add_node(recorded, Decimal("0.002"), created_at=START)
    async with async_session_test() as db:
        workspace = await db.get(Workspace, recorded[4])
        workspace.budget_max_daily_usd = Decimal("0.01")
        await db.commit()
        result = BudgetStatusResponse(
            **await get_workspace_budget_status(recorded[4], recorded[0], db)
        ).model_dump()
    assert result["budget"]["current_daily_cost_usd"] == "0.00"
    assert result["budget"]["daily_budget_used_pct"] == "40.0"
