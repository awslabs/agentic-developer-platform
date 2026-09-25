"""Execute refresh code with real local Git and a strict persistent-I/O contract.

Provider boundaries are synthetic. This does not claim actual S3 CSI acceptance
or test the separately owned ingest-repo parser/provider implementation.
"""
import builtins
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

INGESTION = Path(__file__).resolve().parents[1] / "images" / "ingestion"
REAL_RUN = subprocess.run


def load_script(name):
    spec = importlib.util.spec_from_file_location("review_" + name.replace("-", "_"), INGESTION / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PersistentContract:
    """Reject non-sequential writes/metadata on the simulated shared mount.

    Git is an external process, so its destination is also checked explicitly
    at the subprocess adapter. Ordinary allowed Git operations really execute.
    """
    def __init__(self, root):
        self.root = root.resolve()
        self.published = []

    def contains(self, path):
        return not isinstance(path, int) and Path(path).resolve().is_relative_to(self.root)

    def check_git(self, path):
        if self.contains(path):
            raise OSError("Git metadata/locking is unsupported on persistent mount")

    def install(self, monkeypatch):
        original_open = builtins.open
        original_rename, original_replace = os.rename, os.replace

        contract = self

        class SequentialWriter:
            def __init__(self, file, path):
                self.file, self.path = file, path
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.file.close()
                if args[0] is None:
                    contract.published.append(str(self.path))
            def write(self, value):
                return self.file.write(value)
            def seek(self, *args):
                raise OSError("random writes unsupported")
            def truncate(self, *args):
                raise OSError("in-place updates unsupported")

        def checked_open(path, mode="r", *args, **kwargs):
            if self.contains(path):
                if ".git" in Path(path).parts or str(path).endswith(".lock"):
                    raise OSError("metadata/lock unsupported")
                if mode not in {"r", "rb", "w", "wb"}:
                    raise OSError("only full-object sequential publication supported")
                file = original_open(path, mode, *args, **kwargs)
                if "w" in mode:
                    return SequentialWriter(file, path)
                return file
            return original_open(path, mode, *args, **kwargs)

        def checked_move(original):
            def call(src, dst, *args, **kwargs):
                if self.contains(src) or self.contains(dst):
                    raise OSError("rename unsupported")
                return original(src, dst, *args, **kwargs)
            return call

        monkeypatch.setattr(builtins, "open", checked_open)
        monkeypatch.setattr(os, "rename", checked_move(original_rename))
        monkeypatch.setattr(os, "replace", checked_move(original_replace))


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    scratch, persistent = tmp_path / "scratch", tmp_path / "persistent"
    scratch.mkdir()
    persistent.mkdir()
    cfg = load_script("config").settings
    for key, value in {
        "scratch_base": str(scratch), "state_dir": str(persistent),
        "clone_base": str(persistent / "repos"), "learning_dir": str(persistent / "learning"),
        "code_index_dir": str(persistent / "indexes"),
    }.items():
        setattr(cfg, key, value)
    monkeypatch.setitem(sys.modules, "config", SimpleNamespace(settings=cfg))
    objects = {"wikis/org-repo-wiki.md": "# Existing wiki\n" + "ordinary content " * 20}

    class Store:
        def __init__(self, **kwargs):
            pass
        def get_content(self, key):
            return objects.get(key)
        def put_content(self, key, value):
            objects[key] = value
            return True

    def forbidden(*args, **kwargs):
        raise AssertionError("provider/network call forbidden")

    monkeypatch.setitem(sys.modules, "s3_store", SimpleNamespace(S3ContentStore=Store))
    monkeypatch.setitem(sys.modules, "github_auth", SimpleNamespace(mint_github_token=forbidden))
    import requests
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    mod = load_script("refresh-repos")
    contract = PersistentContract(persistent)
    contract.install(monkeypatch)
    return SimpleNamespace(mod=mod, cfg=cfg, scratch=scratch, persistent=persistent,
                           objects=objects, contract=contract)


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "origin"
    repo.mkdir()
    def git(*args):
        return REAL_RUN(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout.decode().strip()
    git("init", "--quiet")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repo / "example.py").write_text("def old():\n    return 1\n")
    git("add", ".")
    git("commit", "-qm", "old")
    old = git("rev-parse", "HEAD")
    (repo / "example.py").write_text("def updated():\n    return 2\n")
    git("commit", "-qam", "new")
    return repo, old, git("rev-parse", "HEAD")


def local_git_adapter(runtime, repository, monkeypatch, *, ingest=None, clone=None):
    repo, old, new = repository
    destinations = []
    def run(cmd, **kwargs):
        if cmd[:2] == ["git", "clone"]:
            dest = Path(cmd[-1])
            runtime.contract.check_git(dest)
            destinations.append(dest)
            if clone:
                clone(dest)
            return REAL_RUN(["git", "clone", "--quiet", "--no-hardlinks", str(repo), str(dest)], **kwargs)
        if cmd[:2] == ["git", "ls-remote"]:
            return REAL_RUN(["git", "ls-remote", str(repo), "HEAD"], **kwargs)
        if cmd[0] == "git" and "-C" in cmd:
            runtime.contract.check_git(cmd[cmd.index("-C") + 1])
            return REAL_RUN(cmd, **kwargs)
        if cmd[:2] == [sys.executable, "/app/ingest-repo.py"] and ingest:
            return ingest(cmd, kwargs)
        raise AssertionError(f"unexpected subprocess: {cmd!r}")
    monkeypatch.setattr(runtime.mod.subprocess, "run", run)
    return destinations


def test_actual_incremental_git_diff_and_publication(runtime, repository, monkeypatch):
    destinations = local_git_adapter(runtime, repository, monkeypatch)
    prompts = []
    def llm(prompt, **kwargs):
        prompts.append(prompt)
        return "# Updated wiki\nUpdated example.py"
    monkeypatch.setattr(runtime.mod, "call_llm", llm)
    assert runtime.mod.incremental_wiki_update("org/repo", repository[1], repository[2])
    assert "example.py" in prompts[0] and "1 file changed" in prompts[0]
    assert runtime.objects["wikis/org-repo-wiki.md"].startswith("# Updated wiki")
    assert len(destinations) == 1 and destinations[0].is_relative_to(runtime.scratch)
    assert list(runtime.scratch.iterdir()) == []
    # The exact old production helper/destination fails under the same adapter.
    with pytest.raises(OSError, match="Git metadata"):
        runtime.mod.git_clone_full("org/repo", str(runtime.persistent / "repos/org-repo-diff"))


@pytest.mark.parametrize("failure", ["clone-error", "timeout", "cancel"])
def test_actual_incremental_failure_cleanup(runtime, repository, monkeypatch, failure):
    def fail(dest):
        dest.mkdir()
        (dest / "partial").write_text("partial")
        if failure == "clone-error":
            raise subprocess.CalledProcessError(128, "git")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("git", 300)
        raise KeyboardInterrupt()
    local_git_adapter(runtime, repository, monkeypatch, clone=fail)
    before = dict(runtime.objects)
    if failure == "cancel":
        with pytest.raises(KeyboardInterrupt):
            runtime.mod.incremental_wiki_update("org/repo", repository[1], repository[2])
    else:
        assert not runtime.mod.incremental_wiki_update("org/repo", repository[1], repository[2])
    assert not list(runtime.scratch.iterdir())
    assert runtime.objects == before


def test_overlapping_real_attempts_do_not_delete_each_other(runtime, repository, monkeypatch):
    barrier = threading.Barrier(2)
    paths = []
    lock = threading.Lock()
    def overlap(dest):
        with lock:
            paths.append(dest.parent)
        barrier.wait(timeout=10)
        assert all(path.exists() for path in paths)
    local_git_adapter(runtime, repository, monkeypatch, clone=overlap)
    monkeypatch.setattr(runtime.mod, "call_llm", lambda *args, **kwargs: "updated")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(runtime.mod.incremental_wiki_update, "org/repo", repository[1], repository[2]) for _ in range(2)]
        assert all(f.result() for f in futures)
    assert len(set(paths)) == 2
    assert not list(runtime.scratch.iterdir())


