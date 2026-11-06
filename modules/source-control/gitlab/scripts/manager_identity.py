"""Bind the current manager to its fresh registration, retaining old API records."""
from datetime import datetime


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("runner_manager_identity_mismatch")
    return result


def verify_current_manager(managers, registration, version, registered_at, now):
    """GitLab keeps stopped managers 'online' for two hours; require a fresh current one."""
    registered_at = timestamp(registered_at)
    if not isinstance(managers, list) or not all(isinstance(m, dict) for m in managers):
        raise ValueError("runner_manager_identity_mismatch")
    current = [m for m in managers if m.get("system_id") == registration["system_id"]]
    if len(current) != 1:
        raise ValueError("runner_manager_identity_mismatch")
    manager = current[0]
    if (manager.get("status") != "online" or manager.get("version") != version
            or manager.get("platform") != "linux" or manager.get("architecture") != "amd64"
            or not 0 <= (now - timestamp(manager["contacted_at"])).total_seconds() <= 120):
        raise ValueError("runner_manager_identity_mismatch")
    historical_online = [m for m in managers if m is not manager and m.get("status") == "online"]
    if any(timestamp(m["contacted_at"]) >= registered_at for m in historical_online):
        raise ValueError("runner_other_manager_contacted_after_registration")
    return {"historical_manager_records": len(managers) - 1,
            "historical_online_records": len(historical_online)}
