"""Promote the reviewed legacy gateway image within its existing namespace."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import time

NAMESPACE = "adp-gateway-agents"
TARGET = "scaledjob/agent-gateway-worker"


def promote(publication, dry_run=False, run=subprocess.run):
    image = publication["reference"]
    if not re.fullmatch(
        r"879318057152\.dkr\.ecr\.us-east-1\.amazonaws\.com/adp-agent-gateway@sha256:[0-9a-f]{64}",
        image,
    ):
        raise ValueError("Expected the reviewed legacy gateway repository and digest")

    def command(args, **kwargs):
        return run(
            ["kubectl", "-n", NAMESPACE] + args,
            check=True,
            text=True,
            capture_output=True,
            **kwargs,
        )

    current = json.loads(command(["get", TARGET, "-o", "json"]).stdout)
    containers = current["spec"]["jobTargetRef"]["template"]["spec"]["containers"]
    indices = [i for i, c in enumerate(containers) if c["name"] == "agent-worker"]
    if len(indices) != 1:
        raise ValueError("Expected one existing agent-worker container")
    path = f"/spec/jobTargetRef/template/spec/containers/{indices[0]}/image"
    previous = containers[indices[0]]["image"]
    patch = [
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": current["metadata"]["resourceVersion"],
        },
        {"op": "test", "path": path, "value": previous},
        {"op": "replace", "path": path, "value": image},
    ]
    name = "security27-legacy-gateway-" + str(time.time_ns())
    code = "import os,subprocess,anthropic,boto3,anyio; assert os.getuid()==10001; assert 'aws-cli/2.37.4 Python/3.14.7' in subprocess.check_output(['aws','--version'],text=True); print('legacy gateway native imports and CLI passed')"
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {"app.kubernetes.io/name": "security27-legacy-gateway-check"},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 180,
            "ttlSecondsAfterFinished": 600,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "check",
                            "image": image,
                            "command": ["python", "-c", code],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi"},
                            },
                        }
                    ],
                }
            },
        },
    }
    command(["create", "--dry-run=server", "-f", "-", "-o", "json"], input=json.dumps(job))
    command(
        ["patch", TARGET, "--type=json", "-p", json.dumps(patch), "--dry-run=server", "-o", "json"]
    )
    if dry_run:
        return {"image": image, "previous_image": previous, "dry_run": True}
    command(["create", "-f", "-", "-o", "json"], input=json.dumps(job))
    command(["wait", "--for=condition=complete", "job/" + name, "--timeout=180s"])
    pods = json.loads(command(["get", "pods", "-l", "job-name=" + name, "-o", "json"]).stdout)
    image_ids = [
        c["imageID"]
        for p in pods["items"]
        for c in p["status"].get("containerStatuses", [])
        if c["name"] == "check" and c.get("state", {}).get("terminated", {}).get("exitCode") == 0
    ]
    allowed = {image, image.split("@")[0] + "@" + publication["platform_digest"]}
    if len(image_ids) != 1 or image_ids[0] not in allowed:
        raise ValueError("Test Job did not execute the verified manifest or platform digest")
    # KEDA can change status while the test Job runs. Permit status updates,
    # but refuse any concurrent template/spec change before taking a fresh CAS.
    fresh = json.loads(command(["get", TARGET, "-o", "json"]).stdout)
    if fresh["metadata"]["generation"] != current["metadata"]["generation"]:
        raise ValueError("ScaledJob spec changed during the test Job")
    fresh_image = next(
        c["image"]
        for c in fresh["spec"]["jobTargetRef"]["template"]["spec"]["containers"]
        if c["name"] == "agent-worker"
    )
    if fresh_image != previous:
        raise ValueError("ScaledJob image changed during the test Job")
    patch[0]["value"] = fresh["metadata"]["resourceVersion"]
    command(["patch", TARGET, "--type=json", "-p", json.dumps(patch), "-o", "json"])
    after = json.loads(command(["get", TARGET, "-o", "json"]).stdout)
    selected = next(
        c["image"]
        for c in after["spec"]["jobTargetRef"]["template"]["spec"]["containers"]
        if c["name"] == "agent-worker"
    )
    if selected != image:
        raise ValueError("Desired image read-back differs from the reviewed release")
    return {
        "image": image,
        "previous_image": previous,
        "dry_run": False,
        "test_job": name,
        "observed_image_ids": image_ids,
        "desired_image_verified": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    publication = json.loads(
        (root / "docs/security/runs/2026-09-27/legacy-gateway/publication.json").read_text()
    )
    print(json.dumps(promote(publication, args.dry_run)))