@pytest.mark.parametrize("mode", ["incremental", "new", "force"])
def test_refresh_success_state_roundtrip(runtime, repository, monkeypatch, mode):
    def ingest(cmd, kwargs):
        # Exercise refresh's real process boundary with a local ingestion adapter.
        # Parser/provider correctness belongs to separately owned ingest-repo tests.
        root = Path(kwargs["env"]["CLONE_BASE"])
        assert root.is_relative_to(runtime.scratch) and root.exists()
        runtime.contract.check_git(root)
        REAL_RUN(["git", "clone", "--quiet", str(repository[0]), str(root / "org/repo")], check=True)
        source = root / "org/repo/example.py"
        assert "def updated" in source.read_text()
        runtime.mod.save_state("index-output.json", {"files": ["example.py"], "sha": repository[2]})
        return subprocess.CompletedProcess(cmd, 0, b"indexed local fixture", b"")
    local_git_adapter(runtime, repository, monkeypatch, ingest=ingest)
    monkeypatch.setattr(runtime.mod, "call_llm", lambda prompt, **kw: '["python"]' if "topic tags" in prompt else "Updated wiki")
    state = {} if mode == "new" else {"org/repo": {"last_sha": repository[1], "deepwiki_sha": repository[1]}}
    assert runtime.mod.refresh_repo("org/repo", state, force=mode == "force")
    assert state["org/repo"]["last_sha"] == state["org/repo"]["code_index_sha"] == repository[2]
    assert state["org/repo"]["topics"] == ["python"]
    assert state["org/repo"]["deepwiki_sha"] == (None if mode == "force" else repository[2])
    runtime.mod.save_state("repo-state.json", state)
    assert runtime.mod.load_state("repo-state.json") == state
    assert runtime.mod.load_state("index-output.json")["files"] == ["example.py"]
    # Repeat full-object overwrite, never append/rename/random update.
    runtime.mod.save_state("repo-state.json", state)
    assert len(runtime.contract.published) == 3
    assert not list(runtime.scratch.iterdir())
    assert not runtime.mod.refresh_repo("org/repo", state)


