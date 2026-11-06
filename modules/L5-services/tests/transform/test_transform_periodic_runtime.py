"""Opt-in Transform occurrences use the installed bounded repair producer."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from l5_services.events.periodic import produce_due_schedules
from l5_services.models import Configuration, ConfigurationVersion, EventReceipt, Run
from l5_services.models.schedule import ScheduleState
from test_tf06 import NOW, recurring_transform as recurring_transform


async def configure(sessions, trigger, *, enabled=True):
    trigger = {**deepcopy(trigger), "schedule": "* * * * *", "source_policy": "pinned"}
    trigger.pop("branch_selector", None)
    async with sessions() as session:
        version = await session.get(ConfigurationVersion, ("tenant-a", "config-a", 1))
        version.settings = {"enabled": enabled, "triggers": [trigger], "source_refs": trigger["source_refs"]}
        await session.commit()
    return trigger


async def test_opt_in_revision_has_no_history_and_replay_does_not_create_work(recurring_transform):
    sessions, trigger, _, _ = recurring_transform
    trigger = await configure(sessions, trigger, enabled=False)
    service = SimpleNamespace(service_id="transform", sessions=sessions)
    assert await produce_due_schedules(service, now=NOW) == 0
    async with sessions() as session:
        config = await session.get(Configuration, ("tenant-a", "config-a"))
        config.current_revision = 2
        session.add(ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=2,
            settings={"enabled": True, "triggers": [trigger], "source_refs": trigger["source_refs"]},
            state="ready", gaps=[], inherited_fields=[], hash="b" * 64))
        await session.commit()
    enabled_at = NOW + timedelta(days=3, seconds=42)
    assert await produce_due_schedules(service, now=enabled_at) == 0
    assert await produce_due_schedules(service, now=enabled_at + timedelta(seconds=18)) == 1
    assert await produce_due_schedules(service, now=enabled_at + timedelta(seconds=18)) == 0
    async with sessions() as session:
        receipt = await session.scalar(select(EventReceipt))
        assert receipt.state == "pending" and receipt.configuration_revision == 2
        assert receipt.run_input["sources"] == trigger["source_refs"]
        assert receipt.run_input["transform_input"]["goal"] == trigger["goal"]
        assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(ScheduleState)) == 2
        assert await session.scalar(select(func.count()).select_from(Run)) == 0


async def test_missed_ticks_are_retained_without_historical_work(recurring_transform):
    sessions, trigger, _, _ = recurring_transform
    await configure(sessions, trigger)
    service = SimpleNamespace(service_id="transform", sessions=sessions)
    assert await produce_due_schedules(service, now=NOW + timedelta(seconds=1)) == 0
    assert await produce_due_schedules(service, now=NOW + timedelta(minutes=10)) == 1
    async with sessions() as session:
        receipt = await session.scalar(select(EventReceipt))
        assert (receipt.state, receipt.reason) == ("rejected", "missed_tick")
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
    assert await produce_due_schedules(service, now=NOW + timedelta(minutes=10)) == 0


async def test_branch_producer_defers_resolution_without_granting_source_authority(recurring_transform):
    sessions, trigger, _, _ = recurring_transform
    service = SimpleNamespace(service_id="transform", sessions=sessions)
    assert await produce_due_schedules(service, now=NOW) == 1
    async with sessions() as session:
        receipt = await session.scalar(select(EventReceipt))
        assert (receipt.state, receipt.reason, receipt.run_input) == ("pending", None, None)
        assert receipt.target["source_policy"] == "branch" and "pinned_source" not in receipt.target
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
    assert await produce_due_schedules(service, now=NOW) == 0


@pytest.mark.parametrize("service_id", ["security", "devops", "finops"])
async def test_producer_does_not_take_other_services(recurring_transform, service_id):
    sessions, trigger, _, _ = recurring_transform
    await configure(sessions, trigger)
    assert await produce_due_schedules(SimpleNamespace(service_id=service_id, sessions=sessions), now=NOW) == 0
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 0


@pytest.mark.parametrize("reason", ["schedule_gateway_unavailable", "branch_resolution_unavailable"])
async def test_repair_keeps_retryable_reason_and_stops_after_revoked_consent(recurring_transform, reason):
    from unittest.mock import AsyncMock

    from fastapi import HTTPException
    from l5_services.events.recovery import repair_pending_events

    sessions, trigger, _, _ = recurring_transform
    await configure(sessions, trigger)
    service = SimpleNamespace(service_id="transform", sessions=sessions,
        admit_event=AsyncMock(side_effect=HTTPException(503, detail={"code": reason})))
    await produce_due_schedules(service, now=NOW)
    assert await repair_pending_events(service, now=NOW) == 0
    async with sessions() as session:
        receipt = await session.scalar(select(EventReceipt))
        assert (receipt.state, receipt.reason) == ("pending", reason)
    service.admit_event.side_effect = HTTPException(403, detail={"code": "delegation_denied"})
    assert await repair_pending_events(service, now=NOW + timedelta(minutes=1)) == 0
    assert await repair_pending_events(service, now=NOW + timedelta(minutes=2)) == 0
    assert service.admit_event.await_count == 2
    async with sessions() as session:
        receipt = await session.scalar(select(EventReceipt))
        assert (receipt.state, receipt.reason) == ("rejected", "delegation_denied")
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
