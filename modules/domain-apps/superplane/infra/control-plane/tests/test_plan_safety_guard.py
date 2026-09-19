"""The destructive-apply gate fails closed — Issue #5042 (U3), EPIC #4910.

## The reproduction these tests lock down

PR #5283's review (finding 1) showed the apply lane's label gate exiting **0** on a plan
that destroyed an ECR repository, while the PR carried only `enhancement`. The mechanism:

    DESTROYS=$(grep -c 'will be destroyed' plan.txt || echo "0")   # -> "0\n0"
    if [ "$DESTROYS" -gt 0 ]; then ...                             # -> integer expression expected

`grep -c` prints `0` and *also* exits 1, so `|| echo "0"` appended a second line. The
integer comparison then errored — and a command failing inside an `if` condition is exempt
from `set -e`, so the script continued into the `else` branch and printed "Proceeding".

A later checkpoint review of `84e3f7ee` found a second way the same gate failed open, after
the shell had been replaced: the guard resolved the approving PR with `gh pr list --search
<SHA>` — a full-text search — so a merged PR that merely *mentioned* the commit could lend its
approval label to a different revision. The "MERGE, not a mention" section below covers that.

Every test below executes the **real guard** (`check_plan_safety.py`) with a stub `gh` on
PATH-free invocation, so a regression in the guard fails these tests. The review's warning
is the design constraint: passing the existing mocked Terraform suite does not cover any of
these cases, because none of them were exercised.

## Why a stub `gh` rather than mocking Python functions

The guard's failure mode was in how it interpreted a *subprocess's* combination of stdout
and exit code. Patching the Python function that wraps `gh` would replace exactly the
boundary where the bug lived. So these tests write a small executable `gh` and pass its
path in, which keeps the subprocess handling under test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
GUARD = SCRIPTS_DIR / "check_plan_safety.py"


def _repo_root() -> Path:
    """Walk up to the directory containing `.github/`, rather than counting `parents[N]`."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".github" / "workflows").is_dir():
            return candidate
    raise AssertionError("could not locate the repository root from this test file")


sys.path.insert(0, str(Path(__file__).resolve().parent))

import source_derived_names as names  # noqa: E402

DIGEST_ACCOUNT = "879318057152"
ENVIRONMENT = "dev"
REGION = "us-east-1"
REPOSITORY = "aws-e/adp"
BASE_REF = "main"

# A realistic 40-character merge commit. The previous fixture SHA was `abc123`, which is short
# enough that no length or hex validation could have been exercised by it.
MERGE_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
OTHER_SHA = "ffeeddccbbaa99887766554433221100aabbccdd"

# Positive fixtures are DERIVED FROM THE TERRAFORM, not hand-written.
#
# The previous default was the literal `"adp-superplane-dev-api"`, a name this module never
# creates: `main.tf` builds `adp-${var.environment}-superplane-*`, so the real role is
# `adp-dev-superplane-control-plane`. Because both the guard and the fixture encoded the same
# wrong assumption, the suite passed while the guard denied every real resource — the defect
# the `84e3f7ee` checkpoint review reproduced. Deriving the name means a future rename of
# `local.name_prefix` moves the fixture with it, and a guard that disagrees fails here.
DOMAIN_ROLE_NAME = names.iam_role_names(ENVIRONMENT)[0]
DOMAIN_SSM_NAME = names.ssm_parameter_names(ENVIRONMENT)[0]
DOMAIN_ECR_NAME = names.ecr_repository_names()[0]


def _domain_values(name: str = DOMAIN_ROLE_NAME) -> dict:
    return {"name": name, "arn": f"arn:aws:iam::{DIGEST_ACCOUNT}:role/{name}"}


def _plan(*changes: dict) -> dict:
    return {"format_version": "1.2", "resource_changes": list(changes)}


def _change(address: str, actions: list[str], *, values: dict | None = None) -> dict:
    after = values if values is not None else _domain_values()
    before = after if "delete" in actions else None
    return {
        "address": address,
        "change": {"actions": actions, "before": before, "after": after},
    }


