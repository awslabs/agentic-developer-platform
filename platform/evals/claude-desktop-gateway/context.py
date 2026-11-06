#!/usr/bin/env python3
"""Build the minimal, revision-bound context expected by WorkflowProvider."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def context_from_environment(environment):
    source = environment["EXPECTED_SOURCE_REVISION"]
    definition = environment["EXPECTED_DEFINITION_REVISION"]
    correlation = environment["ADP_CORRELATION"]
    if not all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in (source, definition)):
        raise ValueError("invalid source or definition revision")
    if environment["GITHUB_REF"] != "refs/heads/main" or environment["GITHUB_SHA"] != definition:
        raise ValueError("workflow dispatch did not use the pinned main definition")
    if environment["GITHUB_WORKFLOW_SHA"] != definition:
        raise ValueError("running workflow definition differs from pinned revision")
    if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != source:
        raise ValueError("checked-out source differs from pinned revision")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", correlation):
        raise ValueError("invalid dispatch correlation")
    account = environment["EVAL_CONTEXT_ACCOUNT"]
    if not re.fullmatch(r"[0-9]{12}", account) or account != environment["EVAL_EXPECTED_ACCOUNT"]:
        raise ValueError("running account differs from expected dev account")
    if environment["AWS_REGION"] != "us-east-1":
        raise ValueError("running region differs from dev region")
    return {
        "schema_version": 1,
        "repository_id": int(environment["GITHUB_REPOSITORY_ID"]),
        "run_id": int(environment["GITHUB_RUN_ID"]),
        "run_attempt": int(environment["GITHUB_RUN_ATTEMPT"]),
        "workflow_path": ".github/workflows/eval-claude-desktop-gateway.yml",
        "workflow_revision": definition,
        "source_revision": source,
        "account_id": account,
        "region": environment["AWS_REGION"],
        "resource_kind": "cli-evaluation",
        "resource_id": "dev",
        "inputs": {},
        "correlation": correlation,
    }


if __name__ == "__main__":
    try:
        result = context_from_environment(os.environ)
        output = Path(sys.argv[1])
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        output.write_text(json.dumps(result, separators=(",", ":")))
    except (KeyError, IndexError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(f"deployment context refused: {exc}")
