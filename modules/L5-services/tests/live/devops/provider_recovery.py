"""Bounded native recovery observations using the selected broker session."""

import json
import re
import sys
from datetime import UTC, datetime


def read_recovery(account_id, role_arn, region, selection, *, client_factory=None):
    import boto3
    from botocore.config import Config

    remaining = selection["max_requests"]
    if type(remaining) is not int or not 3 <= remaining <= 100:
        raise ValueError("native recovery needs a bounded request allocation")
    client_factory = client_factory or boto3.client
    config = Config(connect_timeout=4, read_timeout=5, retries={"total_max_attempts": 1})
    calls = 0

    def request(method, **arguments):
        nonlocal calls
        calls += 1
        if calls > remaining:
            raise ValueError("native recovery exceeded its request allocation")
        return method(**arguments)

    identity = request(client_factory("sts", region_name=region, config=config).get_caller_identity)
    expected = rf"arn:aws:sts::{re.escape(account_id)}:assumed-role/{re.escape(role_arn.split('/')[-1])}/[^/]+"
    if identity.get("Account") != account_id or not re.fullmatch(expected, identity.get("Arn", "")):
        raise ValueError("selected AWS connection does not match recovery identity")
    client = client_factory("devops-agent", region_name=region, config=config)
    space_id = selection["space_id"]
    if selection["kind"] == "spaces":
        spaces, seen = {}, set()
        token = None
        while True:
            page = request(client.list_agent_spaces, maxResults=100,
                           **({"nextToken": token} if token else {}))
            items = page.get("agentSpaces")
            if not isinstance(items, list):
                raise ValueError("native space inventory is unavailable")
            for item in items:
                identifier = item.get("agentSpaceId") if isinstance(item, dict) else None
                if not isinstance(identifier, str) or not identifier or identifier in spaces:
                    raise ValueError("native space inventory is ambiguous")
                observed = request(client.get_agent_space, agentSpaceId=identifier, includedData="ALL_DATA")
                space = observed.get("agentSpace")
                if (not isinstance(space, dict) or space.get("agentSpaceId") != identifier
                        or not isinstance(space.get("name"), str) or not space["name"]
                        or not isinstance(space.get("createdAt"), datetime) or space["createdAt"].tzinfo is None
                        or not isinstance(observed.get("tags", {}), dict)):
                    raise ValueError("native space inventory changed during readback")
                spaces[identifier] = {"space": {key: value for key, value in space.items() if key != "updatedAt"},
                                      "tags": observed.get("tags", {})}
            token = page.get("nextToken")
            if not token:
                break
            if not isinstance(token, str) or token in seen:
                raise ValueError("native space inventory repeated a cursor")
            seen.add(token)
        if space_id not in spaces:
            raise ValueError("selected recovery space is absent from native inventory")
        return {"spaces": spaces, "requests": calls}
    space = request(client.get_agent_space, agentSpaceId=space_id)
    if (space.get("agentSpace") or {}).get("agentSpaceId") != space_id:
        raise ValueError("native recovery returned another space")
    if selection["kind"] != "work":
        raise ValueError("unsupported recovery observation")
    matches, seen = [], set()
    token = None
    while True:
        page = request(client.list_backlog_tasks, agentSpaceId=space_id, limit=100,
                       **({"nextToken": token} if token else {}))
        tasks = page.get("tasks")
        if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
            raise ValueError("native recovery task inventory is incomplete")
        for task in tasks:
            try:
                description = json.loads(task.get("description", ""))
            except (TypeError, ValueError):
                continue
            if isinstance(description, dict) and description.get("run_id") == selection["run_id"]:
                matches.append(task.get("taskId"))
        token = page.get("nextToken")
        if not token:
            break
        if not isinstance(token, str) or token in seen:
            raise ValueError("native recovery repeated a task cursor")
        seen.add(token)
    if matches != [selection["task_id"]]:
        raise ValueError("recovery must retain exactly one native task for the admitted run")
    task = request(client.get_backlog_task, agentSpaceId=space_id, taskId=selection["task_id"]).get("task")
    if not isinstance(task, dict):
        raise ValueError("native recovery task is unavailable")
    return {"task": task, "matching_task_ids": matches, "requests": calls}


def encode(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).isoformat()
    raise TypeError("unsupported native recovery observation")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 4:
            raise ValueError("selected provider identity is required")
        print(json.dumps(read_recovery(*sys.argv[1:], json.loads(sys.stdin.buffer.read(65537))), default=encode))
    except Exception:
        raise SystemExit("selected native recovery readback unavailable") from None
