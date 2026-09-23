#!/usr/bin/env python3
"""Read or atomically create a signing secret; never rotate an existing value.

Uses the deployment's existing AWS CLI credentials and prints only the value for
capture by the caller. Secret input travels on stdin, never in process arguments.
"""

import argparse
import json
import re
import secrets
import subprocess
import sys


class SecretBootstrapError(RuntimeError):
    def __init__(self, action, code):
        self.code = code
        super().__init__(f"Secrets Manager {action} failed ({code}); no existing key was replaced")


def request(action, region, payload):
    result = subprocess.run(
        [
            "aws",
            "secretsmanager",
            action,
            "--region",
            region,
            "--output",
            "json",
            "--no-cli-pager",
            "--cli-input-json",
            "file:///dev/stdin",
            "--cli-connect-timeout",
            "10",
            "--cli-read-timeout",
            "30",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        match = re.search(r"\(([A-Za-z][A-Za-z0-9]+)\) when calling", result.stderr)
        raise SecretBootstrapError(action, match.group(1) if match else "CommandFailed")
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise SecretBootstrapError(action, "InvalidResponse") from exc


def read_secret(name, region):
    response = request("get-secret-value", region, {"SecretId": name})
    value = response.get("SecretString") if isinstance(response, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise SecretBootstrapError("get-secret-value", "MissingSecretString")
    return value


def ensure_secret(name, region):
    try:
        return read_secret(name, region)
    except SecretBootstrapError as exc:
        if exc.code != "ResourceNotFoundException":
            raise

    candidate = secrets.token_hex(32)
    try:
        request(
            "create-secret",
            region,
            {
                "Name": name,
                "SecretString": candidate,
                "Description": "Independent signing key for single-use identity-linking tokens (#5656)",
            },
        )
    except SecretBootstrapError as exc:
        if exc.code != "ResourceExistsException":
            raise
        # Another deployment created the key after our read. Use its value;
        # put-secret-value here would rotate the key that deployment just used.
        return read_secret(name, region)
    return candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    try:
        value = ensure_secret(args.name, args.region)
    except (SecretBootstrapError, subprocess.TimeoutExpired, OSError) as exc:
        # Do not print CLI input/output or an exception containing secret values.
        print(str(exc) if isinstance(exc, SecretBootstrapError) else "Signing secret bootstrap failed", file=sys.stderr)
        return 1
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
