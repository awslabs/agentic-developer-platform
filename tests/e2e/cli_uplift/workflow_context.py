"""Authenticate an engine-dispatched knowledge qualification before live work."""

import json
import os
import re
import subprocess
from pathlib import Path

from . import config


def build_context(env, inputs, account):
    """Bind the real runner identity to the exact bounded, accepted inputs."""
    # GitHub omits optional empty strings from the workflow inputs context.
    # Restore only this declared empty default before validating the exact scope.
    inputs = {"evaluation_id": "", **inputs}
    sha = env["ADP_CHECKOUT_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid source revision")
    if env["GITHUB_REF"] != "refs/heads/main":
        raise ValueError("Qualification requires reviewed main")
    workflow_sha = env["GITHUB_WORKFLOW_SHA"]
    if workflow_sha != env["GITHUB_SHA"] or not re.fullmatch(
        r"[0-9a-f]{40}", workflow_sha
    ):
        raise ValueError("Workflow revision differs from dispatch")
    expected = {
        "environment": "dev",
        "expected_revision": sha,
        "mode": "start",
        "fixtures_json": "{}",
        "suites": "knowledge",
        "evaluation_id": "",
        "inject_fault": "none",
        "adp_source_revision": sha,
        "adp_definition_revision": workflow_sha,
    }
    if set(inputs) != set(expected) | {"adp_correlation"}:
        raise ValueError("Unexpected qualification inputs")
    if any(inputs[key] != value for key, value in expected.items()):
        raise ValueError("Qualification inputs or revision changed")
    correlation = inputs["adp_correlation"]
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", correlation):
        raise ValueError("Invalid dispatch correlation")
    pinned = json.loads(config.EXAMPLE_PATH.read_text())
    effective = config.from_environment(env)
    if (
        effective["platform_account"] != pinned["platform_account"]
        or effective["region"] != pinned["region"]
        or env["AWS_REGION"] != pinned["region"]
    ):
        raise ValueError("Qualification target differs from pinned configuration")
    if account != effective["platform_account"]:
        raise ValueError("Qualification account changed")
    return {
        "schema_version": 1,
        "repository_id": int(env["GITHUB_REPOSITORY_ID"]),
        "run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "workflow_path": ".github/workflows/eval-cli-uplift.yml",
        "workflow_revision": workflow_sha,
        "source_revision": sha,
        "account_id": account,
        "region": env["AWS_REGION"],
        "resource_kind": "cli-evaluation",
        "resource_id": "dev",
        "inputs": {
            key: value for key, value in inputs.items() if not key.startswith("adp_")
        },
        "correlation": correlation,
    }


def main():
    inputs = json.loads(os.environ["ADP_WORKFLOW_INPUTS"])
    account = subprocess.check_output(
        ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        text=True,
    ).strip()
    env = dict(os.environ)
    env["ADP_CHECKOUT_SHA"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            env["ADP_CHECKOUT_SHA"],
            env["GITHUB_SHA"],
        ],
        check=True,
    )
    context = build_context(env, inputs, account)
    Path("/tmp/adp-cli-context").mkdir(exist_ok=True)
    Path("/tmp/adp-cli-context/deployment-context.json").write_text(json.dumps(context))


if __name__ == "__main__":
    main()
