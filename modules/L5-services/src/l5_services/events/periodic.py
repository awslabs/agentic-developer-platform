"""Produce opt-in service ticks in the already-installed minute repair invocation."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException

from l5_services.events.cron import next_tick
from l5_services.events.schedules import receive_schedule_tick
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.schedule import ScheduleState


def _iso(value):
    return value.astimezone(UTC).isoformat()


async def produce_due_schedules(service, *, now, limit=50):
    if getattr(service, "service_id", None) not in {"well_architected", "transform", "security"}:
        return 0
    if not 1 <= limit <= 50:
        raise ValueError("repair limit must be between 1 and 50")
    floor = now.astimezone(UTC).replace(second=0, microsecond=0)
    # Seed only the current revision; a newly enabled schedule never catches up
    # historical paid work. Disabled/invalid revisions also get a cursor, so
    # they cannot starve the bounded scan of newly opted-in configurations.
    async with service.sessions() as session:
        candidates = (await session.execute(select(Configuration, ConfigurationVersion).join(
            ConfigurationVersion, and_(ConfigurationVersion.tenant_id == Configuration.tenant_id,
                ConfigurationVersion.configuration_id == Configuration.configuration_id,
                ConfigurationVersion.revision == Configuration.current_revision),
        ).where(Configuration.service_id == service.service_id, ~select(ScheduleState.tenant_id).where(
            ScheduleState.tenant_id == Configuration.tenant_id,
            ScheduleState.configuration_id == Configuration.configuration_id,
            ScheduleState.configuration_revision == Configuration.current_revision,
        ).exists()).order_by(Configuration.tenant_id, Configuration.configuration_id).limit(limit))).all()
        for configuration, version in candidates:
            triggers = {}
            for trigger in version.settings.get("triggers", []):
                if trigger.get("kind") != "schedule":
                    continue
                due, reason = None, None
                if version.settings.get("enabled") is not True or trigger.get("enabled") is not True:
                    reason = "trigger_disabled"
                else:
                    try:
                        due = next_tick(trigger.get("schedule"), now)
                    except ValueError:
                        reason = "schedule_invalid"
                triggers[trigger["trigger_id"]] = {"next_due_at": _iso(due) if due else None, "reason": reason}
            dates = [entry["next_due_at"] for entry in triggers.values() if entry["next_due_at"]]
            state = ScheduleState(tenant_id=configuration.tenant_id,
                configuration_id=configuration.configuration_id, configuration_revision=version.revision,
                service_id=service.service_id, initialized_at=now, triggers=triggers,
                next_due_at=datetime.fromisoformat(min(dates)) if dates else None)
            try:
                async with session.begin_nested():
                    session.add(state)
                    await session.flush()
            except IntegrityError:
                pass  # An overlapping repair initialized the same exact revision.
        await session.commit()

    async with service.sessions() as session:
        states = (await session.execute(select(ScheduleState, Configuration, ConfigurationVersion).join(
            Configuration, and_(Configuration.tenant_id == ScheduleState.tenant_id,
                Configuration.configuration_id == ScheduleState.configuration_id,
                Configuration.current_revision == ScheduleState.configuration_revision),
        ).join(ConfigurationVersion, and_(ConfigurationVersion.tenant_id == ScheduleState.tenant_id,
            ConfigurationVersion.configuration_id == ScheduleState.configuration_id,
            ConfigurationVersion.revision == ScheduleState.configuration_revision),
        ).where(ScheduleState.service_id == service.service_id, ScheduleState.next_due_at <= now)
            .order_by(ScheduleState.next_due_at, ScheduleState.tenant_id, ScheduleState.configuration_id)
            .limit(limit))).all()
        produced = 0
        for state, configuration, version in states:
            updated = deepcopy(state.triggers)
            for trigger_id, entry in updated.items():
                if produced >= limit:
                    break
                if entry["next_due_at"] is None or datetime.fromisoformat(entry["next_due_at"]) > now:
                    continue
                scheduled_at = datetime.fromisoformat(entry["next_due_at"])
                try:
                    await receive_schedule_tick(session, tenant_id=state.tenant_id,
                        product_id=configuration.product_id, project_id=configuration.project_id,
                        service_id=service.service_id, trigger_id=trigger_id, scheduled_at=scheduled_at, now=now,
                        configuration_revision=state.configuration_revision,
                        defer_transform_branch=service.service_id == "transform")
                except HTTPException as error:
                    if error.status_code != 409 or error.detail != {"code": "stale_revision"}:
                        raise
                    break
                produced += 1
                trigger = next(item for item in version.settings["triggers"] if item["trigger_id"] == trigger_id)
                entry["next_due_at"] = _iso(next_tick(trigger["schedule"], floor + timedelta(minutes=1)))
            # Receipt commit comes first. A crash or concurrent repair can replay
            # the same tick but cannot create a second event/admission identity.
            state.triggers = updated
            dates = [entry["next_due_at"] for entry in updated.values() if entry["next_due_at"]]
            state.next_due_at = datetime.fromisoformat(min(dates)) if dates else None
            await session.commit()
            if produced >= limit:
                break
        return produced
