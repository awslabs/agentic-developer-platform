"""Team restrictions never broaden tenant grants or another team's access."""

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.internal.tool_entitlements import effective_entitlement
from src.shared.models.tool_entitlement import ToolEntitlement


@pytest.mark.asyncio
async def test_missing_grants_and_team_overrides_are_isolated():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(ToolEntitlement.__table__.create)
    try:
        async with async_sessionmaker(engine)() as db:

            async def read(tenant="tenant-one", team="team-x", tool="browser.start"):
                return await effective_entitlement(db, tenant_id=tenant, team_id=team, tool_name=tool)

            assert await read() == (False, None)
            db.add_all(
                [
                    ToolEntitlement(org_id="tenant-one", team_id="", tool_name="browser.start", enabled=True, max_cost_usd=Decimal("0.20")),
                    ToolEntitlement(org_id="tenant-one", team_id="team-x", tool_name="browser.start", enabled=False),
                    ToolEntitlement(org_id="tenant-one", team_id="team-y", tool_name="browser.start", enabled=True, max_cost_usd=Decimal("0.10")),
                    ToolEntitlement(org_id="tenant-one", team_id="team-x", tool_name="websearch.search", enabled=True),
                ]
            )
            await db.flush()
            assert await read() == (False, None)
            assert await read(team="team-y") == (True, Decimal("0.10"))
            assert await read(team="team-z") == (True, Decimal("0.20"))
            assert await read(tenant="tenant-two", team="team-y") == (False, None)
            assert await read(tool="websearch.search") == (False, None)
            await db.rollback()
    finally:
        await engine.dispose()
