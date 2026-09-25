"""Real Git workspace edits stay inside a credential-free materialized archive."""

import hashlib
import io
import tarfile

import pytest

from lib.codex_workspace import CodexWorkspace, WorkspaceError


def bundle(files):
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w:gz") as stream:
        for path, value in files.items():
            item = tarfile.TarInfo(path)
            if isinstance(value, bytes):
                item.size = len(value)
                stream.addfile(item, io.BytesIO(value))
            else:
                item.type = tarfile.SYMTYPE
                item.linkname = value
                stream.addfile(item)
    return content.getvalue()


@pytest.fixture
def workspace(tmp_path):
    workspace = CodexWorkspace(
        tmp_path / "repo", provider="github", repository="org/repo", source_revision="a" * 40
    )
    data = bundle({"wrapped/src/main.py": b"value = 1\n", "wrapped/README.md": b"fixture\n"})
    workspace.materialize(data, archive_sha256=hashlib.sha256(data).hexdigest())
    return workspace


def test_real_edits_create_a_new_local_commit_without_fabricating_provider_head(workspace):
    original = workspace.state()
    assert (
        original["sourceRevision"] == "a" * 40
        and original["localHead"] != original["sourceRevision"]
    )
    read = workspace.read_file("src/main.py")
    workspace.write_file("src/main.py", "value = 2\n", expected_sha256=read["sha256"])
    assert not workspace.state()["clean"]
    result = workspace.commit("Implement fixture change")
    assert result["clean"] and result["localHead"] != original["localHead"]
    assert (
        result["tree"] != original["tree"]
        and result["sourceRevision"] == original["sourceRevision"]
    )
    assert workspace.read_file("src/main.py")["content"] == "value = 2\n"
    assert workspace.list_files(limit=1)["nextOffset"] == 1
    assert workspace.list_files(prefix="src")["paths"] == ["src/main.py"]


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/etc/passwd",
        ".git/config",
        "src/../../outside",
        "src/.GiT/config",
        "src\\outside",
    ],
)
def test_read_and_write_cannot_escape_or_edit_git_metadata(workspace, path):
    with pytest.raises(WorkspaceError):
        workspace.read_file(path)
    with pytest.raises(WorkspaceError):
        workspace.write_file(path, "bad", expected_sha256=None)


def test_stale_edits_and_overwrite_without_digest_are_refused(workspace):
    original = workspace.read_file("src/main.py")
    with pytest.raises(WorkspaceError, match="stale"):
        workspace.write_file("src/main.py", "bad", expected_sha256="0" * 64)
    with pytest.raises(FileExistsError):
        workspace.write_file("src/main.py", "bad", expected_sha256=None)
    assert workspace.read_file("src/main.py") == original
    workspace.write_file("tests/new.txt", "new", expected_sha256=None)
    assert workspace.read_file("tests/new.txt")["content"] == "new"


def test_symlink_components_cannot_read_or_write_host_files(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("host secret")
    (workspace.root / "escape").symlink_to(outside, target_is_directory=True)
    for operation in [
        lambda: workspace.read_file("escape/secret"),
        lambda: workspace.write_file("escape/secret", "bad", expected_sha256=None),
    ]:
        with pytest.raises(OSError):
            operation()
    assert (outside / "secret").read_text() == "host secret"


@pytest.mark.parametrize(
    "files",
    [
        {"root/.git/config": b"bad"},
        {"root/../escape": b"bad"},
        {"root/a": b"a", "other/b": b"b"},
        {"root/link": "/etc/passwd"},
    ],
)
def test_bad_archives_are_rejected_before_materialization(tmp_path, files):
    workspace = CodexWorkspace(
        tmp_path / "repo", provider="gitlab", repository="group/repo", source_revision="b" * 40
    )
    data = bundle(files)
    with pytest.raises(WorkspaceError):
        workspace.materialize(data, archive_sha256=hashlib.sha256(data).hexdigest())
    assert not workspace.root.exists()


@pytest.mark.parametrize(
    "files",
    [{"root/a": b"file", "root/a/b": b"child"}, {"root/a/b": b"child", "root/a": b"file"}, {}],
)
def test_conflicting_or_empty_archive_leaves_no_workspace(tmp_path, files):
    workspace = CodexWorkspace(
        tmp_path / "repo", provider="github", repository="org/repo", source_revision="a" * 40
    )
    data = bundle(files)
    with pytest.raises(WorkspaceError):
        workspace.materialize(data, archive_sha256=hashlib.sha256(data).hexdigest())
    assert not workspace.root.exists()


def test_hardlinked_host_file_cannot_be_read_or_replaced(workspace, tmp_path):
    import os

    outside = tmp_path / "secret"
    outside.write_text("host secret")
    os.link(outside, workspace.root / "linked")
    with pytest.raises(WorkspaceError):
        workspace.read_file("linked")
    with pytest.raises(WorkspaceError):
        workspace.write_file(
            "linked", "bad", expected_sha256=hashlib.sha256(b"host secret").hexdigest()
        )
    assert outside.read_text() == "host secret"


def test_file_listing_preserves_leading_spaces(workspace):
    workspace.write_file(" leading.txt", "content", expected_sha256=None)
    assert " leading.txt" in workspace.list_files()["paths"]
