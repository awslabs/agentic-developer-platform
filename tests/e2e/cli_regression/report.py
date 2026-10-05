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
    "ec2": "CLI suite on disposable EC2 (including recovery)",
}


def render(jobs, revision, ec2_revision="", ec2_scope="nightly"):
    verified = isinstance(revision, str) and REVISION.fullmatch(revision)
    ec2_verified = isinstance(ec2_revision, str) and REVISION.fullmatch(ec2_revision)
    scope_valid = ec2_scope in {"nightly", "login", "full"}
    lines = [
        "## Nightly CLI regression — combined result",
        "",
        f"Revision at onboarding start: `{revision if verified else 'unverified'}`",
        f"EC2 pinned revision: `{ec2_revision if ec2_verified else 'unverified'}`",
        f"EC2 scope: `{ec2_scope if scope_valid else 'invalid'}`",
        "",
        "| Suite | Result |",
        "|---|---|",
    ]
    passed = bool(verified and ec2_verified and scope_valid)
    for key, label in REQUIRED.items():
        result = (jobs.get(key) or {}).get("result", "missing")
        if result not in {"success", "failure", "cancelled", "skipped"}:
            result = "missing"
        passed = passed and result == "success"
        lines.append(f"| {label} | {result} |")
    if ec2_scope == "nightly":
        lines.extend(
            [
                "",
                "Daily EC2 cases: E01 install, C01 login/refresh, E20 capabilities/doctor, E21 usage/export, E22 Activity, and E23–E38 story scenarios (except the separate E27 tenant-isolation suite). Missing fixtures remain blocked.",
                "**Full CLI story acceptance is not established.** Active controls, "
                "capability contrasts and marked spend reconciliation need their own fixtures.",
            ]
        )
    if ec2_scope == "login":
        lines.extend(
            [
                "",
                "Daily key scenarios: onboarding/Claude/Codex, budgets/rate limits, "
                "and EC2 E01 install + C01 native login/refresh.",
                "**Full CLI acceptance is not established by this scope.** "
                "E02–E22 are outside this login-only gate. The full matrix remains available "
                "with `ec2_scope=full`; see docs/regression-testing/nightly-cli-regression.md "
                "for missing destination/GitHub/hosted/multi-deployment fixtures and "
                "the E16/E17 model-limit guard.",
            ]
        )
    if verified and ec2_verified and revision != ec2_revision:
        lines.extend(
            [
                "",
                "Dev advanced between suites; each revision is reported "
                "separately. This is not a single-revision acceptance run.",
            ]
        )
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
    summary, code = render(
        jobs,
        os.environ.get("CLI_REGRESSION_REVISION", ""),
        os.environ.get("CLI_REGRESSION_EC2_REVISION", ""),
        os.environ.get("CLI_REGRESSION_EC2_SCOPE", "nightly"),
    )
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as output:
        output.write(summary)
    print(summary)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