def _write_stub_gh(
    tmp_path: Path,
    *,
    pr_numbers: list[str],
    labels: list[str],
    exit_code: int = 0,
    merge_commit_sha: str | None = None,
    base_ref: str = BASE_REF,
    base_repo: str = REPOSITORY,
    merged_at: str = "2026-09-16T10:00:00Z",
    rows: list[str] | None = None,
    log: Path | None = None,
) -> Path:
    """A stub `gh` mimicking the real CLI's stdout/exit-code contract.

    Two subcommands are served, matching the two the guard now uses:

    *   `gh api repos/<owner>/<repo>/commits/<sha>/pulls --jq ...` — emits one TAB-separated
        row per associated PR: number, merge_commit_sha, base.ref, base.repo.full_name,
        merged_at. That is the shape the guard's `--jq` template produces, so the stub
        exercises the guard's real parsing.
    *   `gh pr view <n> --json labels --jq .labels[].name` — one label per line.

    `merge_commit_sha` defaults to the commit the guard was asked about, so the common case is
    a genuine merge. `rows` overrides row construction entirely, for malformed responses.
    `log` records each invocation, which is how a test can prove *which* API the guard called
    rather than only what it concluded.
    """
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"EXIT = {exit_code}\n"
        f"PRS = {pr_numbers!r}\n"
        f"LABELS = {labels!r}\n"
        f"MERGE_SHA = {merge_commit_sha!r}\n"
        f"BASE_REF = {base_ref!r}\n"
        f"BASE_REPO = {base_repo!r}\n"
        f"MERGED_AT = {merged_at!r}\n"
        f"ROWS = {rows!r}\n"
        f"LOG = {str(log) if log else None!r}\n"
        "argv = sys.argv[1:]\n"
        "if LOG:\n"
        "    with open(LOG, 'a') as fh:\n"
        "        fh.write(' '.join(argv) + '\\n')\n"
        "if EXIT:\n"
        "    sys.stderr.write('stub gh failure\\n')\n"
        "    sys.exit(EXIT)\n"
        "if argv[:2] == ['api', 'repos'] or (argv[:1] == ['api'] and '/pulls' in argv[1]):\n"
        "    if ROWS is not None:\n"
        "        sys.stdout.write(''.join(r + '\\n' for r in ROWS))\n"
        "    else:\n"
        "        # The commit the guard asked about, parsed out of the endpoint path.\n"
        "        asked = argv[1].split('/commits/')[1].split('/')[0]\n"
        "        merge = MERGE_SHA if MERGE_SHA is not None else asked\n"
        "        for number in PRS:\n"
        "            sys.stdout.write('\\t'.join(\n"
        "                [number, merge, BASE_REF, BASE_REPO, MERGED_AT]) + '\\n')\n"
        "elif argv[:2] == ['pr', 'view']:\n"
        "    sys.stdout.write(''.join(f'{l}\\n' for l in LABELS))\n"
        "else:\n"
        "    sys.stderr.write('stub gh: unexpected invocation %r\\n' % (argv,))\n"
        "    sys.exit(64)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return gh


def _run_guard(
    tmp_path: Path,
    plan: dict,
    *,
    gh: Path | None = None,
    commit_sha: str = MERGE_SHA,
    repository: str | None = REPOSITORY,
    base_ref: str | None = BASE_REF,
) -> subprocess.CompletedProcess:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    argv = [
        sys.executable,
        str(GUARD),
        "--plan-json",
        str(plan_path),
        "--commit-sha",
        commit_sha,
        "--account-id",
        DIGEST_ACCOUNT,
        "--environment",
        ENVIRONMENT,
    ]
    if repository is not None:
        argv += ["--repository", repository]
    if base_ref is not None:
        argv += ["--base-ref", base_ref]
    if gh is not None:
        argv += ["--gh", str(gh)]
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


# ---------------------------------------------------------------------------
# Finding 1: the exact reproduced case.
# ---------------------------------------------------------------------------


def test_delete_without_label_is_denied(tmp_path):
    """The review's reproduction: a destroy plan + a PR labelled only `enhancement`.

    The pre-fix step exited 0 here and printed "Proceeding".
    """
    gh = _write_stub_gh(tmp_path, pr_numbers=["5283"], labels=["enhancement"])
    plan = _plan(
        _change(
            'aws_ecr_repository.superplane["adp-superplane-api"]',
            ["delete"],
            values={"name": "adp-superplane-api"},
        )
    )
    result = _run_guard(tmp_path, plan, gh=gh)

    assert result.returncode != 0, (
        "a destroy plan was approved without the label — this is the exact fail-open "
        f"reproduction from the review.\nstdout:\n{result.stdout}"
    )
    assert "destructive-apply-approved" in result.stdout


