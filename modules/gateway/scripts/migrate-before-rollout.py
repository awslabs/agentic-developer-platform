#!/usr/bin/env python3
"""Run the release's migrations before changing any serving pod template.

The caller supplies the rendered Deployment and pinned release image. The Job
inherits its database configuration and service account, but no Service labels,
ports or server probes. Failure stops the workflow before gateway/Lambda rollout.
"""

import argparse
import copy
import json
import subprocess
import time
import uuid


def command(args, *, input_text=None):
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError(f"kubectl operation failed ({result.returncode}); inspect the migration Job")
    return result.stdout


def migration_job(deployment, image, namespace, name):
    if not image or image == "REPLACE_WITH_GATEWAY_IMAGE":
        raise ValueError("A pinned release image is required")
    spec = copy.deepcopy(deployment["spec"]["template"]["spec"])
    container = next(c for c in spec["containers"] if c["name"] == "bedrockgateway")
    container.update(image=image, command=["env", "PYTHONPATH=/app", "alembic"], args=["upgrade", "head"])
    for key in ("ports", "readinessProbe", "livenessProbe", "startupProbe", "lifecycle"):
        container.pop(key, None)
    spec.update(containers=[container], restartPolicy="Never")
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 900,
            "ttlSecondsAfterFinished": 3600,
            "template": {"metadata": {"labels": {"adp-task": "gateway-migration"}}, "spec": spec},
        },
    }


def rendered_migration_job(manifest, image, namespace):
    deployment = json.loads(
        command(
            [
                "kubectl",
                "create",
                "--dry-run=client",
                "--validate=false",
                "-f",
                manifest,
                "-o",
                "json",
            ]
        )
    )
    name = f"gateway-migrate-{uuid.uuid4().hex[:12]}"
    return migration_job(deployment, image, namespace, name)


def migrate(manifest, image, namespace):
    job = rendered_migration_job(manifest, image, namespace)
    name = job["metadata"]["name"]
    command(["kubectl", "create", "-f", "-"], input_text=json.dumps(job))
    print(f"Waiting for {namespace}/{name} before serving {image}", flush=True)
    deadline = time.monotonic() + 930
    while time.monotonic() < deadline:
        state = json.loads(command(["kubectl", "get", "job", name, "-n", namespace, "-o", "json"]))
        conditions = state.get("status", {}).get("conditions", [])
        if any(c.get("type") == "Failed" and c.get("status") == "True" for c in conditions):
            raise RuntimeError(f"Migration failed: inspect Job {namespace}/{name}. Serving release was not changed.")
        if any(c.get("type") == "Complete" and c.get("status") == "True" for c in conditions):
            print(f"Migrations completed: {namespace}/{name}", flush=True)
            return
        time.sleep(10)
    raise RuntimeError(f"Migration wait expired: inspect Job {namespace}/{name}. Serving release was not changed.")


def verify_admission(manifest, image, namespace):
    job = rendered_migration_job(manifest, image, namespace)
    command(["kubectl", "create", "--dry-run=server", "-f", "-"], input_text=json.dumps(job))
    print(f"Migration Job is admissible in {namespace}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--namespace", default="adp-gateway")
    parser.add_argument("--verify-admission", action="store_true")
    args = parser.parse_args()
    if args.verify_admission:
        verify_admission(args.manifest, args.image, args.namespace)
    else:
        migrate(args.manifest, args.image, args.namespace)