@pytest.mark.parametrize("failure", ["exit", "timeout", "exception", "cancel"])
def test_failed_ingest_keeps_previous_state_retryable(runtime, repository, monkeypatch, failure):
    def ingest(cmd, kwargs):
        root = Path(kwargs["env"]["CLONE_BASE"])
        (root / "partial").write_text("partial")
        if failure == "exit":
            return subprocess.CompletedProcess(cmd, 1, b"", b"failed")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 900)
        if failure == "cancel":
            raise KeyboardInterrupt()
        raise OSError("adapter failed")
    local_git_adapter(runtime, repository, monkeypatch, ingest=ingest)
    state = {"org/repo": {"last_sha": repository[1], "code_index_sha": repository[1]}}
    before = json.loads(json.dumps(state))
    if failure == "cancel":
        with pytest.raises(KeyboardInterrupt):
            runtime.mod.refresh_repo("org/repo", state)
    else:
        assert not runtime.mod.refresh_repo("org/repo", state)
    assert state == before
    assert not list(runtime.scratch.iterdir())


@pytest.mark.parametrize("path", ["persistent", "outside", "symlink"])
def test_scratch_refuses_persistent_or_outside_root(runtime, path):
    if path == "persistent":
        runtime.cfg.scratch_base = str(runtime.persistent)
    elif path == "outside":
        runtime.cfg.scratch_base = "/var"
    else:
        link = runtime.scratch / "redirect"
        link.symlink_to(runtime.persistent, target_is_directory=True)
        runtime.cfg.scratch_base = str(link)
    with pytest.raises(ValueError):
        runtime.mod._new_scratch("ingest-")
    assert list(runtime.persistent.iterdir()) == []


@pytest.mark.parametrize("repo", ["../victim", "org/../../victim", "/absolute", "org/repo/extra", "-flag/repo"])
def test_repo_cannot_escape_clone_root(runtime, repo):
    with pytest.raises(ValueError):
        runtime.mod.refresh_repo(repo, {})
    with pytest.raises(ValueError):
        runtime.mod.incremental_wiki_update(repo, "old", "new")
    assert not list(runtime.scratch.iterdir())


def test_persistent_contract_rejects_unsupported_production_io(runtime):
    path = runtime.persistent / "state.json"
    runtime.mod.save_state("state.json", {"old": 1})
    for mode in ("a", "r+", "w+"):
        with pytest.raises(OSError):
            open(path, mode)
    with open(path, "w") as file:
        with pytest.raises(OSError):
            file.seek(0)
    with pytest.raises(OSError):
        os.replace(path, runtime.persistent / "new.json")
    with pytest.raises(OSError):
        open(runtime.persistent / "index.lock", "w")


def test_unchanged_source_lookup_consumers_read_existing_snapshot(runtime):
    repo = Path(runtime.cfg.clone_base) / "org/repo"
    repo.mkdir(parents=True)
    (repo / "example.py").write_text("def example(): pass")
    (repo / ".deepwiki-wiki.md").write_text("# Snapshot wiki")
    learning = load_script("generate-learning-artifacts")
    assert learning.load_wiki("org/repo") == "# Snapshot wiki"
    assert learning.list_source_files("org/repo")[0]["path"] == "example.py"
    target = Path(runtime.cfg.learning_dir) / "org-repo"
    target.mkdir(parents=True)
    (target / "learning-path.json").write_text(json.dumps({"steps": [{"read": ["example.py"]}]}))
    lint = load_script("lint-wiki")
    assert lint.check_broken_learning_paths(["org/repo"]) == []
    (repo / "example.py").unlink()
    assert "1 file references" in lint.check_broken_learning_paths(["org/repo"])[0]


@pytest.mark.parametrize("failure", ["bad-sha", "timeout"])
def test_diff_failure_is_not_successful_empty_diff(runtime, repository, monkeypatch, failure):
    local_git_adapter(runtime, repository, monkeypatch)
    if failure == "timeout":
        original = runtime.mod.subprocess.run
        def timeout_diff(cmd, **kwargs):
            if "diff" in cmd:
                raise subprocess.TimeoutExpired(cmd, 30)
            return original(cmd, **kwargs)
        monkeypatch.setattr(runtime.mod.subprocess, "run", timeout_diff)
    before = dict(runtime.objects)
    assert not runtime.mod.incremental_wiki_update(
        "org/repo", "0" * 40 if failure == "bad-sha" else repository[1], repository[2]
    )
    assert runtime.objects == before
    assert not list(runtime.scratch.iterdir())


@pytest.mark.parametrize("repo", ["org/repo", "org/.github", "org-name/repo.name", "org/-repo"])
def test_valid_repository_names_preserved(runtime, repo):
    assert runtime.mod._safe_repo(repo) == repo
