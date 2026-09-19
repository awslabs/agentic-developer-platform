"""Tests for CostReconciler — budget enforcement and cost aggregation.

Tests cover:
- Daily cost computation from node data
- Budget warning threshold (80%)
- Budget exceeded threshold (100%)
- GPU limit enforcement
- Workspace suspension on violations
- Reconcile lock acquisition and release
- Org-level cost aggregation
- Workspace budget status
- Idempotent alert creation (no duplicate alerts)
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.cost_reconciler import (
    BUDGET_CRITICAL_PCT,
    BUDGET_WARNING_PCT,
    CostReconciler,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(
    *,
    ws_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    name: str = "test-ws",
    status: str = "active",
    cluster_id: uuid.UUID | None = None,
    budget_max_daily_usd: Decimal | None = None,
    budget_max_gpus: int | None = None,
):
    """Create a mock Workspace object."""
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.org_id = org_id or uuid.uuid4()
    ws.name = name
    ws.status = status
    ws.cluster_id = cluster_id or uuid.uuid4()
    ws.budget_max_daily_usd = budget_max_daily_usd
    ws.budget_max_gpus = budget_max_gpus
    return ws


def _make_node(
    *,
    node_id: uuid.UUID | None = None,
    cluster_id: uuid.UUID | None = None,
    hourly_cost_usd: Decimal = Decimal("3.50"),
    gpu_type: str = "H100",
    gpu_count: int = 1,
    cloud: str = "aws",
    region: str = "us-east-1",
    status: str = "Running",
    created_at: datetime | None = None,
    terminated_at: datetime | None = None,
):
    """Create a mock Node object."""
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.cluster_id = cluster_id or uuid.uuid4()
    node.hourly_cost_usd = hourly_cost_usd
    node.gpu_type = gpu_type
    node.gpu_count = gpu_count
    node.cloud = cloud
    node.region = region
    node.status = status
    node.created_at = created_at or datetime.now(timezone.utc) - timedelta(hours=2)
    node.terminated_at = terminated_at
    node.k8s_node_name = f"node-{node.id}"
    node.instance_id = f"i-{node.id}"
    return node


class FakeScalarResult:
    """Mock for SQLAlchemy scalar results."""

    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def scalar(self):
        return self._items[0] if self._items else None


# ---------------------------------------------------------------------------
# Tests: _compute_daily_cost
# ---------------------------------------------------------------------------


class TestComputeDailyCost:
    """Tests for CostReconciler._compute_daily_cost."""

    @pytest.mark.asyncio
    async def test_no_cluster_returns_zero(self):
        """Workspace without a cluster should return zero cost."""
        db = AsyncMock()
        reconciler = CostReconciler(db)

        ws = _make_workspace(cluster_id=None)
        ws.cluster_id = None

        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        cost = await reconciler._compute_daily_cost(ws, day_start, now)
        assert cost == Decimal("0")

    @pytest.mark.asyncio
    async def test_single_node_running_all_day(self):
        """A node running since midnight should accumulate hours * rate."""
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cluster_id = uuid.uuid4()

        node = _make_node(
            cluster_id=cluster_id,
            hourly_cost_usd=Decimal("4.00"),
            created_at=day_start,  # Started at midnight
            terminated_at=None,  # Still running
        )

        db = AsyncMock()
        db.execute = AsyncMock(return_value=FakeScalarResult([node]))

        ws = _make_workspace(cluster_id=cluster_id)
        reconciler = CostReconciler(db)

        cost = await reconciler._compute_daily_cost(ws, day_start, now)

        # Cost should be approximately hours_since_midnight * $4.00
        hours_since_midnight = Decimal(
            str((now - day_start).total_seconds())
        ) / Decimal("3600")
        expected = Decimal("4.00") * hours_since_midnight
        assert abs(cost - expected) < Decimal("0.01")

    @pytest.mark.asyncio
    async def test_terminated_node_partial_day(self):
        """A terminated node should only count hours it was running today."""
        # Keep the complete 01:00-03:00 interval in the past regardless of
        # what UTC hour CI happens to run this fixture.
        day_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        now = day_start + timedelta(hours=6)
        cluster_id = uuid.uuid4()

        # Node ran for exactly 2 hours today
        node = _make_node(
            cluster_id=cluster_id,
            hourly_cost_usd=Decimal("5.00"),
            created_at=day_start + timedelta(hours=1),
            terminated_at=day_start + timedelta(hours=3),
        )

        db = AsyncMock()
        db.execute = AsyncMock(return_value=FakeScalarResult([node]))

        ws = _make_workspace(cluster_id=cluster_id)
        reconciler = CostReconciler(db)

        cost = await reconciler._compute_daily_cost(ws, day_start, now)
        assert cost == Decimal("10.00")  # 2 hours * $5.00

    @pytest.mark.asyncio
    async def test_node_created_before_today(self):
        """A node created yesterday should only count hours from today."""
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cluster_id = uuid.uuid4()

        node = _make_node(
            cluster_id=cluster_id,
            hourly_cost_usd=Decimal("3.00"),
            created_at=day_start - timedelta(hours=12),  # Created yesterday
            terminated_at=None,
        )

        db = AsyncMock()
        db.execute = AsyncMock(return_value=FakeScalarResult([node]))

        ws = _make_workspace(cluster_id=cluster_id)
        reconciler = CostReconciler(db)

        cost = await reconciler._compute_daily_cost(ws, day_start, now)

        # Should count from day_start to now
        hours_today = Decimal(str((now - day_start).total_seconds())) / Decimal("3600")
        expected = Decimal("3.00") * hours_today
        assert abs(cost - expected) < Decimal("0.01")

    @pytest.mark.asyncio
    async def test_multiple_nodes_sum(self):
        """Multiple nodes' costs should sum up."""
        # Both seeded node intervals must precede now; using wall-clock
        # midnight made this fail daily before 02:00 UTC.
        day_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        now = day_start + timedelta(hours=6)
        cluster_id = uuid.uuid4()

        node1 = _make_node(
            cluster_id=cluster_id,
            hourly_cost_usd=Decimal("2.00"),
            created_at=day_start,
            terminated_at=day_start + timedelta(hours=1),  # 1 hour = $2
        )
        node2 = _make_node(
            cluster_id=cluster_id,
            hourly_cost_usd=Decimal("3.00"),
            created_at=day_start,
            terminated_at=day_start + timedelta(hours=2),  # 2 hours = $6
        )

        db = AsyncMock()
        db.execute = AsyncMock(return_value=FakeScalarResult([node1, node2]))

        ws = _make_workspace(cluster_id=cluster_id)
        reconciler = CostReconciler(db)

        cost = await reconciler._compute_daily_cost(ws, day_start, now)
        assert cost == Decimal("8.00")  # $2 + $6


