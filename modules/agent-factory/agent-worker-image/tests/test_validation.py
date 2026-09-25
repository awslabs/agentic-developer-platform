"""Real Git regressions for isolated tests and exact-commit receipts."""

from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import validation


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    validation.git(tmp_path, "config", "user.email", "test@example.invalid")
    validation.git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "input").write_text("original")
    commit(tmp_path)
    return tmp_path


def commit(repo):
    validation.git(repo, "add", ".")
    validation.git(repo, "commit", "-qm", "input")


def command(source):
    return [sys.executable, "-c", source]


def test_reuse_requires_commit_command_and_environment(repo, monkeypatch):
    argv = command('print("ok")')
    assert validation.run(repo, argv)["passed"]
    assert validation.run(repo, argv)["reused"]
    assert validation.verify(repo)[0]
    assert not validation.run(repo, command('print("different")'))["reused"]
    monkeypatch.setenv("VALIDATION_TEST_SETTING", "changed")
    assert not validation.verify(repo)[0]
    assert not validation.run(repo, argv)["reused"]
    (repo / "input").write_text("new commit")
    commit(repo)
    assert not validation.verify(repo)[0]
    assert not validation.run(repo, argv)["reused"]


def test_failed_or_mutating_checks_cannot_pass(repo):
    failed = command("raise SystemExit(2)")
    assert not validation.run(repo, failed)["passed"]
    assert not validation.run(repo, failed)["reused"]
    mutated = validation.run(
        repo, command('from pathlib import Path; Path("input").write_text("changed")')
    )
    assert not mutated["passed"]
    assert not mutated["inputs_unchanged"]
    assert (repo / "input").read_text() == "original"
    assert not validation.verify(repo)[0]


def test_author_edits_do_not_change_running_test_inputs(repo, tmp_path):
    marker = tmp_path.parent / (tmp_path.name + "-started")
    argv = command(
        f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); "
        'time.sleep(0.5); assert Path("input").read_text() == "original"'
    )
    results = []
    thread = threading.Thread(target=lambda: results.append(validation.run(repo, argv)))
    thread.start()
    deadline = time.monotonic() + 10
    while not marker.exists() and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    (repo / "input").write_text("editing during tests")
    thread.join(timeout=10)
    assert results[0]["passed"]
    assert not validation.verify(repo)[0]
    assert (repo / "input").read_text() == "editing during tests"
    assert len(validation.git(repo, "worktree", "list").splitlines()) == 1


def test_timeout_and_forced_rerun(repo):
    result = validation.run(repo, command("import time; time.sleep(30)"), timeout=1)
    assert result["exit_code"] == 124
    assert not result["passed"]
    argv = command('print("ok")')
    validation.run(repo, argv)
    assert not validation.run(repo, argv, reuse=False)["reused"]
    assert len(validation.git(repo, "worktree", "list").splitlines()) == 1


def test_missing_evidence_dirty_tree_and_cwd_escape(repo):
    assert not validation.verify(repo)[0]
    with pytest.raises(ValueError, match="relative"):
        validation.run(repo, ["true"], cwd="../")
    (repo / "untracked").touch()
    with pytest.raises(ValueError, match="Commit intended"):
        validation.run(repo, ["true"])


def test_finalizer_preserves_dirty_work_without_opening_pr(repo, monkeypatch):
    import entrypoint

    remote = repo.parent / (repo.name + "-remote.git")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    validation.git(repo, "remote", "add", "origin", str(remote))
    (repo / "input").write_text("unfinished")
    monkeypatch.setattr(entrypoint, "WORK_DIR", repo)
    reports = []
    monkeypatch.setattr(entrypoint, "_post_comment", lambda *a: reports.append(a))
    monkeypatch.setattr(entrypoint, "update_invocation_status", lambda *a, **k: None)
    result = entrypoint._handle_success(
        "owner/repo", 1, "agent/issue-1", "developer", "run-1", "now"
    )
    assert result == 1
    assert validation.git(remote, "show", "agent/issue-1-incomplete-run-1:input") == "unfinished"
    assert "no review handoff" in reports[0][4]
    assert reports[0][3] == "failed"
    assert "refs/heads/agent/issue-1\n" not in validation.git(remote, "show-ref") + "\n"


def test_finalizer_stale_receipt_preserves_committed_work(repo, monkeypatch):
    import entrypoint

    remote = repo.parent / (repo.name + "-remote.git")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    validation.git(repo, "remote", "add", "origin", str(remote))
    validation.run(repo, command('print("ok")'))
    (repo / "input").write_text("untested final commit")
    commit(repo)
    monkeypatch.setattr(entrypoint, "WORK_DIR", repo)
    monkeypatch.setattr(entrypoint, "_post_comment", lambda *a: None)
    monkeypatch.setattr(entrypoint, "update_invocation_status", lambda *a, **k: None)
    assert entrypoint._handle_success("o/r", 1, "agent/issue-1", "developer", "run-1", "now") == 1
    assert (
        validation.git(remote, "show", "agent/issue-1-incomplete-run-1:input")
        == "untested final commit"
    )


def test_cli_runs_from_module_and_resets_plan(repo):
    module = repo / "module"
    module.mkdir()
    (module / "source").write_text("module input")
    commit(repo)
    script = Path(validation.__file__).resolve()
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "run",
            "--cwd",
            "module",
            "--",
            sys.executable,
            "-c",
            "import os; from pathlib import Path; "
            'assert Path(os.environ["PWD"]).resolve() == Path.cwd(); '
            'assert Path("source").read_text() == "module input"',
        ],
        cwd=module,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert subprocess.run([sys.executable, str(script), "verify"], cwd=repo).returncode == 0
    assert subprocess.run([sys.executable, str(script), "reset"], cwd=repo).returncode == 0
    assert subprocess.run([sys.executable, str(script), "verify"], cwd=repo).returncode == 1


def test_setup_failure_cleans_worktree_and_leaves_validation_unverified(repo):
    with pytest.raises(FileNotFoundError):
        validation.run(repo, ["missing-adp-validation-test-command"])
    assert len(validation.git(repo, "worktree", "list").splitlines()) == 1
    assert not validation.verify(repo)[0]


def test_supervisor_uses_latest_test_environment_and_never_older_success(repo, monkeypatch):
    argv = command('import os; raise SystemExit(int(os.environ.get("TEST_FAILURE", "0")))')
    validation.run(repo, argv)
    monkeypatch.setenv("TEST_FAILURE", "1")
    assert not validation.verify(repo)[0]
    # Supervisor environment differs from the test shell; it checks recorded evidence.
    assert validation.verify(repo, strict_environment=False)[0]
    assert not validation.run(repo, argv)["passed"]
    assert not validation.verify(repo, strict_environment=False)[0]
