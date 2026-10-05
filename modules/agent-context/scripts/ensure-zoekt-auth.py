#!/usr/bin/env python3
"""Create a private Door-to-Zoekt key once; preserve existing keys on redeploy."""

import argparse
import base64
import json
import secrets
import subprocess


def ensure_key(namespace: str) -> None:
    def read():
        result = subprocess.run(
            [
                "kubectl",
                "get",
                "secret",
                "zoekt-backend-auth",
                "-n",
                namespace,
                "--ignore-not-found",
                "-o",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout) if result.stdout.strip() else None

    existing = read()
    if existing is None:
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {"name": "zoekt-backend-auth", "namespace": namespace},
            "data": {"api-key": base64.b64encode(secrets.token_urlsafe(48).encode()).decode()},
        }
        # Never put credentials in argv, logs, annotations, or a local file.
        result = subprocess.run(
            ["kubectl", "create", "-f", "-"],
            input=json.dumps(manifest),
            capture_output=True,
            text=True,
        )
        # A competing deploy may have created the secret. Read and validate the
        # winner without replacing it or resetting credentials during a rollout.
        existing = read()
        if existing is None:
            raise RuntimeError(
                f"Could not provision Zoekt authentication (kubectl status {result.returncode})"
            )
    value = base64.b64decode(existing.get("data", {}).get("api-key", ""), validate=True).decode()
    if len(value) < 32 or value.upper().startswith("PLACEHOLDER"):
        raise RuntimeError(
            "Existing Zoekt key is invalid; rotate it through coordinated maintenance"
        )
    print("Zoekt backend authentication is configured")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    ensure_key(parser.parse_args().namespace)