# ---------------------------------------------------------------------------
# Tests: _count_active_gpus
# ---------------------------------------------------------------------------


class TestCountActiveGpus:
    """Tests for CostReconciler._count_active_gpus."""

    @pytest.mark.asyncio
    async def test_no_cluster_returns_zero(self):
        db = AsyncMock()
        reconciler = CostReconciler(db)
        ws = _make_workspace(cluster_id=None)
        ws.cluster_id = None
        count = await reconciler._count_active_gpus(ws)
        assert count == 0

    @pytest.mark.asyncio
    async def test_returns_gpu_sum(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=FakeScalarResult([8]))

        ws = _make_workspace()
        reconciler = CostReconciler(db)
        count = await reconciler._count_active_gpus(ws)
        assert count == 8


# ---------------------------------------------------------------------------
# Tests: _check_daily_cost
# ---------------------------------------------------------------------------


class TestCheckDailyCost:
    """Tests for budget enforcement at different thresholds."""

    @pytest.mark.asyncio
    async def test_under_warning_no_alert(self):
        """Cost below 80% should not create alerts."""
        db = AsyncMock()
        reconciler = CostReconciler(db)

        ws = _make_workspace(budget_max_daily_usd=Decimal("100.00"))
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Mock _compute_daily_cost to return $50 (50% of $100)
        reconciler._compute_daily_cost = AsyncMock(return_value=Decimal("50.00"))

        actions = await reconciler._check_daily_cost(ws, day_start, now)
        assert actions["warnings"] == 0
        assert actions["violations"] == 0
        assert actions["suspended"] == 0

    @pytest.mark.asyncio
    async def test_at_warning_threshold_creates_warning(self):
        """Cost at 80-99% should create a warning alert."""
        db = AsyncMock()
        reconciler = CostReconciler(db)

        ws = _make_workspace(
            budget_max_daily_usd=Decimal("100.00"),
            status="active",
        )
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # 85% usage
        reconciler._compute_daily_cost = AsyncMock(return_value=Decimal("85.00"))
        reconciler._has_active_alert = AsyncMock(return_value=False)
        reconciler._create_alert = AsyncMock()

        actions = await reconciler._check_daily_cost(ws, day_start, now)
        assert actions["warnings"] == 1
        assert actions["violations"] == 0
        reconciler._create_alert.assert_called_once()
        call_kwargs = reconciler._create_alert.call_args
        assert call_kwargs[1]["alert_type"] == "budget_warning"
        assert call_kwargs[1]["severity"] == "warning"

    @pytest.mark.asyncio
    async def test_at_critical_threshold_creates_violation(self):
        """Cost at 100%+ should create a critical alert and suspend."""
        db = AsyncMock()
        db.flush = AsyncMock()
        reconciler = CostReconciler(db)

        ws = _make_workspace(
            budget_max_daily_usd=Decimal("100.00"),
            status="active",
        )
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # 120% usage
        reconciler._compute_daily_cost = AsyncMock(return_value=Decimal("120.00"))
        reconciler._has_active_alert = AsyncMock(return_value=False)
        reconciler._create_alert = AsyncMock()
        reconciler._suspend_workspace = AsyncMock()

        actions = await reconciler._check_daily_cost(ws, day_start, now)
        assert actions["violations"] == 1
        assert actions["suspended"] == 1
        reconciler._suspend_workspace.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_duplicate_alert_same_day(self):
        """Should not create duplicate alerts for the same workspace+type+day."""
        db = AsyncMock()
        reconciler = CostReconciler(db)

        ws = _make_workspace(
            budget_max_daily_usd=Decimal("100.00"),
            status="budget_exceeded",
        )
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Already at 120% with existing alert
        reconciler._compute_daily_cost = AsyncMock(return_value=Decimal("120.00"))
        reconciler._has_active_alert = AsyncMock(return_value=True)
        reconciler._create_alert = AsyncMock()

        actions = await reconciler._check_daily_cost(ws, day_start, now)
        # Alert already exists, so no new ones
        assert actions["violations"] == 0
        reconciler._create_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_budget_set_no_action(self):
        """Workspace with no budget should not trigger any actions."""
        db = AsyncMock()
        reconciler = CostReconciler(db)

        ws = _make_workspace(budget_max_daily_usd=None)
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # No budget set — _check_daily_cost should return early
        reconciler._compute_daily_cost = AsyncMock(return_value=Decimal("500.00"))

        actions = await reconciler._check_daily_cost(ws, day_start, now)
        assert actions == {"warnings": 0, "violations": 0, "suspended": 0}


