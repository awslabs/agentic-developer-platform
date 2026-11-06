#!/usr/bin/env python3
"""Run one owner-authorized DA19 selector against private target configuration."""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/live/devops"))

from selector import qualify_journeys
from recovery_selector import qualify_recovery
from context_selector import qualify_context, qualify_acceptance, qualify_portability


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selector", choices=["journeys", "recovery", "context", "portability", "acceptance", "all"])
    parser.add_argument("reviewed_sha")
    parser.add_argument("private_manifest")
    parser.add_argument("private_evidence_dir")
    parser.add_argument("--observations", help="private independent native-evaluator observation bundle (all only)")
    arguments = parser.parse_args()
    if arguments.observations and arguments.selector != "all":
        parser.error("--observations is only supported by the all-selector release driver")
    try:
        if arguments.selector == "all":
            from devops_qualification_driver import run_all

            raise SystemExit(run_all(arguments.reviewed_sha, arguments.private_manifest,
                                     arguments.private_evidence_dir, observations=arguments.observations))
        else:
            selectors = {"journeys": qualify_journeys, "recovery": qualify_recovery,
                         "context": qualify_context, "portability": qualify_portability,
                         "acceptance": qualify_acceptance}
            selectors[arguments.selector](arguments.reviewed_sha, arguments.private_manifest,
                                          arguments.private_evidence_dir)
    except Exception:
        raise SystemExit("DevOps live readback did not pass; inspect private evidence and target") from None
    print("bounded DevOps selector finished; this is not complete live acceptance")
