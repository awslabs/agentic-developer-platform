#!/usr/bin/env python3
"""Enforce the >=85% line+branch gate on the NEW control functions only (#3960).

Why this exists instead of ``--cov-fail-under=85`` on the file
--------------------------------------------------------------
``lib/invocation_status.py`` is a pre-existing module. Its original
``update_status`` code sits at ~64% coverage and this story does not touch it, so a
whole-file threshold would fail the build on code the PR never modified. The usual
response to that is to lower the threshold until the file passes — which is how a
gate stops gating. The issue's requirement is >=85% line **and** branch on the new
control modules and functions, so the gate is scoped to exactly those.

Function spans are resolved with ``inspect`` rather than hardcoded line numbers,
because hardcoded ranges silently stop matching the moment anything above them
shifts — and a range that has drifted off the end of a function reports 100%
coverage of nothing.

Both branch-partial lines and uncovered lines fail. Branch coverage is the
load-bearing half here: a validation function whose reject path never executes is
fully line-covered and completely unproven.

Usage (see .github/workflows/agent-control-ci.yml):
    pytest tests/test_control_token.py \
        --cov=lib.invocation_status --cov=entrypoint --cov-branch \
        --cov-report=json:coverage-control.json
    python tests/assert_control_coverage.py coverage-control.json
"""

from __future__ import annotations

import importlib
import inspect
import json
import sys
from pathlib import Path

MINIMUM_PERCENT = 85.0

# The control functions this story adds, keyed by the module file coverage reports
# them under. Extend this when a new control function lands; a function absent from
# it is not gated at all.
#
# `entrypoint` is gated as well as `lib.invocation_status` because the decisions
# that actually make the channel safe live there: the flag read, the pod-IP hard
# stop, the TTL bound, and the teardown backstops. A gate covering only the DDB
# writer would leave every one of those unproven — and each is a one-sided branch
# whose reject path is the whole point.
NEW_CONTROL_FUNCTIONS = {
    "lib/invocation_status.py": (
        "lib.invocation_status",
        ("register_control_endpoint", "clear_control_endpoint"),
    ),
    "entrypoint.py": (
        "entrypoint",
        (
            "_is_agent_control_enabled",
            "_control_port",
            "_control_token_ttl_seconds",
            "_setup_agent_control",
            "_teardown_agent_control",
            "_install_control_teardown_guard",
            "_revoke_pending_control",
            "_control_sigterm_handler",
        ),
    ),
}


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <coverage-json-path>", file=sys.stderr)
        return 2

    report_path = Path(argv[1])
    if not report_path.is_file():
        print(f"coverage report not found: {report_path}", file=sys.stderr)
        return 2

    # An empty gate is a broken gate, not a passing one. Without this the script
    # prints "All new control functions meet the gate" and exits 0 when the tuple
    # is empty — so deleting its contents is a silent way to disable the check,
    # and every future control function would be ungated by default.
    if not NEW_CONTROL_FUNCTIONS or not any(
        functions for _, functions in NEW_CONTROL_FUNCTIONS.values()
    ):
        return _fail(
            "FAIL: NEW_CONTROL_FUNCTIONS is empty, so this gate measures nothing. "
            "It must name the control functions to enforce; an empty list is a "
            "disabled check, not a satisfied one."
        )

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    data = json.loads(report_path.read_text(encoding="utf-8"))

    failed = False
    for suffix, (module_name, function_names) in NEW_CONTROL_FUNCTIONS.items():
        module = importlib.import_module(module_name)
        try:
            measured = next(value for key, value in data["files"].items() if key.endswith(suffix))
        except StopIteration:
            # The failure mode this guard is really for: coverage collected nothing
            # (a mistyped --cov target measures no file and still exits 0).
            print(
                f"FAIL: no coverage data for {suffix}. Coverage measured nothing — "
                f"check the --cov target (a path like --cov={suffix} collects no data; "
                f"use the dotted module form --cov={module_name}).",
                file=sys.stderr,
            )
            failed = True
            continue

        executed = set(measured["executed_lines"])
        missing = set(measured["missing_lines"])
        partial_branches = {
            entry[0] if isinstance(entry, list) else entry
            for entry in measured.get("missing_branches", [])
        }

        for name in function_names:
            function = getattr(module, name, None)
            if function is None:
                print(f"FAIL: {name} not found in {suffix}", file=sys.stderr)
                failed = True
                continue

            start = function.__code__.co_firstlineno
            end = start + len(inspect.getsource(function).splitlines()) - 1
            span = set(range(start, end + 1))

            covered = executed & span
            uncovered = sorted(missing & span)
            partial = sorted(partial_branches & span)
            total = len(covered) + len(uncovered)
            percent = 100.0 * len(covered) / total if total else 100.0

            status = (
                "OK" if percent >= MINIMUM_PERCENT and not uncovered and not partial else "FAIL"
            )
            print(
                f"{status} {suffix}::{name}: {percent:.1f}% lines ({len(covered)}/{total}), uncovered={uncovered}, partial_branches={partial}"
            )

            if status == "FAIL":
                failed = True

    if failed:
        print(
            f"\nNew control functions must reach {MINIMUM_PERCENT:.0f}% line AND branch coverage (#3960). "
            "Do not lower this threshold: it is scoped to the new functions precisely so that pre-existing "
            "coverage in this module cannot be used to justify weakening it.",
            file=sys.stderr,
        )
        return 1

    print(f"\nAll new control functions meet the {MINIMUM_PERCENT:.0f}% line and branch gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
