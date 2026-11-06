"""Real history tests: restarting must keep story work and acquire newer base code."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.branch_reconciliation import reconcile_git_branch, reconcile_mediated_branch, reconciles_story_branch, verify_reconciled_snapshot


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def history(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    git(remote, "config", "user.name", "Test")
    git(remote, "config", "user.email", "test@example.invalid")
    (remote / "shared.txt").write_text("original\n")
    (remote / "deleted.txt").write_text("obsolete\n")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "initial")
    git(remote, "switch", "-c", "agent/issue-8698")
    (remote / "story.txt").write_text("keep my implementation\n")
    (remote / ".adp/tasks").mkdir(parents=True)
    (remote / ".adp/tasks/8698.json").write_text('{"tasks": [{"id": "one", "status": "done"}]}')
    git(remote, "add", ".")
    git(remote, "commit", "-m", "story and board")
    story = git(remote, "rev-parse", "HEAD")
    git(remote, "switch", "main")
    (remote / "deploy-infra.py").write_text("# newly merged deployment script\n")
    (remote / "deleted.txt").unlink()
    git(remote, "add", "--all")
    git(remote, "commit", "-m", "new base")
    local = tmp_path / "local"
    subprocess.run(["git", "clone", "-q", str(remote), str(local)], check=True)
    git(local, "config", "user.name", "Test")
    git(local, "config", "user.email", "test@example.invalid")
    git(local, "switch", "agent/issue-8698")
    return remote, local, story


def test_restart_keeps_story_board_and_history_and_acquires_base(history):
    remote, local, story = history
    board = (local / ".adp/tasks/8698.json").read_bytes()
    result = reconcile_git_branch(local)
    assert result["status"] == "merged"
    assert (local / "story.txt").read_text() == "keep my implementation\n"
    assert (local / ".adp/tasks/8698.json").read_bytes() == board
    assert (local / "deploy-infra.py").exists()
    assert not (local / "deleted.txt").exists()
    assert git(local, "rev-parse", f"refs/adp/before-base-reconciliation/{story}") == story
    git(local, "merge-base", "--is-ancestor", story, "HEAD")
    git(local, "merge-base", "--is-ancestor", "origin/main", "HEAD")
    assert git(remote, "rev-parse", "agent/issue-8698") == story  # no hidden push
    head = git(local, "rev-parse", "HEAD")
    assert reconcile_git_branch(local)["status"] == "up_to_date"
    assert git(local, "rev-parse", "HEAD") == head


def test_conflict_keeps_both_versions_and_original_head(history):
    remote, local, story = history
    (local / "shared.txt").write_text("story edit\n")
    git(local, "add", ".")
    git(local, "commit", "-m", "story conflict")
    head = git(local, "rev-parse", "HEAD")
    (remote / "shared.txt").write_text("base edit\n")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "base conflict")
    result = reconcile_git_branch(local)
    assert result["status"] == "conflict" and result["conflicts"] == ["shared.txt"]
    assert git(local, "rev-parse", "HEAD") == head
    assert git(local, "show", ":2:shared.txt") == "story edit"
    assert git(local, "show", ":3:shared.txt") == "base edit"
    assert (local / "story.txt").exists() and (local / "deploy-infra.py").exists()
    with pytest.raises(RuntimeError, match="clean story branch"):
        reconcile_git_branch(local)
    assert git(local, "rev-parse", "HEAD") == head


def test_dirty_work_is_never_discarded(history):
    _, local, story = history
    (local / "story.txt").write_text("uncommitted work\n")
    with pytest.raises(RuntimeError, match="clean story branch"):
        reconcile_git_branch(local)
    assert (local / "story.txt").read_text() == "uncommitted work\n"
    assert git(local, "rev-parse", "HEAD") == story


def test_shallow_default_clone_can_resume_an_older_story(history, tmp_path):
    remote, _, story = history
    local = tmp_path / "shallow"
    subprocess.run(["git", "clone", "--depth=1", remote.as_uri(), str(local)], check=True)
    git(local, "config", "user.name", "Test")
    git(local, "config", "user.email", "test@example.invalid")
    git(local, "remote", "set-branches", "--add", "origin", "agent/issue-8698")
    git(local, "fetch", "origin", "agent/issue-8698")
    git(local, "checkout", "agent/issue-8698")
    assert git(local, "rev-parse", "--is-shallow-repository") == "true"
    assert reconcile_git_branch(local)["status"] == "merged"
    git(local, "merge-base", "--is-ancestor", story, "HEAD")
    git(local, "merge-base", "--is-ancestor", "origin/main", "HEAD")
    assert (local / "deploy-infra.py").exists()


def test_mediated_conflict_names_the_preserved_branch_and_required_action(monkeypatch):
    from lib import mediated_github
    monkeypatch.setattr(mediated_github, "_request", lambda body: {"reconciliation": {
        "status": "conflict", "branch": "agent/issue-42", "head_revision": "a" * 40, "base_revision": "b" * 40,
    }})
    with pytest.raises(mediated_github.MediatedConflict, match="agent/issue-42.*authorized branch/PR repair"):
        reconcile_mediated_branch()


def test_mediated_unknown_response_never_launches_on_a_stale_snapshot(monkeypatch):
    from lib import mediated_github
    monkeypatch.setattr(mediated_github, "_request", lambda body: {})
    with pytest.raises(mediated_github.MediatedError, match="verified result"):
        reconcile_mediated_branch()


@pytest.mark.parametrize("head", [None, "a" * 40])
def test_snapshot_must_match_reconciled_revision_even_if_branch_moves_during_transfer(head):
    from lib import mediated_github
    reconciliation = {"head_revision": head, "base_revision": "b" * 40}
    verify_reconciled_snapshot(reconciliation, {"remote_head": head or "b" * 40})
    with pytest.raises(mediated_github.MediatedConflict, match="moved between base reconciliation"):
        verify_reconciled_snapshot(reconciliation, {"remote_head": "c" * 40})


@pytest.mark.parametrize("persona", ["developer", "architect", "product", "pm", "aidlc", "operations", "intent-refinement",
                                      "agent-codex-developer", "agent-codex-architect", "agent-codex-operations"])
def test_shared_startup_covers_branch_personas(persona):
    assert reconciles_story_branch(persona)
    assert not reconciles_story_branch(persona, review_cycle=True)


@pytest.mark.parametrize("persona", ["reviewer", "agent-codex-reviewer", "agent-codex-pm", "agent-codex-product", "agent-codex-intent-refinement"])
def test_exact_review_and_report_only_roles_keep_their_source_contract(persona):
    assert not reconciles_story_branch(persona)