@pytest.mark.parametrize(
    "actions",
    [
        ["delete"],
        ["delete", "create"],  # destroy-then-create replacement
        ["create", "delete"],  # create-before-destroy replacement
    ],
    ids=["delete", "replace-delete-first", "replace-create-first"],
)
def test_all_deleting_action_sets_require_approval(tmp_path, actions):
    """Both replacement orderings count as destructive.

    The pre-fix guard reported "No destroys in plan" for a `must be replaced` plan, because
    it matched the literal text `will be destroyed`.
    """
    gh = _write_stub_gh(tmp_path, pr_numbers=["5283"], labels=["enhancement"])
    result = _run_guard(
        tmp_path, _plan(_change("aws_iam_role.superplane", actions)), gh=gh
    )
    assert result.returncode != 0, (
        f"actions {actions} were not treated as destructive.\nstdout:\n{result.stdout}"
    )


def test_delete_with_exact_label_is_approved(tmp_path):
    """The guard must still permit an approved destroy — it fails closed, not shut."""
    gh = _write_stub_gh(
        tmp_path,
        pr_numbers=["5283"],
        labels=["enhancement", "destructive-apply-approved"],
    )
    result = _run_guard(
        tmp_path, _plan(_change("aws_iam_role.superplane", ["delete"])), gh=gh
    )
    assert result.returncode == 0, (
        f"an approved destroy was blocked:\n{result.stdout}\n{result.stderr}"
    )
    assert "Approved" in result.stdout


@pytest.mark.parametrize(
    "label",
    [
        "destructive-apply-approved-later",
        "not-destructive-apply-approved",
        "destructive-apply",
    ],
)
def test_near_match_label_is_rejected(tmp_path, label):
    """Substring matching accepted labels that are not the label.

    `grep -c 'destructive-apply-approved'` matches `destructive-apply-approved-later`.
    """
    gh = _write_stub_gh(tmp_path, pr_numbers=["5283"], labels=[label])
    result = _run_guard(
        tmp_path, _plan(_change("aws_iam_role.superplane", ["delete"])), gh=gh
    )
    assert result.returncode != 0, (
        f"near-match label {label!r} was accepted as approval"
    )


def test_safe_addition_needs_no_approval(tmp_path):
    """A create-only plan proceeds without a label — the guard is not a blanket block."""
    result = _run_guard(tmp_path, _plan(_change("aws_iam_role.superplane", ["create"])))
    assert result.returncode == 0, f"a create-only plan was blocked:\n{result.stdout}"
    assert "No destructive change" in result.stdout


def test_no_op_plan_is_approved(tmp_path):
    result = _run_guard(tmp_path, _plan())
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# Fail-closed on lookup and parse failures.
# ---------------------------------------------------------------------------


def test_gh_api_failure_denies(tmp_path):
    """A transient API error must not read as "no label present" or as approval."""
    gh = _write_stub_gh(tmp_path, pr_numbers=[], labels=[], exit_code=1)
    result = _run_guard(
        tmp_path, _plan(_change("aws_iam_role.superplane", ["delete"])), gh=gh
    )
    assert result.returncode != 0, "a gh failure did not deny the destructive apply"


def test_no_associated_pr_denies(tmp_path):
    gh = _write_stub_gh(tmp_path, pr_numbers=[], labels=[])
    result = _run_guard(
        tmp_path, _plan(_change("aws_iam_role.superplane", ["delete"])), gh=gh
    )
    assert result.returncode != 0


def test_ambiguous_pr_association_denies(tmp_path):
    """Two merged PRs for one commit: refuse rather than take `.[0]`.

    The pre-fix code used `--jq '.[0].number'`, approving on whichever sorted first.
    """
    gh = _write_stub_gh(
        tmp_path, pr_numbers=["5283", "5999"], labels=["destructive-apply-approved"]
    )
    result = _run_guard(
        tmp_path, _plan(_change("aws_iam_role.superplane", ["delete"])), gh=gh
    )
    assert result.returncode != 0, (
        "an ambiguous PR association was resolved by guessing"
    )
    assert "multiple" in result.stdout.lower()


