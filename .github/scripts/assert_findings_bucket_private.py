#!/usr/bin/env python3
"""Assert the nightly findings bucket is not publicly readable
(intent #4290, unit U5, defect #4533).

Why this script exists instead of the one-liner it replaces
-----------------------------------------------------------
The documented smoke check was::

    aws s3api get-bucket-policy-status --bucket <b> --query PolicyStatus.IsPublic

``GetBucketPolicyStatus`` reports the public-ness of a bucket *policy*, and it
is only defined for buckets that have one. The findings bucket deliberately has
none — ``platform/infra/modules/security-scans/main.tf`` locks it down with an
``aws_s3_bucket_public_access_block`` and attaches no ``aws_s3_bucket_policy``
— so the call raises ``NoSuchBucketPolicy`` and the check can never return
``false``. It failed 100% of runs on the deployed target while the bucket was in
fact private, which is worse than no check: the natural reactions are to halt a
healthy run or to stop reading the step. Either way the intent's "findings are
never public" criterion went unverified.

So the assertion here checks the property that actually governs public access,
and treats "no bucket policy at all" as the pass it is.

The predicate, in the order S3 itself evaluates access
-----------------------------------------------------
1. ``GetPublicAccessBlock`` — all four flags must be ``true``. Checked
   individually, not merely for the configuration's presence: a block with
   ``RestrictPublicBuckets`` flipped off is a materially different bucket from
   one with all four set. ``RestrictPublicBuckets=true`` is the *governing*
   control, because with it a public policy cannot take effect even if one is
   attached.
2. ``GetBucketPolicyStatus`` — ``IsPublic`` must be ``false`` **when a policy
   exists**. ``NoSuchBucketPolicy`` is an explicit pass: no policy means no
   policy-granted public access. It is matched on its error code and nothing
   else, so it cannot mask a different failure.
3. Everything else fails. ``AccessDenied``, throttling, a missing bucket and an
   absent public-access-block configuration are all *unknowns*, and an unknown
   is not a pass. There is no ``|| true`` anywhere in this file — a check that
   is green regardless of the bucket's state is the vacuous-gate failure this
   EPIC already hit once (#4517).

Why the ACL leg is absent
-------------------------
An ACL cannot grant public access here: ``BlockPublicAcls`` and
``IgnorePublicAcls`` being true makes any public grant both un-settable and
inert, and the bucket additionally runs ``BucketOwnerEnforced`` ownership, which
disables ACLs outright. Asserting step 1 therefore already covers the ACL path;
a separate ``get-bucket-acl`` leg would add a call whose result cannot change
the verdict.

Usage
-----
::

    python3 .github/scripts/assert_findings_bucket_private.py --bucket <bucket>

Exit 0 means private. Exit 1 prints a named cause on stderr as a GitHub
workflow error annotation.
"""

from __future__ import annotations

import argparse
import sys

# All four must be true. Named individually rather than iterated over whatever
# keys the API happens to return: a flag that stops being returned would
# otherwise silently stop being checked.
REQUIRED_PUBLIC_ACCESS_BLOCK_FLAGS = (
    "BlockPublicAcls",
    "IgnorePublicAcls",
    "BlockPublicPolicy",
    "RestrictPublicBuckets",
)

# The one error code that means "private" rather than "unknown": a bucket with
# no policy has no policy-granted public access.
NO_SUCH_BUCKET_POLICY = "NoSuchBucketPolicy"


class BucketPrivacyError(RuntimeError):
    """The bucket is public, or its privacy could not be established.

    Both cases are failures, and deliberately share one type: an unattended
    nightly must not distinguish "proven public" from "unknown" by passing one
    of them.
    """


