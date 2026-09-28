"""Exercise the actual source staging helper against real local Git fixtures."""

import importlib.util
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location(
    "prepare", Path(__file__).resolve().parents[1] / "prepare-webhook-source.py"
)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo,
        text=True,
    ).strip()


def test_only_committed_bytes_are_packaged(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "handler.py").write_text("committed")
    (repo / ".gitignore").write_text(".env\n")
    git(repo, "add", "handler.py", ".gitignore")
    git(repo, "commit", "-qm", "fixture")
    sha = git(repo, "rev-parse", "HEAD")
    (repo / "handler.py").write_text("dirty workspace")
    (repo / "untracked.py").write_text("untracked")
    (repo / ".env").write_text("ignored")
    staged = prepare.prepare(repo, sha, tmp_path)
    assert (staged / "handler.py").read_text() == "committed"
    assert not (staged / "untracked.py").exists() and not (staged / ".env").exists()
    assert not (staged / ".git").exists()


def test_committed_symlink_refused(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "escape").symlink_to("/etc/passwd")
    git(tmp_path, "add", "escape")
    git(tmp_path, "commit", "-qm", "fixture")
    with pytest.raises(AssertionError, match="Non-regular"):
        prepare.prepare(tmp_path, git(tmp_path, "rev-parse", "HEAD"), tmp_path)
