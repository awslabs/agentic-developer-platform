"""CLI-11 protected delivery and real server request schemas."""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))
spec = importlib.util.spec_from_file_location("machine_cli", CLI / "adp-machine.py")
machine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(machine)
OP = "56240000-0000-4000-8000-000000000050"
REV = "a" * 64


@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    monkeypatch.setattr(machine.common, "ensure_can_mutate", lambda *a, **k: None)


def args(*words):
    return machine.parser().parse_args(list(words))


def principal():
    return {
        "canonical_service_principal_id": "principal",
        "tenant_id": "org",
        "display_name": "Fixture",
        "status": "active",
        "revision": REV,
        "aliases": [{"id": "alias", "alias_id": "registry-id", "alias_source": "agent_registry", "is_active": True}],
    }


def test_principal_registration_uses_canonical_real_schema():
    from src.admin.persona_models.schemas import RegisterServicePrincipalRequest

    client = Mock()
    client.request.side_effect = [{"org_id": "org"}, {"version": "1.0"}, {"canonical_service_principal_id": "principal"}, principal()]
    result = machine.execute(
        args(
            "service-principal",
            "register",
            "--org",
            "org",
            "--name",
            "Fixture",
            "--alias-source",
            "agent_registry",
            "--alias-id",
            "registry-id",
            "--operation-id",
            OP,
            "--yes",
        ),
        client,
    )
    assert result["status"] == "ok"
    method, path, body = client.request.call_args_list[2].args
    assert method == "POST" and path == "/service-principals/register"
    parsed = RegisterServicePrincipalRequest.model_validate(body)
    assert str(parsed.operation_id) == OP
    assert result["detail"]["canonical_service_principal_id"] == "principal"


def test_old_server_and_foreign_tenant_refused_before_mutation():
    for responses in ([{"org_id": "foreign"}], [{"org_id": "org"}, {"version": "old"}]):
        client = Mock()
        client.request.side_effect = responses
        with pytest.raises(machine.common.CliError):
            machine.execute(
                args(
                    "service-principal",
                    "register",
                    "--org",
                    "org",
                    "--name",
                    "Fixture",
                    "--alias-source",
                    "agent_registry",
                    "--alias-id",
                    "registry-id",
                    "--operation-id",
                    OP,
                    "--yes",
                ),
                client,
            )
        assert all(call.args[0] == "GET" for call in client.request.call_args_list)


def test_malformed_mutation_acknowledgement_is_pending_without_retry():
    client = Mock()
    client.request.side_effect = [{"org_id": "org"}, {"version": "1.0"}, {}]
    result = machine.execute(
        args(
            "service-principal",
            "register",
            "--org",
            "org",
            "--name",
            "Fixture",
            "--alias-source",
            "agent_registry",
            "--alias-id",
            "registry-id",
            "--operation-id",
            OP,
            "--yes",
        ),
        client,
    )
    assert result["status"] == "pending"
    assert sum(call.args[0] == "POST" for call in client.request.call_args_list) == 1


def test_cognito_secret_goes_only_to_private_file(tmp_path):
    from src.admin.machine_agents import Register

    os.chmod(tmp_path, 0o700)
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({"name": "Fixture"}))
    target = tmp_path / "credentials.json"
    row = {
        "id": "client",
        "client_id": "client",
        "org_id": "org",
        "identity_type": "cognito-client",
        "revision": REV,
        "name": "Fixture",
        "status": "active",
    }
    client = Mock()
    client.request.side_effect = [
        {"org_id": "org"},
        {"version": "1.0"},
        row,
        row,
        {"client_id": "client", "client_secret": "never-log-this", "token_endpoint": "https://auth.test/oauth2/token", "scopes": []},
    ]
    result = machine.execute(
        args(
            "agent",
            "register",
            "--identity-type",
            "cognito-client",
            "--org",
            "org",
            "--operation-id",
            OP,
            "--spec-file",
            str(metadata),
            "--credential-file",
            str(target),
            "--yes",
        ),
        client,
    )
    assert result["status"] == "ok"
    assert "never-log-this" not in json.dumps(result)
    assert json.loads(target.read_text())["client_secret"] == "never-log-this"
    assert target.stat().st_mode & 0o777 == 0o600
    request = Register.model_validate(client.request.call_args_list[2].args[2])
    assert str(request.operation_id) == OP
    assert request.agent == {"name": "Fixture"}


