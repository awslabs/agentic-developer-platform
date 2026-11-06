"""Record a run-bound Insights release after the owning deployment succeeds."""

import hashlib
import json
import os
import pathlib
import re
import subprocess
from datetime import UTC, datetime


MODULE = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/insights-deploy.yml"


def evidence(env, read):
    source = env["ADP_SOURCE_REVISION"]
    revision = env["ADP_WORKFLOW_REVISION"]
    if not all(re.fullmatch(r"[a-f0-9]{40}", value) for value in (source, revision)):
        raise ValueError("Immutable source and workflow revisions are required")
    account = read(["sts", "get-caller-identity"])["Account"]
    if not re.fullmatch(r"[0-9]{12}", account) or account != env["ADP_ACCOUNT_ID"]:
        raise ValueError("Deployment identity differs from the selected account")
    environment = env["ADP_ENVIRONMENT"]
    region = env["AWS_REGION"]
    cluster_name = f"adp-{environment}-eks-cluster"
    cluster = read(["eks", "describe-cluster", "--name", cluster_name, "--region", region])["cluster"]
    if cluster["name"] != cluster_name or cluster["arn"] != f"arn:aws:eks:{region}:{account}:cluster/{cluster_name}":
        raise ValueError("Deployment cluster differs from selected target")
    repository_id = int(env["GITHUB_REPOSITORY_ID"])
    run_id = int(env["GITHUB_RUN_ID"])
    run_attempt = int(env["GITHUB_RUN_ATTEMPT"])
    if min(repository_id, run_id, run_attempt) < 1:
        raise ValueError("Run identity must be positive")
    identity = {
        "repository_id": repository_id,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "source_revision": source,
        "workflow_revision": revision,
        "workflow_path": WORKFLOW,
        "account_id": account,
        "region": region,
        "resource_id": cluster_name + "/adp-insights",
    }
    inputs = {name: env["ADP_INPUT_" + name.upper()] for name in ("environment", "stage", "mode", "image")}
    context = {
        "schema_version": 1,
        **identity,
        "resource_kind": "eks-namespace",
        "inputs": inputs,
        "correlation": env.get("ADP_CORRELATION", ""),
    }
    assets = {
        path.relative_to(MODULE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((MODULE / "platform").glob("*.tf"))
    }
    if not assets:
        raise ValueError("Infrastructure definition is missing")
    image = inputs["image"]
    if image and not re.fullmatch(r"[a-zA-Z0-9._:/-]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("Release image is not immutable")
    release = {
        "schema_version": 1,
        **identity,
        "component": "insights",
        "image_digest": image.rsplit("@", 1)[-1] if image else None,
        "assets": assets,
        "produced_at": datetime.now(UTC).isoformat(),
    }
    return context, release


def main():
    def read(parts):
        return json.loads(subprocess.check_output(["aws", *parts, "--output", "json"], timeout=20))

    context, release = evidence(os.environ, read)
    directory = pathlib.Path(os.environ["RUNNER_TEMP"])
    for name, filename, payload in (
        ("adp-deployment-context", "deployment-context.json", context),
        ("adp-release-insights", "release.json", release),
    ):
        destination = directory / name / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, sort_keys=True).encode()
        if len(encoded) > 65536:
            raise ValueError("Evidence exceeds the bounded artifact size")
        destination.write_bytes(encoded)


if __name__ == "__main__":
    main()