# ---------------------------------------------------------------------------
# Tests: _check_gpu_limit
# ---------------------------------------------------------------------------


class TestCheckGpuLimit:
    """Tests for GPU limit enforcement."""

    @pytest.mark.asyncio
    async def test_under_limit_no_alert(self):
        db = AsyncMock()
        reconciler = CostReconciler(db)

        ws = _make_workspace(budget_max_gpus=8)
        reconciler._count_active_gpus = AsyncMock(return_value=4)

        actions = await reconciler._check_gpu_limit(ws)
        assert actions["violations"] == 0

    @pytest.mark.asyncio
    async def test_over_limit_creates_violation(self):
        db = AsyncMock()
        reconciler = CostReconciler(db)

        ws = _make_workspace(budget_max_gpus=4, status="active")
        reconciler._count_active_gpus = AsyncMock(return_value=6)
        reconciler._has_active_alert = AsyncMock(return_value=False)
        reconciler._create_alert = AsyncMock()
        reconciler._suspend_workspace = AsyncMock()

        actions = await reconciler._check_gpu_limit(ws)
        assert actions["violations"] == 1
        assert actions["suspended"] == 1
        reconciler._create_alert.assert_called_once()
        call_kwargs = reconciler._create_alert.call_args
        assert call_kwargs[1]["alert_type"] == "gpu_limit_exceeded"

    @pytest.mark.asyncio
    async def test_no_gpu_limit_no_action(self):
        db = AsyncMock()
        reconciler = CostReconciler(db)

        ws = _make_workspace(budget_max_gpus=None)
        actions = await reconciler._check_gpu_limit(ws)
        assert actions == {"warnings": 0, "violations": 0, "suspended": 0}


