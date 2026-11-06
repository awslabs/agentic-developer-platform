#!/usr/bin/env python3
"""Apply the gateway release without resetting capacity owned by the HPA.

Compute the ordinary client-side apply result without persisting it, then update
that exact object with its observed resourceVersion. A concurrent scale or other
write refuses the release instead of overwriting newer state. Call after schema
migrations so their duration cannot make the replica observation stale.
"""

import argparse
import json
import re
import subprocess


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def command(namespace, args, document=None):
    result = subprocess.run(
        ["kubectl", "--request-timeout=30s", "-n", namespace, *args],
        input=json.dumps(document) if document is not None else None,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    # Manifests and API diagnostics may contain configuration values. Do not log
    # them or turn Forbidden/timeouts/conflicts into an apparent first install.
    require(result.returncode == 0, f"Gateway deployment {args[0]} failed; no unfenced fallback is permitted")
    return result.stdout


def identity(document, namespace):
    require(
        document.get("apiVersion") == "apps/v1"
        and document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == "bedrockgateway"
        and document["metadata"].get("namespace") == namespace,
        "Gateway deployment identity differs",
    )


def apply_deployment(manifest, image, namespace):
    require(re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", namespace), "A valid Kubernetes namespace is required")
    require(re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image), "A digest-pinned gateway image is required")
    desired = json.loads(command(namespace, ["create", "--dry-run=client", "--validate=false", "-f", manifest, "-o", "json"]))
    identity(desired, namespace)
    containers = [c for c in desired["spec"]["template"]["spec"]["containers"] if c.get("name") == "bedrockgateway"]
    require(len(containers) == 1, "Exactly one gateway container is required")
    containers[0]["image"] = image
    # Only a successful, explicitly NotFound-aware read authorizes first create.
    observed = command(namespace, ["get", "deployment", "bedrockgateway", "--ignore-not-found", "-o", "json"])
    if not observed.strip():
        desired["spec"]["replicas"] = 2
        command(namespace, ["create", "--save-config", "-f", "-"], desired)
        print("deployment.apps/bedrockgateway created (baseline replicas: 2)")
        return

    current = json.loads(observed)
    identity(current, namespace)
    require(not current["metadata"].get("deletionTimestamp"), "Gateway deployment is terminating")
    replicas = current["spec"].get("replicas")
    require(type(replicas) is int and replicas >= 0, "Gateway desired replicas are unreadable")
    binding = {key: current["metadata"].get(key) for key in ("uid", "resourceVersion")}
    require(all(isinstance(value, str) and value for value in binding.values()), "Gateway concurrency binding is missing")
    # Keep an explicit field: merely removing replicas from the manifest lets
    # client-side apply prune its last-applied value and default capacity to one.
    desired["spec"]["replicas"] = replicas
    candidate = json.loads(command(namespace, ["apply", "--dry-run=server", "-f", "-", "-o", "json"], desired))
    identity(candidate, namespace)
    require(
        all(candidate["metadata"].get(key) == value for key, value in binding.items()) and candidate["spec"].get("replicas") == replicas,
        "Gateway changed while preparing apply; rerun from fresh state",
    )
    template_changed = candidate["spec"].get("template") != current["spec"].get("template")
    # Retain server-computed defaults, pruning, managed fields and last-applied
    # configuration. The normal update endpoint preserves status independently.
    candidate.pop("status", None)
    # Ordinary `kubectl replace` rewrites last-applied from this full readback,
    # incorrectly adopting unrelated controller fields for future apply pruning.
    # Raw PUT preserves the exact dry-run result and its UID/resourceVersion fence.
    endpoint = f"/apis/apps/v1/namespaces/{namespace}/deployments/bedrockgateway?fieldManager=kubectl-client-side-apply"
    command(namespace, ["replace", "--raw", endpoint, "-f", "-"], candidate)
    action = "replaced" if template_changed else "unchanged"
    print(f"deployment.apps/bedrockgateway {action} (preserved desired replicas: {replicas})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--namespace", default="adp-gateway")
    args = parser.parse_args()
    apply_deployment(args.manifest, args.image, args.namespace)
