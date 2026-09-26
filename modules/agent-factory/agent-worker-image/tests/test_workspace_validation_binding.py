"""Automatic workspace/check binding and real isolated edit-validation flow."""

import base64
import copy
import hashlib
import io
import json
import os
import tarfile
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.codex_workspace import CodexWorkspace
from lib.codex_workspace_tools import WORKSPACE_TOOLS
from lib.run_identity import CONTROL_ENDPOINT_ENV
from lib.task_run_client import TaskRunClient, TaskRunClientError


@pytest.fixture
def bound(tmp_path, monkeypatch):
    monkeypatch.setenv(CONTROL_ENDPOINT_ENV, "https://gateway.example.test/internal/v1/agent")
    client = TaskRunClient()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for name, content in {
            "value.txt": b"wrong\n",
            "test.sh": b'test "$(cat value.txt)" = expected\n',
        }.items():
            entry = tarfile.TarInfo("root/" + name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    workspace = CodexWorkspace(
        tmp_path / "repo",
        provider="github",
        repository="org/repo",
        repository_id="456",
        source_revision="a" * 40,
    )
    workspace.materialize(raw.getvalue(), archive_sha256=hashlib.sha256(raw.getvalue()).hexdigest())
    attempt = {
        "run": {
            "task_id": "tsk_" + str(uuid.uuid4()),
            "invocation_id": str(uuid.uuid4()),
            "generation": 1,
        },
        "runtime_attempt_id": str(uuid.uuid4()),
    }
    tools = sorted(WORKSPACE_TOOLS | {"validation.run"})
    binding = {
        "alias": "app",
        "binding": {
            "provider": "github",
            "repository": "org/repo",
            "repository_id": "456",
            "connection_id": "installation:123",
            "base_branch": "main",
            "validation_checks": [
                {
                    "name": "unit",
                    "image": os.environ.get("ADP_CODEX_VALIDATION_IMAGE", "sha256:" + "a" * 64),
                    "argv": ["/bin/sh", "test.sh"],
                    "max_output_bytes": 8192,
                }
            ],
        },
    }
    authority = {
        "schema_version": "1.0",
        "identity": {**attempt["run"], "runtime_attempt_id": attempt["runtime_attempt_id"]},
        "task": {"tool_grants": tools, "repository_binding": binding},
    }
    client.tool_authorize = Mock(side_effect=lambda body: copy.deepcopy(authority))
    artifacts = []

    def publish(body):
        content = base64.b64decode(body["content_base64"])
        artifacts.append(json.loads(content))
        assert hashlib.sha256(content).hexdigest() == body["content_sha256"]
        key = f"{attempt['run']['task_id']}:application/json:{body['content_sha256']}"
        return {
            "schema_version": "1.0",
            "artifact_id": "art_"
            + str(uuid.UUID(bytes=hashlib.sha256(key.encode()).digest()[:16], version=4)),
            "content_sha256": body["content_sha256"],
            "content_type": "application/json",
            "version": 1,
            "expires_at": None,
        }

    client.artifact = Mock(side_effect=publish)

    def invoke(name, payload):
        return client.tool(
            name,
            {
                "schema_version": "1.0",
                "attempt": attempt,
                "operation_id": str(uuid.uuid4()),
                "operation": name.split(".")[1],
                "payload": payload,
            },
        )

    return SimpleNamespace(
        client=client,
        workspace=workspace,
        attempt=attempt,
        tools=tools,
        authority=authority,
        binding=binding,
        invoke=invoke,
        artifacts=artifacts,
    )


def test_approved_checks_bind_to_provisioned_workspace_and_are_cleared(bound):
    bound.client.bind_workspace(attempt=bound.attempt, workspace=bound.workspace, tools=bound.tools)
    assert bound.client._validation_tool.repository == bound.workspace.root
    assert bound.client._validation_tool.checks["unit"].argv == ("/bin/sh", "test.sh")
    bound.client.clear_credential()
    assert bound.client._workspace_tools is None and bound.client._validation_tool is None


@pytest.mark.parametrize("fault", ["repository", "checks", "attempt", "permission"])
def test_invalid_binding_never_leaves_partially_bound_tools(bound, fault):
    if fault == "repository":
        bound.binding["binding"]["repository_id"] = "789"
    elif fault == "checks":
        bound.binding["binding"]["validation_checks"] = []
    elif fault == "attempt":
        bound.authority["identity"]["runtime_attempt_id"] = str(uuid.uuid4())
    else:
        bound.authority["task"]["tool_grants"] = []
    with pytest.raises(TaskRunClientError):
        bound.client.bind_workspace(
            attempt=bound.attempt, workspace=bound.workspace, tools=bound.tools
        )
    assert bound.client._workspace_tools is None and bound.client._validation_tool is None


def test_check_policy_change_prevents_execution(bound):
    bound.client.bind_workspace(attempt=bound.attempt, workspace=bound.workspace, tools=bound.tools)
    executor = bound.client._validation_tool.executor = Mock()
    bound.binding["binding"]["validation_checks"][0]["argv"] = ["different"]
    with pytest.raises(TaskRunClientError, match="policy changed"):
        bound.invoke(
            "validation.run", {"check": "unit", "commit": bound.workspace.state()["localHead"]}
        )
    executor.run_repository.assert_not_called()
    bound.client.artifact.assert_not_called()


def test_provisioned_workspace_edit_commit_validate_with_real_docker(bound):
    if not os.environ.get("ADP_CODEX_VALIDATION_IMAGE"):
        pytest.skip("requires an explicitly provisioned immutable Docker image")
    bound.client.bind_workspace(attempt=bound.attempt, workspace=bound.workspace, tools=bound.tools)
    before = bound.workspace.state()["localHead"]
    failed = bound.invoke("validation.run", {"check": "unit", "commit": before})["result"]
    assert failed["status"] == "failed" and failed["commit"] == before
    read = bound.invoke("repository.read", {"path": "value.txt"})["result"]
    bound.invoke(
        "repository.write",
        {"path": "value.txt", "content": "expected\n", "expected_sha256": read["sha256"]},
    )
    commit = bound.invoke("repository.commit", {"message": "Repair value"})["result"]["localHead"]
    assert commit != before
    passed = bound.invoke("validation.run", {"check": "unit", "commit": commit})["result"]
    assert passed["status"] == "passed" and passed["commit"] == commit
    assert len(bound.artifacts) == 5


def test_validation_archive_includes_export_ignored_files_without_substitution(bound):
    from lib.codex_validation import DockerValidationExecutor, ValidationCheck

    (bound.workspace.root / ".gitattributes").write_text(
        "value.txt export-ignore\nstamp.txt export-subst\n"
    )
    (bound.workspace.root / "stamp.txt").write_text("$Format:%H$\n")
    commit = bound.workspace.commit("Add export attributes")["localHead"]
    executor = DockerValidationExecutor()

    def inspect_archive(**kwargs):
        with tarfile.open(kwargs["archive"]) as archive:
            assert archive.extractfile("value.txt").read() == b"wrong\n"
            assert archive.extractfile("stamp.txt").read() == b"$Format:%H$\n"
        return {"status": "passed"}

    executor.run = Mock(side_effect=inspect_archive)
    result = executor.run_repository(
        check=ValidationCheck(name="unit", image="sha256:" + "a" * 64, argv=("true",)),
        repository=bound.workspace.root,
        expected_head=commit,
    )
    assert result["tree"] == bound.workspace.state()["tree"]


def test_publication_exports_host_manifest_and_returns_small_receipt(bound):
    bound.tools.append("change.create")
    bound.client.bind_workspace(attempt=bound.attempt, workspace=bound.workspace, tools=bound.tools)
    bound.workspace.write_file(path="value.txt", content="expected\n", expected_sha256=hashlib.sha256(b"wrong\n").hexdigest())
    state = bound.workspace.commit("Repair")

    def publish(body):
        manifest = bound.artifacts[-1]
        assert manifest["tree"] == state["tree"]
        assert manifest["local_head"] == body["commit"]
        assert manifest["changes"][0]["path"] == "value.txt"
        assert base64.b64decode(manifest["changes"][0]["content_base64"]) == b"expected\n"
        return {"task_id": bound.attempt["run"]["task_id"], "local_head": state["localHead"],
                "tree": state["tree"], "repository_id": "456", "provider_head": "e" * 40}

    bound.client.repository_publication = Mock(side_effect=publish)
    result = bound.invoke("change.create", {"commit": state["localHead"], "title": "Repair", "body": "Validated"})
    assert result["operation_status"] == "confirmed"
    assert "changes" not in result["result"]
    assert len(bound.artifacts) == 2
    bound.client.clear_credential()
    assert bound.client._publication_tool is None


@pytest.mark.parametrize("permission", ["repository.read", "repository.write", "repository.commit", "validation.run"])
def test_publication_missing_prerequisite_leaves_no_bound_tools(bound, permission):
    bound.tools.append("change.create")
    bound.tools.remove(permission)
    with pytest.raises(TaskRunClientError):
        bound.client.bind_workspace(attempt=bound.attempt, workspace=bound.workspace, tools=bound.tools)
    assert bound.client._workspace_tools is None
    assert bound.client._validation_tool is None
    assert bound.client._publication_tool is None
