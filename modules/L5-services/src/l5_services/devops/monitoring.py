"""Persist native readback and monitoring health independently of ADP admission."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from l5_services.models.run import Run

TERMINAL = {"succeeded", "failed", "cancelled", "partial"}
OUTCOMES = {"running": "running", "completed": "succeeded", "failed": "failed",
            "stopped": "cancelled", "unknown": "outcome_unknown"}


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _format(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def reconcile_native_run(sessions, tenant_id, run_id, *, now, observation=None, error=None,
                               provider_readback_ref=None, source="readback", interval_seconds=300,
                               stall_after_seconds=1800):
    if (observation is None) == (error is None) or not 60 <= interval_seconds <= 3600:
        raise ValueError("one observation or monitoring error and a bounded interval required")
    if source not in {"readback", "event"} or (source == "event" and (provider_readback_ref is not None or error is not None)):
        raise ValueError("events cannot establish a provider terminal outcome")
    async with sessions() as session:
        run = await session.get(Run, (tenant_id, run_id))
        if run is None or run.service_id != "devops" or run.native_job_ref is None:
            return None
        if run.state in TERMINAL:
            return run
        monitoring = dict(run.monitoring or {})
        if source == "event":
            if (observation.get("native_job_ref") != run.native_job_ref
                    or observation.get("outcome") not in OUTCOMES
                    or _instant(observation["observed_at"]) > now):
                raise ValueError("unrecognized or mismatched native event")
            previous = monitoring.get("latest_observation") or {}
            if previous and _instant(observation.get("provider_updated_at", observation["observed_at"])) <= _instant(
                    previous.get("provider_updated_at", previous["observed_at"])):
                return run
            monitoring["next_poll_at"] = _format(now)
            changed = await session.execute(update(Run).where(
                Run.tenant_id == tenant_id, Run.run_id == run_id, Run.revision == run.revision,
            ).values(revision=Run.revision + 1, monitoring=monitoring, next_check_at=now),
                execution_options={"synchronize_session": False})
            if changed.rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
            return await session.get(Run, (tenant_id, run_id), populate_existing=True)
        monitoring.setdefault("poll_attempts", 0)
        monitoring.setdefault("consecutive_poll_errors", 0)
        monitoring.setdefault("progress_state", "unknown")
        monitoring["poll_attempts"] += 1
        monitoring["last_poll_attempt_at"] = _format(now)
        next_check = now + timedelta(seconds=interval_seconds)
        new_state = run.state
        provider_state = run.provider_state
        evidence = run.provider_readback_ref
        if error is not None:
            monitoring["state"] = "degraded"
            monitoring["consecutive_poll_errors"] += 1
            monitoring["last_poll_error"] = {"code": error}
        else:
            if observation.get("native_job_ref") != run.native_job_ref:
                raise ValueError("provider observation belongs to a different native execution")
            outcome = observation.get("outcome")
            if outcome not in OUTCOMES or observation.get("progress_coverage") not in {"complete", "partial", "unavailable"}:
                raise ValueError("unrecognized provider observation")
            observed_at = _instant(observation["observed_at"])
            if observed_at > now:
                raise ValueError("provider observation is in the future")
            previous = monitoring.get("latest_observation") or {}
            previous_version = previous.get("provider_updated_at", previous.get("observed_at"))
            observed_version = observation.get("provider_updated_at", observation["observed_at"])
            if previous_version and _instant(observed_version) <= _instant(previous_version):
                return run
            monitoring.update(state="current", consecutive_poll_errors=0,
                              last_successful_poll_at=_format(now), latest_observation=observation)
            monitoring.pop("last_poll_error", None)
            fingerprint = observation.get("progress_fingerprint")
            if observation["progress_coverage"] == "complete" and fingerprint:
                last = previous.get("progress_fingerprint")
                if last != fingerprint:
                    monitoring["last_progress_at"] = _format(now)
                    monitoring["progress_state"] = "observed"
                elif (now - _instant(monitoring.get("last_progress_at", observation["observed_at"]))).total_seconds() >= stall_after_seconds:
                    monitoring["progress_state"] = "suspected_stalled"
            else:
                monitoring["progress_state"] = "unknown"
            if outcome in {"completed", "failed", "stopped"}:
                if provider_readback_ref is not None:
                    new_state = OUTCOMES[outcome]
                    provider_state = outcome
                    evidence = provider_readback_ref
                    monitoring["state"] = "finished"
                    next_check = None
            elif outcome == "running" and run.state not in {"cancel_requested", "outcome_unknown"}:
                provider_state = "running"
        if next_check is not None:
            monitoring["next_poll_at"] = _format(next_check)
        else:
            monitoring.pop("next_poll_at", None)
        changed = await session.execute(update(Run).where(
            Run.tenant_id == tenant_id, Run.run_id == run_id, Run.revision == run.revision,
        ).values(revision=Run.revision + 1, monitoring=monitoring, state=new_state,
                 provider_state=provider_state, provider_readback_ref=evidence, next_check_at=next_check),
            execution_options={"synchronize_session": False})
        if changed.rowcount != 1:
            await session.rollback()
            return None
        await session.commit()
        return await session.get(Run, (tenant_id, run_id), populate_existing=True)
