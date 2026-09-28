"""Tests for Alembic migration 030 — budget_usage.total_cost_usd precision.

Issue #4287 (Wave 3 of #4075). The scale is the whole point:

  - ``NUMERIC(14,6)``, not ``(10,2)``. Cost is computed to six decimal places
    (``pricing.calculate_cost``) and ``usage_logs.cost_usd`` already stores six,
    so a two-place accumulator made the budget ledger the only place in the
    pipeline that rounded to cents. For sub-cent models that rounds to nothing:
    a burst of haiku traffic accrued real spend while the denominator the cap is
    enforced against stayed at zero.
  - The integral range must not shrink. ``(10,6)`` would have left four integral
    digits, making any row above $9,999.99 unstorable — an outage on the ledger
    write path for exactly the biggest spenders. ``(14,6)`` keeps eight.
  - The ORM column and the migration must agree. If the model says ``(14,6)``
    and the migration never ran, the write still succeeds against the old
    ``(10,2)`` column and silently truncates — the original bug, with no error.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import Numeric
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.base import Base
from src.shared.models.budget import BudgetUsage

EXPECTED_PRECISION = 14
EXPECTED_SCALE = 6


class TestOrmColumn:
    """The ORM side of the contract, read straight off the mapped column."""

    @pytest.fixture(scope="class")
    def column(self):
        return BudgetUsage.__table__.c.total_cost_usd

    def test_is_numeric_with_six_decimal_places(self, column):
        """Six places, matching pricing.calculate_cost and usage_logs.cost_usd."""
        assert isinstance(column.type, Numeric)
        assert column.type.scale == EXPECTED_SCALE

    def test_integral_range_did_not_shrink(self, column):
        """(14,6) keeps 8 integral digits; (10,6) would have left only 4."""
        assert column.type.precision == EXPECTED_PRECISION
        integral_digits = column.type.precision - column.type.scale
        assert integral_digits >= 8, "narrowing the integral range breaks the ledger write for large accumulated totals"

    def test_is_not_nullable(self, column):
        """A NULL accumulator would make the enforced denominator undefined."""
        assert column.nullable is False


class TestSchema:
    @pytest.fixture
    async def engine(self):
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_column_exists_on_created_schema(self, engine):
        async with engine.connect() as conn:
            columns = await conn.run_sync(lambda c: {col["name"] for col in sa_inspect(c).get_columns("budget_usage")})
        assert "total_cost_usd" in columns


class TestRevisionChain:
    @pytest.fixture(scope="class")
    def module(self):
        path = Path(__file__).parents[2] / "alembic" / "versions" / "030_budget_usage_cost_precision.py"
        spec = importlib.util.spec_from_file_location("m030", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_chains_onto_029(self, module):
        assert module.revision == "030_budget_usage_cost_precision"
        assert module.down_revision == "029_orchestration_graph"

    def test_revision_ids_fit_alembic_version_column(self, module):
        """#4123: an id over 32 chars runs upgrade() then rolls back on Postgres.

        SQLite does not enforce VARCHAR length, so CI cannot catch this at
        runtime — only a static check can.
        """
        assert len(module.revision) <= 32
        assert len(module.down_revision) <= 32

    def test_downgrade_exists_and_is_documented_as_lossy(self, module):
        """Parity requires a downgrade; honesty requires it be labelled.

        Narrowing back to (10,2) rounds every accumulated total to cents, so
        reverting the PR restores the code but NOT the data. An operator reading
        only the function signature would assume otherwise.
        """
        assert callable(module.downgrade)
        assert "LOSSY" in (module.downgrade.__doc__ or "") + (module.__doc__ or "")
