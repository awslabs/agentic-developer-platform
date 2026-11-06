#!/usr/bin/env python3
"""Check signing infrastructure before publishing code; migrate state once."""

import argparse
import json
import subprocess
import tempfile
import time


def state_environment(variables, now):
    result = dict(variables)
    arn = result.get("STATE_SIGNING_SECRET_ARN")
    if not arn or arn == result.get("GITHUB_CLIENT_SECRET_ARN"):
        raise ValueError("Apply gateway infrastructure to provision the dedicated state signing secret before deploying broker code.")
    if result.get("STATE_FORMAT_VERSION") != "2":
        result["STATE_LEGACY_ACCEPT_UNTIL"] = str(int(now) + 600)
        result["STATE_FORMAT_VERSION"] = "2"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--function-name", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()

    def aws(*arguments):
        result = subprocess.run(
            ["aws", *arguments, "--region", args.region, "--output", "json"],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout) if result.stdout.strip() else {}

    config = aws("lambda", "get-function-configuration", "--function-name", args.function_name)
    old = config.get("Environment", {}).get("Variables", {})
    variables = state_environment(old, time.time())
    secret = aws("secretsmanager", "get-secret-value", "--secret-id", variables["STATE_SIGNING_SECRET_ARN"])["SecretString"]
    key = json.loads(secret).get("key", "") if secret.startswith("{") else secret
    if not isinstance(key, str) or len(key) < 32:
        raise ValueError("Dedicated state signing secret is invalid; existing broker code has not been changed.")
    if variables != old:
        # The full Lambda environment can contain unrelated secrets. Keep it out
        # of command arguments/logs, and preserve it with revision-based locking.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as output:
            json.dump({"Variables": variables}, output)
            output.flush()
            aws(
                "lambda",
                "update-function-configuration",
                "--function-name",
                args.function_name,
                "--revision-id",
                config["RevisionId"],
                "--environment",
                "file://" + output.name,
            )
        aws("lambda", "wait", "function-updated", "--function-name", args.function_name)
    print("Dedicated state signing secret validated; broker code can be published.")


if __name__ == "__main__":
    main()
