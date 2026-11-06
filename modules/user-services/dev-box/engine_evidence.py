"""Bind verified Dev Box observations to an exact engine workflow run."""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path


def engine_evidence(manifest: dict, receipt: dict, environment: dict) -> tuple[dict, dict]:
    identity = ("source_revision", "definition_revision", "account_id", "region", "module_sha256")
    if any(receipt.get(key) != manifest.get(key) for key in identity):
        raise ValueError("runtime receipt does not belong to the selected release")
    if receipt.get("release_manifest_sha256") != manifest.get("manifest_sha256"):
        raise ValueError("runtime receipt release hash mismatch")
    if receipt.get("component") != "dev-box" or receipt.get("launch_template_version", 0) < 1:
        raise ValueError("verified Dev Box template required")
    if environment["INPUT_CUSTOMER_ACCOUNT_ID"] != receipt["account_id"]:
        raise ValueError("selected target account changed")
    if environment["ADP_SOURCE_REVISION"] != receipt["source_revision"]:
        raise ValueError("selected source changed")
    if environment["ADP_DEFINITION_REVISION"] != receipt["definition_revision"]:
        raise ValueError("workflow definition changed")
    if environment["INPUT_ENVIRONMENT"] != manifest["environment"]:
        raise ValueError("selected environment changed")
    common = {
        "schema_version": 1,
        "repository_id": int(environment["GITHUB_REPOSITORY_ID"]),
        "run_id": int(environment["GITHUB_RUN_ID"]),
        "run_attempt": int(environment["GITHUB_RUN_ATTEMPT"]),
        "workflow_path": ".github/workflows/dev-box-deploy.yml",
        "workflow_revision": receipt["definition_revision"],
        "source_revision": receipt["source_revision"],
        "account_id": receipt["account_id"],
        "region": receipt["region"],
        "resource_id": receipt["launch_template_id"],
    }
    context = {
        **common,
        "resource_kind": "ec2-launch-template",
        "inputs": {
            "customer_account_id": environment["INPUT_CUSTOMER_ACCOUNT_ID"],
            "customer_aws_label": environment["INPUT_CUSTOMER_AWS_LABEL"],
            "customer_user_id": environment["INPUT_CUSTOMER_USER_ID"],
            "environment": environment["INPUT_ENVIRONMENT"],
        },
        "correlation": environment["ADP_CORRELATION"],
    }
    release = {
        **common,
        "component": "dev-box",
        "image_digest": receipt.get("controller_digest"),
        "dev_box_receipt": receipt,
        "assets": {"dev-box-module.tar.gz": receipt["module_sha256"]},
        "produced_at": datetime.now(UTC).isoformat(),
    }
    return context, release


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    context, release = engine_evidence(
        json.loads(arguments.manifest.read_text()), json.loads(arguments.receipt.read_text()), os.environ
    )
    context_dir = arguments.output / "context"
    release_dir = arguments.output / "release"
    context_dir.mkdir(parents=True, exist_ok=True)
    release_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "deployment-context.json").write_text(json.dumps(context, sort_keys=True) + "\n")
    (release_dir / "release.json").write_text(json.dumps(release, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
