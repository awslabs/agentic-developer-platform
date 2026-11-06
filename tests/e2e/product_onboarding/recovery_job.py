"""Restore a selected Product evaluation after the workflow runner exits."""

import argparse
import os

from tests.e2e.cli_uplift import ports

from . import live, product_execution
from .product_profile_api import PROTECTED_SELECTIONS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--hold-for-rollback", action="store_true")
    args = parser.parse_args(argv)
    live.select(args.cases)
    if args.cases not in PROTECTED_SELECTIONS:
        raise ValueError(
            "Protected recovery requires a reviewed ordered Product selection"
        )
    if args.hold_for_rollback and args.cases != "api_product_lifecycle":
        raise ValueError("Product recovery hold is limited to the isolated lifecycle")
    run_id = live.workflow_run_id()
    cfg = {
        **live.resolved_config(args.expected_revision),
        "product_cases": args.cases,
        **({"hold_for_rollback": True} if args.hold_for_rollback else {}),
    }
    aws = ports.default_ports(cfg)["aws"]
    result = product_execution.recover(cfg, run_id, aws)
    if result["cleanup_verified"] is not True:
        raise ValueError("Product recovery did not verify cleanup")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("cleanup_ok=true\n")
    return result


if __name__ == "__main__":
    main()
