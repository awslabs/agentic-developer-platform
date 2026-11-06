"""Refresh a story's base without replacing its commits or forcing a push."""

import re
import subprocess


def reconcile_git_branch(repo, *, base_branch=None):
    def git(*args, check=True):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                              text=True, check=check, timeout=120)

    branch = git("branch", "--show-current").stdout.strip()
    if not branch or git("status", "--porcelain").stdout.strip():
        raise RuntimeError("Base reconciliation requires a clean story branch")
    if base_branch is None:
        remote = git("ls-remote", "--symref", "origin", "HEAD").stdout.splitlines()
        refs = [line.split()[1] for line in remote if line.startswith("ref: refs/heads/")]
        if len(refs) != 1:
            raise RuntimeError("Cannot determine the repository default branch")
        base_branch = refs[0].removeprefix("refs/heads/")
    git("check-ref-format", f"refs/heads/{base_branch}")
    if branch == base_branch:
        raise RuntimeError("Base reconciliation must not modify the default branch")
    shallow = git("rev-parse", "--is-shallow-repository").stdout.strip()
    fetch = ["fetch", "--no-tags"] + (["--unshallow"] if shallow == "true" else [])
    git(*fetch, "origin", f"refs/heads/{base_branch}:refs/remotes/origin/{base_branch}")
    head = git("rev-parse", "HEAD").stdout.strip()
    base = git("rev-parse", f"refs/remotes/origin/{base_branch}").stdout.strip()
    ancestor = git("merge-base", "--is-ancestor", base, head, check=False)
    result = {"branch": branch, "head_revision": head, "base_revision": base}
    if ancestor.returncode == 0:
        return {**result, "status": "up_to_date", "conflicts": []}
    if ancestor.returncode != 1:
        raise RuntimeError("Cannot verify story/base ancestry")
    git("update-ref", f"refs/adp/before-base-reconciliation/{head}", head)
    merged = git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                 "merge", "--no-edit", "--no-ff", base, check=False)
    conflicts = git("diff", "--name-only", "--diff-filter=U", "-z").stdout.rstrip("\0").split("\0")
    conflicts = [path for path in conflicts if path]
    if merged.returncode and not conflicts:
        raise RuntimeError("Story/base merge failed; previous head retained in refs/adp/before-base-reconciliation")
    # Pending conflicts remain in the index for the agent to resolve. No push,
    # reset, force update or automatic choice of either side occurs here.
    return {**result, "status": "conflict" if conflicts else "merged", "conflicts": conflicts}


def reconcile_mediated_branch():
    from lib import mediated_github as github

    result = github._request({"operation": "reconcile_work_branch"}).get("reconciliation", {})
    if result.get("status") == "conflict":
        raise github.MediatedConflict(
            f"Story branch {result.get('branch')} at {result.get('head_revision')} conflicts "
            f"with base {result.get('base_revision')}; no branch work was discarded. "
            "An authorized branch/PR repair must resolve the base merge before restarting. "
            "Restarting alone cannot resolve these conflicts."
        )
    if result.get("status") not in {"new_branch", "up_to_date", "merged"}:
        raise github.MediatedError("Story branch reconciliation did not return a verified result")
    return result


def reconciles_story_branch(persona, *, review_cycle=False):
    # Exact-PR review/repair preparation owns its revision and publication lease.
    # Report-only Codex roles intentionally read current main, without a branch.
    return not review_cycle and persona not in {
        "reviewer", "agent-codex-reviewer", "agent-codex-product",
        "agent-codex-pm", "agent-codex-intent-refinement",
    }


def verify_reconciled_snapshot(reconciliation, materialized):
    """Do not launch on a different head that appeared during archive transfer."""
    from lib import mediated_github as github

    expected = reconciliation.get("head_revision") or reconciliation.get("base_revision")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise github.MediatedError("Story reconciliation did not identify the prepared revision")
    if materialized.get("remote_head") != expected:
        raise github.MediatedConflict(
            "Story source moved between base reconciliation and materialization. "
            "Retry startup to reconcile the new revision; no work was discarded."
        )
