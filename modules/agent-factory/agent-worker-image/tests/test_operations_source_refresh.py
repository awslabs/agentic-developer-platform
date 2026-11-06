"""Exercise repair refresh with real Git trees and mediated archive boundaries."""

import base64
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import operations_repair as repair


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    remote, local = tmp_path / "remote", tmp_path / "local"
    remote.mkdir()
    git(remote, "init", "--initial-branch=main")
    git(remote, "config", "user.name", "Test")
    git(remote, "config", "user.email", "test@example.invalid")
    original = "checkout=old\n" + "# separator\n" * 12 + "install=system\n"
    (remote / "release.py").write_text(original)
    (remote / "removed.py").write_text("remove me\n")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "base")
    base = git(remote, "rev-parse", "HEAD")
    git(remote, "checkout", "-b", "agent/issue-12")
    subprocess.run(["git", "clone", "--quiet", str(remote), str(local)], check=True)
    git(local, "config", "user.name", "Test")
    git(local, "config", "user.email", "test@example.invalid")
    repair.github._record_publication(repair.github._publication_state_path(str(local)),
        head=base, tree=git(local, "rev-parse", "HEAD^{tree}"))
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_STORY", "true")
    monkeypatch.setenv("AGENT_TYPE", "operations")
    monkeypatch.setenv("WORK_DIR", str(local))
    calls, archives = [], []

    def state():
        return {"repository": "example/service", "branch": "agent/issue-12", "default_branch": "main",
                "branch_head": git(remote, "rev-parse", "HEAD"), "default_branch_head": git(remote, "rev-parse", "main")}

    def archive(*, ref, repository):
        assert ref == "agent/issue-12" and repository == "example/service"
        archives.append(state()["branch_head"])
        data = subprocess.check_output(["git", "-C", str(remote), "archive", "--format=tar.gz", "--prefix=source/", "HEAD"])
        return data, {"commit_sha": archives[-1], "branch": ref, "repository": repository}

    def request(body):
        calls.append(body)
        if body["operation"] == "validate_repair_edit":
            change = body["changes"][0]
            return {"path": change["path"], "sha256": hashlib.sha256(base64.b64decode(change["content_base64"])).hexdigest()}
        if body["operation"] == "publish_commit":
            assert body["expected_head"] == state()["branch_head"]
            for change in body["changes"]:
                path = remote / change["path"]
                if change.get("deleted"):
                    path.unlink()
                else:
                    path.write_bytes(base64.b64decode(change["content_base64"]))
                    path.chmod(int(change["mode"], 8) & 0o777)
            git(remote, "add", "--all")
            git(remote, "commit", "-m", body["message"])
            return {"commit_sha": state()["branch_head"]}
        if body["operation"] == "upsert_pull_request":
            return {"pull_request": {"number": 12}}
        raise AssertionError(body)

    monkeypatch.setattr(repair.github, "read_repository", lambda: {"repository": state()})
    monkeypatch.setattr(repair.github, "_fetch_archive", archive)
    monkeypatch.setattr(repair.github, "_request", request)

    def advance(content=None):
        if content is not None:
            (remote / "release.py").write_text(content)
        git(remote, "add", "--all")
        git(remote, "commit", "-m", "remote update")

    def publish():
        return repair.invoke("repair_publish", {"title": "repair", "body": "Run required CI before merge"})

    return local, remote, original, calls, archives, advance, publish


def test_refresh_preserves_new_checkout_and_local_install_repair(scenario):
    local, remote, original, calls, archives, advance, publish = scenario
    (local / "release.py").write_text(original.replace("install=system", "install=isolated"))
    advance(original.replace("checkout=old", "checkout=reviewed"))
    result = publish()
    expected = original.replace("checkout=old", "checkout=reviewed").replace("install=system", "install=isolated")
    assert result["workspace_refreshed"] is True
    assert (local / "release.py").read_text() == (remote / "release.py").read_text() == expected
    assert len(archives) == 1
    assert [change["path"] for change in next(c for c in calls if c["operation"] == "publish_commit")["changes"]] == ["release.py"]
    publish()
    assert len([c for c in calls if c["operation"] == "publish_commit"]) == 1


