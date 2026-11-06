"""Bounded, explicitly requested readback of an existing DevOps investigation."""

from datetime import UTC, datetime, timedelta

RECOVERY_SECONDS = 900


def recovery_deadline(run, kind, now):
    """A readback window never replaces current owner consent or job identity."""
    recovery = (getattr(run, "monitoring", None) or {}).get("devops_recovery")
    if not isinstance(recovery, dict):
        return None
    if (run.service_id != "devops" or run.input.get("capability") != "investigation"
            or getattr(run, "origin", None) != "adp_submitted"
            or kind not in {"poll", "collect"}
            or run.state not in {"running", "collecting", "outcome_unknown", "expired"}
            or not isinstance(run.native_job_ref, dict) or not run.native_job_ref
            or now < run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC)):
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
            **(run.monitoring or {}), "state": "recovery_pending", "devops_recovery": {
        "requested_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=RECOVERY_SECONDS)).isoformat(),
        "native_job_ref": run.native_job_ref,
    }}


def readback_authority(run, kind, now):
    """Use fresh read-only consent only inside this run's pinned recovery window."""
    if recovery_deadline(run, kind, now) is not None:
        authority = run.monitoring["devops_recovery"].get("authorization")
        if isinstance(authority, dict) and set(authority) == {"delegation_id", "delegation_version"}:
            return authority
    admission = run.admission if isinstance(run.admission, dict) else {}
    return {key: admission.get(key) for key in ("delegation_id", "delegation_version")}


def recovery_error(code="devops_observation_window_expired"):
    return {"code": code,
            "message": "ADP's observation window ended before the investigation report was collected.",
            "action": "Reconcile this run to read the existing AWS investigation and collect its report. Do not start another investigation."}
