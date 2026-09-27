"""Update existing SkyPilot containers with optimistic concurrency checks.

The workflow validates the requested image against the reviewed release lock
before invoking this helper. It does not create infrastructure or change RBAC.
"""

import argparse
import json
import re
import subprocess


def update(namespace, image, dry_run=False, run=subprocess.run, transport_image=None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", namespace):
        raise ValueError("invalid namespace")
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("image must be digest-pinned")
    if transport_image and not re.fullmatch(
        r"[^\s@]+@sha256:[0-9a-f]{64}", transport_image
    ):
        raise ValueError("transport image must be digest-pinned")
    base = ["kubectl", "-n", namespace]

    def command(args):
        return run(base + args, check=True, text=True, capture_output=True)

    current = json.loads(
        command(["get", "deployment/skypilot-api", "-o", "json"]).stdout
    )
    containers = current["spec"]["template"]["spec"]["containers"]
    indices = [i for i, c in enumerate(containers) if c["name"] == "skypilot-api"]
    if len(indices) != 1:
        raise ValueError("existing deployment must contain one skypilot-api container")
    path = f"/spec/template/spec/containers/{indices[0]}/image"
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
    transport_changes = []
    if transport_image:
        transport = [
            i
            for i, c in enumerate(containers)
            if c["name"] == "authenticated-transport"
        ]
        if len(transport) != 1:
            raise ValueError(
                "existing deployment must contain one authenticated-transport container"
            )
        transport_path = f"/spec/template/spec/containers/{transport[0]}/image"
        transport_previous = containers[transport[0]]["image"]
        transport_changes = [
            {"op": "test", "path": transport_path, "value": transport_previous},
            {"op": "replace", "path": transport_path, "value": transport_image},
        ]
        patch.extend(transport_changes)
    args = [
        "patch",
        "deployment/skypilot-api",
        "--type=json",
        "-p",
        json.dumps(patch),
        "-o",
        "json",
    ]
    if dry_run:
        args.append("--dry-run=server")
    applied = json.loads(command(args).stdout)
    if not dry_run:
        try:
            command(["rollout", "status", "deployment/skypilot-api", "--timeout=300s"])
        except subprocess.CalledProcessError:
            # Refuse to overwrite a later deployment or another operator's image.
            rollback = [
                {
                    "op": "test",
                    "path": "/metadata/generation",
                    "value": applied["metadata"]["generation"],
                },
                {"op": "test", "path": path, "value": image},
                {"op": "replace", "path": path, "value": previous},
            ]
            if transport_changes:
                rollback.extend(
                    [
                        {
                            "op": "test",
                            "path": transport_path,
                            "value": transport_image,
                        },
                        {
                            "op": "replace",
                            "path": transport_path,
                            "value": transport_previous,
                        },
                    ]
                )
            command(
                [
                    "patch",
                    "deployment/skypilot-api",
                    "--type=json",
                    "-p",
                    json.dumps(rollback),
                ]
            )
            command(["rollout", "status", "deployment/skypilot-api", "--timeout=300s"])
            raise
    return {
        "namespace": namespace,
        "deployment": "skypilot-api",
        "previous_image": previous,
        "image": image,
        "transport_image": transport_image,
        "dry_run": dry_run,
        "rollout_verified": not dry_run,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--transport-image")
    args = parser.parse_args()
    print(
        json.dumps(
            update(
                args.namespace,
                args.image,
                args.dry_run,
                transport_image=args.transport_image,
            )
        )
    )