def test_conflict_is_visible_and_cannot_publish_until_explicitly_resolved(scenario):
    local, remote, original, calls, archives, advance, publish = scenario
    (local / "release.py").write_text(original.replace("install=system", "install=mine"))
    advance(original.replace("install=system", "install=theirs"))
    result = publish()
    assert result["published"] is False and result["conflicts"] == ["release.py"]
    assert "<<<<<<<" in (local / "release.py").read_text()
    assert publish()["published"] is False
    assert not any(c["operation"] == "publish_commit" for c in calls)
    current = repair.invoke("repair_read", {"path": "release.py"})
    resolved = original.replace("install=system", "install=resolved")
    repair.invoke("repair_write", {"path": "release.py", "content": resolved, "expected_sha256": current["sha256"]})
    assert publish()["pull_request"]["number"] == 12
    assert (remote / "release.py").read_text() == resolved
    assert len(archives) == 1


def test_remote_additions_local_deletion_and_executable_mode_survive(scenario):
    local, remote, original, calls, archives, advance, publish = scenario
    (local / "removed.py").unlink()
    (local / "release.py").chmod(0o755)
    (remote / "new.py").write_text("new remote feature\n")
    advance()
    publish()
    for root in (local, remote):
        assert not (root / "removed.py").exists()
        assert (root / "new.py").read_text() == "new remote feature\n"
        assert (root / "release.py").stat().st_mode & 0o111 == 0o111
        assert git(root, "ls-files", "--stage", "release.py").startswith("100755 ")


def test_unrelated_main_change_does_not_refresh_existing_repair_branch(scenario, monkeypatch):
    local, remote, original, calls, archives, advance, publish = scenario
    (local / "release.py").write_text(original + "# local repair\n")
    git(remote, "checkout", "main")
    (remote / "unrelated.py").write_text("unrelated\n")
    advance()
    git(remote, "checkout", "agent/issue-12")
    publish()
    assert archives == []


def test_moved_archive_keeps_local_repair(scenario, monkeypatch):
    local, remote, original, calls, archives, advance, publish = scenario
    changed = original + "# local repair\n"
    (local / "release.py").write_text(changed)
    advance(original.replace("checkout=old", "checkout=reviewed"))
    original_fetch = repair.github._fetch_archive
    def moved(**kwargs):
        data, result = original_fetch(**kwargs)
        return data, {**result, "commit_sha": "a" * 40}
    monkeypatch.setattr(repair.github, "_fetch_archive", moved)
    with pytest.raises(repair.github.MediatedConflict, match="moved during refresh"):
        publish()
    assert (local / "release.py").read_text() == changed
    assert not any(c["operation"] == "publish_commit" for c in calls)


def test_local_changes_while_fetching_are_not_overwritten(scenario, monkeypatch):
    local, remote, original, calls, archives, advance, publish = scenario
    (local / "release.py").write_text(original + "# first\n")
    advance(original.replace("checkout=old", "checkout=reviewed"))
    original_fetch = repair.github._fetch_archive
    changed = original + "# first\n# concurrent edit\n"
    def concurrent(**kwargs):
        result = original_fetch(**kwargs)
        (local / "release.py").write_text(changed)
        return result
    monkeypatch.setattr(repair.github, "_fetch_archive", concurrent)
    with pytest.raises(repair.github.MediatedConflict, match="local repair changed"):
        publish()
    assert (local / "release.py").read_text() == changed
    assert not any(c["operation"] == "publish_commit" for c in calls)
    monkeypatch.setattr(repair.github, "_fetch_archive", original_fetch)
    publish()
    assert (remote / "release.py").read_text() == changed.replace("checkout=old", "checkout=reviewed")


def test_failed_authorized_archive_preserves_original_repair(scenario, monkeypatch):
    local, remote, original, calls, archives, advance, publish = scenario
    changed = original + "# local repair\n"
    (local / "release.py").write_text(changed)
    advance(original.replace("checkout=old", "checkout=reviewed"))
    def denied(**kwargs):
        raise repair.github.MediatedRefused("current scope denied")
    monkeypatch.setattr(repair.github, "_fetch_archive", denied)
    with pytest.raises(repair.github.MediatedRefused, match="scope denied"):
        publish()
    assert (local / "release.py").read_text() == changed
    assert not any(c["operation"] == "publish_commit" for c in calls)


def test_edits_during_publication_remain_in_next_delta(scenario, monkeypatch):
    local, remote, original, calls, archives, advance, publish = scenario
    (local / "release.py").write_text(original + "# first\n")
    request = repair.github._request
    def concurrent(body):
        result = request(body)
        if body["operation"] == "publish_commit":
            (local / "release.py").write_text(original + "# first\n# second\n")
            git(local, "add", "release.py")
        return result
    monkeypatch.setattr(repair.github, "_request", concurrent)
    publish()
    monkeypatch.setattr(repair.github, "_request", request)
    publish()
    assert (remote / "release.py").read_text().endswith("# first\n# second\n")
    assert len([c for c in calls if c["operation"] == "publish_commit"]) == 2