# ---------------------------------------------------------------------------
# The PR association must be a MERGE of this revision, not a mention of it.
#
# Reproduced in the `84e3f7ee` checkpoint review: `_resolve_pr` ran
#     gh pr list --state merged --search <SHA> --json number
# a FULL-TEXT search. A stub returning merged PR #999999 — whose real merge commit was a
# different SHA, and which carried `destructive-apply-approved` — made the guard print
# "PR #999999 carries the exact label. Approved." and exit 0 on a plan deleting an ECR
# repository. The recorded call log showed only the text search and the label lookup: nothing
# had ever compared a commit.
#
# Note what the pre-existing duplicate/no-result tests above could NOT catch. Both concern how
# MANY rows come back. This class of defect returns exactly one row, and it is wrong.
# ---------------------------------------------------------------------------


def test_uniquely_returned_approved_pr_from_a_different_merge_is_denied(tmp_path):
    """The reproduction. One result, approved, merged — but it merged a different commit.

    This is the case the review named: "a uniquely returned, approved PR belongs to a
    different merge; duplicate/no-result tests alone do not exercise this case."
    """
    gh = _write_stub_gh(
        tmp_path,
        pr_numbers=["999999"],
        labels=["destructive-apply-approved"],
        merge_commit_sha=OTHER_SHA,
    )
    result = _run_guard(
        tmp_path,
        _plan(
            _change(
                'aws_ecr_repository.superplane["adp-superplane-api"]',
                ["delete"],
                values={"name": "adp-superplane-api"},
            )
        ),
        gh=gh,
        commit_sha=MERGE_SHA,
    )
    assert result.returncode != 0, (
        "an approval label on a PR that merged a DIFFERENT commit authorised this destroy — "
        f"the exact fail-open the review reproduced.\nstdout:\n{result.stdout}"
    )
    assert OTHER_SHA in result.stdout, (
        "the denial does not say which merge commit was actually found, so an operator "
        f"cannot tell why it was refused:\n{result.stdout}"
    )


def test_association_is_read_from_the_commit_endpoint_not_a_text_search(tmp_path):
    """Assert on the API the guard CALLS, not only on its conclusion.

    A future edit could reintroduce `gh pr list --search` and still deny this suite's negative
    cases by accident. Recording invocations pins the mechanism: the commit's associated-PR
    endpoint must be consulted, and no full-text search may appear.
    """
    log = tmp_path / "gh-calls.log"
    gh = _write_stub_gh(
        tmp_path,
        pr_numbers=["5283"],
        labels=["destructive-apply-approved"],
        log=log,
    )
    result = _run_guard(
        tmp_path, _plan(_change("aws_iam_role.superplane", ["delete"])), gh=gh
    )
    assert result.returncode == 0, f"an approved destroy was blocked:\n{result.stdout}"

    calls = log.read_text(encoding="utf-8")
    assert f"/commits/{MERGE_SHA}/pulls" in calls, (
        f"the guard did not resolve the PR from the commit's associations:\n{calls}"
    )
    assert "--search" not in calls, (
        f"the guard is still resolving the PR by full-text search:\n{calls}"
    )


def test_unmerged_pr_cannot_approve(tmp_path):
    """An OPEN PR is associated with its own commits. Its label must not approve an apply."""
    gh = _write_stub_gh(
        tmp_path,
        pr_numbers=["5283"],
        labels=["destructive-apply-approved"],
        merged_at="null",
    )
    result = _run_guard(
        tmp_path, _plan(_change("aws_iam_role.superplane", ["delete"])), gh=gh
    )
    assert result.returncode != 0, "an unmerged PR's label approved a destructive apply"
    assert "not merged" in result.stdout


def test_pr_from_another_repository_cannot_approve(tmp_path):
    """A label is only meaningful in the repository whose review process applied it."""
    gh = _write_stub_gh(
        tmp_path,
        pr_numbers=["5283"],
        labels=["destructive-apply-approved"],
        base_repo="someone-else/adp-fork",
    )
    result = _run_guard(
        tmp_path, _plan(_change("aws_iam_role.superplane", ["delete"])), gh=gh
    )
    assert result.returncode != 0, "a cross-repository approval was accepted"
    assert "adp-fork" in result.stdout


def test_pr_targeting_a_different_base_cannot_approve(tmp_path):
    gh = _write_stub_gh(
        tmp_path,
        pr_numbers=["5283"],
        labels=["destructive-apply-approved"],
        base_ref="some-feature-branch",
    )
    result = _run_guard(
        tmp_path, _plan(_change("aws_iam_role.superplane", ["delete"])), gh=gh
    )
    assert result.returncode != 0, (
        "an approval from a different base branch was accepted"
    )


