"""Observe access to one selected native space using the broker-selected role."""

import json
import re
import sys


def read_access(account_id, role_arn, region, space_id, *, client_factory=None):
    import boto3
    from botocore.config import Config

    client_factory = client_factory or boto3.client
    bounds = Config(connect_timeout=4, read_timeout=5, retries={"total_max_attempts": 1})
    identity = client_factory("sts", region_name=region, config=bounds).get_caller_identity()
    expected = rf"arn:aws:sts::{re.escape(account_id)}:assumed-role/{re.escape(role_arn.split('/')[-1])}/[^/]+"
    if identity.get("Account") != account_id or not re.fullmatch(expected, identity.get("Arn", "")):
        raise ValueError("selected connection differs from the native access identity")
    response = client_factory("devops-agent", region_name=region, config=bounds).get_agent_space(agentSpaceId=space_id)
    space = response.get("agentSpace") if isinstance(response, dict) else None
    if not isinstance(space, dict) or space.get("agentSpaceId") != space_id:
        raise ValueError("native access readback returned another space")
    return {"account_id": identity["Account"], "role_arn": role_arn, "region": region,
            "assumed_role_arn": identity["Arn"], "agent_space_id": space["agentSpaceId"], "requests": 2}


if __name__ == "__main__":
    if len(sys.argv) != 5:
        raise SystemExit("native access readback requires one selected space")
    try:
        print(json.dumps(read_access(*sys.argv[1:])))
    except Exception:
        raise SystemExit("selected native access readback unavailable") from None