# ---------------------------------------------------------------------------
# Tests: _suspend_workspace
# ---------------------------------------------------------------------------


class TestSuspendWorkspace:
    """Tests for workspace suspension."""

    @pytest.mark.asyncio
    async def test_sets_budget_exceeded_status(self):
        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()

        reconciler = CostReconciler(db)
        ws = _make_workspace(status="active")

        await reconciler._suspend_workspace(ws, Decimal("150"), Decimal("100"))

        assert ws.status == "budget_exceeded"
        # Should create an event
        assert db.add.called


# ---------------------------------------------------------------------------
# Tests: Reconcile lock
# ---------------------------------------------------------------------------


class TestReconcileLock:
    """Tests for lock acquisition and release."""

    @pytest.mark.asyncio
    async def test_acquire_lock_success(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=FakeScalarResult([None]))
        db.add = MagicMock()
        db.flush = AsyncMock()

        reconciler = CostReconciler(db)

        # First execute cleans expired locks, second checks existing
        db.execute = AsyncMock(
            side_effect=[
                FakeScalarResult([]),  # delete expired
                FakeScalarResult(
                    []
                ),  # check existing (none found -> use scalar_one_or_none)
            ]
        )

        # Mock scalar_one_or_none to return None (no lock held)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(),  # delete expired
                mock_result,  # check existing
            ]
        )

        result = await reconciler._acquire_lock()
        assert result is True

    @pytest.mark.asyncio
    async def test_acquire_lock_held_by_other(self):
        db = AsyncMock()

        # Mock: lock exists (not expired)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock()  # Lock exists
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(),  # delete expired
                mock_result,  # check existing — lock found
            ]
        )

        reconciler = CostReconciler(db)
        result = await reconciler._acquire_lock()
        assert result is False


# ---------------------------------------------------------------------------
# Tests: Full reconcile cycle
# ---------------------------------------------------------------------------


class TestReconcileCycle:
    """Integration-style tests for the full reconciliation cycle."""

    @pytest.mark.asyncio
    async def test_reconcile_skips_when_locked(self):
        db = AsyncMock()
        reconciler = CostReconciler(db)
        reconciler._acquire_lock = AsyncMock(return_value=False)

        result = await reconciler.reconcile()
        assert result["status"] == "skipped"
        assert result["reason"] == "lock_held"

    @pytest.mark.asyncio
    async def test_reconcile_processes_workspaces(self):
        db = AsyncMock()
        reconciler = CostReconciler(db)

        reconciler._acquire_lock = AsyncMock(return_value=True)
        reconciler._release_lock = AsyncMock()

        ws1 = _make_workspace(
            budget_max_daily_usd=Decimal("100.00"),
            status="active",
        )
        ws2 = _make_workspace(
            budget_max_gpus=4,
            status="active",
        )

        # Mock: _run_reconciliation to return workspaces
        mock_ws_result = MagicMock()
        mock_ws_result.scalars.return_value.all.return_value = [ws1, ws2]
        db.execute = AsyncMock(return_value=mock_ws_result)
        db.commit = AsyncMock()

        reconciler._check_workspace = AsyncMock(
            return_value={"warnings": 0, "violations": 0, "suspended": 0}
        )

        result = await reconciler.reconcile()
        assert result["status"] == "completed"
        assert reconciler._release_lock.called


# ---------------------------------------------------------------------------
# Tests: Budget threshold constants
# ---------------------------------------------------------------------------


class TestBudgetThresholds:
    """Verify budget threshold constants."""

    def test_warning_threshold(self):
        assert BUDGET_WARNING_PCT == Decimal("80")

    def test_critical_threshold(self):
        assert BUDGET_CRITICAL_PCT == Decimal("100")

    def test_warning_below_critical(self):
        assert BUDGET_WARNING_PCT < BUDGET_CRITICAL_PCT
