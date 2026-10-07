"""Run existing source scanners without the cloud/evaluation dispatch wrapper."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import yaml

from diff_security_findings import process_tool_findings

STEPS = {
    "checkov": ("Install Checkov", "Run Checkov"),
    "semgrep": ("Install Semgrep", "Fetch semgrep registry ruleset", "Run Semgrep"),
    "bandit": ("Install Bandit", "Run Bandit"),
    "detect-secrets": ("Install detect-secrets", "Run detect-secrets"),
    "npm-audit": ("Run npm audit",),
}
PACKAGES = {"modules/gateway/frontend", "modules/agent-factory/agent"}


def validate(tool, data):
    if not isinstance(data, dict) or data.get("error"):
        raise ValueError("Scanner did not produce a successful structured report")
    if tool in {"checkov", "semgrep", "bandit"}:
        runs = data.get("runs")
        if not isinstance(runs, list) or not runs:
            raise ValueError("Missing SARIF runs")
        for run in runs:
            if not run.get("tool", {}).get("driver", {}).get("name") or not isinstance(
                run.get("results"), list
            ):
                raise ValueError("Missing SARIF scanner/results")
            if any(
                i.get("executionSuccessful") is False
                for i in run.get("invocations", [])
            ):
                raise ValueError("Scanner execution was unsuccessful")
    elif tool == "npm-audit":
        if not isinstance(data.get("auditReportVersion"), int) or not isinstance(
            data.get("vulnerabilities"), dict
        ):
            raise ValueError("Missing npm audit results")
    elif tool == "detect-secrets":
        if not data.get("plugins_used") or not isinstance(data.get("results"), dict):
            raise ValueError("Missing detector execution/results")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=STEPS)
    parser.add_argument("--package", default="")
    args = parser.parse_args()
    if (args.tool == "npm-audit") != bool(args.package) or (
        args.package and args.package not in PACKAGES
    ):
        parser.error("npm audit requires a reviewed package directory")
    definition = yaml.safe_load(Path(".github/workflows/security-scan.yml").read_text())
    env = {
        **os.environ,
        **{k: str(v) for k, v in definition["env"].items() if k.endswith("_VERSION")},
    }
    for name in STEPS[args.tool]:
        matches = [
            s for s in definition["jobs"][args.tool]["steps"] if s.get("name") == name
        ]
        if len(matches) != 1 or "run" not in matches[0] or "uses" in matches[0]:
            raise ValueError(f"Scanner step contract changed: {name}")
        step = matches[0]
        command = re.sub(r"\$\{\{ env\.([A-Z_]+) }}", r"$\1", step["run"])
        if "${{" in command:
            raise ValueError("Unsupported scanner expression")
        cwd = args.package or "."
        subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", command],
            cwd=cwd,
            env=env,
            check=True,
        )
    suffix = "json" if args.tool in {"npm-audit", "detect-secrets"} else "sarif"
    source = Path(args.package or ".") / f"{args.tool}-results.{suffix}"
    candidates = sorted(source.rglob("*.sarif")) if source.is_dir() else [source]
    if not candidates:
        raise ValueError("Scanner produced no report")
    target = Path("test-results/security") / (
        args.package.replace("/", "-") or args.tool
    )
    target.mkdir(parents=True, exist_ok=True)
    for index, candidate in enumerate(candidates):
        validate(args.tool, json.loads(candidate.read_text()))
        name = (
            candidate.name
            if args.tool == "detect-secrets"
            else f"npm-audit-{args.package.replace('/', '-')}.json"
            if args.tool == "npm-audit"
            else f"{args.tool}-{index}.{suffix}"
        )
        shutil.copyfile(candidate, target / name)
    findings = process_tool_findings(args.tool, target, Path(".github/security"))
    # Keep current findings and baselines separate. No automatic baseline refresh.
    (target / "gate.json").write_text(json.dumps(findings, indent=2) + "\n")
    blocked = any(
        severity in {"critical", "high"}
        for severity in findings["new_severities"].values()
    )
    print(
        f"{args.tool}: {findings['new_count']} new findings; high/critical gate {'FAIL' if blocked else 'PASS'}"
    )
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
