"""Read the deployed dev revision; never substitute the current main SHA."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from tests.e2e.cli_uplift import config, live, ports


def snapshot(cfg, aws, http):
    identity = aws.call("sts", "get_caller_identity")
    if identity.get("Account") != cfg["platform_account"]:
        raise ValueError("Nightly CLI regression requires the configured dev account")
    evidence = {}
    revision = live._deployed_revision(aws, http, cfg)(evidence)
    if not config.REVISION.fullmatch(revision):
        raise ValueError("Deployment evidence must resolve to one full commit SHA")
    return revision, evidence["revision_source"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ec2", action="store_true")
    args = parser.parse_args(argv)
    cfg = (
        config.from_environment(os.environ)
        if args.ec2
        else config.load(config.EXAMPLE_PATH)
    )
    transport = ports.default_ports(cfg)
    revision, source = snapshot(cfg, transport["aws"], transport["http"])
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"revision={revision}\n")
    if args.ec2:
        with Path(os.environ["GITHUB_ENV"]).open("a") as environment:
            environment.write(f"CLI_UPLIFT_EVAL_EXPECTED_REVISION={revision}\n")
    label = "EC2 suite" if args.ec2 else "onboarding start"
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(
            f"## Nightly CLI regression — dev\n\n"
            f"Revision at {label}: `{revision}` (source: `{source}`).\n\n"
            "Onboarding → budgets/rate limits → EC2 regression.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
