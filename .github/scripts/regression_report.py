"""Fail-closed regression evidence and profile aggregation (no cloud access)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

DAILY_CRON = "17 2 * * 1-6"
WEEKLY_CRON = "17 2 * * 0"


def profile(event, schedule, requested="daily"):
    if event == "schedule":
        if schedule not in {DAILY_CRON, WEEKLY_CRON}:
            raise ValueError("Unrecognized regression schedule")
        return "weekly" if schedule == WEEKLY_CRON else "daily"
    if requested not in {"daily", "weekly"}:
        raise ValueError("Unknown regression profile")
    return requested


def junit(path, inventory):
    """Inspect actual testcase elements, not potentially misleading XML totals."""
    expected = json.loads(Path(inventory).read_text())
    if (
        not isinstance(expected, list)
        or not expected
        or len(set(expected)) != len(expected)
    ):
        raise ValueError("Missing, empty or duplicate expected case inventory")
    cases = ET.parse(path).getroot().findall(".//testcase")
    found = []
    counts = dict(expected=len(expected), executed=0, passed=0, failed=0, skipped=0)
    for case in cases:
        identities = [
            p.get("value")
            for p in case.findall("./properties/property")
            if p.get("name") == "adp_case_id"
        ]
        if len(identities) != 1:
            raise ValueError("Every result must identify its collected scenario")
        found.extend(identities)
        if case.find("skipped") is not None:
            counts["skipped"] += 1
        else:
            counts["executed"] += 1
            failed = case.find("failure") is not None or case.find("error") is not None
            counts["failed" if failed else "passed"] += 1
    complete = len(found) == len(set(found)) and set(found) == set(expected)
    counts["missing"] = len(set(expected) - set(found))
    counts["status"] = (
        "pass" if complete and counts["passed"] == len(expected) else "incomplete"
    )
    return counts


def aggregate(jobs, required):
    rows = [(key, (jobs.get(key) or {}).get("result", "missing")) for key in required]
    passed = bool(rows) and all(result == "success" for _, result in rows)
    body = "| Suite | Result |\n|---|---|\n" + "".join(
        f"| {key} | {result} |\n" for key, result in rows
    )
    body += "\n**PASS**\n" if passed else "\n**FAIL / INCOMPLETE**\n"
    return body, 0 if passed else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("profile")
    evidence = sub.add_parser("junit")
    evidence.add_argument("--suite", required=True)
    evidence.add_argument("--junit", required=True)
    evidence.add_argument("--inventory", required=True)
    evidence.add_argument("--output", required=True)
    combined = sub.add_parser("aggregate")
    combined.add_argument("--required", nargs="+", required=True)
    args = parser.parse_args(argv)
    if args.command == "profile":
        selected = profile(
            os.environ["GITHUB_EVENT_NAME"],
            os.environ.get("REGRESSION_SCHEDULE", ""),
            os.environ.get("REGRESSION_PROFILE", "daily"),
        )
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"profile={selected}\n")
            profiles = json.loads(
                (Path(__file__).parents[1] / "regression-profiles.json").read_text()
            )
            output.write(f"cli_scope={profiles[selected]['cli_scope']}\n")
        return 0
    if args.command == "junit":
        try:
            result = junit(args.junit, args.inventory)
        except (OSError, ValueError, TypeError, ET.ParseError) as exc:
            result = {"status": "incomplete", "reason": str(exc)}
        result.update(
            suite=args.suite, source_revision=os.environ.get("GITHUB_SHA", "")
        )
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=2) + "\n")
        body = f"### {args.suite}\n\n```json\n{json.dumps(result, indent=2)}\n```\n"
        code = 0 if result["status"] == "pass" else 1
    else:
        requested = os.environ.get("REGRESSION_MODULES", "")
        selection = None
        if requested:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from tests.regression.catalog import plan

            selection = plan(requested)
            args.required = ["profile", "before", "after", "smoke", *selection["lanes"]]
        body, code = aggregate(
            json.loads(os.environ.get("REGRESSION_JOBS", "{}")), args.required
        )
        if selection:
            body += (
                "\nCoverage remains partial: "
                f"{len(selection['without_automated_e2e'])} selected scenarios have no configured E2E runner. "
                "Suite success describes execution only; consult module-plan.json for tags and gaps.\n"
            )
            if not selection["lanes"]:
                body = body.replace("**PASS**", "**FAIL / INCOMPLETE**")
                code = 1
        if "REGRESSION_REVISION_BEFORE" in os.environ:
            before = os.environ["REGRESSION_REVISION_BEFORE"]
            after = os.environ.get("REGRESSION_REVISION_AFTER", "")
            if not re.fullmatch(r"[0-9a-f]{40}", before) or after != before:
                body = body.replace("**PASS**", "**FAIL / INCOMPLETE**")
                body += "\nDeployment revision missing or changed during regression; rerun against a stable release.\n"
                code = 1
            else:
                body += f"\nVerified gateway revision before and after: `{before}`.\n"
    print(body)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write(body)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
