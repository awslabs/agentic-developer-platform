"""Preserve the installer's authenticated runtime during image publication."""

from __future__ import annotations

import argparse
import json
import subprocess


def image_only(deployment: dict) -> bool:
    if not deployment:
        return False
    containers = {
        c["name"]: c for c in deployment["spec"]["template"]["spec"]["containers"]
    }
    installed = (
        deployment.get("metadata", {})
        .get("labels", {})
        .get("adp.aws-e.io/installation")
    )
    proxy = containers.get("authenticated-transport")
    if not installed and proxy is None:
        return False
    backend = containers.get("skypilot-api", {})
    if not (
        proxy
        and proxy.get("command") == ["python", "-m", "app.skypilot_proxy"]
        and backend.get("command") == ["python3", "/skypilot-bootstrap/bootstrap.py"]
        and "--host=127.0.0.1" in backend.get("args", [])
    ):
        raise ValueError(
            "Unrecognized installed SkyPilot runtime; use its installer for configuration changes"
        )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["inspect"])
    parser.add_argument("--namespace", required=True)
    args = parser.parse_args()
    result = subprocess.run(
        [
            "kubectl",
            "get",
            "deployment",
            "skypilot-api",
            "-n",
            args.namespace,
            "--ignore-not-found",
            "-o",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    preserve = image_only(json.loads(result.stdout) if result.stdout.strip() else {})
    print(str(preserve).lower())


if __name__ == "__main__":
    main()