def test_unsafe_or_existing_credential_output_never_registers(tmp_path):
    os.chmod(tmp_path, 0o700)
    metadata = tmp_path / "metadata.json"
    metadata.write_text('{"name":"Fixture"}')
    target = tmp_path / "credentials.json"
    target.write_text("existing")
    client = Mock()
    client.request.side_effect = [{"org_id": "org"}, {"version": "1.0"}]
    with pytest.raises(FileExistsError):
        machine.execute(
            args(
                "agent",
                "register",
                "--identity-type",
                "cognito-client",
                "--org",
                "org",
                "--operation-id",
                OP,
                "--spec-file",
                str(metadata),
                "--credential-file",
                str(target),
                "--yes",
            ),
            client,
        )
    assert target.read_text() == "existing"
    assert all(call.args[0] == "GET" for call in client.request.call_args_list)


def test_metadata_reads_never_fetch_or_print_credentials():
    client = Mock()
    client.request.side_effect = [
        {"org_id": "org"},
        {"version": "1.0"},
        {"items": [{"client_id": "client", "org_id": "org", "client_secret": "hidden"}]},
    ]
    result = machine.execute(args("agent", "list", "--identity-type", "cognito-client", "--org", "org"), client)
    assert "hidden" not in json.dumps(result) and "client_secret" not in json.dumps(result)
    assert not any(call.args[1].endswith("/credentials") for call in client.request.call_args_list)


def test_cognito_retirement_request_carries_uuid_and_reviewed_revision():
    from src.admin.machine_agents import Patch

    row = {"id": "client", "client_id": "client", "org_id": "org", "identity_type": "cognito-client", "revision": REV, "status": "active"}
    client = Mock()
    client.request.side_effect = [{"org_id": "org"}, {"version": "1.0"}, row, {**row, "status": "retired"}, {**row, "status": "retired"}]
    result = machine.execute(
        args(
            "agent",
            "deregister",
            "client",
            "--identity-type",
            "cognito-client",
            "--org",
            "org",
            "--operation-id",
            OP,
            "--expected-revision",
            REV,
            "--yes",
        ),
        client,
    )
    assert result["status"] == "ok" and result["detail"]["status"] == "retired"
    request = Patch.model_validate(client.request.call_args_list[3].args[2])
    assert request.deregister and str(request.operation_id) == OP and request.expected_revision == REV


def test_sql_create_serializer_and_pending_readback():
    from src.admin.machine_accounts import Register

    client = Mock()
    client.request.side_effect = [{"org_id": "org"}, {"version": "1.0"}, {"id": "account", "org_id": "org"}, {}]
    result = machine.execute(
        args(
            "service-account",
            "create",
            "--identity-type",
            "sql-iam",
            "--org",
            "org",
            "--name",
            "Fixture",
            "--department",
            "dept",
            "--team",
            "team",
            "--role-arn",
            "arn:aws:iam::123456789012:role/fixture",
            "--operation-id",
            OP,
            "--yes",
        ),
        client,
    )
    assert result["status"] == "pending"
    request = Register.model_validate(client.request.call_args_list[2].args[2])
    assert request.account.department_id == "dept" and request.account.team_id == "team"


def test_machine_contract_path_matches_built_gateway():
    from src.app import create_app

    paths = {route.path for route in create_app().routes if hasattr(route, "path")}
    assert machine.BASE + "/registration-contract" in paths
    assert machine.BASE + "/register" in paths
