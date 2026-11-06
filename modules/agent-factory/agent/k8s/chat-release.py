"""Produce bounded chat context and hashed live release evidence for D2/D3."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

KEYS = (
    "scaledjob/chat-agent-worker",
    "job/adp-chat-supervisor-once",
    "configmap/chat-crew-pod-template",
    "validatingadmissionpolicy/adp-chat-sandbox-template",
    "validatingadmissionpolicybinding/adp-chat-sandbox-template",
    "validatingadmissionpolicy/adp-chat-supervisor-pods",
    "validatingadmissionpolicybinding/adp-chat-supervisor-pods",
    "validatingadmissionpolicy/adp-crew-pod-template",
    "validatingadmissionpolicybinding/adp-crew-pod-template",
    "networkpolicy/chat-sandbox-deny",
)


def sha(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def checked(env, read):
    source = env["ADP_RELEASE_SOURCE"]
    revision = env["ADP_RELEASE_DEFINITION"]
    if not re.fullmatch(r"[a-f0-9]{40}", source) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("immutable chat source and definition required")
    account = read(["aws", "sts", "get-caller-identity"])["Account"]
    if account != env["ACCOUNT_ID"]:
        raise ValueError("chat deployment account differs from credentials")
    cluster = read(["aws", "eks", "describe-cluster", "--name", env["EKS_CLUSTER"]])["cluster"]
    if (
        cluster["name"] != env["EKS_CLUSTER"]
        or cluster["arn"] != f"arn:aws:eks:{env['AWS_REGION']}:{account}:cluster/{cluster['name']}"
    ):
        raise ValueError("chat deployment cluster differs from credentials")
    return {
        "schema_version": 1,
        "repository_id": int(env["GITHUB_REPOSITORY_ID"]),
        "run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "workflow_path": ".github/workflows/chat-agent-deploy.yml",
        "workflow_revision": revision,
        "source_revision": source,
        "account_id": account,
        "region": env["AWS_REGION"],
        "resource_id": cluster["name"] + "/adp-gateway-agents",
    }


def context(env, read):
    return {
        **checked(env, read),
        "resource_kind": "eks-namespace",
        "inputs": {
            "legacy_security_update": env.get("INPUT_LEGACY_SECURITY_UPDATE", "False"),
            "legacy_dry_run": env.get("INPUT_LEGACY_DRY_RUN", "True"),
            "crew_controls_only": env.get("INPUT_CREW_CONTROLS_ONLY", "False"),
            "skip_deploy": env.get("INPUT_SKIP_DEPLOY", "False"),
            "adp_approved_revision": env.get("INPUT_APPROVED_REVISION", ""),
            "manual_source_revision": env.get("INPUT_MANUAL_SOURCE_REVISION", ""),
            "customer_account_id": env.get("INPUT_CUSTOMER_ACCOUNT_ID", ""),
            "customer_user_id": env.get("INPUT_CUSTOMER_USER_ID", ""),
            "customer_aws_label": env.get("INPUT_CUSTOMER_AWS_LABEL", ""),
        },
        "correlation": env.get("ADP_CORRELATION", ""),
    }


def release(env, read, component):
    if component not in {"agent-worker", "chat-worker"}:
        raise ValueError("unsupported chat release component")
    data = checked(env, read)
    images = {}
    for key in ("SANDBOX_IMAGE", "SUPERVISOR_IMAGE"):
        image = env[key]
        match = re.fullmatch(
            r"([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/adp-chat-agent@(sha256:[a-f0-9]{64})",
            image,
        )
        if not match or (match[1], match[2]) != (data["account_id"], data["region"]):
            raise ValueError("chat release images must be pinned in the verified registry")
        images[key] = match[3]
    if images["SANDBOX_IMAGE"] == images["SUPERVISOR_IMAGE"]:
        raise ValueError("supervisor and sandbox may not share an image")
    assets = {}
    for key in KEYS:
        kind, name = key.split("/", 1)
        args = ["kubectl", "get", kind, name]
        if kind not in {"validatingadmissionpolicy", "validatingadmissionpolicybinding"}:
            args += ["-n", "adp-gateway-agents"]
        value = read(args)
        if value.get("metadata", {}).get("name") != name or value["metadata"].get(
            "deletionTimestamp"
        ):
            raise ValueError("chat release object is missing or being deleted")
        assets[key] = sha(value.get("data") if kind == "configmap" else value.get("spec"))
    return {
        **data,
        "component": component,
        "image_digest": images["SANDBOX_IMAGE"],
        "supervisor_image_digest": images["SUPERVISOR_IMAGE"],
        "manifests_sha256": sha(assets),
        "assets": assets,
        "produced_at": datetime.now(UTC).isoformat(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("context", "release"))
    parser.add_argument("component", nargs="?")
    args = parser.parse_args()

    def read(command):
        return json.loads(
            subprocess.check_output(
                [*command, "--output", "json"] if command[0] == "aws" else [*command, "-o", "json"],
                timeout=30,
            )
        )

    if args.kind == "context":
        if args.component is not None:
            raise ValueError("context does not select a component")
        payload = context(os.environ, read)
        target = (
            Path(os.environ["RUNNER_TEMP"]) / "adp-deployment-context" / "deployment-context.json"
        )
    else:
        payload = release(os.environ, read, args.component)
        target = (
            Path(os.environ["RUNNER_TEMP"]) / ("adp-release-" + args.component) / "release.json"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
