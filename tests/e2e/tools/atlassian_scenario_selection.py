"""Derive protected workflow bindings from the runner's actual selection."""

import json
import os
import sys
from pathlib import Path


SCENARIOS = {
    "SCN-8454-01": "setup",
    "SCN-8454-02": "matrix",
    "SCN-8454-03": "authority",
    "SCN-8454-04": "recovery",
    "SCN-8454-05": "regression",
    "SCN-8454-06": "handoff",
}


def selection_flags(pattern, *, scenarios=None):
    """A glob and an exact selector require the same bindings for each case.

    These flags select configuration steps only. They neither register missing
    scenarios nor attest that any selected scenario passed.
    """
    from tests.e2e.tools.runner import select_scenarios

    if not isinstance(pattern, str) or not 0 < len(pattern) <= 512:
        raise ValueError("A bounded scenario selector is required")
    selected = (
        select_scenarios(pattern)
        if scenarios is None
        else select_scenarios(pattern, scenarios)
    )
    identifiers = {case.identifier for case in selected}
    atlassian = {name for name in identifiers if name.startswith("SCN-8454-")}
    if not atlassian <= SCENARIOS.keys():
        raise ValueError("Selected Atlassian scenario has no binding contract")
    flags = {
        "atlassian": bool(atlassian),
        **{
            "atlassian_" + name: identifier in atlassian
            for identifier, name in SCENARIOS.items()
        },
        "atlassian_clients": bool(atlassian - {"SCN-8454-01"}),
        "atlassian_native": bool(atlassian & {"SCN-8454-02", "SCN-8454-04"}),
        "atlassian_full": atlassian == SCENARIOS.keys(),
    }
    return flags


def main():
    try:
        flags = selection_flags(os.environ["SCENARIO_FILTER"])
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with Path(output).open("a") as stream:
                stream.write(
                    "".join(
                        f"{key}={str(value).lower()}\n" for key, value in flags.items()
                    )
                )
    except (KeyError, OSError, ValueError):
        print("Scenario selection could not be bound", file=sys.stderr)
        return 2
    print(json.dumps(flags, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
