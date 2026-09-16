"""Exercise the CLI workflow with isolated ADP/AWS boundaries, never live credentials."""

import base64
import importlib.util
import io
import json
import stat
import subprocess
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "cli/adp-bedrock.py"
spec = importlib.util.spec_from_file_location("adp_bedrock_cli", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

ACCOUNT = "123456789012"
EXTERNAL = "fixture-external-id"
ORG = {"id": "org-sophos", "name": "SOPHOS-IT"}
TEAM = {"id": "team-engineering", "name": "Engineering"}
USER = {"id": "user-canonical", "email": "developer@example.test", "github_username": "developer"}


class FakeApi:
    base = "https://gateway.example.test/api"

    def __init__(self):
        self.calls = []
        self.rows = []
        self.mappings = []
        self.verified = True
        self.after_verify = None
        self.bad_package = False
        self.fail_after_register = False

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.startswith("/admin/organizations/org-sophos/teams?"):
            return {"items": [TEAM], "total": 1, "has_more": False}
        if path.startswith("/admin/organizations?"):
            return {"items": [ORG], "total": 1, "has_more": False}
        if path.startswith("/admin/users?"):
            return {"items": [USER], "total": 1, "has_more": False}
        if path == "/me/bedrock-routing/selection":
            return {"effective": {"rung": "team", "account_id": ACCOUNT, "destination_id": "destination-fixture", "source": "platform_admin"}}
        if path == cli.ROUTING + "/effective/" + USER["id"]:
            return {"rung": "user", "account_id": ACCOUNT, "destination_id": "destination-fixture", "source": "platform_admin"}
        if path == cli.ROUTING + "/destinations":
            if method == "GET":
                return self.rows
            destination = {
                "id": "destination-fixture",
                "account_id": body["account_id"],
                "owner_org_id": body["link_to_org_id"],
                "label": body["label"],
                "region": body["region"],
                "usable_for_routing": False,
                "connection_id": None,
            }
            self.rows.append(destination)
            if self.fail_after_register:
                self.fail_after_register = False
                raise cli.CliError("Connection interrupted after registration")
            return {"destination": destination, "launch_url": "must-never-open-a-browser"}
        if path.endswith("/setup"):
            destination = self.rows[0]
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as bundle:
                bundle.writestr("template.yaml", "Resources: {}")
                bundle.writestr(
                    "parameters.json",
                    json.dumps(
                        [
                            {"ParameterKey": "Nickname", "ParameterValue": destination["label"]},
                            {"ParameterKey": "ExternalId", "ParameterValue": EXTERNAL},
                        ]
                    ),
                )
                bundle.writestr("README.md", "Create this role in the expected AWS account")
                if self.bad_package:
                    bundle.writestr("../escape.txt", "must not be extracted")
            return {
                "account_id": ACCOUNT,
                "region": destination["region"],
                "role_arn": f"arn:aws:iam::{ACCOUNT}:role/ADP-Agent-{destination['label']}",
                "download_base64": base64.b64encode(buffer.getvalue()).decode(),
            }
        if path.endswith("/verify"):
            if self.after_verify:
                self.after_verify()
            self.rows[0]["usable_for_routing"] = self.verified
            return {"verified": self.verified, "reason": None if self.verified else "role_missing_bedrock_permission", "destination": self.rows[0]}
        if path == cli.ROUTING + "/mappings":
            return self.mappings
        if method == "PUT" and path.startswith(cli.ROUTING + "/mappings/"):
            return {"scope": path.rsplit("/", 1)[1], **body}
        raise AssertionError((method, path))


class FakeAws(cli.Aws):
    def __init__(self):
        super().__init__("fixture-profile", "us-east-1")
        self.account = ACCOUNT
        self.calls = []
        self.exists = False
        self.status = "CREATE_COMPLETE"
        self.name = None
        self.parameter_file = None
        self.fail_wait = False

    def call(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("sts", "get-caller-identity"):
            return {"Account": self.account}
        if args[:2] == ("cloudformation", "describe-stacks"):
            if not self.exists:
                return None
            name = args[args.index("--stack-name") + 1]
            return {
                "Stacks": [{"StackStatus": self.status, "Outputs": [{"OutputKey": "RoleArn", "OutputValue": f"arn:aws:iam::{ACCOUNT}:role/{name}"}]}]
            }
        if args[:2] == ("cloudformation", "create-stack"):
            self.exists = True
            self.parameter_file = Path(args[args.index("--parameters") + 1].removeprefix("file://"))
            assert stat.S_IMODE(self.parameter_file.stat().st_mode) == 0o600
            assert stat.S_IMODE(self.parameter_file.parent.stat().st_mode) == 0o700
            assert EXTERNAL in self.parameter_file.read_text()
            assert EXTERNAL not in " ".join(args)
            assert args[-2:] == ("--capabilities", "CAPABILITY_NAMED_IAM")
            return {}
        if args[:2] == ("cloudformation", "wait"):
            if self.fail_wait:
                raise cli.CliError("Stack waiter interrupted")
            self.status = "CREATE_COMPLETE"
            return {}
        raise AssertionError(args)


@pytest.fixture
def environment(monkeypatch):
    api, aws = FakeApi(), FakeAws()
    monkeypatch.setattr(cli, "Aws", lambda *args: aws)
    return api, aws


def arguments(*extra):
    return cli.parser().parse_args(["connect", "--account", ACCOUNT, "--org", "SOPHOS-IT", "--yes", *extra])


def mutations(api):
    return [call for call in api.calls if call[0] != "GET"]


@pytest.mark.parametrize(
    "extra, expected",
    [
        ([], "org:org-sophos"),
        (["--team", "Engineering"], "team:org-sophos:team-engineering"),
        (["--user", "developer@example.test"], "user:user-canonical"),
    ],
)
def test_one_command_provisions_verifies_and_assigns_the_selected_scope(environment, extra, expected, capsys):
    api, aws = environment
    result = cli.run(arguments(*extra), api)
    assert result["mapping"] == {"scope": expected, "destination_id": "destination-fixture"}
    assert result["verified"] and result["assigned"]
    assert len(api.rows) == 1
    assert result["role_name"].startswith("ADP-Agent-bedrock-sophos-it-")
    assert len(result["role_name"]) <= 64
    assert [call[1] for call in mutations(api)] == [
        cli.ROUTING + "/destinations",
        cli.ROUTING + "/destinations/destination-fixture/verify",
        cli.ROUTING + "/mappings/" + expected,
    ]
    assert aws.calls[0] == ("sts", "get-caller-identity")
    assert not aws.parameter_file.exists(), "temporary CloudFormation secrets must be cleaned"
    cli.display(result, False)
    output = capsys.readouterr()
    assert "Connected AWS account" in output.out
    assert EXTERNAL not in output.out + output.err


def test_account_mismatch_refuses_before_any_registration_or_provisioning(environment):
    api, aws = environment
    aws.account = "999999999999"
    with pytest.raises(cli.CliError, match="expected 123456789012"):
        cli.run(arguments(), api)
    assert not mutations(api)
    assert all(call[0] == "sts" for call in aws.calls)


def test_profile_is_checked_again_after_confirmation(environment, monkeypatch):
    api, aws = environment

    def confirm(*args):
        aws.account = "999999999999"

    monkeypatch.setattr(cli, "confirm", confirm)
    with pytest.raises(cli.CliError, match="expected 123456789012"):
        cli.run(arguments(), api)
    assert not aws.exists
    assert all(call[0] == "sts" for call in aws.calls)


def test_wrong_cloudformation_output_never_reaches_assignment(environment, monkeypatch):
    api, aws = environment
    original = aws.call

    def call(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[:2] == ("cloudformation", "describe-stacks") and result:
            result["Stacks"][0]["Outputs"][0]["OutputValue"] = "arn:aws:iam::999999999999:role/other"
        return result

    monkeypatch.setattr(aws, "call", call)
    with pytest.raises(cli.CliError, match="expected role ARN"):
        cli.run(arguments(), api)
    assert not any(call[0] == "PUT" for call in api.calls)


def test_dry_run_performs_only_reads(environment):
    api, aws = environment
    result = cli.run(arguments("--team", "Engineering", "--dry-run"), api)
    assert result["dry_run"] and result["scope"]["scope_id_team"] == TEAM["id"]
    assert not mutations(api)
    assert all(call[0] == "sts" for call in aws.calls)


def test_no_implicit_approval_in_scripts(environment):
    api, aws = environment
    args = arguments()
    args.yes = False
    with pytest.raises(cli.CliError, match="--yes"):
        cli.run(args, api)
    assert not mutations(api)


def test_download_and_resume_need_no_aws_credentials_and_preserve_team(environment, tmp_path, monkeypatch, capsys):
    api, aws = environment
    monkeypatch.setattr(cli, "Aws", lambda *args: pytest.fail("AWS credentials must not be required for a handoff"))
    directory = tmp_path / "handoff"
    downloaded = cli.run(arguments("--team", "Engineering", "--download", str(directory)), api)
    assert not downloaded["assigned"]
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert set(path.name for path in directory.iterdir()) == cli.PACKAGE_FILES | {"destination.json"}
    for path in directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert EXTERNAL not in (directory / "destination.json").read_text()
    assert len(mutations(api)) == 1
    resumed = cli.run(cli.parser().parse_args(["connect", "--resume", str(directory), "--yes"]), api)
    assert resumed["mapping"]["scope"] == "team:org-sophos:team-engineering"
    assert len(api.rows) == 1
    assert not aws.calls
    assert EXTERNAL not in capsys.readouterr().err


@pytest.mark.parametrize("change", ["gateway", "scope", "account"])
def test_resume_refuses_changed_gateway_account_or_incomplete_scope(environment, tmp_path, change):
    api, _ = environment
    directory = tmp_path / "handoff"
    cli.run(arguments("--team", "Engineering", "--download", str(directory)), api)
    path = directory / "destination.json"
    data = json.loads(path.read_text())
    if change == "gateway":
        data["gateway_url"] = "https://another.example.test/api"
    elif change == "scope":
        data["scope"].pop("scope_id_team")
    else:
        data["account_id"] = "999999999999"
    path.write_text(json.dumps(data))
    api.calls.clear()
    with pytest.raises(cli.CliError):
        cli.run(cli.parser().parse_args(["connect", "--resume", str(directory), "--yes"]), api)
    assert not mutations(api)


def test_failed_verification_does_not_assign(environment):
    api, aws = environment
    api.verified = False
    with pytest.raises(cli.CliError, match="role_missing_bedrock_permission"):
        cli.run(arguments(), api)
    assert aws.exists
    assert not any(call[0] == "PUT" for call in api.calls)


def test_rerun_after_interrupted_registration_reuses_destination(environment):
    api, aws = environment
    api.fail_after_register = True
    with pytest.raises(cli.CliError, match="interrupted"):
        cli.run(arguments(), api)
    result = cli.run(arguments(), api)
    assert result["assigned"]
    assert len(api.rows) == 1
    assert sum(call[:2] == ("cloudformation", "create-stack") for call in aws.calls) == 1


def test_rerun_after_interrupted_wait_reuses_existing_stack(environment):
    api, aws = environment
    aws.fail_wait = True
    with pytest.raises(cli.CliError, match="waiter"):
        cli.run(arguments(), api)
    assert not aws.parameter_file.exists()
    aws.fail_wait = False
    assert cli.run(arguments(), api)["assigned"]
    assert len(api.rows) == 1
    assert sum(call[:2] == ("cloudformation", "create-stack") for call in aws.calls) == 1


def test_existing_rollback_stack_is_never_updated_or_replaced(environment):
    api, aws = environment
    aws.exists, aws.status = True, "ROLLBACK_COMPLETE"
    with pytest.raises(cli.CliError, match="ROLLBACK_COMPLETE"):
        cli.run(arguments(), api)
    assert not any(call[:2] in {("cloudformation", "create-stack"), ("cloudformation", "update-stack")} for call in aws.calls)
    assert not any(call[0] == "PUT" for call in api.calls)


def test_a_concurrent_rule_change_is_not_silently_overwritten(environment):
    api, _ = environment
    api.after_verify = lambda: api.mappings.append({"scope_type": "org", "scope_id_org": ORG["id"], "destination_id": "someone-elses-rule"})
    with pytest.raises(cli.CliError, match="changed during setup"):
        cli.run(arguments(), api)
    assert not any(call[0] == "PUT" for call in api.calls)


def test_invalid_package_is_never_extracted_or_provisioned(environment, tmp_path):
    api, aws = environment
    api.bad_package = True
    with pytest.raises(cli.CliError, match="package is invalid"):
        cli.run(arguments(), api)
    assert not aws.exists
    assert not any(call[0] == "PUT" for call in api.calls)


def test_shared_download_directory_refused_before_registration(environment, tmp_path):
    api, _ = environment
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    with pytest.raises(cli.CliError, match="private output directory"):
        cli.run(arguments("--download", str(directory)), api)
    assert not mutations(api)


def test_scope_lookup_is_paginated_and_ambiguous_names_are_refused():
    class Paged:
        def request(self, method, path):
            return {"items": [ORG] if "page=1&" in path else [{"id": "second", "name": "SOPHOS-IT"}], "has_more": "page=1&" in path, "total": 2}

    rows = cli.pages(Paged(), "/admin/organizations")
    assert len(rows) == 2
    assert cli.resolve(rows, "second", ["name"], "Organization")["id"] == "second"
    with pytest.raises(cli.CliError, match="ambiguous"):
        cli.resolve(rows, "SOPHOS-IT", ["name"], "Organization")


def test_aws_failure_redacts_parameter_values(monkeypatch):
    captured = []

    def run(command, **kwargs):
        captured.append(command)
        return subprocess.CompletedProcess(command, 1, "", f"An error occurred (AccessDenied): secret value {EXTERNAL}")

    monkeypatch.setattr(cli.subprocess, "run", run)
    aws = cli.Aws("sophos", "us-east-1")
    with pytest.raises(cli.CliError, match="AccessDenied") as error:
        aws.call("cloudformation", "create-stack", "--parameters", "file:///private/parameters.json")
    assert EXTERNAL not in str(error.value)
    assert "sophos" in captured[0]


def test_installed_cli_exposes_one_connect_action_without_authentication(run_adp):
    result = run_adp(["bedrock", "connect", "--help"])
    assert result.returncode == 0, result.stderr
    assert "--download" in result.stdout and "--resume" in result.stdout
    assert "--scope" not in result.stdout


@pytest.mark.parametrize("status", [200, 302, 403])
def test_api_reuses_auth_helper_and_never_forwards_tokens_on_redirect(monkeypatch, tmp_path, status, capsys):
    fixture_token = "fixture-access-token"
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler protocol
            received.append((self.path, self.headers.get("Authorization")))
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/must-not-receive-token")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "detail": {"message": fixture_token}}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / ".bedrock-gateway"
    config.mkdir()
    (config / "config.json").write_text(json.dumps({"gateway_url": f"http://127.0.0.1:{server.server_port}/api"}))
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    calls = []

    def token(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, fixture_token, "")

    monkeypatch.setattr(cli.subprocess, "run", token)
    try:
        api = cli.Api()
        if status == 200:
            assert api.request("GET", "/test")["ok"]
        else:
            with pytest.raises(cli.CliError) as error:
                api.request("GET", "/test")
            assert fixture_token not in str(error.value)
            assert "administrator" in str(error.value) if status == 403 else "redirected" in str(error.value)
        assert received == [("/api/test", "Bearer " + fixture_token)]
        assert calls == [["bash", str(SCRIPT.with_name("bg-cognito-auth.sh")), "token"]]
        assert fixture_token not in str(calls)
        output = capsys.readouterr()
        assert fixture_token not in output.out + output.err
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_authentication_refusal_precedes_any_aws_call(environment, monkeypatch):
    api, aws = environment

    def forbidden(*args):
        raise cli.CliError("This operation requires an ADP platform administrator.")

    monkeypatch.setattr(api, "request", forbidden)
    with pytest.raises(cli.CliError, match="platform administrator"):
        cli.run(arguments(), api)
    assert not aws.calls


def test_existing_destination_is_verified_and_assigned_without_aws(environment, monkeypatch):
    api, _ = environment
    api.rows = [
        {
            "id": "existing",
            "label": "connection_existing",
            "account_id": ACCOUNT,
            "owner_org_id": ORG["id"],
            "region": "us-east-1",
            "connection_id": "connection",
            "usable_for_routing": False,
        }
    ]
    monkeypatch.setattr(cli, "Aws", lambda *args: pytest.fail("Reusing a destination must not invoke AWS CLI"))
    result = cli.run(cli.parser().parse_args(["connect", "--destination", "existing", "--org", ORG["id"], "--yes"]), api)
    assert result["assigned"]
    assert result["mapping"]["destination_id"] == "existing"
    assert [item[1] for item in mutations(api)] == [cli.ROUTING + "/destinations/existing/verify", cli.ROUTING + "/mappings/org:" + ORG["id"]]


def test_reused_destination_account_mismatch_is_read_only(environment):
    api, _ = environment
    cli.run(arguments(), api)
    api.calls.clear()
    with pytest.raises(cli.CliError, match="different AWS account"):
        cli.run(
            cli.parser().parse_args(["connect", "--destination", api.rows[0]["id"], "--account", "999999999999", "--org", ORG["id"], "--yes"]), api
        )
    assert not mutations(api)


@pytest.mark.parametrize("options,rung", [([], "team"), (["--user", "developer@example.test"], "user")])
def test_status_uses_canonical_effective_route_without_aws(environment, options, rung):
    api, aws = environment
    result = cli.run(cli.parser().parse_args(["status", *options]), api)
    assert result["effective"]["rung"] == rung
    assert result["verification"] == "configured_route_only"
    assert not mutations(api) and not aws.calls


def test_verify_does_not_assign_or_provision(environment):
    api, aws = environment
    cli.run(arguments(), api)
    api.calls.clear()
    aws.calls.clear()
    result = cli.run(cli.parser().parse_args(["verify", api.rows[0]["id"]]), api)
    assert result["verified"] and not result["assigned"]
    assert len(mutations(api)) == 1 and not aws.calls


def test_json_handoff_is_pending_and_excludes_setup_secrets(environment, tmp_path, capsys):
    api, _ = environment
    result = cli.run(arguments("--download", str(tmp_path / "private")), api)
    cli.display(result, True)
    output = capsys.readouterr()
    envelope = json.loads(output.out)
    assert envelope["status"] == "pending"
    assert envelope["command"] == "admin bedrock connect"
    assert "--resume" in envelope["next_action"]
    assert EXTERNAL not in output.out


def test_setup_provider_marks_existing_org_rule_configured_without_claiming_inference(environment):
    api, aws = environment
    result = cli.run(arguments(), api)
    api.mappings = [{"scope_type": "org", "scope_id_org": ORG["id"], "destination_id": result["destination_id"]}]
    api.calls.clear()
    aws.calls.clear()
    outcome = cli.status({"api": api, "org": ORG["id"]})
    assert outcome["status"] == "configured"
    assert outcome["detail"]["verification"] == "stored_destination_verification"
    assert not mutations(api) and not aws.calls


def test_setup_provider_noninteractive_missing_rule_stays_pending(environment):
    api, aws = environment
    outcome = cli.configure({"api": api, "org": ORG["id"], "interactive": False})
    assert outcome["status"] == "pending"
    assert not mutations(api) and not aws.calls


def test_admin_command_group_help_is_available_without_login(run_adp):
    result = run_adp(["admin", "bedrock", "connect", "--help"])
    assert result.returncode == 0
    assert "--destination" in result.stdout and "--download" in result.stdout
    result = run_adp(["admin", "--help"])
    assert "bedrock" in result.stdout
