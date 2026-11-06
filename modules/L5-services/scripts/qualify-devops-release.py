#!/usr/bin/env python3
"""Shared-dispatch entry point for DevOps preflight and five-selector execution."""

import argparse

from devops_qualification_driver import preflight, run_all


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "run"))
    parser.add_argument("source_sha")
    parser.add_argument("fixture_path")
    parser.add_argument("evidence_dir")
    parser.add_argument("--observations")
    arguments = parser.parse_args(argv)
    selected = preflight if arguments.action == "preflight" else run_all
    result = selected(arguments.source_sha, arguments.fixture_path, arguments.evidence_dir,
                      observations=arguments.observations)
    return 0 if arguments.action == "preflight" else result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit("DevOps release qualification failed; inspect private evidence") from None
