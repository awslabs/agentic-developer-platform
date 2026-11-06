"""The installed repair owner produces bounded UTC ticks and retains replay evidence."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from l5_services.events.cron import matches, next_tick
from l5_services.events.periodic import produce_due_schedules
from l5_services.events.recovery import repair_pending_events
from l5_services.events.schedules import receive_schedule_tick
from l5_services.models import Configuration, ConfigurationVersion, EventReceipt
from l5_services.models.schedule import EventObservation, ScheduleState
from test_wa06_schedule import SCOPE, SOURCE, TICK, TRIGGER
from test_wa06_schedule import review_store as review_store


@pytest.mark.parametrize("expression,tick", [
    ("0 12 * * 4", TICK), ("*/15 10-14 * * 1-5", TICK),
    ("0 12 1 * 4", TICK), ("0 0 * * 0,7", datetime(2026, 11, 1, tzinfo=UTC)),
    ("0 0 29 2 *", datetime(2028, 2, 29, tzinfo=UTC)),
])
def test_cron_matches_numeric_ranges_steps_and_standard_day_alternatives(expression, tick):
    assert matches(expression, tick)
    assert next_tick(expression, tick - timedelta(seconds=1)) == tick
    assert not matches(expression, tick + timedelta(seconds=1))


@pytest.mark.parametrize("expression", ["* * * *", "61 * * * *", "*/0 * * * *", "0 0 31 2 *", "@hourly", "cron(0 1 * * ? *)", "0 0 * * MON", "0 0 * * 8"])
def test_cron_rejects_unsupported_or_unreachable_ticks(expression):
    with pytest.raises(ValueError):
        next_tick(expression, TICK)


async def enabled(sessions):
    async with sessions() as session:
        version = await session.get(ConfigurationVersion, (SCOPE["tenant_id"], "config-example", 1))
        version.settings = {"enabled": True, "triggers": [TRIGGER], "source_refs": [SOURCE]}
        await session.commit()


async def test_disabled_state_has_no_event_and_new_revision_starts_at_current_tick(review_store):
    service = SimpleNamespace(service_id="well_architected", sessions=review_store)
    assert await produce_due_schedules(service, now=TICK) == 0
    async with review_store() as session:
        assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 0
        state = await session.get(ScheduleState, (SCOPE["tenant_id"], "config-example", 1))
        assert state.next_due_at is None and state.triggers[TRIGGER["trigger_id"]]["reason"] == "trigger_disabled"
        configuration = await session.get(Configuration, (SCOPE["tenant_id"], "config-example"))
        configuration.current_revision = 2
        session.add(ConfigurationVersion(tenant_id=SCOPE["tenant_id"], configuration_id="config-example", revision=2,
            settings={"enabled": True, "triggers": [TRIGGER], "source_refs": [SOURCE]}, state="ready", gaps=[], inherited_fields=[], hash="b" * 64))
        await session.commit()
    assert await produce_due_schedules(service, now=TICK) == 1
    assert await produce_due_schedules(service, now=TICK + timedelta(seconds=40)) == 0
    async with review_store() as session:
        receipt = await session.scalar(select(EventReceipt))
        assert receipt.state == "pending" and receipt.configuration_revision == 2
        state = await session.get(ScheduleState, (SCOPE["tenant_id"], "config-example", 2))
        assert state.next_due_at.replace(tzinfo=UTC) == TICK + timedelta(days=7)


async def test_nonminute_initialization_never_creates_a_tick_before_enablement(review_store):
    async with review_store() as session:
        version = await session.get(ConfigurationVersion, (SCOPE["tenant_id"], "config-example", 1))
        version.settings = {"enabled": True, "triggers": [{**TRIGGER, "schedule": "* * * * *"}], "source_refs": [SOURCE]}
        await session.commit()
    service = SimpleNamespace(service_id="well_architected", sessions=review_store)
    initialized_at = TICK + timedelta(seconds=42)
    assert await produce_due_schedules(service, now=initialized_at) == 0
    async with review_store() as session:
        assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 0
        state = await session.get(ScheduleState, (SCOPE["tenant_id"], "config-example", 1))
        assert state.next_due_at.replace(tzinfo=UTC) == TICK + timedelta(minutes=1)
        assert state.initialized_at.replace(tzinfo=UTC) == initialized_at
    assert await produce_due_schedules(service, now=TICK + timedelta(minutes=1)) == 1
    async with review_store() as session:
        receipt = await session.scalar(select(EventReceipt))
        assert datetime.fromisoformat(receipt.target["scheduled_at"]) == TICK + timedelta(minutes=1)


async def test_limit_bounds_events_across_multiple_triggers_and_configurations(review_store):
    async with review_store() as session:
        version = await session.get(ConfigurationVersion, (SCOPE["tenant_id"], "config-example", 1))
        version.settings = {"enabled": True, "triggers": [{**TRIGGER, "trigger_id": f"trigger-{index}"} for index in range(3)], "source_refs": [SOURCE]}
        session.add(Configuration(**{**SCOPE, "product_id": "product-second"}, configuration_id="config-second",
            current_revision=1, service_id="well_architected", owner_id="owner", handoff_state="linked", relationship_revision="1"))
        session.add(ConfigurationVersion(tenant_id=SCOPE["tenant_id"], configuration_id="config-second", revision=1,
            settings={"enabled": True, "triggers": [{**TRIGGER, "trigger_id": f"trigger-{index}"} for index in range(2)], "source_refs": [SOURCE]},
            state="ready", gaps=[], inherited_fields=[], hash="c" * 64))
        await session.commit()
    service = SimpleNamespace(service_id="well_architected", sessions=review_store)
    assert await produce_due_schedules(service, now=TICK, limit=2) == 2
    async with review_store() as session:
        assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 2
        state = await session.get(ScheduleState, (SCOPE["tenant_id"], "config-example", 1))
        assert state.next_due_at.replace(tzinfo=UTC) == TICK
    assert await produce_due_schedules(service, now=TICK, limit=2) == 2
    assert await produce_due_schedules(service, now=TICK, limit=2) == 1
    assert await produce_due_schedules(service, now=TICK, limit=2) == 0
    async with review_store() as session:
        assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 5


async def test_bounded_initialization_does_not_starve_later_configurations(review_store):
    service = SimpleNamespace(service_id="well_architected", sessions=review_store)
    async with review_store() as session:
        for index in range(4):
            session.add(Configuration(**{**SCOPE, "product_id": f"product-{index}"}, configuration_id=f"config-{index}",
                current_revision=1, service_id="well_architected", owner_id="owner", handoff_state="linked", relationship_revision="1"))
            session.add(ConfigurationVersion(tenant_id=SCOPE["tenant_id"], configuration_id=f"config-{index}", revision=1,
                settings={"enabled": False, "triggers": []}, state="disabled", gaps=[], inherited_fields=[], hash="c" * 64))
        await session.commit()
    for _ in range(3):
        assert await produce_due_schedules(service, now=TICK, limit=2) == 0
    async with review_store() as session:
        assert await session.scalar(select(func.count()).select_from(ScheduleState)) == 5


async def test_missed_tick_is_recorded_and_no_historical_catchup_occurs(review_store):
    await enabled(review_store)
    service = SimpleNamespace(service_id="well_architected", sessions=review_store)
    assert await produce_due_schedules(service, now=TICK - timedelta(minutes=1)) == 0
    assert await produce_due_schedules(service, now=TICK + timedelta(minutes=6)) == 1
    async with review_store() as session:
        receipt = await session.scalar(select(EventReceipt))
        assert (receipt.state, receipt.reason) == ("rejected", "missed_tick")
        assert await session.scalar(select(func.count()).select_from(EventObservation)) == 1
    assert await produce_due_schedules(service, now=TICK + timedelta(minutes=7)) == 0


async def test_duplicate_arrivals_are_retained_without_changing_first_receipt(review_store):
    await enabled(review_store)
    async with review_store() as session:
        first = await receive_schedule_tick(session, **SCOPE, service_id="well_architected", trigger_id=TRIGGER["trigger_id"], scheduled_at=TICK, now=TICK)
        before = deepcopy(first.target)
        again = await receive_schedule_tick(session, **SCOPE, service_id="well_architected", trigger_id=TRIGGER["trigger_id"], scheduled_at=TICK, now=TICK + timedelta(seconds=1))
        assert first.event_id == again.event_id and again.target == before
        observations = (await session.scalars(select(EventObservation).order_by(EventObservation.observed_at))).all()
        assert [item.kind for item in observations] == ["received", "duplicate"]


async def test_gateway_denial_is_terminal_and_transport_failure_remains_retryable(review_store):
    await enabled(review_store)
    service = SimpleNamespace(service_id="well_architected", sessions=review_store,
        admit_event=AsyncMock(side_effect=HTTPException(503, detail={"code": "native_readback_unavailable"})))
    await produce_due_schedules(service, now=TICK)
    assert await repair_pending_events(service, now=TICK) == 0
    async with review_store() as session:
        receipt = await session.scalar(select(EventReceipt))
        assert (receipt.state, receipt.reason) == ("pending", "native_readback_unavailable")
    service.admit_event.side_effect = HTTPException(403, detail={"code": "delegation_denied"})
    assert await repair_pending_events(service, now=TICK + timedelta(minutes=1)) == 0
    assert await repair_pending_events(service, now=TICK + timedelta(minutes=2)) == 0
    assert service.admit_event.await_count == 2
    async with review_store() as session:
        receipt = await session.scalar(select(EventReceipt))
        assert (receipt.state, receipt.reason) == ("rejected", "delegation_denied")
        reasons = (await session.scalars(select(EventObservation.reason).where(EventObservation.kind == "repair_denied"))).all()
        assert reasons == ["native_readback_unavailable", "delegation_denied"]