@pytest.mark.parametrize(
    "merge_commit_sha",
    [
        "",
        "null",
        "not-hex-at-all",
        MERGE_SHA[:7],
        MERGE_SHA[:12],
        MERGE_SHA[:39],
        MERGE_SHA + "0",
        MERGE_SHA[:-1] + "g",
    ],
    ids=[
        "empty",
        "null",
        "non-hex",
        "abbrev-7",
        "abbrev-12",
        "abbrev-39",
        "too-long",
        "non-hex-final-char",
    ],
)
def test_unusable_merge_commit_value_denies(tmp_path, merge_commit_sha):
    """A missing, abbreviated or malformed merge commit is a failed verification, not a pass.

    The abbreviations are the sharp cases: each is a genuine PREFIX of the dispatched commit,
    so a `startswith`, or a comparison truncated to the shorter of the two, would accept them.
    Both values here are full SHAs in the real lane, so no abbreviation is legitimate.
    """
    gh = _write_stub_gh(
        tmp_path,
        pr_numbers=["5283"],
        labels=["destructive-apply-approved"],
        merge_commit_sha=merge_commit_sha,
    )
    result = _run_guard(
        tmp_path, _plan(_change("aws_iam_role.superplane", ["delete"])), gh=gh
    )
    assert result.returncode != 0, (
        f"merge commit {merge_commit_sha!r} was accepted as matching {MERGE_SHA}"
    )


def test_merge_commit_case_difference_still_matches(tmp_path):
    """Case is normalised, not treated as a mismatch.

    Hex case names the SAME commit, so denying an uppercase value would be strictness that
    buys no safety and could block a legitimate approved destroy. Recorded as a deliberate
    decision, because the surrounding tests all deny and this one must not look like an
    oversight in them.
    """
    gh = _write_stub_gh(
        tmp_path,
        pr_numbers=["5283"],
        labels=["destructive-apply-approved"],
        merge_commit_sha=MERGE_SHA.upper(),
    )
    result = _run_guard(
        tmp_path, _plan(_change("aws_iam_role.superplane", ["delete"])), gh=gh
    )
    assert result.returncode == 0, (
        f"an uppercase spelling of the dispatched commit was refused:\n{result.stdout}"
    )


def test_malformed_dispatched_commit_sha_denies(tmp_path):
    """The value the guard is TOLD to verify must itself be a full SHA.

    Symmetry matters here. If only the API's field were validated, then passing
    `--commit-sha ""` or a short value would make the comparison unsatisfiable-or-trivial
    depending on which side the check ran on. Both sides are validated, so a caller cannot
    weaken the check through its own input.
    """
    gh = _write_stub_gh(
        tmp_path,
        pr_numbers=["5283"],
        labels=["destructive-apply-approved"],
        merge_commit_sha=MERGE_SHA,
    )
    result = _run_guard(
        tmp_path,
        _plan(_change("aws_iam_role.superplane", ["delete"])),
        gh=gh,
        commit_sha=MERGE_SHA[:7],
    )
    assert result.returncode != 0, (
        "an abbreviated dispatched commit was accepted, so the merge comparison was not "
        f"actually made:\n{result.stdout}"
    )


@pytest.mark.parametrize(
    "row",
    [
        "5283",
        "5283\t" + MERGE_SHA,
        "5283\t" + MERGE_SHA + "\tmain",
        "5283\t" + MERGE_SHA + "\tmain\t" + REPOSITORY,
        "5283\t"
        + MERGE_SHA
        + "\tmain\t"
        + REPOSITORY
        + "\t2026-09-16T10:00:00Z\textra",
    ],
    ids=["one-field", "two", "three", "four-truncated", "six-unexpected"],
)
def test_truncated_or_unexpected_association_row_denies(tmp_path, row):
    """A short row must not silently read as "these fields were absent, so unconstrained".

    The four-field case is the dangerous one: dropping only the trailing `merged_at` would, on
    a positional unpack that tolerated it, leave the merge check comparing the wrong field.
    """
    gh = _write_stub_gh(
        tmp_path,
        pr_numbers=["5283"],
        labels=["destructive-apply-approved"],
        rows=[row],
    )
    result = _run_guard(
        tmp_path, _plan(_change("aws_iam_role.superplane", ["delete"])), gh=gh
    )
    assert result.returncode != 0, f"malformed association row {row!r} was accepted"


