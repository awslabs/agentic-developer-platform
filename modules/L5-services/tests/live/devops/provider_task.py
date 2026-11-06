"""Read one pinned native task through the owner-selected credential broker role."""

import json
import re
import sys
from datetime import UTC, datetime


def read_task(account_id, role_arn, region, space_id, task_id, *, client_factory=None):
    import boto3
    from botocore.config import Config

    client_factory = client_factory or boto3.client
    bounds = Config(connect_timeout=4, read_timeout=5, retries={"total_max_attempts": 1})
    identity = client_factory("sts", region_name=region, config=bounds).get_caller_identity()
    name = role_arn.split("/")[-1]
    expected = rf"arn:aws:sts::{re.escape(account_id)}:assumed-role/{re.escape(name)}/[^/]+"
    if (identity.get("Account") != account_id or not re.fullmatch(expected, identity.get("Arn", ""))):
        raise ValueError("selected AWS connection does not match native provider identity")
    observed = client_factory("devops-agent", region_name=region, config=bounds).get_backlog_task(
        agentSpaceId=space_id, taskId=task_id)
    task = observed.get("task") if isinstance(observed, dict) else None
    if not isinstance(task, dict):
        raise ValueError("native task readback unavailable")
    result = {key: task.get(key) for key in ("agentSpaceId", "taskId", "executionId", "status", "taskType")}
    for key in ("createdAt", "updatedAt"):
        value = task.get(key)
        if isinstance(value, datetime) and value.tzinfo is not None:
            result[key] = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return result


if __name__ == "__main__":
    if len(sys.argv) != 6:
        raise SystemExit("provider readback needs one selected native task")
    try:
        print(json.dumps(read_task(*sys.argv[1:])))
    except Exception:
        raise SystemExit("selected native provider readback unavailable") from None
