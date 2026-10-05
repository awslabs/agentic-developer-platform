"""AWS account registration stays reference-only at the CLI boundary (#5637)."""

from __future__ import annotations

import ast
import base64
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).parents[4]
SCRIPT = REPO / "modules/gateway/cli/adp-superplane.py"

spec = importlib.util.spec_from_file_location("adp_superplane_reconcile", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

ACCOUNT_ID = "123456789012"
CONNECTION_ID = "66666666-7777-4888-8999-aaaaaaaaaaaa"
ACCOUNT_RECORD = "11111111-2222-3333-4444-555555555555"


def executable_source() -> str:
    tree = ast.parse(SCRIPT.read_text())
    holders = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            node.body.pop(0)
    return ast.unparse(tree)


class RecordingApi:
    base = "https://gateway.example.test/api"

    def __init__(self, response=None) -> None:
        self.sent: list[tuple[str, str, object]] = []
        self.response = response or {
            "id": ACCOUNT_RECORD,
            "name": "prod",
            "provider": "aws",
            "account_id": ACCOUNT_ID,
            "status": "Active",
            "adp_credential_ids": [CONNECTION_ID],
        }

    def request(self, method, path, body=None, **kwargs):
        self.sent.append((method, path, body))
        if (method, path) == ("GET", cli.ACCOUNT_ADAPTER_SUPPORT):
            return {"version": 2, "features": ["account-vault-reference-v1"]}
        return self.response


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # Shaped transport fixture, not a claim that a stub verified a signature.
    claims = {
        "sub": "user-1",
        "custom:org_id": "org-1",
        "custom:account_type": "human",
        "token_use": "access",
        "client_id": "fixture-client",
        "iss": "https://issuer.example.test",
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    monkeypatch.setattr(cli.common, "access_token", lambda: f"header.{payload}.signature")
    monkeypatch.setattr(cli.common, "deployment_stamp", lambda: {"deployment_id": "test", "deployment": "test"})
    monkeypatch.setattr(cli.common, "gateway_url", lambda: "https://gateway.example.test/api")


def _register(api, *extra):
    return cli.run(
        cli.parser().parse_args(
            [
                "aws-onboard",
                "register",
                "--account-id",
                ACCOUNT_ID,
                "--credential-id",
                CONNECTION_ID,
                "--name",
                "prod",
                "--yes",
                *extra,
            ]
        ),
        api,
    )


def test_registration_sends_only_the_opaque_reference() -> None:
    api = RecordingApi()
    result = _register(api)

    assert api.sent == [
        ("GET", cli.ACCOUNT_ADAPTER_SUPPORT, None),
        ("GET", cli.common.CAPABILITIES_PATH, None),
        (
            "POST",
            cli.API_BASE + "/accounts",
            {
                "name": "prod",
                "provider": "aws",
                "account_id": ACCOUNT_ID,
                "adp_credential_id": CONNECTION_ID,
            },
        ),
    ]
    assert result["status"] == "ok"
    assert result["detail"]["account"]["id"] == ACCOUNT_RECORD
    assert "role_arn" not in json.dumps(api.sent)
    assert "external_id" not in json.dumps(api.sent)


def test_both_preserved_command_forms_use_the_same_contract() -> None:
    api = RecordingApi()
    result = cli.run(
        cli.parser().parse_args(
            [
                "account",
                "onboard",
                "--name",
                "prod",
                "--provider",
                "aws",
                "--account-id",
                ACCOUNT_ID,
                "--credential-id",
                CONNECTION_ID,
                "--yes",
            ]
        ),
        api,
    )

    assert result["status"] == "ok"
    assert api.sent[2][1:] == (
        cli.API_BASE + "/accounts",
        {
            "name": "prod",
            "provider": "aws",
            "account_id": ACCOUNT_ID,
            "adp_credential_id": CONNECTION_ID,
        },
    )


def test_dry_run_does_not_resolve_or_write_the_connection() -> None:
    api = RecordingApi()
    result = _register(api, "--dry-run")

    assert result["detail"]["dry_run"] is True
    assert api.sent == []


def test_a_pasted_secret_arn_is_refused_without_being_sent() -> None:
    api = RecordingApi()
    with pytest.raises(cli.CliError) as raised:
        cli.run(
            cli.parser().parse_args(
                [
                    "aws-onboard",
                    "register",
                    "--account-id",
                    ACCOUNT_ID,
                    "--credential-id",
                    "arn:aws:secretsmanager:us-east-1:1:secret:x",
                    "--yes",
                ]
            ),
            api,
        )

    assert raised.value.code == "usage_error"
    assert api.sent == []


def test_account_and_connection_inputs_are_required() -> None:
    for argv in (
        ["aws-onboard", "register", "--account-id", ACCOUNT_ID],
        ["account", "onboard", "--name", "prod", "--provider", "aws", "--account-id", ACCOUNT_ID],
    ):
        with pytest.raises(cli.CliError) as raised:
            cli.parser().parse_args(argv)
        assert raised.value.code == "usage_error"


def test_malformed_success_is_reported_as_uncertain() -> None:
    api = RecordingApi({"id": ACCOUNT_RECORD, "account_id": ACCOUNT_ID, "adp_credential_ids": []})
    with pytest.raises(cli.CliError) as raised:
        _register(api)

    assert raised.value.code == "malformed_response"
    assert "may have succeeded" in str(raised.value)
    assert len(api.sent) == 3


def test_old_server_is_actionable_and_receives_no_mutation() -> None:
    class OldServer(RecordingApi):
        def request(self, method, path, body=None, **kwargs):
            self.sent.append((method, path, body))
            if (method, path) == ("GET", cli.ACCOUNT_ADAPTER_SUPPORT):
                return {"version": 1, "features": []}
            pytest.fail("old server received an account mutation")

    api = OldServer()
    with pytest.raises(cli.CliError) as raised:
        _register(api)

    assert raised.value.code == "account_adapter_unavailable"
    assert raised.value.exit_code == 4
    assert api.sent == [("GET", cli.ACCOUNT_ADAPTER_SUPPORT, None)]


def test_no_iam_or_secret_material_is_created_by_this_cli() -> None:
    source = executable_source()
    for forbidden in ("boto3", "create_role", "put_role_policy", "create_secret", "ExternalId"):
        assert forbidden not in source
    for flag in ("--role-arn", "--external-id", "--secret-arn"):
        assert flag not in source


def test_registration_paths_exist_without_retired_aws_routes() -> None:
    allowlist = {tuple(entry) for entry in json.loads((REPO / "modules/gateway/src/domain_proxy/superplane_routes.json").read_text())}
    assert {("POST", "/accounts"), ("GET", "/accounts"), ("DELETE", "/accounts/{account_id}")} <= allowlist
    source = executable_source()
    assert "/aws/onboarding-plan" not in source
    assert "/aws/accounts" not in source


def test_account_output_never_prints_server_trust_material() -> None:
    api = RecordingApi(
        {
            "id": ACCOUNT_RECORD,
            "account_id": ACCOUNT_ID,
            "adp_credential_ids": [CONNECTION_ID],
            "role_arn": "arn:aws:iam::123456789012:role/private",
            "external_id": "private-external-id",
        }
    )
    registered = _register(api)
    assert "role_arn" not in registered["detail"]["account"]
    assert "external_id" not in registered["detail"]["account"]

    class ListApi(RecordingApi):
        def request(self, method, path, body=None, **kwargs):
            if (method, path) == ("GET", cli.API_BASE + "/accounts"):
                return {"accounts": [self.response], "total": 1}
            return super().request(method, path, body, **kwargs)

    listed = cli.run(cli.parser().parse_args(["account", "list"]), ListApi(api.response))
    assert "role_arn" not in listed["detail"]["accounts"][0]
    assert "external_id" not in listed["detail"]["accounts"][0]
