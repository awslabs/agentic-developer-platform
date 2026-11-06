"""Reapply an operations repair onto its current mediated branch snapshot."""

import re
import subprocess
import tempfile
from pathlib import Path

from lib import mediated_github as github


def unmerged_paths(repo):
    return sorted(set(github._git(repo, "diff", "--name-only", "--diff-filter=U", "-z").split("\0")) - {""})


def stage_resolved_conflict(repo, path):
    """Only an explicit, validated repair edit can resolve a conflicted path."""
    if not (Path(repo) / ".git").exists():
        return
    if path not in unmerged_paths(repo):
        return
    content = (Path(repo) / path).read_bytes()
    if re.search(br"(?m)^(<<<<<<< |\|\|\|\|\|\|\| |=======$|>>>>>>> )", content):
        return
    github._git(repo, "add", "--", path)


def refresh_source(repo, state, *, confirmed_head, base):
    """Keep remote edits and replay the actual local delta using Git three-way apply.

    The snapshot is fetched through existing mediated authority; no provider
    credential or arbitrary remote is introduced. An unchanged branch does not
    cause an archive fetch or a gratuitous rebase onto moving main.
    """
    current = state["branch_head"] or state["default_branch_head"]
    if current == confirmed_head:
        return False
    conflicts = unmerged_paths(repo)
    if conflicts:
        raise github.MediatedConflict("resolve the current repair conflicts before refreshing: " + ", ".join(conflicts))

    github.collect_changes(repo, base=base)
    original_tree = github._git(repo, "write-tree").strip()
    patch = github._git(repo, "diff", "--cached", "--binary", "--full-index", base, binary=True)
    with tempfile.TemporaryDirectory(prefix="adp-repair-source-") as temporary:
        target = str(Path(temporary) / "repository")
        snapshot = github.materialize_repository(target, ref=state.get("branch"), repository=state["repository"])
        if snapshot["remote_head"] != current:
            raise github.MediatedConflict("repair branch moved during refresh; retry publication against the current branch")
        # Import objects through a local path, never through a provider token.
        github._git(repo, "-c", "protocol.file.allow=always", "fetch", "--no-tags", "--no-write-fetch-head", target, snapshot["local_head"])
        remote_tree = github._git(repo, "rev-parse", snapshot["local_head"] + "^{tree}").strip()
        github.collect_changes(repo, base=base)
        if github._git(repo, "write-tree").strip() != original_tree:
            raise github.MediatedConflict("local repair changed during refresh; retry publication without discarding those edits")
        backup = github._git(repo, "commit-tree", original_tree, "-m", "Local repair before mediated source refresh").strip()
        github._git(repo, "update-ref", "refs/adp/repair-before-refresh", backup)
        try:
            github._git(repo, "read-tree", "--reset", "-u", remote_tree)
            if patch:
                applied = subprocess.run(
                    ["git", "apply", "--3way", "--index", "--whitespace=nowarn", "-"],
                    cwd=repo, input=patch, capture_output=True, check=False,
                )
                if applied.returncode and not unmerged_paths(repo):
                    raise github.MediatedError("local repair could not be reapplied; original edits were restored")
            # Conflicts retain Git's unmerged index and conflict-marked sources.
            # repair_read/write/replace can resolve these; collect_changes must
            # not silently stage them and publish conflict markers.
            github._record_publication(github._publication_state_path(repo), head=current, tree=remote_tree)
        except Exception:
            github._git(repo, "read-tree", "--reset", "-u", original_tree)
            raise
    return True
