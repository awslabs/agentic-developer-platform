"""Download a named release through its signed S3 context and archive receipt."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path

from tests.e2e.product_onboarding.product_completion_regression import release_artifact, select_manifest


def download(environment, destination, *, store, github):
    from src.orchestration.deployment_workflow_provider import WorkflowContext

    release_id = environment["PRODUCT_RELEASE_ID"]
    run_id, _ = release_artifact(release_id)
    attempt = int(release_id.rsplit("-", 1)[1])
    repository = environment["GITHUB_REPOSITORY"]
    repository_id = int(environment["GITHUB_REPOSITORY_ID"])
    run = github(f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}")
    workflow = ".github/workflows/product-profile-release.yml"
    if (run.get("id") != int(run_id) or run.get("run_attempt") != attempt
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or run.get("repository", {}).get("id") != repository_id
            or run.get("head_branch") != "main" or run.get("path") != workflow):
        raise ValueError("Named release does not identify a successful main workflow attempt")
    expected = dict(
        account=environment["ACCOUNT_ID"], environment=environment["ENVIRONMENT"],
        region=environment["PRODUCT_TARGET_REGION"], repository=repository,
        repository_id=repository_id, run_id=int(run_id), attempt=attempt,
        workflow_path=workflow, workflow_revision=run["head_sha"],
    )
    context_result = store.read(kind="context", name="product-profile-release.yml", **expected)
    if context_result is None:
        raise ValueError("Named release has no authenticated workflow context")
    context = WorkflowContext.model_validate_json(context_result[0])
    for field, value in {
        "repository_id": repository_id, "run_id": int(run_id), "run_attempt": attempt,
        "workflow_path": workflow, "workflow_revision": run["head_sha"],
        "account_id": expected["account"], "region": expected["region"],
    }.items():
        if getattr(context, field) != value:
            raise ValueError("Named release context identity differs from its signed attempt")
    if context.inputs.get("environment") != expected["environment"]:
        raise ValueError("Named release context targets another environment")
    result = store.read_archive(
        source_revision=context.source_revision, kind="workflow-artifact",
        name="product-profile-manifest", **expected,
    )
    if result is None:
        raise ValueError("Named release has no authenticated manifest archive")
    with zipfile.ZipFile(io.BytesIO(result[0])) as archive:
        entries = archive.infolist()
        if (len(entries) != 1 or entries[0].filename != "manifest.json"
                or not 0 < entries[0].file_size <= 65536):
            raise ValueError("Named release archive must contain one bounded manifest")
        payload = archive.read(entries[0])
    if (hashlib.sha256(payload).hexdigest() != environment["PRODUCT_RELEASE_MANIFEST_SHA256"]
            or json.loads(payload).get("source_revision") != context.source_revision):
        raise ValueError("Named release manifest digest or source differs from its selection")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(payload)
    return select_manifest(
        destination, release_id=release_id,
        manifest_sha256=environment["PRODUCT_RELEASE_MANIFEST_SHA256"], environment=environment,
    )


def main():
    from deployment_evidence.store import EvidenceStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    download(os.environ, args.destination, store=EvidenceStore(), github=lambda path: json.loads(
        subprocess.check_output(["gh", "api", path], timeout=30)
    ))


if __name__ == "__main__":
    main()
