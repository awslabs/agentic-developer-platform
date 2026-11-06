"""Write a bounded release receipt only for the deployed, ECR-resolved image."""

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

WORKFLOW = ".github/workflows/tools-facade-deploy.yml"


def evidence(environment: dict[str, str]) -> tuple[dict, dict]:
    source = environment["ADP_SOURCE_REVISION"]
    definition = environment["ADP_WORKFLOW_REVISION"]
    if not all(re.fullmatch(r"[a-f0-9]{40}", revision) for revision in (source, definition)):
        raise ValueError("An immutable source and workflow revision are required")
    account = environment["ADP_ACCOUNT_ID"]
    region = environment["AWS_REGION"]
    image = environment["DEPLOYED_IMAGE"]
    if not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(r"[a-z0-9-]+", region):
        raise ValueError("An explicit AWS account and region are required")
    prefix = f"{account}.dkr.ecr.{region}.amazonaws.com/adp-tools-facade@"
    if not image.startswith(prefix) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image[len(prefix) :]):
        raise ValueError("The running façade must use a digest from its own ECR repository")
    digest = image[len(prefix) :]
    if digest != environment["OBSERVED_ECR_DIGEST"]:
        raise ValueError("The deployed image digest does not exist in the target ECR repository")
    run_identity = {
        "repository_id": int(environment["GITHUB_REPOSITORY_ID"]),
        "run_id": int(environment["GITHUB_RUN_ID"]),
        "run_attempt": int(environment["GITHUB_RUN_ATTEMPT"]),
    }
    if min(run_identity.values()) < 1:
        raise ValueError("The release requires a positive run identity")
    deployment_environment = environment["ADP_ENVIRONMENT"]
    if deployment_environment not in ("dev", "prod"):
        raise ValueError("Unknown deployment environment")
    identity = {
        **run_identity,
        "source_revision": source,
        "workflow_revision": definition,
        "workflow_path": WORKFLOW,
        "account_id": account,
        "region": region,
        "resource_id": f"{environment['ADP_TOOLS_EKS_CLUSTER_NAME']}/{environment['ADP_TOOLS_NAMESPACE']}/tools-facade",
    }
    rollback_digest = environment.get("ADP_TOOLS_FACADE_ROLLBACK_DIGEST", "")
    if rollback_digest and rollback_digest != digest:
        raise ValueError("Rollback receipt differs from the requested digest")
    inputs = {"environment": deployment_environment}
    if rollback_digest:
        inputs["rollback_to_digest"] = rollback_digest
    context = {
        "schema_version": 1,
        **identity,
        "resource_kind": "eks-deployment",
        "inputs": inputs,
        "correlation": environment.get("ADP_CORRELATION", ""),
    }
    release = {
        "schema_version": 1,
        **identity,
        "component": "tools-facade",
        "image_digest": digest,
        "assets": {},
        "produced_at": datetime.now(UTC).isoformat(),
    }
    return context, release


def main() -> None:
    context, release = evidence(os.environ)
    directory = Path(os.environ["RUNNER_TEMP"])
    for folder, filename, payload in (
        ("adp-deployment-context", "deployment-context.json", context),
        ("adp-release-tools-facade", "release.json", release),
    ):
        target = directory / folder / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, sort_keys=True).encode()
        if len(encoded) > 65536:
            raise ValueError("Release artifact is too large")
        target.write_bytes(encoded)


if __name__ == "__main__":
    main()
