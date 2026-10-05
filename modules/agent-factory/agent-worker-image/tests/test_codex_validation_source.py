"""A separate host reconstructs exactly the tree named by validation evidence."""

import copy
import hashlib
import io
import tarfile
import base64
import threading
import uuid
from types import SimpleNamespace

import pytest

from lib.codex_validation_source import apply_validation_manifest
from lib.codex_workspace import CodexWorkspace, WorkspaceError


@pytest.fixture
def pair(tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, content in [("source/main.py", b"value = 1\n"), ("source/unchanged.txt", b"preserved\n")]:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    data = stream.getvalue()
    def workspace(name):
        result = CodexWorkspace(tmp_path / name, provider="github", repository="org/repo",
                                source_revision="a" * 40, repository_id="123")
        result.materialize(data, archive_sha256=hashlib.sha256(data).hexdigest())
        return result
    return workspace("worker"), workspace("service")


def change(worker):
    original = worker.read_file("main.py")
    worker.write_file("main.py", "value = 2\n", expected_sha256=original["sha256"])
    worker.write_file("new.py", "new = True\n", expected_sha256=None)
    state = worker.commit("Implement change")
    return worker.export_changes(expected_head=state["localHead"])


def test_separate_validation_checkout_preserves_exact_tree_not_local_commit_identity(pair):
    worker, service = pair
    manifest = change(worker)
    state = apply_validation_manifest(service, manifest)
    assert state["tree"] == worker.state()["tree"]
    assert state["localHead"] != manifest["local_head"]
    assert state["clean"]
    assert service.read_file("main.py")["content"] == "value = 2\n"
    assert service.read_file("unchanged.txt")["content"] == "preserved\n"


def test_baseline_validation_can_transfer_an_empty_delta_but_publication_cannot(pair):
    worker, service = pair
    head = worker.state()["localHead"]
    with pytest.raises(WorkspaceError, match="no committed changes"):
        worker.export_changes(expected_head=head)
    manifest = worker.export_changes(expected_head=head, allow_empty=True)
    assert manifest["changes"] == []
    assert apply_validation_manifest(service, manifest)["tree"] == worker.state()["tree"]


@pytest.mark.parametrize("field,value", [
    ("repository_id", "456"), ("repository", "other/repo"), ("provider", "gitlab"),
    ("source_revision", "b" * 40), ("base_tree", "b" * 40), ("tree", "b" * 40),
    ("local_head", "not-a-commit"), ("command", "untrusted"),
])
def test_changed_authority_or_fabricated_tree_never_replaces_service_checkout(pair, field, value):
    worker, service = pair
    before = service.state()
    manifest = change(worker)
    manifest[field] = value
    with pytest.raises(WorkspaceError):
        apply_validation_manifest(service, manifest)
    assert service.state() == before
    assert service.read_file("main.py")["content"] == "value = 1\n"


@pytest.mark.parametrize("alter", [
    {"path": "../escape"}, {"path": ".git/config"}, {"path": "a\nfile"},
    {"mode": "120000"}, {"deleted": True}, {"content_base64": "%%%"},
])
def test_invalid_delta_is_refused_before_materialization(pair, alter):
    worker, service = pair
    before = service.state()
    manifest = change(worker)
    manifest["changes"][0].update(alter)
    with pytest.raises(WorkspaceError):
        apply_validation_manifest(service, manifest)
    assert service.state() == before


def test_duplicate_change_paths_are_not_order_dependent(pair):
    worker, service = pair
    manifest = change(worker)
    manifest["changes"].append(copy.deepcopy(manifest["changes"][0]))
    with pytest.raises(WorkspaceError, match="Duplicate"):
        apply_validation_manifest(service, manifest)


@pytest.fixture
def service_run(pair):
    worker, original = pair
    data = original._git_bytes("archive", "--format=tar.gz", "--prefix=source/", "HEAD")
    manifest = change(worker)
    attempt = {"run": {"task_id": "tsk_" + str(uuid.uuid4()), "invocation_id": str(uuid.uuid4()), "generation": 1},
               "runtime_attempt_id": str(uuid.uuid4())}
    check = {"name": "acceptance", "image": "registry.example/checks@sha256:" + "a" * 64, "argv": ["true"]}
    binding = {"binding": {"provider": "github", "repository": "org/repo", "repository_id": "123", "validation_checks": [check]}}
    class Authority:
        calls = 0
        revoke = False
        def authorize(self, **kwargs):
            assert kwargs == {"attempt": attempt, "tool": "validation.run"}
            self.calls += 1
            current = copy.deepcopy(binding)
            if self.revoke and self.calls == 3:
                current["binding"]["validation_checks"][0]["image"] = "registry.example/other@sha256:" + "b" * 64
            return SimpleNamespace(task={"repository_binding": current, "tool_grants": ["repository.read", "validation.run"]})
        def tool_authorize(self, body):
            assert body["attempt"] == attempt and body["tool"] == "repository.read"
            return {"schema_version": "1.0", "identity": {**attempt["run"], "runtime_attempt_id": attempt["runtime_attempt_id"]},
                    "task": {"tool_grants": ["repository.read", "validation.run"], "repository_binding": binding}}
        def repository_source(self, body):
            assert body["attempt"] == attempt and body["index"] == 0
            digest = hashlib.sha256(data).hexdigest()
            return {"schema_version": "1.0", "repository_binding": binding, "commit": "a" * 40,
                    "archive_sha256": digest, "byte_length": len(data), "index": 0, "chunk_count": 1,
                    "chunk_sha256": digest, "content_base64": base64.b64encode(data).decode()}
    class Executor:
        calls = 0
        def run_repository(self, **kwargs):
            self.calls += 1
            assert kwargs["check"].document()["image"] == check["image"]
            assert (kwargs["repository"] / "main.py").read_text() == "value = 2\n"
            return {"status": "passed", "tree": manifest["tree"], "commit": kwargs["expected_head"]}
    return dict(authority=Authority(), attempt=attempt, manifest=manifest, check_name="acceptance",
                executor=Executor(), cancelled=threading.Event())


def test_service_runner_uses_gateway_source_and_maps_only_the_verified_tree(service_run):
    from lib.codex_validation_service_runner import run_service_validation
    result = run_service_validation(**service_run)
    assert result["tree"] == service_run["manifest"]["tree"]
    assert result["commit"] == service_run["manifest"]["local_head"]
    assert result["validationCommit"] != result["commit"]
    assert result["sourceRevision"] == "a" * 40


def test_service_runner_rejects_unknown_checks_without_execution(service_run):
    from lib.codex_validation_service_runner import run_service_validation
    from lib.codex_validation import ValidationUnavailable
    service_run["check_name"] = "unregistered"
    with pytest.raises(ValidationUnavailable):
        run_service_validation(**service_run)
    assert service_run["executor"].calls == 0


def test_service_runner_rechecks_policy_after_validation(service_run):
    from lib.codex_validation_service_runner import run_service_validation
    from lib.codex_validation import ValidationUnavailable
    service_run["authority"].revoke = True
    with pytest.raises(ValidationUnavailable, match="authority changed"):
        run_service_validation(**service_run)
    assert service_run["executor"].calls == 1
