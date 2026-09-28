"""Maintenance previews, serialization, acknowledgement and secret boundaries."""

import io
import json
import urllib.error
from unittest.mock import Mock
from uuid import uuid4

import pytest

from src.admin.connections.maintenance import AppKeyRequest
from src.admin.org_connections.schemas import GitHubConnectionAttachRequest
from tests.cli.test_adp_github_admin import cli, common

OLD = str(uuid4())
OP = str(uuid4())
KEY = "private-test-material-" * 8


class Api:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body))
        if method == "GET":
            if path.endswith("/maintenance"):
                return {"contract": "app-maintenance-v1", "app_id": "123", "key_version": OLD, "private_key": KEY}
            return {"connections": [], "total": 0}
        if isinstance(self.result, Exception):
            raise self.result
        if body and "private_key" in body:
            AppKeyRequest.model_validate_json(json.dumps(body))
        elif method == "POST":
            GitHubConnectionAttachRequest.model_validate_json(json.dumps(body))
        return self.result


def args(*extra):
    return cli.parser().parse_args(
        ["rotate-key", "--expect-app-id", "123", "--expect-key-version", OLD, "--operation-id", OP, "--credentials-stdin", *extra]
    )


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(common, "ensure_can_mutate", Mock())
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({"private_key": KEY})))


def test_rotation_preview_does_not_read_stdin_or_mutate(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", Mock(read=Mock(side_effect=AssertionError("secret read"))))
    api = Api()
    result = cli.run(args("--dry-run"), api)
    assert result["status"] == "dry_run"
    assert all(call[0] == "GET" for call in api.calls)
    assert KEY not in json.dumps(result)


def test_serialized_rotation_schema_and_whitelisted_ack():
    api = Api({"app_id": "123", "rotated": True, "operation_id": OP, "key_version": OP, "private_key": KEY})
    result = cli.run(args("--yes"), api)
    assert result["status"] == "ok"
    assert KEY not in json.dumps(result)
    assert api.calls[-1][2]["operation_id"] == OP


@pytest.mark.parametrize("result", [None, {}, {"app_id": "foreign", "rotated": True}, common.CliError(KEY, "http_error", 5), OSError(KEY)])
def test_unknown_or_mismatched_ack_never_prints_key(result):
    with pytest.raises(common.CliError) as exc:
        cli.run(args("--yes"), Api(result))
    assert exc.value.code == "unknown_mutation_outcome"
    assert KEY not in str(exc.value)


def test_stale_review_refuses_before_secret_read(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", Mock(read=Mock(side_effect=AssertionError("secret read"))))
    request = args("--yes")
    request.expect_app_id = "456"
    api = Api()
    with pytest.raises(common.CliError):
        cli.run(request, api)
    assert len(api.calls) == 1


def test_maintenance_status_does_not_echo_unknown_fields():
    result = cli.run(cli.parser().parse_args(["status", "--maintenance"]), Api())
    assert KEY not in json.dumps(result)


def test_real_http_error_serializer_discards_secret(monkeypatch):
    monkeypatch.setattr(common, "gateway_url", lambda: "https://gateway.example/api")
    monkeypatch.setattr(common, "access_token", lambda: "fixture")
    api = common.Api()
    body = json.dumps({"detail": [{"input": KEY, "msg": KEY}]}).encode()
    api.opener = Mock()
    api.opener.open.side_effect = urllib.error.HTTPError("https://gateway.example/api", 422, "invalid", {}, io.BytesIO(body))
    with pytest.raises(common.CliError) as exc:
        api.request("POST", "/admin/connections/github/app/maintenance/rotate-key", {"private_key": KEY})
    assert KEY not in str(exc.value)


def test_org_binding_serializes_canonical_schema():
    api = Api({"org_id": "test", "installation_id": "42", "routable": True})
    result = cli.run(cli.parser().parse_args(["org-binding", "add", "--org", "test", "--installation", "42", "--yes"]), api)
    assert result["status"] == "ok"
    assert api.calls[-1][1] == "/admin/organizations/test/connections/github"


@pytest.mark.parametrize("active,manage", [(False, True), (True, False)])
def test_disconnect_refuses_foreign_or_unmanaged_installation(active, manage):
    from tests.cli.test_adp_github import cli as user_cli
    from tests.cli.test_adp_github import connection

    api = Mock()
    row = connection(installation_id=42)
    row.update(is_active_tenant=active, can_manage=manage)
    api.request.return_value = {"connections": [row]}
    with pytest.raises(common.CliError) as exc:
        user_cli.run(user_cli.parser().parse_args(["disconnect", "--installation", "42", "--yes"]), api)
    assert exc.value.exit_code == 3
    assert all(call.args[0] == "GET" for call in api.request.call_args_list)


@pytest.mark.parametrize("residual,expected", [([], "ok"), (["pending_cleanup"], "pending")])
def test_disconnect_uses_real_saga_response_schema(residual, expected):
    from src.admin.connections.schemas import DeleteConnectionResponse
    from tests.cli.test_adp_github import cli as user_cli
    from tests.cli.test_adp_github import connection

    api = Mock()
    acknowledgement = DeleteConnectionResponse(
        installation_id=42,
        deleted=True,
        local_revoked=True,
        provider_revoked=True,
        residual=residual,
    ).model_dump(mode="json")
    api.request.side_effect = [{"connections": [connection(installation_id=42)]}, acknowledgement]
    result = user_cli.run(user_cli.parser().parse_args(["disconnect", "--installation", "42", "--yes"]), api)
    assert result["status"] == expected
    assert api.request.call_args.args == ("DELETE", "/admin/connections/github/42")
