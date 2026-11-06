#!/usr/bin/env python3
"""Render the optional, atomic adapter binding into the Gateway ConfigMap."""

import argparse
import json
import re
import subprocess
import sys

FIELDS = {
    "observe_url": "ADP_TOOL_ADAPTER_OBSERVE_URL",
    "assertion_key_arn": "ADP_TOOL_ADAPTER_ASSERTION_KEY_ARN",
    "core_role_arn": "ADP_TOOL_ADAPTER_CORE_ROLE_ARN",
    "adapter_role_arn": "ADP_TOOL_ADAPTER_ADAPTER_ROLE_ARN",
    "release": "ADP_TOOL_ADAPTER_RELEASE",
}


def bindings(value, account, region):
    if value is None:
        return {name: "" for name in FIELDS.values()}
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError("Incomplete adapter configuration")
    patterns = {
        "observe_url": r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9_-]+)?/internal/v1/tool-adapters/observe",
        "assertion_key_arn": rf"arn:aws:kms:{re.escape(region)}:{account}:key/[a-f0-9-]{{36}}",
        "core_role_arn": rf"arn:aws:iam::{account}:role/[A-Za-z0-9_+=,.@/-]+",
        "adapter_role_arn": rf"arn:aws:iam::{account}:role/[A-Za-z0-9_+=,.@/-]+",
    }
    if any(not isinstance(value[name], str) or not re.fullmatch(pattern, value[name]) for name, pattern in patterns.items()):
        raise ValueError("Invalid adapter deployment authority")
    if value["core_role_arn"] == value["adapter_role_arn"]:
        raise ValueError("Adapter and core roles must be distinct")
    release = value["release"]
    if (
        not isinstance(release, dict)
        or set(release) != {"contract_version", "source_revision", "image_digest", "group", "group_generation", "backend_generation"}
        or release["contract_version"] != "1.0"
        or any(
            not isinstance(release[name], str) or not re.fullmatch(pattern, release[name])
            for name, pattern in {
                "source_revision": r"[0-9a-f]{40}",
                "image_digest": r"sha256:[0-9a-f]{64}",
                "group": r"[a-z][a-z0-9-]{0,63}",
            }.items()
        )
        or any(type(release[name]) is not int or release[name] < 1 for name in ("group_generation", "backend_generation"))
    ):
        raise ValueError("Invalid adapter release binding")
    return {variable: json.dumps(value[name], separators=(",", ":")) if name == "release" else value[name] for name, variable in FIELDS.items()}


def unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate configuration field")
        result[name] = value
    return result


def load(environment, account, region):
    result = subprocess.run(
        ["aws", "ssm", "get-parameter", "--name", f"/adp/{environment}/gateway/tool-adapter-config", "--region", region, "--output", "json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        if "(ParameterNotFound)" in result.stderr:
            return bindings(None, account, region)
        raise ValueError("Cannot read adapter configuration")
    raw = json.loads(result.stdout)["Parameter"]["Value"]
    if not isinstance(raw, str) or len(raw.encode()) > 4096:
        raise ValueError("Invalid adapter configuration size")
    return bindings(json.loads(raw, object_pairs_hook=unique), account, region)


def render(template, values):
    for name, value in values.items():
        placeholder = f'"__{name}__"'
        if template.count(placeholder) != 1:
            raise ValueError("Adapter configuration template mismatch")
        template = template.replace(placeholder, json.dumps(value))
    return template


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
        raise SystemExit("Invalid adapter deployment target")
    try:
        result = render(sys.stdin.read(), load(args.environment, args.account, args.region))
    except (ValueError, TypeError, KeyError, subprocess.SubprocessError):
        raise SystemExit("Adapter configuration unavailable or invalid; deployment refused") from None
    print(result, end="")


if __name__ == "__main__":
    main()
