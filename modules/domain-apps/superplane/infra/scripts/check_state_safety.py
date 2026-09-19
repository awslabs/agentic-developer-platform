"""Gate the STATE a destroy is about to act on — Issue #5042 (U3), EPIC #4910.

## Why state, and not configuration

`terraform destroy` deletes what is in STATE, not what the configuration declares. So the
question is not "does this module declare a VPC" — `tests/test_platform_isolation.py`
answers that at PR time by reading source — but "has a platform resource ever ENDED UP in
this state file", through an import, a `moved` block, a hand-edited state, or a resource
declared here once and later deleted from source without being removed from state. In that
last case the configuration is clean, the source-reading test passes, and destroy still
deletes the platform's VPC.

## What the previous version got wrong

PR #5283's review (finding 3) reproduced the state guard accepting `aws_iam_role.gateway`
and `aws_ecr_repository.gateway`, printing that they were domain-owned, and continuing to
destroy. Its allowlist was a type alternation, and both of those entries have a type this
module legitimately owns — the *instance* belongs to the gateway.

Type checking alone cannot fix that, because `terraform state list` prints addresses with
no values, so names like `gateway` are not distinguishable from `superplane_api` without
knowing the naming convention. This script therefore does two things:

1.  Validates every state address' leaf TYPE against the domain allowlist, resolving
     through module nesting so `module.core.aws_vpc.main` cannot hide behind `module.`.
2.  Requires the destroy lane to then produce a SAVED DESTROY PLAN and validate that
     (via `check_plan_safety.py --expect-destroy`, which sees the resource VALUES and so
     can attribute each instance by name/ARN), and to apply that same plan file.

Neither step alone is sufficient, and the split is why the destroy lane now saves a plan
instead of running `terraform destroy -auto-approve`.

Exit codes: 0 = state is domain-owned, 1 = denied, 2 = state is empty (nothing to destroy).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from domain_ownership import OwnershipError, validate_state_addresses  # noqa: E402

EXIT_OK = 0
EXIT_DENIED = 1
EXIT_EMPTY = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-list",
        required=True,
        type=Path,
        help="file containing `terraform state list` output",
    )
    parser.add_argument("--account-id", default="")
    parser.add_argument("--environment", default="")
    args = parser.parse_args(argv)

    try:
        text = args.state_list.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"::error::Could not read the state listing ({exc}). Refusing to destroy blind."
        )
        return EXIT_DENIED

    addresses = [line.strip() for line in text.splitlines() if line.strip()]
    if not addresses:
        print("State is empty — there is nothing to destroy.")
        return EXIT_EMPTY

    print(f"Resources in this module's state ({len(addresses)}):")
    for address in addresses:
        print(f"  {address}")

    try:
        report = validate_state_addresses(
            addresses,
            account_id=args.account_id or None,
            environment=args.environment or None,
        )
    except OwnershipError as exc:
        print(
            f"::error::Could not validate the state listing: {exc}. Nothing was destroyed."
        )
        return EXIT_DENIED

    if not report.ok:
        print(
            "::error::This module's state contains resources outside the domain's ownership."
        )
        print("A destroy would delete them. Nothing was destroyed.")
        for violation in report.violations:
            print(f"  - {violation}")
        print()
        print(
            "The Superplane domain app owns IAM roles/policies, ECR repositories and SSM "
            "parameters only. It consumes VPC, EKS, database and CloudFront through "
            "read-only platform interfaces and must never hold them in its own state "
            "(platform isolation requirement, 2026-09-16). Resolve with `terraform state "
            "rm` for anything the platform owns before retrying."
        )
        return EXIT_DENIED

    print(f"Confirmed: all {report.checked} state entries have domain-owned types.")
    print(
        "NOTE: this is a type-level check. `terraform state list` carries no values, so "
        "per-instance ownership is validated on the saved destroy plan by "
        "check_plan_safety.py --expect-destroy before anything is deleted."
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
