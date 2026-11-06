"""Record bounded knowledge-door deployment identity after the engine rollout succeeds."""

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path


WORKFLOW = ".github/workflows/agent-context-deploy.yml"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
CORRELATION = re.compile(
    r"[0-9a-f]{64}|orch:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
SHA = re.compile(r"[0-9a-f]{40}")


def evidence(env, read):
    source = env["ADP_SOURCE_REVISION"]
    revision = env["ADP_WORKFLOW_REVISION"]
    correlation = env["ADP_CORRELATION"]
    # Same correlation vocabulary as the workflow guard and the tools dispatch guard:
    # a 64-hex digest, or the engine run id the operations dispatch correlates by.
    if (
        not SHA.fullmatch(source)
        or not SHA.fullmatch(revision)
        or not CORRELATION.fullmatch(correlation)
    ):
        raise ValueError("Engine delivery identity is invalid")
    account = read(["aws", "sts", "get-caller-identity"])["Account"]
    if not re.fullmatch(r"[0-9]{12}", account) or account != env["ACCOUNT_ID"]:
        raise ValueError("Deployment account differs from selected target")
    environment = env["ENVIRONMENT"]
    region = env["AWS_REGION"]
    namespace = env["NAMESPACE"]
    cluster_name = f"adp-{environment}-eks-cluster"
    cluster = read(["aws", "eks", "describe-cluster", "--name", cluster_name, "--region", region])[
        "cluster"
    ]
    if (
        cluster["name"] != cluster_name
        or cluster["arn"] != f"arn:aws:eks:{region}:{account}:cluster/{cluster_name}"
    ):
        raise ValueError("Deployment cluster differs from selected target")
    repository = (
        f"{account}.dkr.ecr.{region}.amazonaws.com/adp-{environment}-agent-context-context-mcp"
    )
    details = read(
        [
            "aws",
            "ecr",
            "describe-images",
            "--repository-name",
            f"adp-{environment}-agent-context-context-mcp",
            "--image-ids",
            f"imageTag={source}",
            "--region",
            region,
        ]
    )["imageDetails"]
    if len(details) != 1 or source not in details[0].get("imageTags", []):
        raise ValueError("Release image is not tagged with approved source")
    digest = details[0]["imageDigest"]
    if not DIGEST.fullmatch(digest):
        raise ValueError("Release image digest is invalid")
    expected_image = f"{repository}@{digest}"
    deployment = read(
        ["kubectl", "get", "deployment", "context-mcp", "-n", namespace, "-o", "json"]
    )
    containers = deployment["spec"]["template"]["spec"]["containers"]
    if (
        len(containers) != 1
        or containers[0]["name"] != "context-mcp"
        or containers[0]["image"] != expected_image
    ):
        raise ValueError("Deployed Door image differs from approved source")
    key_digest = deployment["spec"]["template"]["metadata"]["annotations"].get(
        "adp.dev/door-verification-keys"
    )
    if not isinstance(key_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", key_digest):
        raise ValueError("Door identity keys are not recorded in the Deployment")
    pods = read(
        [
            "kubectl",
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            "app.kubernetes.io/name=context-mcp",
            "-o",
            "json",
        ]
    )["items"]
    if not pods or any(
        len(pod["status"].get("containerStatuses", [])) != 1
        or pod["status"]["containerStatuses"][0].get("name") != "context-mcp"
        or not pod["status"]["containerStatuses"][0].get("ready")
        or pod["status"]["containerStatuses"][0].get("imageID", "").split("@")[-1] != digest
        for pod in pods
    ):
        raise ValueError("Running Door replicas do not match approved image digest")
    identity = {
        "repository_id": int(env["GITHUB_REPOSITORY_ID"]),
        "run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "source_revision": source,
        "workflow_revision": revision,
        "workflow_path": WORKFLOW,
        "account_id": account,
        "region": region,
        "resource_id": f"{cluster_name}/{namespace}",
    }
    if min(identity["repository_id"], identity["run_id"], identity["run_attempt"]) < 1:
        raise ValueError("Run identity must be positive")
    context = {
        "schema_version": 1,
        **identity,
        "resource_kind": "eks-namespace",
        "inputs": {
            "environment": env["ADP_INPUT_ENVIRONMENT"],
            "customer_account_id": env["ADP_INPUT_CUSTOMER_ACCOUNT_ID"],
            "customer_aws_label": env["ADP_INPUT_CUSTOMER_AWS_LABEL"],
            "customer_user_id": env["ADP_INPUT_CUSTOMER_USER_ID"],
        },
        "correlation": correlation,
    }
    release = {
        "schema_version": 1,
        **identity,
        "component": "knowledge-door",
        "image_digest": digest,
        "door_identity_sha256": key_digest,
        "assets": {},
        "produced_at": datetime.now(UTC).isoformat(),
    }
    return context, release


def main():
    def read(parts):
        return json.loads(
            subprocess.check_output(
                [*parts, "--output", "json"] if parts[0] == "aws" else parts, timeout=20
            )
        )

    context, release = evidence(os.environ, read)
    directory = Path(os.environ["RUNNER_TEMP"])
    for name, filename, payload in (
        ("adp-deployment-context", "deployment-context.json", context),
        ("adp-release-knowledge-door", "release.json", release),
    ):
        encoded = json.dumps(payload, sort_keys=True).encode()
        if len(encoded) > 65536:
            raise ValueError("Deployment evidence exceeds bounded artifact size")
        destination = directory / name / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(encoded)


if __name__ == "__main__":
    main()
