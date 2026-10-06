"""Actual bootstrap input and bound-PR checkout, without live provider calls."""
import json
from types import SimpleNamespace

import pytest

from lib.review_cycle_input import ENV, checkout_cycle_input, prepare_cycle_input


def envelope():
    return {
        "intent": {"trigger": "engine_review_cycle"}, "persona": "developer", "source_ref": {"repo": "org/repo"},
        "review_cycle_input": {"action": "repair", "repo": "org/repo", "pr_number": 77, "head_sha": "a" * 40,
            "accepted_scope": '{"node":{"title":"Accepted story"}}', "operation_key": "dispatch:1",
            "findings": [{"finding_id": "F1", "summary": "Boundary fails", "evidence_refs": []}],
            "remaining_attempts": 2, "remaining_spend_usd": "4.00"},
    }


def test_input_reaches_actual_child_environment(monkeypatch):
    monkeypatch.setenv(ENV, "stale-other-run")
    value = prepare_cycle_input(envelope())
    import os
    assert json.loads(os.environ[ENV]) == value
    assert value["findings"][0]["finding_id"] == "F1"
    assert value["remaining_attempts"] == 2
    prepare_cycle_input({})
    assert ENV not in os.environ


@pytest.mark.parametrize("field,value", [("repo", "other/repo"), ("head_sha", "bad"), ("action", "merge"),
    ("pr_number", True), ("accepted_scope", ""), ("findings", "not-a-list")])
def test_malformed_or_cross_scope_input_refuses(monkeypatch, field, value):
    monkeypatch.delenv(ENV, raising=False)
    message = envelope()
    message["review_cycle_input"][field] = value
    with pytest.raises(RuntimeError):
        prepare_cycle_input(message)


def test_checkout_uses_bound_pr_without_creating_or_resetting_branch():
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(stdout=json.dumps({"headRefName": "existing-pr-branch", "isCrossRepository": False, "state": "OPEN", "baseRefOid": "b" * 40}))
        if command == ["git", "rev-parse", "--is-shallow-repository"]:
            return SimpleNamespace(stdout="false")
        return SimpleNamespace(stdout="a" * 40 if command == ["git", "rev-parse", "HEAD"] else "existing-pr-branch")
    branch, head = checkout_cycle_input(envelope()["review_cycle_input"], run=run, cwd="/isolated")
    assert branch == "existing-pr-branch" and head == "a" * 40
    assert calls == [["gh", "pr", "view", "77", "--repo", "org/repo", "--json", "headRefName,isCrossRepository,state,baseRefOid"],
        ["git", "check-ref-format", "--branch", "existing-pr-branch"],
        ["git", "remote", "set-branches", "--add", "origin", "existing-pr-branch"],
        ["gh", "pr", "checkout", "77", "--repo", "org/repo"], ["git", "rev-parse", "HEAD"],
        ["git", "branch", "--show-current"],
        ["git", "rev-parse", "--is-shallow-repository"],
        ["git", "fetch", "--no-tags", "origin", "b" * 40, "+refs/heads/*:refs/remotes/origin/*"],
        ["git", "cat-file", "-e", "b" * 40 + "^{commit}"]]


def test_moved_head_refuses_before_model_exec():
    def run(command, **kwargs):
        return SimpleNamespace(stdout=json.dumps({"headRefName": "existing-pr-branch", "isCrossRepository": False, "state": "OPEN", "baseRefOid": "b" * 40})
            if command[:3] == ["gh", "pr", "view"] else "b" * 40)
    with pytest.raises(RuntimeError, match="head changed"):
        checkout_cycle_input(envelope()["review_cycle_input"], run=run, cwd="/isolated")