def _error_code(exc: Exception) -> str:
    """The AWS error code from a botocore ClientError, or "" for anything else.

    Read defensively so a non-botocore exception reaching here cannot be
    mistaken for a recognised, passable error code.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    if not isinstance(error, dict):
        return ""
    return str(error.get("Code") or "")


def assert_public_access_block(s3_client, bucket: str) -> dict:
    """Step 1: every public-access-block flag is true. Returns the flags.

    An absent configuration raises rather than passing:
    ``NoSuchPublicAccessBlockConfiguration`` means nothing is blocking public
    access, which is the opposite of the property being asserted.
    """
    try:
        response = s3_client.get_public_access_block(Bucket=bucket)
    except Exception as exc:
        code = _error_code(exc) or type(exc).__name__
        raise BucketPrivacyError(
            f"cannot read the public-access-block on {bucket} ({code}). "
            "This is the control that governs public access, so an unreadable "
            "one leaves privacy unproven and is treated as a failure. If the "
            "code is AccessDenied, the calling role is missing "
            "s3:GetBucketPublicAccessBlock on this bucket; if it is "
            "NoSuchPublicAccessBlockConfiguration, the bucket has no block at "
            "all and platform/infra/modules/security-scans/main.tf has "
            "regressed."
        ) from exc

    config = response.get("PublicAccessBlockConfiguration") or {}
    unset = [
        flag
        for flag in REQUIRED_PUBLIC_ACCESS_BLOCK_FLAGS
        if config.get(flag) is not True
    ]
    if unset:
        raise BucketPrivacyError(
            f"public-access-block on {bucket} does not set {', '.join(unset)} "
            "to true, so public access is not fully blocked. Expected all of "
            f"{', '.join(REQUIRED_PUBLIC_ACCESS_BLOCK_FLAGS)}; see "
            "platform/infra/modules/security-scans/main.tf."
        )
    return config


def assert_bucket_policy_not_public(s3_client, bucket: str) -> bool:
    """Step 2: no bucket policy, or one that is not public.

    Returns True when a policy exists and is non-public, False when no policy
    exists at all. Both are passes; the distinction is returned only so the
    caller can say which one it observed, because "no policy" and "a
    non-public policy" are different bucket states that a reader of the log
    should be able to tell apart.
    """
    try:
        response = s3_client.get_bucket_policy_status(Bucket=bucket)
    except Exception as exc:
        if _error_code(exc) == NO_SUCH_BUCKET_POLICY:
            # The deployed shape, and the case the replaced one-liner treated
            # as an error. No policy => no policy-granted public access.
            return False
        code = _error_code(exc) or type(exc).__name__
        raise BucketPrivacyError(
            f"cannot read the bucket-policy status on {bucket} ({code}). Only "
            f"{NO_SUCH_BUCKET_POLICY} means 'no policy, therefore private'; "
            "every other error leaves the policy's public-ness unknown and is "
            "treated as a failure. If the code is AccessDenied, the calling "
            "role is missing s3:GetBucketPolicyStatus on this bucket."
        ) from exc

    if (response.get("PolicyStatus") or {}).get("IsPublic") is True:
        raise BucketPrivacyError(
            f"the bucket policy on {bucket} makes it PUBLIC. Whole-repo "
            "security findings are published here; treat this as an incident, "
            "not a test failure."
        )
    return True


def assert_bucket_is_private(s3_client, bucket: str) -> dict:
    """Run the full predicate. Raises BucketPrivacyError unless private.

    Returns the observed state so the caller can log what it actually saw
    rather than a bare "ok".
    """
    flags = assert_public_access_block(s3_client, bucket)
    has_policy = assert_bucket_policy_not_public(s3_client, bucket)
    return {
        "bucket": bucket,
        "publicAccessBlock": {
            flag: flags.get(flag) for flag in REQUIRED_PUBLIC_ACCESS_BLOCK_FLAGS
        },
        "hasBucketPolicy": has_policy,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Assert an S3 bucket is not publicly readable. Exits 0 when "
            "private, 1 with a named cause otherwise."
        )
    )
    parser.add_argument(
        "--bucket",
        required=True,
        help="Bucket to check, e.g. adp-dev-security-scans-<account>.",
    )
    return parser


def main(argv: list[str] | None = None, s3_client=None) -> int:
    args = build_parser().parse_args(argv)

    if s3_client is None:
        import boto3  # noqa: PLC0415 - imported late so --help works unprovisioned

        s3_client = boto3.client("s3")

    try:
        state = assert_bucket_is_private(s3_client, args.bucket)
    except BucketPrivacyError as exc:
        print(f"::error title=Findings bucket privacy::{exc}", file=sys.stderr)
        return 1

    policy_note = (
        "a non-public bucket policy"
        if state["hasBucketPolicy"]
        else "no bucket policy (nothing to grant public access)"
    )
    print(
        f"{args.bucket} is private: all four public-access-block flags are "
        f"true, and there is {policy_note}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
