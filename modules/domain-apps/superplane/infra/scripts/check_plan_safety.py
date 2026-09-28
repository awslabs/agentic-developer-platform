"""Gate a Terraform plan before apply — Issue #5042 (U3), EPIC #4910.

## What this replaces, and why a rewrite rather than a patch

PR #5283's review reproduced two P1 defects in the apply lane's inline shell:

*   **Finding 1 — the destructive-approval gate failed open.** The step wrote

        DESTROYS=$(grep -c 'will be destroyed' plan.txt || echo "0")

    `grep -c` prints `0` AND exits 1 when it matches nothing, so `|| echo "0"` appended a
    second line: the variable became `"0\n0"`. The following `[ "$DESTROYS" -gt 0 ]` then
    raised `integer expression expected` — and because a command that fails *inside an
    `if` condition* is exempt from `set -e`, the shell took the `else` branch and printed
    "Proceeding". Reproduced: exit 0 on a plan destroying an ECR repository, and exit 0
    with "No destroys in plan" on a `must be replaced` plan.

*   **Finding 3 — the isolation guard proved nothing about ownership.** Matching plan TEXT
    for resource-type prefixes accepted both `module.core.aws_vpc.main will be destroyed`
    (the type is hidden behind `module.`) and `aws_iam_role.gateway will be destroyed`
    (the type is one we own; the instance is not).

The review's instruction was explicit — "do not just remove the extra echo while retaining
text-only delete detection" — so this reads `terraform show -json` and decides on structure.

## The three properties that make this fail closed

1.  **Any parse or lookup failure denies.** A malformed plan, an unreadable file, a `gh`
    API error and an ambiguous PR association all exit non-zero. The pre-fix version's
    defining flaw was that its error path was indistinguishable from its success path.
2.  **Deletion is detected from `change.actions`, not from prose.** A replacement is
    `["delete","create"]` or `["create","delete"]` depending on lifecycle, and both count.
3.  **Approval requires an EXACT label on a VERIFIED PR association.** Two independent things
    are needed here, and an earlier draft of this file had only the first:
    *   the label must match exactly — substring matching accepted
        `destructive-apply-approved-later`;
    *   the PR must be the one that MERGED this revision. A later checkpoint review found
        `_resolve_pr` using `gh pr list --search <SHA>`, a full-text search that returns any
        merged PR *mentioning* the SHA. A stub PR whose real merge commit was a different SHA
        approved a destroy plan and the guard exited 0. See `_resolve_pr` for what replaced
        it.

Exit codes: 0 = safe to apply, 1 = denied (with the reason on stderr).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from domain_ownership import OwnershipError, validate_plan  # noqa: E402

REQUIRED_LABEL = "destructive-apply-approved"


def _fail(message: str) -> int:
    print(f"::error::{message}", file=sys.stderr)
    print(f"DENIED: {message}")
    return 1


def _load_plan(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OwnershipError(f"could not read plan JSON at {path}: {exc}") from exc
    if not text.strip():
        raise OwnershipError(f"plan JSON at {path} is empty")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise OwnershipError(f"plan JSON at {path} is not valid JSON: {exc}") from exc


def _pr_labels(
    pr_number: str, gh: str = "gh", *, repository: str | None = None
) -> list[str]:
    """Fetch labels for a PR. Any failure raises — never returns an empty list on error.

    The distinction matters: "the PR has no labels" and "the label lookup failed" must not
    both be representable as `[]`, or a transient API error becomes an approval bypass.

    `repository` is passed through so the labels are read from the SAME repository the
    association was verified against. Without it `gh pr view 123` resolves against whatever
    remote the working directory has, which need not be the dispatched repository — the
    verified PR and the labelled PR could be two different objects that happen to share a
    number.
    """
    command = [gh, "pr", "view", pr_number]
    if repository:
        command += ["--repo", repository]
    command += ["--json", "labels", "--jq", ".labels[].name"]
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode != 0:
        raise OwnershipError(
            f"could not read labels for PR #{pr_number} (gh exit {proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _resolve_pr(
    commit_sha: str,
    gh: str = "gh",
    *,
    repository: str | None = None,
    expected_base: str | None = None,
) -> str:
    """Resolve the PR that a commit was actually MERGED BY, and verify the association.

    ## Why not a text search

    A checkpoint review of `84e3f7ee` reproduced the defect this replaces. The previous
    implementation ran

        gh pr list --state merged --search <COMMIT> --json number

    which is a full-text search. A merged PR that merely *mentions* the SHA — in its title,
    body or a comment — is returned by it. The review's stub returned exactly that: merged PR
    #999999, whose real merge commit was a different SHA, and the guard printed
    "PR #999999 carries the exact label. Approved." and exited 0 on a plan deleting an ECR
    repository. Checking that the search returned only ONE result does not help: one wrong
    answer is still wrong. Nothing in the old code path ever compared a commit.

    ## What is verified now

    The commit→PR association comes from the REST endpoint whose semantics are "pull requests
    associated with this commit" (`/commits/{sha}/pulls`), and then four properties of the
    returned PR must hold before its labels are consulted at all:

    1.  it is merged at all — an open PR's label cannot approve an apply of a merged revision;
    2.  its repository matches the repository this run is dispatched against;
    3.  its base branch matches, when the caller states one;
    4.  its `merge_commit_sha` IS the dispatched revision.

    (4) is the relationship that makes this an association rather than a mention. The apply
    lane runs on a push to the default branch, so `GITHUB_SHA` is the merge commit itself —
    for both merge-commit and squash strategies GitHub reports that commit as
    `merge_commit_sha`, so equality is the correct test and not an over-strict one. A commit
    that is merely *in* a PR's branch history is deliberately not accepted, because that is
    exactly what an unmerged or unrelated branch can also produce.

    Ambiguity (more than one associated PR) and truncation still deny.
    """
    args = [
        gh,
        "api",
        f"repos/{repository}/commits/{commit_sha}/pulls"
        if repository
        else f"repos/{{owner}}/{{repo}}/commits/{commit_sha}/pulls",
        "--jq",
        # One compact line per associated PR, so a truncated response cannot look like a
        # complete one with fewer entries.
        ".[] | [(.number|tostring), .merge_commit_sha, .base.ref, "
        '.base.repo.full_name, (.merged_at // "null")] | @tsv',
    ]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise OwnershipError(
            f"could not resolve the PR associated with commit {commit_sha} (gh exit "
            f"{proc.returncode}): {proc.stderr.strip()}"
        )

    rows = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 5:
            raise OwnershipError(
                f"malformed PR association row for commit {commit_sha}: {line!r}; refusing "
                f"to interpret a truncated or unexpected response"
            )
        rows.append(fields)

    if not rows:
        raise OwnershipError(
            f"no pull request is associated with commit {commit_sha}; cannot verify the "
            f"'{REQUIRED_LABEL}' label"
        )
    if len(rows) > 1:
        numbers = ", ".join(row[0] for row in rows)
        raise OwnershipError(
            f"commit {commit_sha} is associated with multiple pull requests ({numbers}); "
            f"refusing to guess which one carries approval"
        )

    number, merge_commit_sha, base_ref, base_repo, merged_at = rows[0]

    if merged_at in ("", "null"):
        raise OwnershipError(
            f"PR #{number} is associated with commit {commit_sha} but is not merged; an "
            f"unmerged PR's label cannot approve an apply of this revision"
        )

    if repository and base_repo != repository:
        raise OwnershipError(
            f"PR #{number} belongs to repository {base_repo!r}, but this run is dispatched "
            f"against {repository!r}; refusing a cross-repository approval"
        )

    if expected_base and base_ref != expected_base:
        raise OwnershipError(
            f"PR #{number} targets base branch {base_ref!r}, but this run expects "
            f"{expected_base!r}; refusing an approval from a different base"
        )

    # The association itself. `/commits/{sha}/pulls` can associate a PR with a commit that is
    # in its branch history without that commit being what merged — so the merge commit is
    # compared explicitly, and a mismatch denies rather than being tolerated.
    if not merge_commit_sha or not _sha_matches(merge_commit_sha, commit_sha):
        raise OwnershipError(
            f"PR #{number} is associated with commit {commit_sha}, but its merge commit is "
            f"{merge_commit_sha or 'unknown'} — the association is not a merge of the "
            f"dispatched revision, so its approval label refers to a different change"
        )

    return number


FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _sha_matches(left: str, right: str) -> bool:
    """Both values must be full 40-character hex SHAs and equal.

    Deliberately NOT prefix-tolerant. An earlier draft of this function accepted abbreviation
    on either side, comparing only the shorter length — which means a 7-character value would
    have satisfied a 40-character dispatched commit, and 7 hex characters collide often enough
    that git itself lengthens abbreviations to avoid it. Nothing needs the tolerance: both
    inputs here are full SHAs (`GITHUB_SHA` and the API's `merge_commit_sha`), so accepting a
    prefix would only ever have widened what counts as approval.

    A non-conforming value on either side returns False rather than raising, so the caller
    reports one denial reason — "the association is not a merge of this revision" — for both a
    mismatch and a malformed field.
    """
    return (
        FULL_SHA.match(left.strip().lower()) is not None
        and FULL_SHA.match(right.strip().lower()) is not None
        and left.strip().lower() == right.strip().lower()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-json", required=True, type=Path)
    parser.add_argument("--commit-sha", default="")
    parser.add_argument("--account-id", default="")
    parser.add_argument("--environment", default="")
    parser.add_argument(
        "--gh", default="gh", help="gh executable (overridden by tests)"
    )
    parser.add_argument(
        "--repository",
        default="",
        help=(
            "owner/name of the repository this run is dispatched against (GITHUB_REPOSITORY). "
            "The approving PR must belong to it, so an approval label cannot be imported "
            "from a fork or another repository."
        ),
    )
    parser.add_argument(
        "--base-ref",
        default="",
        help=(
            "branch the approving PR must have targeted (e.g. main). Omitted means any base "
            "is accepted; the merge-commit identity check applies either way."
        ),
    )
    parser.add_argument(
        "--expect-destroy",
        action="store_true",
        help=(
            "Validate a saved DESTROY plan: deletions are expected, so the label check is "
            "skipped (the destroy lane has its own typed module-name and account-ID gates), "
            "but per-instance ownership is still enforced — and a plan that deletes nothing "
            "is rejected as not being the destroy that was requested."
        ),
    )
    args = parser.parse_args(argv)

    try:
        plan = _load_plan(args.plan_json)
        report = validate_plan(
            plan,
            account_id=args.account_id or None,
            environment=args.environment or None,
        )
    except OwnershipError as exc:
        return _fail(f"Plan could not be validated, so it is not safe to apply: {exc}")

    print(f"Validated {report.checked} resource change(s) in the plan.")

    if not report.ok:
        print("Resources this domain does not own, or must not touch:")
        for violation in report.violations:
            print(f"  - {violation}")
        return _fail(
            "This plan touches resources outside the Superplane domain's ownership. A "
            "domain apply must consume platform interfaces read-only (platform isolation "
            "requirement, 2026-09-16). Nothing was applied."
        )
    print("Confirmed: every changed resource is domain-owned.")

    if args.expect_destroy:
        # A destroy plan's deletions are the point, so approval is not re-checked here —
        # the destroy lane gates on a typed module name and a typed account ID matched
        # against the caller's real identity. What still matters, and is enforced above, is
        # that every resource being deleted is one this domain owns.
        #
        # A destroy plan that deletes NOTHING is rejected: it means the saved plan is not the
        # destroy that was asked for (a stale plan file, or a plan taken without
        # `-destroy`), and applying it would be an unreviewed no-op at best.
        if not report.has_destructive_changes:
            return _fail(
                "A destroy was requested, but the saved plan contains no deletions. This is "
                "not the destroy that was requested — refusing to apply it."
            )
        print(
            f"Destroy plan validated: {len(report.destructive)} domain-owned deletion(s)."
        )
        for entry in report.destructive:
            print(f"  - {entry}")
        return 0

    if not report.has_destructive_changes:
        print("No destructive change (delete or replacement) in plan.")
        return 0

    print(f"Plan contains {len(report.destructive)} destructive change(s):")
    for entry in report.destructive:
        print(f"  - {entry}")

    if not args.commit_sha:
        return _fail(
            "Plan is destructive but no commit SHA was supplied, so the "
            f"'{REQUIRED_LABEL}' label cannot be verified."
        )

    try:
        pr_number = _resolve_pr(
            args.commit_sha,
            args.gh,
            repository=args.repository or None,
            expected_base=args.base_ref or None,
        )
        labels = _pr_labels(pr_number, args.gh, repository=args.repository or None)
    except OwnershipError as exc:
        return _fail(
            f"Plan is destructive and approval could not be verified: {exc}. Nothing was "
            f"applied."
        )

    # Exact match. `grep -c 'destructive-apply-approved'` also matched
    # `destructive-apply-approved-later` and `not-destructive-apply-approved`.
    if REQUIRED_LABEL not in labels:
        near = [
            label
            for label in labels
            if REQUIRED_LABEL in label or label in REQUIRED_LABEL
        ]
        if near:
            print(
                f"Labels resembling but not equal to the required label: {', '.join(near)}"
            )
        return _fail(
            f"This apply would make {len(report.destructive)} destructive change(s), but "
            f"PR #{pr_number} does not carry the exact '{REQUIRED_LABEL}' label "
            f"(labels: {', '.join(labels) or 'none'}). Add the label and re-run."
        )

    print(f"PR #{pr_number} carries the exact '{REQUIRED_LABEL}' label. Approved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