def test_shallow_clone_tracks_only_the_assigned_pr_branch(tmp_path):
    import subprocess

    def git(*args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True, timeout=30)

    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-b", "main", cwd=origin)
    git("config", "user.name", "Test", cwd=origin)
    git("config", "user.email", "test@example.test", cwd=origin)
    (origin / "inventory.json").write_text('{"pinned_evidence": true}\n')
    git("add", "inventory.json", cwd=origin)
    git("commit", "-m", "pinned inventory", cwd=origin)
    inventory = git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    git("commit", "--allow-empty", "-m", "main", cwd=origin)
    git("checkout", "-b", "agent/issue-77", cwd=origin)
    (origin / "implementation.txt").write_text("existing implementation\n")
    git("add", "implementation.txt", cwd=origin)
    git("commit", "-m", "implementation", cwd=origin)
    head = git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    clone = tmp_path / "clone"
    git("clone", "--depth=1", "--branch", "main", origin.as_uri(), str(clone), cwd=tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        git("cat-file", "-e", f"{inventory}^{{commit}}", cwd=clone)
    # The provider's base can also advance after the worker clone was created.
    git("checkout", "main", cwd=origin)
    git("commit", "--allow-empty", "-m", "new base", cwd=origin)
    base = git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    git("checkout", "-b", "agent/issue-88", cwd=origin)
    (origin / "ui-contract.txt").write_text("pinned sibling contract\n")
    git("add", ".", cwd=origin)
    git("commit", "-m", "sibling contract", cwd=origin)
    sibling = git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    git("checkout", "main", cwd=origin)

    def gh_checkout():
        git("fetch", "origin", "+refs/heads/agent/issue-77:refs/remotes/origin/agent/issue-77", cwd=clone)
        return git("checkout", "-b", "agent/issue-77", "--track", "origin/agent/issue-77", cwd=clone)

    # gh's checkout sequence reproduces the live failure on a real shallow clone.
    with pytest.raises(subprocess.CalledProcessError) as failure:
        gh_checkout()
    assert "not a branch" in failure.value.stderr

    def run(command, **kwargs):
        assert kwargs["timeout"] in {30, 120}
        if command[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(stdout=json.dumps({"headRefName": "agent/issue-77", "isCrossRepository": False, "state": "OPEN", "baseRefOid": git("rev-parse", "main", cwd=origin).stdout.strip()}))
        if command[:3] == ["gh", "pr", "checkout"]:
            return gh_checkout()
        return subprocess.run(command, text=True, capture_output=True, check=True, **kwargs)

    value = {**envelope()["review_cycle_input"], "head_sha": head}
    assert checkout_cycle_input(value, run=run, cwd=clone) == ("agent/issue-77", head)
    assert git("rev-parse", "@{upstream}", cwd=clone).stdout.strip() == head
    assert git("rev-parse", "agent/issue-77", cwd=origin).stdout.strip() == head
    assert git("rev-parse", "HEAD", cwd=origin).stdout.strip() == base
    assert git("rev-parse", "--is-shallow-repository", cwd=clone).stdout.strip() == "false"
    assert git("show", f"{inventory}:inventory.json", cwd=clone).stdout == '{"pinned_evidence": true}\n'
    assert git("cat-file", "-t", base, cwd=clone).stdout.strip() == "commit"
    assert git("show", f"{sibling}:ui-contract.txt", cwd=clone).stdout == "pinned sibling contract\n"
    assert git("rev-parse", "HEAD", cwd=clone).stdout.strip() == head
    assert git("status", "--porcelain", cwd=clone).stdout == ""
    assert git("config", "--get-all", "remote.origin.fetch", cwd=clone).stdout.splitlines() == [
        "+refs/heads/main:refs/remotes/origin/main",
        "+refs/heads/agent/issue-77:refs/remotes/origin/agent/issue-77",
    ]


@pytest.mark.parametrize("action,moved", [("review", False), ("review", True), ("repair", False)])
def test_merged_pr_with_deleted_branch_uses_exact_retained_head(tmp_path, action, moved):
    import subprocess

    def git(*args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True, timeout=30)

    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-b", "main", cwd=origin)
    git("config", "user.name", "Test", cwd=origin)
    git("config", "user.email", "test@example.test", cwd=origin)
    git("commit", "--allow-empty", "-m", "main", cwd=origin)
    git("checkout", "-b", "agent/issue-77", cwd=origin)
    (origin / "implementation.txt").write_text("delivered implementation\n")
    git("add", "implementation.txt", cwd=origin)
    git("commit", "-m", "implementation", cwd=origin)
    head = git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    git("update-ref", "refs/pull/77/head", head, cwd=origin)
    git("checkout", "main", cwd=origin)
    git("merge", "--ff-only", "agent/issue-77", cwd=origin)
    git("branch", "-d", "agent/issue-77", cwd=origin)
    clone = tmp_path / "clone"
    git("clone", "--depth=1", "--branch", "main", origin.as_uri(), str(clone), cwd=tmp_path)

    def run(command, **kwargs):
        if command[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(stdout=json.dumps({"headRefName": "agent/issue-77", "isCrossRepository": False, "state": "MERGED", "baseRefOid": head}))
        return subprocess.run(command, text=True, capture_output=True, check=True, **kwargs)

    value = {**envelope()["review_cycle_input"], "action": action, "head_sha": "0" * 40 if moved else head}
    if action == "repair" or moved:
        with pytest.raises(RuntimeError, match="open PR|head changed"):
            checkout_cycle_input(value, run=run, cwd=clone)
        assert git("branch", "--show-current", cwd=clone).stdout.strip() == "main"
    else:
        branch, actual = checkout_cycle_input(value, run=run, cwd=clone)
        assert actual == head and branch.startswith("adp-review/pr-77-")
        assert (clone / "implementation.txt").read_text() == "delivered implementation\n"
        assert git("rev-parse", "HEAD", cwd=clone).stdout.strip() == head
    assert git("branch", "--list", "agent/issue-77", cwd=origin).stdout == ""
