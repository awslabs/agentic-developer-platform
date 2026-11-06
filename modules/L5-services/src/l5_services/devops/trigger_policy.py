"""Evaluate selected DevOps trigger controls without widening event authority."""

from datetime import UTC, datetime

from fastapi import HTTPException


def require_new_work(settings, trigger, *, event=None, now=None):
    now = now or datetime.now(UTC)
    devops = settings.get("devops") or {}
    if any(value is not None and type(value) is not bool for value in (
        devops.get("paused"), trigger.get("paused")
    )):
        raise HTTPException(409, detail={"code": "trigger_policy_unavailable"})
    if devops.get("paused") is True or trigger.get("paused") is True:
        raise HTTPException(409, detail={"code": "trigger_paused"})
    if event is None:
        return
    allowed_severities = trigger.get("severities")
    allowed_environments = trigger.get("environment_refs")
    if (not isinstance(allowed_severities, list) or not allowed_severities
            or any(not isinstance(value, str) or not value for value in allowed_severities)
            or event.get("severity") not in allowed_severities
            or not isinstance(allowed_environments, list) or not allowed_environments
            or event.get("environment_ref") not in allowed_environments):
        raise HTTPException(409, detail={"code": "incident_filtered"})
    windows = trigger.get("maintenance_windows", [])
    if not isinstance(windows, list):
        raise HTTPException(409, detail={"code": "maintenance_policy_unavailable"})
    for window in windows:
        try:
            starts = datetime.fromisoformat(window["starts_at"].replace("Z", "+00:00"))
            ends = datetime.fromisoformat(window["ends_at"].replace("Z", "+00:00"))
            if (starts.tzinfo is None or ends.tzinfo is None or starts >= ends
                    or not isinstance(window["environment_refs"], list)
                    or not window["environment_refs"]):
                raise ValueError("invalid maintenance window")
        except (KeyError, TypeError, AttributeError, ValueError):
            raise HTTPException(409, detail={"code": "maintenance_policy_unavailable"}) from None
        if (event["environment_ref"] in window["environment_refs"]
                and starts.astimezone(UTC) <= now < ends.astimezone(UTC)):
            raise HTTPException(409, detail={"code": "maintenance_exclusion"})
