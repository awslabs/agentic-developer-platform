"""PAT publication must not disclose bytes through precreated filesystem entries."""

import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import entrypoint


def test_predictable_temporary_symlink_cannot_receive_token(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("private-existing-content")
    predictable = tmp_path / ".adp-gh-token.tmp"
    predictable.symlink_to(victim)
    destination = tmp_path / ".adp-gh-token"
    entrypoint._write_pat_token_file("synthetic-pat", str(destination))
    assert victim.read_text() == "private-existing-content"
    assert predictable.is_symlink()
    assert destination.read_text() == "synthetic-pat"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize("entry", ["readable-file", "symlink", "hardlink"])
def test_destination_is_replaced_privately_without_modifying_existing_inode(tmp_path, entry):
    destination = tmp_path / ".adp-gh-token"
    previous = tmp_path / "previous"
    previous.write_text("previous-content")
    previous.chmod(0o644)
    if entry == "readable-file":
        destination.write_text("old-token")
        destination.chmod(0o644)
    elif entry == "symlink":
        destination.symlink_to(previous)
    else:
        destination.hardlink_to(previous)
    entrypoint._write_pat_token_file("synthetic-pat", str(destination))
    assert previous.read_text() == "previous-content"
    assert not destination.is_symlink()
    assert destination.read_text() == "synthetic-pat"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == [".adp-gh-token", "previous"]


def test_replace_failure_cleans_temporary_and_preserves_previous_token(tmp_path, monkeypatch):
    destination = tmp_path / ".adp-gh-token"
    destination.write_text("previous-token")

    def refuse(*args):
        raise PermissionError("synthetic publication refusal")

    monkeypatch.setattr(entrypoint.os, "replace", refuse)
    with pytest.raises(PermissionError):
        entrypoint._write_pat_token_file("synthetic-pat", str(destination))
    assert destination.read_text() == "previous-token"
    assert list(tmp_path.iterdir()) == [destination]


def test_predictable_readable_temporary_file_never_receives_token(tmp_path):
    predictable = tmp_path / ".adp-gh-token.tmp"
    predictable.write_text("public-existing-content")
    predictable.chmod(0o644)
    destination = tmp_path / ".adp-gh-token"
    entrypoint._write_pat_token_file("synthetic-pat", str(destination))
    assert predictable.read_text() == "public-existing-content"
    assert destination.read_text() == "synthetic-pat"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
