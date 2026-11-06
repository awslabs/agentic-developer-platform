"""Bounded observation of a Security job that outlived its execution deadline."""

from datetime import UTC, datetime, timedelta


RECOVERY_SECONDS = 900


def recovery_deadline(run, kind, now):
    """This grants no authority; the broker must still verify current consent."""
    recovery = (getattr(run, "monitoring", None) or {}).get("security_recovery")
    if not isinstance(recovery, dict):
        return None
    deadline = run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC)
    if (run.service_id != "security" or kind not in {"poll", "collect"}
            or run.state not in {"running", "collecting", "outcome_unknown", "expired"}
            or not isinstance(run.native_job_ref, dict) or not run.native_job_ref
            or now < deadline):
        return None
    try:
        start = datetime.fromisoformat(recovery["requested_at"])
        end = datetime.fromisoformat(recovery["expires_at"])
        if (start.tzinfo is None or end.tzinfo is None or start > now
                or not start < end <= start + timedelta(seconds=RECOVERY_SECONDS)
                or recovery["native_job_ref"] != run.native_job_ref):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return end if now < end else None


def request_recovery(run, now):
    return {"progress_state": "unknown", "poll_attempts": 0, "consecutive_poll_errors": 0,
            **(run.monitoring or {}), "state": "recovery_pending", "security_recovery": {
        "requested_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=RECOVERY_SECONDS)).isoformat(),
        "native_job_ref": run.native_job_ref,
    }}


def recovery_error(code="security_observation_window_expired"):
    return {"code": code,
            "message": "ADP's observation window ended. The existing AWS assessment may still finish.",
            "action": "Reconcile this assessment to check the existing AWS job and collect its results. Do not start a duplicate scan."}
