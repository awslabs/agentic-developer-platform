"""Verify infrastructure charges against independent resource-hour fixtures."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.base import Base
from src.shared.models.dev_box import DevBox, DevBoxResource, DevBoxTemplate, DevBoxUsage
from src.shared.models.organization import Organization, User

from controller.cost import record_interval


@pytest_asyncio.fixture
async def usage_database():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    tables = [Organization.__table__, User.__table__, DevBoxTemplate.__table__, DevBox.__table__,
              DevBoxResource.__table__, DevBoxUsage.__table__]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(Organization(id="example-org", name="Example Org"))
        db.add(User(id="example-owner", org_id="example-org", team_id="example-team",
                    email="owner@example.test", cognito_sub="example-owner"))
        db.add(DevBoxTemplate(id="template", name="assistant", revision="r1", profile="crew",
                              instance_size="small", disk_gb=40, approved=True))
        await db.flush()
        db.add(DevBox(id="box", user_id="example-owner", org_id="example-org", deployment_id="example-deployment",
                      template_id="template", template_revision="r1", profile="crew", status="running"))
        await db.flush()
        for identifier, kind, size in (("instance", "instance", None), ("disk", "workspace_disk", 40), ("snapshot", "snapshot", 8)):
            db.add(DevBoxResource(
                id=identifier, box_id="box", user_id="example-owner", org_id="example-org",
                deployment_id="example-deployment", machine_generation=1, resource_kind=kind,
                provider_resource_id=f"owned-{identifier}", size_gb=size, deletion_state="present",
            ))
        await db.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_running_cost_stops_at_observed_stop_while_disk_and_snapshot_continue(usage_database):
    observed_start = datetime(2026, 1, 1, tzinfo=UTC)
    async with usage_database() as db:
        assert await record_interval(db, "instance", observed_start, observed_start + timedelta(hours=2), ".12", "provider-r1") == 1
        assert await record_interval(db, "disk", observed_start, observed_start + timedelta(hours=3), ".001", "provider-r1") == 1
        assert await record_interval(db, "snapshot", observed_start, observed_start + timedelta(hours=5), ".0001", "provider-r1") == 1
        await db.commit()
    async with usage_database() as db:
        rows = {row.resource_id: row for row in (await db.scalars(select(DevBoxUsage))).all()}
        assert rows["instance"].cost_usd == Decimal("0.240000")
        assert rows["disk"].cost_usd == Decimal("0.120000")
        assert rows["snapshot"].cost_usd == Decimal("0.004000")
        assert rows["instance"].interval_end == observed_start.replace(tzinfo=None) + timedelta(hours=2)
        assert sum((row.cost_usd for row in rows.values()), Decimal(0)) == Decimal("0.364000")
        assert all(row.reconciled is False for row in rows.values())


@pytest.mark.asyncio
async def test_replayed_and_overlapping_provider_intervals_charge_only_uncovered_time(usage_database):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    async with usage_database() as db:
        assert await record_interval(db, "instance", start, start + timedelta(hours=1), ".10", "revision-one") == 1
        await db.commit()
    async with usage_database() as db:
        assert await record_interval(db, "instance", start, start + timedelta(hours=1), ".10", "revision-two") == 0
        assert await record_interval(db, "instance", start + timedelta(minutes=30), start + timedelta(hours=2), ".10", "revision-two") == 1
        await db.commit()
    async with usage_database() as db:
        rows = list((await db.scalars(select(DevBoxUsage).order_by(DevBoxUsage.interval_start))).all())
        assert len(rows) == 2
        assert rows[0].interval_end == rows[1].interval_start
        assert sum((row.cost_usd for row in rows), Decimal(0)) == Decimal("0.200000")


@pytest.mark.asyncio
async def test_contiguous_observations_extend_one_row_per_hour_instead_of_adding_a_row_each_pass(usage_database):
    """#9548: a 7-second observation cadence must not create a row per pass. Contiguous, same-rate
    intervals extend the latest unreconciled row up to one hour; the charge stays exact."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    async with usage_database() as db:
        cursor = start
        for _ in range(600):  # 70 minutes of 7-second passes
            assert await record_interval(db, "instance", cursor, cursor + timedelta(seconds=7), ".12", "policy:p1:.12") == 1
            cursor += timedelta(seconds=7)
        await db.commit()
    async with usage_database() as db:
        rows = list((await db.scalars(select(DevBoxUsage).where(DevBoxUsage.resource_id == "instance").order_by(DevBoxUsage.interval_start))).all())
        assert len(rows) == 2
        assert rows[0].interval_start == start.replace(tzinfo=None)
        assert rows[0].interval_end == rows[1].interval_start
        assert rows[1].interval_end == (start + timedelta(seconds=4200)).replace(tzinfo=None)
        assert all((row.interval_end - row.interval_start) <= timedelta(hours=1) for row in rows)
        assert sum((row.cost_usd for row in rows), Decimal(0)) == (Decimal("4200") / Decimal("3600") * Decimal(".12")).quantize(Decimal("0.000001"))


@pytest.mark.asyncio
async def test_rate_changes_gaps_and_reconciled_rows_start_a_new_row(usage_database):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    async with usage_database() as db:
        assert await record_interval(db, "disk", start, start + timedelta(minutes=10), ".001", "policy:p1:.001") == 1
        # same rate, contiguous: extended
        assert await record_interval(db, "disk", start + timedelta(minutes=10), start + timedelta(minutes=20), ".001", "policy:p1:.001") == 1
        # rate change: new row
        assert await record_interval(db, "disk", start + timedelta(minutes=20), start + timedelta(minutes=30), ".002", "policy:p1:.002") == 1
        # gap: new row
        assert await record_interval(db, "disk", start + timedelta(minutes=40), start + timedelta(minutes=50), ".002", "policy:p1:.002") == 1
        await db.commit()
    async with usage_database() as db:
        rows = list((await db.scalars(select(DevBoxUsage).where(DevBoxUsage.resource_id == "disk").order_by(DevBoxUsage.interval_start))).all())
        assert [(row.interval_start.minute, row.interval_end.minute) for row in rows] == [(0, 20), (20, 30), (40, 50)]
        rows[-1].reconciled = True
        await db.commit()
    async with usage_database() as db:
        # a reconciled row is never extended
        assert await record_interval(db, "disk", start + timedelta(minutes=50), start + timedelta(minutes=55), ".002", "policy:p1:.002") == 1
        rows = list((await db.scalars(select(DevBoxUsage).where(DevBoxUsage.resource_id == "disk").order_by(DevBoxUsage.interval_start))).all())
        assert len(rows) == 4 and rows[-1].reconciled is False and rows[-1].interval_start.minute == 50


@pytest.mark.asyncio
async def test_unknown_or_deleted_resource_never_produces_estimated_charge(usage_database):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    async with usage_database() as db:
        resource = await db.get(DevBoxResource, "disk")
        resource.provider_resource_id = "intent:unobserved"
        await db.commit()
    async with usage_database() as db:
        with pytest.raises(ValueError, match="Provider ownership"):
            await record_interval(db, "disk", start, start + timedelta(hours=1), ".001", "provider-r1")
        await db.rollback()
    async with usage_database() as db:
        resource = await db.get(DevBoxResource, "disk")
        resource.provider_resource_id = "owned-disk"
        resource.deletion_state = "verified_deleted"
        await db.commit()
    async with usage_database() as db:
        with pytest.raises(ValueError, match="Provider ownership"):
            await record_interval(db, "disk", start, start + timedelta(hours=1), ".001", "provider-r1")
        assert await db.scalar(select(func.count(DevBoxUsage.id))) == 0
