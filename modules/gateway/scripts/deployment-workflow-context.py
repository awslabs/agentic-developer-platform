"""Publish bounded, non-secret identity evidence from an approved workflow."""

import json
import os
import re
import subprocess
from pathlib import Path


def context(env, read):
    workflow = env["CONTEXT_WORKFLOW"]
    if workflow not in {".github/workflows/gateway-deploy.yml", ".github/workflows/run-gateway-migrations.yml"}:
        raise ValueError("unapproved context workflow")
    source, revision = env["CONTEXT_SOURCE"], env["CONTEXT_REVISION"]
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (source, revision)):
        raise ValueError("immutable source and definition required")
    account = read(["sts", "get-caller-identity"])["Account"]
    if account != (env.get("CUSTOMER_ACCOUNT_ID") or env["ACCOUNT_ID"]):
        raise ValueError("deployment credential account differs from configuration")
    region = env["AWS_REGION"]
    cluster = env.get("EKS_CLUSTER") or f"adp-{env['ENVIRONMENT']}-eks-cluster"
    namespace = env.get("NAMESPACE") or "adp-gateway"
    actual = read(["eks", "describe-cluster", "--name", cluster, "--region", region])["cluster"]
    if actual["name"] != cluster or actual["arn"].split(":")[3:] != [region, account, "cluster/" + cluster]:
        raise ValueError("deployment cluster identity differs from configuration")
    names = ["account_id", "customer_account_id", "customer_aws_label", "customer_user_id", "environment"]
    if workflow.endswith("run-gateway-migrations.yml"):
        names.append("expected_image")
    return {
        "schema_version": 1,
        "repository_id": int(env["CONTEXT_REPOSITORY_ID"]),
        "run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "workflow_path": workflow,
        "workflow_revision": revision,
        "source_revision": source,
        "account_id": account,
        "region": region,
        "resource_kind": "eks-namespace",
        "resource_id": cluster + "/" + namespace,
        "inputs": {name: env["INPUT_" + name.upper()] for name in names},
        "correlation": env.get("CONTEXT_CORRELATION", ""),
    }


def main():
    def read(args):
        return json.loads(subprocess.check_output(["aws", *args, "--output", "json"], timeout=20))

    payload = json.dumps(context(os.environ, read), sort_keys=True).encode()
    if len(payload) > 65536:
        raise ValueError("workflow context too large")
    target = Path(os.environ["RUNNER_TEMP"]) / "adp-deployment-context" / "deployment-context.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


if __name__ == "__main__":
    main()
