#!/usr/bin/env python3
"""Read the protected exact-role exception list for platform-account connections."""

import argparse
import json
import re
import subprocess


def validate(raw, account):
    if not isinstance(raw, str) or len(raw.encode()) > 4096:
        raise ValueError("Invalid platform operator role list")
    if raw == "":
        return ""
    roles = raw.split(",")
    pattern = rf"arn:aws:iam::{account}:role/[A-Za-z0-9_+=.@/-]+"
    if any(not re.fullmatch(pattern, role) for role in roles) or len(set(roles)) != len(roles):
        raise ValueError("Expected unique exact IAM role ARNs in the target account")
    return ",".join(roles)


def load(environment, account, region):
    result = subprocess.run(
        ["aws", "ssm", "get-parameter", "--name",
         f"/adp/{environment}/gateway/platform-operator-role-arns",
         "--region", region, "--output", "json"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        if "(ParameterNotFound)" in result.stderr:
            return validate(f"arn:aws:iam::{account}:role/ADP-Agent-{environment}-platform", account)
        raise ValueError("Cannot read platform operator role configuration")
    return validate(json.loads(result.stdout)["Parameter"]["Value"], account)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment")
    parser.add_argument("region")
    parser.add_argument("account")
    args = parser.parse_args()
    if not (
        re.fullmatch(r"[a-z][a-z0-9-]{0,31}", args.environment)
        and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", args.region)
        and re.fullmatch(r"[0-9]{12}", args.account)
    ):
        raise SystemExit("Invalid platform operator deployment target")
    try:
        value = load(args.environment, args.account, args.region)
    except (ValueError, TypeError, KeyError, subprocess.SubprocessError):
        raise SystemExit("Platform operator role configuration unavailable or invalid; deployment refused") from None
    print(value)


if __name__ == "__main__":
    main()
