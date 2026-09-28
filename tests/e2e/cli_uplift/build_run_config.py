"""Write the validated run config both Actions jobs must share.

    python -m tests.e2e.cli_uplift.build_run_config /tmp/cli-uplift-eval/run-config.json

R5: the evaluate job used to build this inline, in a Python heredoc, into its own
/tmp. The recovery job runs as a SEPARATE job on a fresh runner, so it had no way
to obtain that file and fell back to the checked-in example — which carries no
`state_bucket`, so `--restore` raised "needs a durable state store" on every
single run. The workflow converted that into a `::warning::` and the recovery
gate went green while the run's IAM roles, CloudFormation stacks, secrets and
Cognito users were still live. Only the instances were swept, by tag and age.

So the overlay lives in `config.from_environment()` and both jobs run this. That
makes the two configs the same config by construction, and lets the offline suite
assert it — which an inline heredoc could never be.

Nothing here is a secret. Every overlaid value is an identifier: an account
number, a role ARN, a bucket name, a Secrets Manager secret NAME. The file is
written 0600 anyway, because it names the fixtures a run will touch.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import config


def summary(resolved):
    """Lines the job log prints so an operator can see what will and will not run."""
    bindings = [key for key in config.BINDINGS if resolved.get(key)]
    github = sorted(
        key for key, value in (resolved.get("github") or {}).items() if value
    )
    lines = [
        f"Revision under test: {resolved['expected_revision']}",
        "Destination bindings: "
        + (
            ", ".join(bindings)
            if bindings
            else "none — install/login do not require destination roles"
        ),
        "GitHub fixtures: "
        + (", ".join(github) if github else "none — the GitHub cases will BLOCK"),
        # #5413. Names them rather than counting them, because "3 deployments" was
        # printable while all three pointed at the same gateway; the names are what
        # let an operator see at a glance that this is dev/integration/preprod and
        # not one URL under three labels. The URLs themselves are not printed: they
        # are not secret, but a log line is not where a reviewer should be reading
        # them from, and the written run config has them.
        "Deployment bindings: "
        + (
            ", ".join(
                str(entry.get("name") or "?") for entry in resolved["deployments"]
            )
            if resolved.get("deployments")
            else "none — the multi-deployment cases (E16/E17) will BLOCK"
        ),
        "Durable state: "
        + (
            f"enabled ({resolved['state_bucket']})"
            if resolved.get("state_bucket")
            else "DISABLED — this run cannot be resumed or cleaned up from a later "
            "Actions run, and the recovery sweep will only be able to reach its "
            "instances; set CLI_UPLIFT_EVAL_STATE_BUCKET"
        ),
    ]
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_path")
    parser.add_argument("--check-ready", action="store_true")
    parser.add_argument("--suites", default="login")
    args = parser.parse_args(argv)
    resolved = config.from_environment(os.environ)
    if args.check_ready:
        # No AWS calls or mutations: name every missing binding before OIDC,
        # instance launch or a state-store write can obscure the setup problem.
        suites = tuple(part.strip() for part in args.suites.split(",") if part.strip())
        config.require_bindings(resolved, suites)
        config.require(
            resolved.get("state_bucket"),
            "Set CLI_UPLIFT_EVAL_STATE_BUCKET for script delivery and recovery",
        )
        role = config.ROLE_ARN.fullmatch(os.environ.get("EVAL_ROLE_ARN", ""))
        config.require(
            role and role.group(1) == resolved["platform_account"],
            "Set AWS_CLI_UPLIFT_EVAL_ROLE_ARN (or AWS_E2E_ROLE_ARN) to an "
            "Actions role in platform account " + resolved["platform_account"],
        )
    target = Path(args.output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(resolved, indent=2, sort_keys=True) + "\n")
    target.chmod(0o600)
    for line in summary(resolved):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
