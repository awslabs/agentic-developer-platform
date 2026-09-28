#!/usr/bin/env python3
"""Prepare a narrow shared-worker patch and its durable Terraform inputs.

Reads a kubectl JSON snapshot and trusted deployment URLs; never contacts or
changes the cluster. Apply the reviewed JSON patch separately. Retain the
generated account-specific overlay with the rollout record and pass it to the
next managed reconciliation; shared dev inputs also serve customer accounts.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse


def prepare(snapshot: dict, *, account_id: str, image: str, control_endpoint: str, gitlab_url: str) -> tuple[list[dict], dict]:
    if not re.fullmatch(r"[0-9]{12}", account_id):
        raise ValueError("A verified target AWS account is required")
    if not re.fullmatch(rf"{account_id}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/adp-agent-runtime@sha256:[0-9a-f]{{64}}", image):
        raise ValueError("Pin an immutable agent-runtime image in the verified account")
    for url in (control_endpoint, gitlab_url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Endpoints must be trusted deployment-owned HTTPS URLs")
    if not control_endpoint.rstrip("/").endswith("/internal/v1/agent"):
        raise ValueError("The control endpoint must preserve the internal agent route")
    metadata = snapshot["metadata"]
    if snapshot.get("kind") != "ScaledJob" or metadata["name"] != "agent-scaledjob" or metadata["namespace"] != "adp-agents":
        raise ValueError("Only the existing shared agent ScaledJob is supported")
    spec = snapshot["spec"]["jobTargetRef"]["template"]["spec"]
    if spec.get("serviceAccountName") != "agent-scaledjob-sa":
        raise ValueError("The existing shared worker service account is required")
    containers = spec["containers"]
    matches = [i for i, c in enumerate(containers) if c["name"] == "agent-worker"]
    if len(matches) != 1:
        raise ValueError("Exactly one agent-worker container is required")
    index = matches[0]
    worker = containers[index]
    env = worker.get("env", [])
    if any(e["name"] == "ADP_AGENT_AUTHORITY_ENABLED" and e.get("value", "").lower() == "true" for e in env):
        raise ValueError("Protected authority must remain disabled")
    base = f"/spec/jobTargetRef/template/spec/containers/{index}"
    patch = [
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "test", "path": base + "/name", "value": "agent-worker"},
        {"op": "test", "path": base + "/image", "value": worker["image"]},
    ]
    if worker["image"] != image:
        patch.append({"op": "replace", "path": base + "/image", "value": image})
    updates = {"ADP_AGENT_CONTROL_ENDPOINT": control_endpoint.rstrip("/"), "GITLAB_URL": gitlab_url.rstrip("/")}
    if "env" not in worker:
        patch.append({"op": "add", "path": base + "/env", "value": []})
    for name, value in updates.items():
        entries = [i for i, entry in enumerate(env) if entry["name"] == name]
        if len(entries) > 1:
            raise ValueError(f"Duplicate runtime environment entry: {name}")
        entry = {"name": name, "value": value}
        if entries:
            if env[entries[0]] != entry:
                patch.append({"op": "replace", "path": base + f"/env/{entries[0]}", "value": entry})
        else:
            patch.append({"op": "add", "path": base + "/env/-", "value": entry})
    # The endpoint itself is already emitted by agent-authority-bootstrap.tf;
    # gitlab/url is read from SSM by scaledjob.tf. Do not copy URLs into tfvars.
    return patch, {"agent_image": image, "gitlab_webhook_enabled": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "account-id", "image", "control-endpoint", "gitlab-url", "patch-out", "tfvars-out"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    patch, overlay = prepare(json.loads(Path(args.snapshot).read_text()), account_id=args.account_id, image=args.image, control_endpoint=args.control_endpoint, gitlab_url=args.gitlab_url)
    Path(args.patch_out).write_text(json.dumps(patch, indent=2) + "\n")
    target = Path(args.tfvars_out)
    existing = json.loads(target.read_text()) if target.exists() else {}
    target.write_text(json.dumps(existing | overlay, indent=2) + "\n")
    print(f"Prepared {args.patch_out} and {args.tfvars_out}; no runtime changes made")


if __name__ == "__main__":
    main()
