"""Read-only Stage 1 release inventory, run from the existing deployment runner."""

import json
import os
import subprocess


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def kubernetes(kind, name, namespace):
    result = subprocess.run(
        ["kubectl", "get", kind, name, "-n", namespace, "-o", "json", "--ignore-not-found", "--request-timeout=15s"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


identity = json.loads(run("aws", "sts", "get-caller-identity", "--output", "json"))
expected = os.environ.get("CUSTOMER_ACCOUNT_ID") or os.environ["ACCOUNT_ID"]
if identity["Account"] != expected:
    raise SystemExit("Refused: runner identity does not match deployment target")
run("aws", "eks", "update-kubeconfig", "--name", "adp-dev-eks-cluster", "--region", "us-east-1")
gateway = kubernetes("deployment", "bedrockgateway", "adp-gateway")
config = kubernetes("configmap", "bedrockgateway-config", "adp-gateway")
if config is None:
    config = kubernetes("configmap", "gateway-config", "adp-gateway")
workers = kubernetes("scaledjob", "agent-scaledjob", "adp-agents")
keys = {
    "AGENT_AUTHORITY_ENABLED",
    "ADP_WORK_CLAIMS_ENABLED",
    "BG_ORCH_DISPATCH_REPO",
    "AGENT_WORKER_IMAGE_DIGESTS",
    "AGENT_WORKER_SERVICE_ACCOUNT",
    "AGENT_AUTHORITY_TABLE",
}
report = {
    "identity": {k: identity[k] for k in ("Account", "Arn")},
    "gateway_images": [c.get("image") for c in (gateway or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])],
    "gateway_flags": {k: v for k, v in (config or {}).get("data", {}).items() if k in keys},
    "signing_secret_exists": kubernetes("secret", "agent-authority-signing", "adp-gateway") is not None,
    "worker_template": [
        {
            "name": c.get("name"),
            "image": c.get("image"),
            "flags": [
                e
                for e in c.get("env", [])
                if e.get("name") in {"ADP_AGENT_AUTHORITY_ENABLED", "ADP_WORK_CLAIMS_ENABLED", "ADP_AGENT_CONTROL_ENDPOINT"}
            ],
        }
        for c in (workers or {}).get("spec", {}).get("jobTargetRef", {}).get("template", {}).get("spec", {}).get("containers", [])
    ],
}
print(json.dumps(report, indent=2))
