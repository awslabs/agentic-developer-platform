"""App-owned source and exact-revision at-most-once CLI contracts."""

import importlib.util
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[4]
CLI = ROOT / "modules/gateway/cli"
spec = importlib.util.spec_from_file_location("superplane_lifecycle_test", CLI / "adp-superplane.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
extension = cli.lifecycle_helper()
WORKSPACE = str(uuid.uuid4())
REV = "a" * 64


def snapshot():
    return {
        "workspace_id": WORKSPACE,
        "status": "Active",
        "is_default": False,
        "deployments": [],
        "provider_connections": [],
        "provider_handles": [],
        "billing_state": "unconfirmed",
        "revision": REV,
    }


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(extension.common, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(extension.common, "gateway_url", lambda: "https://example.invalid/api")
    monkeypatch.setattr(extension.common, "authenticated_scope", lambda: "tenant:user")
    monkeypatch.setattr(extension.common, "ensure_can_mutate", Mock())


def test_app_source_matches_served_copy():
    assert (CLI / "adp-superplane-lifecycle.py").read_bytes() == (
        ROOT / "modules/domain-apps/superplane/cli/adp-superplane-lifecycle.py"
    ).read_bytes()


def test_workspace_dry_run_reads_only(isolated):
    api = Mock()
    api.request.return_value = snapshot()
    args = cli.parser().parse_args(["workspace", "delete", WORKSPACE, "--dry-run"])
    assert extension.workspace_delete(args, api)["status"] == "dry_run"
    assert [call.args[0] for call in api.request.call_args_list] == ["GET"]


def test_workspace_protection_and_unseen_revision(isolated):
    api = Mock()
    value = snapshot()
    value["is_default"] = True
    api.request.return_value = value
    args = cli.parser().parse_args(["workspace", "delete", WORKSPACE, "--yes", "--operation-id", str(uuid.uuid4())])
    assert extension.workspace_delete(args, api)["status"] == "unavailable"
    api.request.return_value = snapshot()
    with pytest.raises(cli.CliError):
        extension.workspace_delete(args, api)
    assert all(call.args[0] == "GET" for call in api.request.call_args_list)


def test_lost_delete_not_replayed(isolated):
    api = Mock()
    api.request.side_effect = [snapshot(), OSError("lost"), snapshot()]
    args = cli.parser().parse_args(["workspace", "delete", WORKSPACE, "--yes", "--expected-revision", REV, "--operation-id", str(uuid.uuid4())])
    assert extension.workspace_delete(args, api)["status"] == "pending"
    assert extension.workspace_delete(args, api)["detail"]["replayed_without_write"]
    assert [call.args[0] for call in api.request.call_args_list] == ["GET", "DELETE", "GET"]


def test_namespace_assertion_not_override():
    args = cli.parser().parse_args(
        [
            "deploy",
            "create",
            "--name",
            "model-a",
            "--model",
            "model",
            "--operation-id",
            str(uuid.uuid4()),
            "--profile-id",
            "profile",
            "--plan-revision",
            REV,
            "--approval-id",
            str(uuid.uuid4()),
            "--namespace",
            "owned",
            "--dry-run",
            "--workspace",
            WORKSPACE,
        ]
    )
    result = cli.run(args, Mock())
    deployment = result["detail"]["deployment"]
    assert deployment["expected_namespace"] == "owned" and "namespace" not in deployment


def test_workspace_event_cursor_route():
    api = Mock()
    api.request.return_value = {"workspace_id": WORKSPACE, "events": [], "next_cursor": None, "has_more": False}
    args = cli.parser().parse_args(["events", "--workspace", WORKSPACE])
    assert extension.events(args, api)["status"] == "ok"
    assert f"/events/workspaces/{WORKSPACE}?" in api.request.call_args.args[1]
    api.request.return_value["workspace_id"] = str(uuid.uuid4())
    with pytest.raises(cli.CliError):
        extension.events(args, api)


def connection_snapshot(connection, credential="credential-a"):
    return {
        "workspace_id": WORKSPACE,
        "connection_id": connection,
        "provider": "aws",
        "status": "pending",
        "credential": {"credential_id": credential, "service": "aws", "label": "test"},
        "binding": {"workspace_id": WORKSPACE},
        "admits_new_work": False,
        "allows_renewal": False,
        "revision": REV,
    }


@pytest.mark.parametrize("mismatch", [False, True])
def test_provider_create_readback_and_safe_ack(isolated, mismatch):
    connection = str(uuid.uuid4())
    api = Mock()
    api.request.side_effect = [
        {"features": ["provider-connection-operation-id-v1"]},
        {"connection_id": connection, "status": "pending", "untrusted_extra": "do-not-persist"},
        connection_snapshot(connection, "wrong" if mismatch else "credential-a"),
    ]
    args = cli.parser().parse_args(
        [
            "provider-connection",
            "create",
            "--workspace",
            WORKSPACE,
            "--credential-id",
            "credential-a",
            "--service",
            "aws",
            "--label",
            "test",
            "--provider",
            "aws",
            "--yes",
            "--operation-id",
            connection,
        ]
    )
    result = extension.provider_connection(args, api)
    assert result["status"] == "pending"
    if mismatch:
        assert result["detail"]["outcome"] == "unknown"
    else:
        assert result["detail"]["observed"]["connection_id"] == connection
    assert "do-not-persist" not in str(result)
    assert all("do-not-persist" not in path.read_text() for path in extension.common.state_dir().rglob("*.json"))


def test_provider_ack_target_mismatch_not_replayed(isolated):
    connection = str(uuid.uuid4())
    before = connection_snapshot(connection)
    api = Mock()
    api.request.side_effect = [before, {"connection_id": str(uuid.uuid4()), "status": "disabled"}, before]
    args = cli.parser().parse_args(
        [
            "provider-connection",
            "revoke",
            "--workspace",
            WORKSPACE,
            "--connection",
            connection,
            "--expected-revision",
            REV,
            "--yes",
            "--operation-id",
            str(uuid.uuid4()),
        ]
    )
    assert extension.provider_connection(args, api)["detail"]["outcome"] == "unknown"
    assert extension.provider_connection(args, api)["detail"]["replayed_without_write"]
    assert [call.args[0] for call in api.request.call_args_list] == ["GET", "DELETE", "GET"]


def test_provider_create_lost_reply_recovers_by_exact_get_without_another_write(isolated):
    operation = str(uuid.uuid4())
    args = cli.parser().parse_args(
        [
            "provider-connection",
            "create",
            "--workspace",
            WORKSPACE,
            "--credential-id",
            "credential-a",
            "--service",
            "aws",
            "--label",
            "test",
            "--provider",
            "aws",
            "--operation-id",
            operation,
            "--yes",
        ]
    )
    writes = []

    def request(method, path, body=None):
        if path.endswith("/capabilities"):
            return {"features": ["provider-connection-operation-id-v1"]}
        if method == "POST":
            writes.append(body)
            raise cli.common.CliError("lost accepted response", "unavailable", 5)
        assert method == "GET" and path.endswith("/" + operation)
        return connection_snapshot(operation)

    api = Mock()
    api.request.side_effect = request
    assert extension.provider_connection(args, api)["detail"]["outcome"] == "unknown"
    replay = extension.provider_connection(args, api)
    assert replay["detail"]["replayed_without_write"]
    assert replay["detail"]["observed"]["connection_id"] == operation
    assert len(writes) == 1 and writes[0]["operation_id"] == operation


def test_provider_create_refuses_domain_without_operation_contract(isolated):
    api = Mock()
    api.request.return_value = {"features": []}
    args = cli.parser().parse_args(
        [
            "provider-connection",
            "create",
            "--workspace",
            WORKSPACE,
            "--credential-id",
            "credential-a",
            "--service",
            "aws",
            "--label",
            "test",
            "--provider",
            "aws",
            "--operation-id",
            str(uuid.uuid4()),
            "--yes",
        ]
    )
    with pytest.raises(cli.common.CliError, match="nothing was written"):
        extension.provider_connection(args, api)
    assert all(call.args[0] == "GET" for call in api.request.call_args_list)
