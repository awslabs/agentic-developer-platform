"""Workspace tool receipts bind real edits to one authorized Task attempt."""

import base64
import hashlib
import io
import json
import tarfile
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.codex_workspace import CodexWorkspace
from lib.codex_workspace_tools import WORKSPACE_TOOLS, WorkspaceTools
from lib.task_run_client import TaskRunClientError


@pytest.fixture
def tool(tmp_path):
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w:gz") as archive:
        entry = tarfile.TarInfo("root/main.txt")
        entry.size = 3
        archive.addfile(entry, io.BytesIO(b"old"))
    data = content.getvalue()
    workspace = CodexWorkspace(
        tmp_path / "repo", provider="github", repository="org/repo", source_revision="a" * 40
    )
    workspace.materialize(data, archive_sha256=hashlib.sha256(data).hexdigest())
    attempt = {
        "run": {
            "task_id": "tsk_" + str(uuid.uuid4()),
            "invocation_id": str(uuid.uuid4()),
            "generation": 1,
        },
        "runtime_attempt_id": str(uuid.uuid4()),
    }
    client = Mock()
    authority = {
        "schema_version": "1.0",
        "identity": {**attempt["run"], "runtime_attempt_id": attempt["runtime_attempt_id"]},
        "task": {
            "tool_grants": sorted(WORKSPACE_TOOLS),
            "repository_binding": {"binding": {"provider": "github", "repository": "org/repo"}},
        },
    }
    client.tool_authorize.return_value = authority
    artifacts = []

    def publish(body):
        decoded = base64.b64decode(body["content_base64"])
        assert hashlib.sha256(decoded).hexdigest() == body["content_sha256"]
        artifacts.append(json.loads(decoded))
        digest = body["content_sha256"]
        key = f"{attempt['run']['task_id']}:application/json:{digest}"
        return {
            "schema_version": "1.0",
            "artifact_id": "art_"
            + str(uuid.UUID(bytes=hashlib.sha256(key.encode()).digest()[:16], version=4)),
            "content_sha256": digest,
            "content_type": "application/json",
            "version": 1,
            "expires_at": None,
        }

    client.artifact.side_effect = publish
    handler = WorkspaceTools(client, attempt=attempt, workspace=workspace, tools=WORKSPACE_TOOLS)

    def invoke(operation, payload):
        return handler.invoke(
            "repository." + operation,
            {
                "schema_version": "1.0",
                "attempt": attempt,
                "operation_id": str(uuid.uuid4()),
                "operation": operation,
                "payload": payload,
            },
        )

    return SimpleNamespace(
        client=client,
        handler=handler,
        workspace=workspace,
        invoke=invoke,
        artifacts=artifacts,
        authority=authority,
    )


def test_read_edit_commit_and_state_have_verified_artifact_receipts(tool):
    original = tool.invoke("read", {"path": "main.txt"})["result"]
    result = tool.invoke(
        "write", {"path": "main.txt", "content": "new", "expected_sha256": original["sha256"]}
    )
    assert result["operation_status"] == "confirmed"
    committed = tool.invoke("commit", {"message": "Repair application"})["result"]
    assert committed["clean"] and committed["sourceRevision"] == "a" * 40
    assert tool.invoke("state", {})["result"]["localHead"] == committed["localHead"]
    assert tool.workspace.read_file("main.txt")["content"] == "new"
    assert len(tool.artifacts) == 4


def test_stale_edit_returns_a_durable_refusal_without_changing_source(tool):
    result = tool.invoke(
        "write", {"path": "main.txt", "content": "new", "expected_sha256": "0" * 64}
    )
    assert result["result"]["status"] == "rejected"
    assert tool.workspace.read_file("main.txt")["content"] == "old"
    assert tool.artifacts == [result["result"]]


def test_revoked_tool_cannot_edit_workspace(tool):
    tool.authority["task"]["tool_grants"] = []
    with pytest.raises(TaskRunClientError):
        tool.invoke("write", {"path": "new.txt", "content": "new", "expected_sha256": None})
    assert not (tool.workspace.root / "new.txt").exists()
    assert tool.artifacts == []


def test_foreign_attempt_cannot_use_workspace(tool):
    with pytest.raises(TaskRunClientError):
        tool.handler.invoke(
            "repository.state",
            {
                "schema_version": "1.0",
                "attempt": {},
                "operation_id": str(uuid.uuid4()),
                "operation": "state",
                "payload": {},
            },
        )
    tool.client.tool_authorize.assert_not_called()


def test_revocation_after_edit_prevents_success_receipt(tool):
    tool.client.tool_authorize.side_effect = [tool.authority, TaskRunClientError("revoked")]
    with pytest.raises(TaskRunClientError):
        tool.invoke("write", {"path": "new.txt", "content": "new", "expected_sha256": None})
    assert tool.artifacts == []
    # Local effect may already have happened; the caller's journal must retain
    # unknown, not replay it or claim successful completion.
    assert tool.workspace.read_file("new.txt")["content"] == "new"


def test_publication_manifest_uses_committed_tree_and_provider_source_identity(tool):
    tool.workspace.repository_id = "456"
    before = tool.workspace.state()
    tool.workspace.write_file(
        path="main.txt", content="new", expected_sha256=hashlib.sha256(b"old").hexdigest()
    )
    committed = tool.workspace.commit("Repair application")
    manifest = tool.workspace.export_changes(expected_head=committed["localHead"])
    assert manifest["source_revision"] == "a" * 40
    assert manifest["base_tree"] == before["tree"]
    assert (
        manifest["tree"] == committed["tree"] and manifest["local_head"] == committed["localHead"]
    )
    assert manifest["changes"] == [
        {
            "path": "main.txt",
            "mode": "100644",
            "deleted": False,
            "content_base64": base64.b64encode(b"new").decode(),
        }
    ]


def test_publication_refuses_dirty_or_wrong_head_and_empty_change(tool):
    from lib.codex_workspace import WorkspaceError

    tool.workspace.repository_id = "456"
    head = tool.workspace.state()["localHead"]
    with pytest.raises(WorkspaceError, match="no committed changes"):
        tool.workspace.export_changes(expected_head=head)
    tool.workspace.write_file(
        path="main.txt", content="new", expected_sha256=hashlib.sha256(b"old").hexdigest()
    )
    with pytest.raises(WorkspaceError, match="clean"):
        tool.workspace.export_changes(expected_head=head)
    tool.workspace.commit("Repair application")
    with pytest.raises(WorkspaceError, match="clean"):
        tool.workspace.export_changes(expected_head=head)


def test_publication_manifest_handles_binary_executable_and_deletion(tool):
    tool.workspace.repository_id = "456"
    (tool.workspace.root / "main.txt").unlink()
    (tool.workspace.root / "new.bin").write_bytes(b"\x00\xff\x80")
    (tool.workspace.root / "new.bin").chmod(0o755)
    head = tool.workspace.commit("Replace source")["localHead"]
    changes = tool.workspace.export_changes(expected_head=head)["changes"]
    assert changes == [
        {"path": "main.txt", "mode": "100644", "deleted": True, "content_base64": None},
        {
            "path": "new.bin",
            "mode": "100755",
            "deleted": False,
            "content_base64": base64.b64encode(b"\x00\xff\x80").decode(),
        },
    ]
