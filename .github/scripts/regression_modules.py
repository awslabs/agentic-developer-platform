"""Resolve module inputs into a stored test plan without accessing a target."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.regression.catalog import plan  # noqa: E402


def describe(result):
    lines = [
        "### Module test selection",
        "",
        f"Modules: {', '.join(result['modules'])}",
        "",
        "| Test ID | Kind | Stored test | Runner |",
        "|---|---|---|---|",
    ]
    from tests.regression.catalog import lane

    for key, row in result["tests"].items():
        lines.append(
            f"| {key} | {row['kind']} | `{row['path']}::{row['selector']}` | {lane(row) or 'Outside module coordinator'} |"
        )
    lines += [
        "",
        f"Scenarios with no mapped test: {len(result['unmapped_scenarios'])}.",
        f"Scenarios without a configured E2E runner: {len(result['without_automated_e2e'])}.",
        "",
        result["coverage_claim"],
        "",
        "CLI includes install/login prerequisites. Shell evaluation lanes run their whole suite; browser and CLI selection is focused.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules", default=os.environ.get("REGRESSION_MODULES", ""))
    parser.add_argument("--output", default="test-results/module-plan.json")
    args = parser.parse_args(argv)
    # Blank input in Actions preserves the scheduled daily/weekly profiles.
    if not args.modules and os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write("lanes=[]\nmodules=\ncli_scope=\n")
        return 0
    try:
        result = plan(args.modules)
    except ValueError as exc:
        parser.error(str(exc))
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + "\n")
    print(describe(result))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write(describe(result))
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"lanes={json.dumps(result['lanes'])}\n")
            output.write(f"modules={','.join(result['modules'])}\n")
            output.write(f"cli_scope={result['cli_scope']}\n")
    if not result["lanes"]:
        print(
            "No configured E2E runner for the selected modules. See module-plan.json.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