def test_labels_are_read_from_the_verified_repository(tmp_path):
    """`gh pr view <n>` without `--repo` resolves against the checkout's remote.

    The verified PR and the labelled PR would then be able to differ. Pinned by asserting the
    label lookup names the same repository the association was checked against.
    """
    log = tmp_path / "gh-calls.log"
    gh = _write_stub_gh(
        tmp_path,
        pr_numbers=["5283"],
        labels=["destructive-apply-approved"],
        log=log,
    )
    _run_guard(tmp_path, _plan(_change("aws_iam_role.superplane", ["delete"])), gh=gh)

    view_calls = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith("pr view")
    ]
    assert view_calls, "the guard never looked up labels"
    for call in view_calls:
        assert f"--repo {REPOSITORY}" in call, (
            f"the label lookup is not scoped to the verified repository: {call!r}"
        )


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "not json at all",
        "[]",
        '{"resource_changes": "not-a-list"}',
        '{"format_version":"1.2","resource_changes":[{"address":"aws_iam_role.x"}]}',
    ],
    ids=[
        "empty",
        "whitespace",
        "invalid-json",
        "bare-list",
        "changes-not-list",
        "change-missing-actions",
    ],
)
def test_malformed_plan_denies(tmp_path, body):
    """A guard that cannot parse its input must refuse, not assume safety."""
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(body, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(GUARD),
            "--plan-json",
            str(plan_path),
            "--commit-sha",
            "abc",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, f"malformed plan {body!r} was accepted as safe"


def test_missing_plan_file_denies(tmp_path):
    result = subprocess.run(
        [sys.executable, str(GUARD), "--plan-json", str(tmp_path / "absent.json")],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_apply_lane_supplies_the_association_inputs():
    """The real lane must PASS `--repository` and `--base-ref`, or those checks are inert.

    Both default to "unset means unconstrained", so a lane that forgets them keeps exiting 0
    on cases this suite proves are denied — a guard that is present but not actually guarding.
    That is the same shape as the earlier undeclared-`SP_ACCOUNT_ID` bug: it failed closed only
    because the step aborted, which is not the same as the check having run.

    Asserted against the workflow file because no unit test of the script can see whether the
    caller supplies its inputs.
    """
    workflow = (
        _repo_root() / ".github" / "workflows" / "superplane-infra-apply.yml"
    ).read_text(encoding="utf-8")
    doc = yaml.safe_load(workflow)
    job = next(iter(doc["jobs"].values()))
    guard_steps = [
        step
        for step in job["steps"]
        if "check_plan_safety.py" in (step.get("run") or "")
    ]
    assert guard_steps, "the apply lane no longer invokes the plan-safety guard at all"

    for step in guard_steps:
        run = step["run"]
        assert "--repository" in run, (
            "the apply lane does not pass --repository, so an approval label could be "
            "honoured from a fork or another repository"
        )
        assert "--base-ref" in run, (
            "the apply lane does not pass --base-ref, so an approval from an unrelated base "
            "branch would be honoured"
        )
        assert "--commit-sha" in run, (
            "the apply lane does not pass --commit-sha, so there is nothing to verify the "
            "PR association against"
        )


# ---------------------------------------------------------------------------
# Finding 3: ownership, not prefixes.
# ---------------------------------------------------------------------------


def test_nested_module_platform_vpc_is_denied(tmp_path):
    """`module.core.aws_vpc.main` — the type is hidden behind the module prefix.

    The pre-fix regex was anchored with `^  # (aws_vpc\\.|...)`, so it never saw this.
    """
    plan = _plan(
        {
            "address": "module.core.aws_vpc.main",
            "change": {
                "actions": ["delete"],
                "before": {"id": "vpc-123"},
                "after": None,
            },
        }
    )
    result = _run_guard(
        tmp_path,
        plan,
        gh=_write_stub_gh(
            tmp_path, pr_numbers=["1"], labels=["destructive-apply-approved"]
        ),
    )
    assert result.returncode != 0, "a nested-module VPC destroy was permitted"
    assert "aws_vpc" in result.stdout


def test_allowed_type_with_foreign_name_is_denied(tmp_path):
    """`aws_iam_role.gateway` — a type we own, an instance we do not.

    Explicitly named in the review: the pre-fix guard accepted this.
    """
    plan = _plan(
        _change(
            "aws_iam_role.gateway",
            ["delete"],
            values={
                "name": "bedrockgw-dev-role",
                "arn": f"arn:aws:iam::{DIGEST_ACCOUNT}:role/bedrockgw-dev-role",
            },
        )
    )
    result = _run_guard(
        tmp_path,
        plan,
        gh=_write_stub_gh(
            tmp_path, pr_numbers=["1"], labels=["destructive-apply-approved"]
        ),
    )
    assert result.returncode != 0, (
        "a destroy of the gateway's IAM role was permitted because the TYPE is one this "
        "module legitimately creates"
    )


def test_near_miss_resource_type_is_denied(tmp_path):
    """`aws_iam_role_policies_exclusive` deletes inline policies and is not allowlisted."""
    plan = _plan(_change("aws_iam_role_policies_exclusive.superplane", ["create"]))
    result = _run_guard(tmp_path, plan)
    assert result.returncode != 0, "a near-miss resource type passed the allowlist"


def test_foreign_account_arn_is_denied(tmp_path):
    """A plan whose ARNs name another account is targeting somewhere nobody selected."""
    plan = _plan(
        _change(
            "aws_iam_role.superplane",
            ["create"],
            values={
                "name": DOMAIN_ROLE_NAME,
                "arn": f"arn:aws:iam::605440105851:role/{DOMAIN_ROLE_NAME}",
            },
        )
    )
    result = _run_guard(tmp_path, plan)
    assert result.returncode != 0, (
        "a plan naming upstream's account 605440105851 was accepted"
    )
    assert "605440105851" in result.stdout


def test_cross_environment_resource_is_denied(tmp_path):
    """A dev run must not plan changes to another environment's resources.

    This is the blast radius the per-environment state key exists to prevent ("two
    environments share state; one apply destroys the other's resources").
    """
    plan = _plan(
        _change(
            "aws_iam_role.superplane_api",
            ["delete"],
            # The same role, named for a DIFFERENT environment — derived the same way, so
            # this is the real prod name rather than an invented one.
            values={
                "name": names.iam_role_names("prod")[0],
                "arn": (
                    f"arn:aws:iam::{DIGEST_ACCOUNT}:role/"
                    f"{names.iam_role_names('prod')[0]}"
                ),
            },
        )
    )
    result = _run_guard(
        tmp_path,
        plan,
        gh=_write_stub_gh(
            tmp_path, pr_numbers=["1"], labels=["destructive-apply-approved"]
        ),
    )
    assert result.returncode != 0, "a dev run was allowed to destroy a prod-named role"
    assert "prod" in result.stdout


def test_environment_independent_ecr_name_is_accepted(tmp_path):
    """ECR repository names carry no environment segment, by design.

    U2's lock names `adp-superplane-api` because a repository holds images, which are
    environment-independent. An earlier draft of this guard required the environment token
    in every identifier and wrongly rejected the real plan; this test pins the correction
    so it cannot regress into over-blocking.
    """
    plan = _plan(
        _change(
            'aws_ecr_repository.superplane["adp-superplane-api"]',
            ["create"],
            values={"name": "adp-superplane-api"},
        )
    )
    result = _run_guard(tmp_path, plan)
    assert result.returncode == 0, (
        f"the real ECR repository name was rejected:\n{result.stdout}"
    )


def test_unattributable_resource_is_denied(tmp_path):
    """No identifying value -> deny unknown ownership rather than assume."""
    plan = _plan(
        {
            "address": "aws_iam_role.mystery",
            "change": {"actions": ["create"], "before": None, "after": {}},
        }
    )
    result = _run_guard(tmp_path, plan)
    assert result.returncode != 0


def test_domain_owned_plan_is_accepted(tmp_path):
    """The positive case: a real domain plan must pass all of the above."""
    plan = _plan(
        _change("aws_iam_role.superplane_api", ["create"]),
        _change(
            f'aws_ecr_repository.superplane["{DOMAIN_ECR_NAME}"]',
            ["create"],
            values={"name": DOMAIN_ECR_NAME},
        ),
        _change(
            "aws_ssm_parameter.namespace",
            ["create"],
            values={"name": DOMAIN_SSM_NAME},
        ),
        {
            "address": "data.aws_eks_cluster.platform",
            "change": {
                "actions": ["read"],
                "before": None,
                "after": {"name": "adp-dev"},
            },
        },
    )
    result = _run_guard(tmp_path, plan)
    assert result.returncode == 0, (
        f"a legitimate domain plan was rejected:\n{result.stdout}\n{result.stderr}"
    )
    assert "every changed resource is domain-owned" in result.stdout
