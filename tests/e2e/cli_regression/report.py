"""Combine job outcomes without allowing skipped or blocked suites to pass."""

from __future__ import annotations

import json
import os
from pathlib import Path

from tests.e2e.cli_uplift.config import REVISION

REQUIRED = {
    "guards": "Orchestration checks",
    "prepare": "Deployed revision",
    "onboarding": "CLI onboarding and Claude/Codex conversations",
    "budgets": "Budget and rate-limit enforcement",
    "ec2": "Complete CLI suite on disposable EC2 (including recovery)",
}


def render(jobs, revision):
    verified = isinstance(revision, str) and REVISION.fullmatch(revision)
    lines = [
        "## Nightly CLI regression — combined result",
        "",
        f"Revision: `{revision if verified else 'unverified'}`",
        "",
        "| Suite | Result |",
        "|---|---|",
    ]
    passed = bool(verified)
    for key, label in REQUIRED.items():
        result = (jobs.get(key) or {}).get("result", "missing")
        if result not in {"success", "failure", "cancelled", "skipped"}:
            result = "missing"
        passed = passed and result == "success"
        lines.append(f"| {label} | {result} |")
    lines.extend(
        [
            "",
            "**PASS**" if passed else "**FAIL / INCOMPLETE — not all suites passed.**",
            "",
            "See each suite's job summary for assertions and findings, and the "
            "EC2 report artifacts for blocked/not-run scenarios and cleanup evidence.",
            "A missing fixture, skipped suite or failed recovery cannot pass this run.",
        ]
    )
    return "\n".join(lines) + "\n", 0 if passed else 1


def main():
    jobs = json.loads(os.environ.get("CLI_REGRESSION_JOBS", "{}"))
    summary, code = render(jobs, os.environ.get("CLI_REGRESSION_REVISION", ""))
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as output:
        output.write(summary)
    print(summary)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
