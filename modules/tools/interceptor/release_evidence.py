"""Bind the published Lambda alias receipt to the workflow's reviewed identity."""

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

WORKFLOW = ".github/workflows/tools-interceptor-deploy.yml"


def build_release(context, alias, run_id, run_attempt):
    if (
        context.get("schema_version") != 1
        or context.get("workflow_path") != WORKFLOW
        or context.get("resource_kind") != "lambda-function"
        or context.get("run_id") != run_id
        or context.get("run_attempt") != run_attempt
        or not re.fullmatch(r"[1-9][0-9]*", alias.get("version", ""))
        or not re.fullmatch(r"[A-Za-z0-9+/]{43}=", alias.get("code_sha256", ""))
    ):
        raise ValueError("Lambda release is not bound to the reviewed workflow and version")
    identity = {key: context[key] for key in (
        "repository_id", "run_id", "run_attempt", "source_revision", "workflow_revision",
        "workflow_path", "account_id", "region", "resource_id",
    )}
    resource = identity["resource_id"]
    if not re.fullmatch(
        rf"arn:aws:lambda:{re.escape(identity['region'])}:{identity['account_id']}:function:[A-Za-z0-9_-]+", resource
    ):
        raise ValueError("Lambda release resource differs from the bound workflow target")
    return {
        "schema_version": 1,
        **identity,
        "component": "tools-interceptor",
        "lambda_version": alias["version"],
        "code_sha256": alias["code_sha256"],
        "assets": {},
        "produced_at": datetime.now(UTC).isoformat(),
    }


def main():
    directory = Path(os.environ["RUNNER_TEMP"])
    release_file = directory / "adp-release-tools-interceptor" / "release.json"
    context = json.loads((directory / "adp-deployment-context" / "deployment-context.json").read_text())
    alias = json.loads(release_file.read_text())
    release = build_release(context, alias, int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"]))
    encoded = json.dumps(release, sort_keys=True).encode()
    if len(encoded) > 65536:
        raise ValueError("Lambda release artifact exceeds bounded size")
    release_file.write_bytes(encoded)


if __name__ == "__main__":
    main()
