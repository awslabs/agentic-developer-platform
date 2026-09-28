"""Exercise `adp aws` against isolated ADP/AWS boundaries, never live credentials.

The properties worth testing here are the ones a wrong implementation gets wrong
silently: a role created in the wrong account, a second connection for one
account after an interrupted run, an ExternalId that reaches a process argument,
a disconnect that reads as "your AWS role is gone", and a connection reported
usable on a stored verdict rather than a call.
"""

import base64
import importlib.util
import io
import json
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "cli/adp-aws.py"
spec = importlib.util.spec_from_file_location("adp_aws_cli", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

#: The genuine local-AWS wrapper, captured before any fixture substitutes it.
RealAws = cli.Aws

ACCOUNT = "123456789012"
OTHER_ACCOUNT = "999999999999"
EXTERNAL = "fixture-external-id"
USER_TAG = "db-id-fixture-user"
NAME = "personal-" + ACCOUNT
EXISTING_ARN = f"arn:aws:iam::{ACCOUNT}:role/PreExisting"


class FakeApi:
    """The gateway's AWS-connect surface, as the CLI sees it.

    Deliberately models the *records*, not just the responses: connections
    accumulate in ``self.rows`` so a test can assert that a retry produced one
    connection rather than two.
    """

    base = "https://gateway.example.test/api"

    def __init__(self):
        self.calls = []
        self.rows = []
        self.counter = 0
        self.verified = True
        self.reason = "The IAM role has not been created yet."
        self.bad_package = False
        self.fail_after_connect = False
        self.import_reuses = False

    def _row(self, label, account_id, status="pending", source=None):
        self.counter += 1
        scopes = {"account_id": account_id, "status": status}
        if source:
            scopes["source"] = source
        row = {
            "id": f"credential-{self.counter}",
            "service": "aws",
            "credential_type": "aws_role",
            "label": label,
            "scope": "user",
            "scopes": scopes,
        }
        self.rows.append(row)
        return row

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/auth/credentials?scope=user":
            # The real endpoint returns every service; the CLI must filter.
            return [*self.rows, {"id": "other", "service": "github", "credential_type": "oauth_token", "label": "gh", "scopes": {}}]
        if path == cli.CONNECT + "/connect":
            row = self._row(body["nickname"], body["account_id"])
            if self.fail_after_connect:
                self.fail_after_connect = False
                raise cli.CliError("Connection interrupted after the connection was saved")
            return {"credential_id": row["id"], "launch_url": "must-never-open-a-browser"}
        if path == cli.CONNECT + "/import":
            if self.import_reuses:
                return {"credential_id": self.rows[0]["id"], "account_id": body["account_id"], "role_arn": body["role_arn"], "reused": True}
            row = self._row(body["nickname"], body["account_id"], source="imported_role")
            row["scopes"]["role_arn"] = body["role_arn"]
            return {"credential_id": row["id"], "account_id": body["account_id"], "role_arn": body["role_arn"], "reused": False}
        if path == cli.CONNECT + "/verify":
            row = next(row for row in self.rows if row["id"] == body["credential_id"])
            assert body["fresh"] is True, "adp aws verify must not accept a replayed verdict"
            if not self.verified:
                return {"status": "failed", "reason": self.reason}
            row["scopes"]["status"] = "verified"
            return {"status": "verified", "routing_capable": False, "routing_reason": "role_pinned_to_single_user"}
        if path.endswith("/setup"):
            row = next(row for row in self.rows if path == f"{cli.CONNECT}/{row['id']}/setup")
            return self._setup(row)
        if method == "DELETE" and path.startswith("/auth/credentials/"):
            target = path.rsplit("/", 1)[1]
            self.rows = [row for row in self.rows if row["id"] != target]
            return {}
        raise AssertionError((method, path))

    def _setup(self, row):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("template.yaml", "Resources: {}")
            bundle.writestr(
                "parameters.json",
                json.dumps(
                    [
                        {"ParameterKey": "Nickname", "ParameterValue": row["label"]},
                        {"ParameterKey": "ExternalId", "ParameterValue": EXTERNAL},
                        {"ParameterKey": "UserSessionTag", "ParameterValue": USER_TAG},
                    ]
                ),
            )
            bundle.writestr("README.md", f"Apply this in AWS account {ACCOUNT}")
            if self.bad_package:
                bundle.writestr("../escape.txt", "must not be extracted")
        return {
            "credential_id": row["id"],
            "account_id": row["scopes"]["account_id"],
            "role_arn": f"arn:aws:iam::{row['scopes']['account_id']}:role/ADP-Agent-{row['label']}",
            "region": "us-east-1",
            "status": row["scopes"]["status"],
            "launch_url": "https://console.aws.amazon.com/must-never-open",
            "download_filename": f"adp-aws-{row['scopes']['account_id']}.zip",
            "download_base64": base64.b64encode(buffer.getvalue()).decode(),
        }


class FakeAws(cli.Aws):
    def __init__(self):
        super().__init__("fixture-profile", "us-east-1")
        self.account = ACCOUNT
        self.calls = []
        self.exists = False
        self.status = "CREATE_COMPLETE"
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
            assert EXTERNAL not in " ".join(args), "the ExternalId must travel in a file, never in argv"
            assert args[-2:] == ("--capabilities", "CAPABILITY_NAMED_IAM")
            return {}
        if args[:2] == ("cloudformation", "wait"):
            if self.fail_wait:
                raise cli.CliError("Stack waiter interrupted")
            self.status = "CREATE_COMPLETE"
            return {}
        raise AssertionError(args)


@pytest.fixture
def environment(monkeypatch, tmp_path):
    """A CLI with a fake gateway, a fake local AWS CLI and a private HOME.

    HOME is redirected because the helper writes handoff progress to
    ``~/.adp/state``; a test that leaked would rewrite the developer's own.
    """
    api, aws = FakeApi(), FakeAws()
    monkeypatch.setattr(cli, "Aws", lambda *args: aws)
    monkeypatch.setattr(cli.common.Path, "home", lambda: tmp_path)
    return api, aws


def arguments(*extra, command="connect"):
    return cli.parser().parse_args([command, "--account", ACCOUNT, "--yes", *extra])


def mutations(api):
    return [call for call in api.calls if call[0] != "GET"]


# ---------------------------------------------------------------------------
# Provisioning a new role
# ---------------------------------------------------------------------------


def test_one_command_creates_the_role_and_verifies_the_connection(environment, capsys):
    api, aws = environment
    result = cli.run(arguments(), api)

    assert result["verified"] is True
    assert result["account_id"] == ACCOUNT
    assert result["role_arn"] == f"arn:aws:iam::{ACCOUNT}:role/ADP-Agent-{NAME}"
    assert len(api.rows) == 1
    assert [call[1] for call in mutations(api)] == [cli.CONNECT + "/connect", cli.CONNECT + "/verify"]
    # The account is checked before anything is saved in ADP.
    assert aws.calls[0] == ("sts", "get-caller-identity")
    assert not aws.parameter_file.exists(), "temporary CloudFormation parameters must be cleaned up"

    cli.display(result, False, "connect")
    output = capsys.readouterr()
    assert "Connected AWS account" in output.out
    assert "Shared Bedrock routing is unchanged" in output.out
    assert EXTERNAL not in output.out + output.err


def test_connecting_never_assigns_shared_bedrock_routing(environment):
    """Connecting a personal account is one capability; routing shared inference
    to it is another. This helper must not touch the routing surface at all."""
    api, _ = environment
    cli.run(arguments(), api)
    assert not any("routing" in call[1] or "/mappings" in call[1] for call in api.calls)


def test_account_mismatch_refuses_before_anything_is_saved(environment):
    """--profile only picks local credentials. If they belong to another account,
    creating the role there would plant this user's trust policy in an account
    nobody meant to touch."""
    api, aws = environment
    aws.account = OTHER_ACCOUNT
    with pytest.raises(cli.CliError, match=f"not {ACCOUNT}"):
        cli.run(arguments("--profile", "someone-else"), api)
    assert not mutations(api)
    assert not api.rows
    assert all(call[0] == "sts" for call in aws.calls)


def test_profile_is_checked_again_after_confirmation(environment, monkeypatch):
    api, aws = environment

    def confirm(*args, **kwargs):
        aws.account = OTHER_ACCOUNT

    monkeypatch.setattr(cli, "confirm", confirm)
    with pytest.raises(cli.CliError, match=f"not {ACCOUNT}"):
        cli.run(arguments(), api)
    assert not aws.exists


def test_local_aws_credentials_are_never_sent_to_adp(environment):
    api, _ = environment
    cli.run(arguments(), api)
    sent = json.dumps([call[2] for call in api.calls])
    for secret in ("AccessKeyId", "SecretAccessKey", "SessionToken", "aws_access_key_id", "fixture-profile"):
        assert secret not in sent


def test_wrong_stack_output_leaves_the_connection_unverified(environment, monkeypatch):
    api, aws = environment
    original = aws.call

    def call(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[:2] == ("cloudformation", "describe-stacks") and result:
            result["Stacks"][0]["Outputs"][0]["OutputValue"] = f"arn:aws:iam::{OTHER_ACCOUNT}:role/other"
        return result

    monkeypatch.setattr(aws, "call", call)
    with pytest.raises(cli.CliError, match="expected role ARN"):
        cli.run(arguments(), api)
    assert not any(call[1] == cli.CONNECT + "/verify" for call in api.calls)


def test_existing_rollback_stack_is_never_replaced(environment):
    api, aws = environment
    aws.exists, aws.status = True, "ROLLBACK_COMPLETE"
    with pytest.raises(cli.CliError, match="ROLLBACK_COMPLETE"):
        cli.run(arguments(), api)
    assert not any(call[:2] == ("cloudformation", "create-stack") for call in aws.calls)


def test_invalid_package_is_never_applied(environment):
    """A package holding anything but the three known files is not one we know how
    to apply — and a traversal entry must never be written to disk."""
    api, aws = environment
    api.bad_package = True
    with pytest.raises(cli.CliError, match="package is invalid"):
        cli.run(arguments(), api)
    assert not aws.exists


def test_missing_aws_cli_points_at_the_handoff(environment, monkeypatch):
    api, _ = environment
    monkeypatch.setattr(cli, "Aws", lambda *args: RealAws("fixture-profile", "us-east-1"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(cli.CliError, match="--download") as error:
        cli.run(arguments(), api)
    assert error.value.code == "aws_cli_missing"
    assert not mutations(api)


# ---------------------------------------------------------------------------
# Registering a role that already exists
# ---------------------------------------------------------------------------


def existing_role_arguments(*extra):
    return cli.parser().parse_args(["connect", "--account", ACCOUNT, "--role-arn", EXISTING_ARN, "--yes", *extra])


def test_existing_role_is_registered_and_then_proved(environment, tmp_path, monkeypatch):
    api, _ = environment
    monkeypatch.setattr(cli, "Aws", lambda *args: pytest.fail("Registering an existing role must not invoke the AWS CLI"))
    secret_file = tmp_path / "private" / "external.json"
    cli.common.write_json(secret_file, {"external_id": EXTERNAL})

    result = cli.run(existing_role_arguments("--external-id-file", str(secret_file)), api)

    assert result["verified"] is True
    assert result["role_arn"] == EXISTING_ARN
    imported = next(call for call in api.calls if call[1] == cli.CONNECT + "/import")
    assert imported[2]["external_id"] == EXTERNAL
    # Registering is not verifying: the import call is followed by a real check.
    assert [call[1] for call in mutations(api)] == [cli.CONNECT + "/import", cli.CONNECT + "/verify"]


def test_existing_role_external_id_never_reaches_process_arguments(environment, tmp_path):
    api, _ = environment
    secret_file = tmp_path / "private" / "external.json"
    cli.common.write_json(secret_file, {"external_id": EXTERNAL})
    argv = ["connect", "--account", ACCOUNT, "--role-arn", EXISTING_ARN, "--external-id-file", str(secret_file), "--yes"]
    assert EXTERNAL not in " ".join(argv)
    cli.run(cli.parser().parse_args(argv), api)
    # And no flag exists that would accept it directly.
    assert "--external-id " not in cli.parser().format_help()


def test_external_id_may_be_supplied_on_stdin(environment, monkeypatch):
    api, _ = environment
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({"external_id": EXTERNAL})))
    result = cli.run(existing_role_arguments("--external-id-stdin"), api)
    assert result["verified"]


def test_world_readable_external_id_file_is_refused(environment, tmp_path):
    api, _ = environment
    directory = cli.common.private_directory(tmp_path / "private")
    secret_file = directory / "external.json"
    secret_file.write_text(json.dumps({"external_id": EXTERNAL}))
    secret_file.chmod(0o644)
    with pytest.raises(cli.CliError, match="0600"):
        cli.run(existing_role_arguments("--external-id-file", str(secret_file)), api)
    assert not mutations(api)


def test_registering_without_an_external_id_must_be_explicit(environment, monkeypatch):
    """A role that trusts ADP with no confused-deputy guard is legitimate, but
    silently registering one would weaken the connection unnoticed."""
    api, _ = environment
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(cli.CliError, match="--no-external-id"):
        cli.run(existing_role_arguments(), api)
    assert not mutations(api)

    result = cli.run(existing_role_arguments("--no-external-id"), api)
    assert "external_id" not in next(call for call in api.calls if call[1] == cli.CONNECT + "/import")[2]
    assert result["verified"]


def test_role_arn_from_another_account_is_refused_locally(environment):
    api, _ = environment
    args = cli.parser().parse_args(["connect", "--account", ACCOUNT, "--role-arn", f"arn:aws:iam::{OTHER_ACCOUNT}:role/Other", "--yes"])
    with pytest.raises(cli.CliError, match="different AWS account"):
        cli.run(args, api)
    assert not mutations(api)


@pytest.mark.parametrize("value", [f"arn:aws:iam::{ACCOUNT}:user/NotARole", f"arn:aws:iam::{ACCOUNT}:role/*", "PreExisting"])
def test_non_role_arns_are_refused(environment, value):
    api, _ = environment
    args = cli.parser().parse_args(["connect", "--account", ACCOUNT, "--role-arn", value, "--yes", "--no-external-id"])
    with pytest.raises(cli.CliError):
        cli.run(args, api)
    assert not mutations(api)


def test_repeat_registration_reuses_the_same_connection(environment, monkeypatch):
    api, _ = environment
    first = cli.run(existing_role_arguments("--no-external-id"), api)
    api.import_reuses = True
    second = cli.run(existing_role_arguments("--no-external-id"), api)
    assert second["connection_id"] == first["connection_id"]
    assert second["reusing"] is True
    assert len(api.rows) == 1


def test_download_and_profile_are_meaningless_for_an_existing_role(environment, tmp_path):
    api, _ = environment
    with pytest.raises(cli.CliError, match="nothing to download or create"):
        cli.run(existing_role_arguments("--download", str(tmp_path / "out")), api)
    assert not mutations(api)


# ---------------------------------------------------------------------------
# Administrator handoff: download, apply elsewhere, resume
# ---------------------------------------------------------------------------


def test_download_then_resume_needs_no_local_aws_credentials(environment, tmp_path, monkeypatch, capsys):
    """The whole point of the handoff: the ADP user cannot create IAM resources,
    so neither step may require AWS credentials on their machine."""
    api, _ = environment
    monkeypatch.setattr(cli, "Aws", lambda *args: pytest.fail("A handoff must not require local AWS credentials"))
    directory = tmp_path / "handoff"

    downloaded = cli.run(arguments("--download", str(directory)), api)
    assert downloaded["verified"] is False
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert {path.name for path in directory.iterdir()} == cli.PACKAGE_FILES | {"connection.json"}
    for path in directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # The ExternalId belongs in the administrator's parameters, not in metadata.
    assert EXTERNAL not in (directory / "connection.json").read_text()
    assert EXTERNAL in (directory / "parameters.json").read_text()
    assert USER_TAG in (directory / "parameters.json").read_text()

    resumed = cli.run(cli.parser().parse_args(["connect", "--resume", str(directory), "--yes"]), api)
    assert resumed["verified"] is True
    assert resumed["connection_id"] == downloaded["connection_id"]
    assert len(api.rows) == 1
    assert EXTERNAL not in capsys.readouterr().err


def test_download_exits_pending_with_the_resume_command(environment, tmp_path, monkeypatch, capsys):
    """Exit 4 is how a script tells "waiting on a human" from "broken"."""
    api, _ = environment
    monkeypatch.setattr(cli, "Api", lambda: api)
    directory = tmp_path / "handoff"
    code = cli.main(["connect", "--account", ACCOUNT, "--yes", "--download", str(directory), "--json"])
    assert code == 4
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "pending"
    assert "--resume" in envelope["next_action"]
    assert EXTERNAL not in json.dumps(envelope)


def test_a_pending_handoff_is_discoverable_afterwards(environment, tmp_path, monkeypatch):
    """An interrupted setup must be findable by its owner later, without hunting
    for the directory they chose."""
    api, _ = environment
    monkeypatch.setattr(cli, "Aws", lambda *args: pytest.fail("A handoff must not require local AWS credentials"))
    directory = tmp_path / "handoff"
    cli.run(arguments("--download", str(directory)), api)

    listed = cli.run(cli.parser().parse_args(["list"]), api)
    assert listed["pending_handoff"] == str(directory)
    assert [(row["name"], row["status"]) for row in listed["connections"]] == [(NAME, "pending")]

    cli.run(cli.parser().parse_args(["connect", "--resume", str(directory), "--yes"]), api)
    assert cli.run(cli.parser().parse_args(["list"]), api)["pending_handoff"] is None


@pytest.mark.parametrize("change", ["gateway", "account", "missing"])
def test_resume_refuses_a_changed_gateway_account_or_deleted_connection(environment, tmp_path, change):
    api, _ = environment
    directory = tmp_path / "handoff"
    cli.run(arguments("--download", str(directory)), api)
    path = directory / "connection.json"
    data = json.loads(path.read_text())
    if change == "gateway":
        data["gateway_url"] = "https://another.example.test/api"
    elif change == "account":
        data["account_id"] = OTHER_ACCOUNT
    else:
        api.rows.clear()
    path.write_text(json.dumps(data))
    api.calls.clear()
    with pytest.raises(cli.CliError):
        cli.run(cli.parser().parse_args(["connect", "--resume", str(directory), "--yes"]), api)
    assert not mutations(api)


def test_resume_ignores_no_other_setup_options(environment, tmp_path):
    api, _ = environment
    directory = tmp_path / "handoff"
    cli.run(arguments("--download", str(directory)), api)
    with pytest.raises(cli.CliError, match="No other setup options"):
        cli.run(cli.parser().parse_args(["connect", "--resume", str(directory), "--account", OTHER_ACCOUNT, "--yes"]), api)


def test_shared_download_directory_is_refused_before_anything_is_saved(environment, tmp_path):
    api, _ = environment
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    with pytest.raises(cli.CliError, match="private directory"):
        cli.run(arguments("--download", str(directory)), api)
    assert not mutations(api)


def test_another_connections_download_directory_is_refused(environment, tmp_path):
    api, _ = environment
    directory = tmp_path / "handoff"
    cli.run(arguments("--download", str(directory)), api)
    with pytest.raises(cli.CliError, match="another connection's setup"):
        cli.run(arguments("--name", "second", "--download", str(directory)), api)


# ---------------------------------------------------------------------------
# Repeated and interrupted runs
# ---------------------------------------------------------------------------


def test_rerun_after_an_interrupted_save_reuses_the_connection(environment):
    """Otherwise the second attempt makes a second record for one account, and the
    role that eventually appears matches only one of them."""
    api, aws = environment
    api.fail_after_connect = True
    with pytest.raises(cli.CliError, match="interrupted"):
        cli.run(arguments(), api)

    result = cli.run(arguments(), api)
    assert result["verified"] and result["reusing"] is True
    assert len(api.rows) == 1
    assert sum(call[:2] == ("cloudformation", "create-stack") for call in aws.calls) == 1


def test_rerun_after_an_interrupted_wait_reuses_the_existing_stack(environment):
    api, aws = environment
    aws.fail_wait = True
    with pytest.raises(cli.CliError, match="waiter"):
        cli.run(arguments(), api)
    assert not aws.parameter_file.exists()

    aws.fail_wait = False
    assert cli.run(arguments(), api)["verified"]
    assert len(api.rows) == 1
    assert sum(call[:2] == ("cloudformation", "create-stack") for call in aws.calls) == 1


def test_a_name_already_used_for_another_account_is_refused(environment):
    api, _ = environment
    cli.run(arguments("--name", "work"), api)
    args = cli.parser().parse_args(["connect", "--account", OTHER_ACCOUNT, "--name", "work", "--yes"])
    with pytest.raises(cli.CliError, match="already have a connection named work"):
        cli.run(args, api)
    assert len(api.rows) == 1


def test_failed_verification_reports_the_reason_and_leaves_the_connection(environment):
    """An interrupted setup must stay reusable: the connection is kept so the next
    run resumes it instead of creating a duplicate."""
    api, _ = environment
    api.verified = False
    with pytest.raises(cli.CliError, match="has not been created yet"):
        cli.run(arguments(), api)
    assert len(api.rows) == 1
    assert api.rows[0]["scopes"]["status"] == "pending"


def test_expired_session_is_reported_as_authentication_required(environment):
    api, aws = environment

    def expired(*args, **kwargs):
        raise cli.CliError("Sign in again with adp login.", "authentication_required", 2)

    api.request = expired
    with pytest.raises(cli.CliError) as error:
        cli.run(arguments(), api)
    assert error.value.exit_code == 2


def test_authentication_refusal_precedes_any_aws_call(environment):
    api, aws = environment

    def forbidden(*args, **kwargs):
        raise cli.CliError("This operation requires an authorized ADP administrator.", "insufficient_privileges", 3)

    api.request = forbidden
    with pytest.raises(cli.CliError):
        cli.run(cli.parser().parse_args(["list"]), api)
    assert not aws.calls


# ---------------------------------------------------------------------------
# Preview, confirmation, verify and disconnect
# ---------------------------------------------------------------------------


def test_dry_run_performs_only_reads(environment, capsys):
    api, aws = environment
    result = cli.run(arguments("--dry-run"), api)
    assert result["dry_run"] and not result["reusing"]
    assert not mutations(api)
    assert not aws.calls
    cli.display(result, False, "connect")
    output = capsys.readouterr().out
    assert "No changes made" in output
    assert "read-only access" in output


def test_non_interactive_changes_require_explicit_approval(environment, monkeypatch):
    api, _ = environment
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    args = arguments()
    args.yes = False
    with pytest.raises(cli.CliError, match="--yes"):
        cli.run(args, api)
    assert not mutations(api)


def test_declining_the_prompt_changes_nothing(environment, monkeypatch):
    api, _ = environment
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda: "no")
    args = arguments()
    args.yes = False
    with pytest.raises(cli.CliError, match="Cancelled"):
        cli.run(args, api)
    assert not mutations(api)


def test_verify_proves_the_connection_now_rather_than_replaying(environment, capsys):
    """FakeApi asserts fresh=True on every verify, so this also pins that the
    stored verdict is not what the command reports."""
    api, _ = environment
    cli.run(arguments(), api)
    api.calls.clear()

    result = cli.run(cli.parser().parse_args(["verify", NAME, "--yes"]), api)
    assert result["verified"] is True
    assert [call[1] for call in mutations(api)] == [cli.CONNECT + "/verify"]
    cli.display(result, False, "verify")
    assert "just now" in capsys.readouterr().out


def test_verify_reports_a_broken_connection_as_failed(environment, monkeypatch, capsys):
    api, _ = environment
    cli.run(arguments(), api)
    api.verified = False
    api.reason = "The role's trust policy rejected the assume request."
    monkeypatch.setattr(cli, "Api", lambda: api)

    code = cli.main(["verify", NAME, "--yes", "--json"])
    assert code == 5
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "failed"
    assert envelope["error"]["code"] == "not_verified"


def test_verify_dry_run_does_not_probe_or_change_stored_evidence(environment):
    api, _ = environment
    cli.run(arguments(), api)
    api.calls.clear()

    result = cli.run(cli.parser().parse_args(["verify", NAME, "--dry-run"]), api)

    assert result["action"] == "refresh_verification_evidence"
    assert result["dry_run"] is True
    assert not mutations(api)


def test_verify_requires_confirmation_before_the_probe(environment, monkeypatch):
    api, _ = environment
    cli.run(arguments(), api)
    api.calls.clear()
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)

    with pytest.raises(cli.CliError, match="--yes"):
        cli.run(cli.parser().parse_args(["verify", NAME]), api)

    assert not mutations(api)


def test_verify_and_disconnect_resolve_by_name_or_id(environment):
    api, _ = environment
    created = cli.run(arguments(), api)
    assert cli.resolve_connection(api, NAME)["id"] == created["connection_id"]
    assert cli.resolve_connection(api, created["connection_id"])["label"] == NAME
    with pytest.raises(cli.CliError, match="missing or ambiguous"):
        cli.resolve_connection(api, "no-such-connection")


def test_disconnect_removes_the_adp_connection_and_says_what_it_keeps(environment, capsys):
    api, _ = environment
    cli.run(arguments(), api)

    result = cli.run(cli.parser().parse_args(["disconnect", NAME, "--yes"]), api)
    assert result["disconnected"] is True
    assert api.rows == []
    assert [call[0] for call in mutations(api)] == ["POST", "POST", "DELETE"]

    cli.display(result, False, "disconnect")
    output = capsys.readouterr().out
    assert "left in place" in output
    assert "CloudFormation stack" in output


def test_disconnect_never_touches_aws(environment):
    """Deleting the role is the user's decision in their own account, not a side
    effect of removing an ADP record."""
    api, aws = environment
    cli.run(arguments(), api)
    aws.calls.clear()
    cli.run(cli.parser().parse_args(["disconnect", NAME, "--yes"]), api)
    assert not aws.calls


def test_disconnect_dry_run_explains_the_effect_without_removing(environment, capsys):
    api, _ = environment
    cli.run(arguments(), api)
    result = cli.run(cli.parser().parse_args(["disconnect", NAME, "--dry-run"]), api)
    assert result["dry_run"]
    assert len(api.rows) == 1
    cli.display(result, False, "disconnect")
    output = capsys.readouterr().out
    assert "Would keep the IAM role" in output
    assert "No changes made" in output


def test_disconnect_is_confirmed_before_removal(environment, monkeypatch):
    api, _ = environment
    cli.run(arguments(), api)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda: "no")
    with pytest.raises(cli.CliError, match="Cancelled"):
        cli.run(cli.parser().parse_args(["disconnect", NAME]), api)
    assert len(api.rows) == 1


def test_disconnect_confirms_the_removal_against_adp(environment, monkeypatch):
    """The outcome is read back, not assumed: a DELETE that reports success but
    leaves the connection listed must not be reported as disconnected.

    Issue #5039: this previously simulated the DELETE raising `gateway_unavailable`,
    because the shared transport could not tell a successful empty 204 from an
    unreachable gateway. adp_common now recognises an empty 204, so that premise
    is gone — but the readback it justified is still worth having, since it is
    what catches a delete the server accepted without acting on.
    """
    api, _ = environment
    cli.run(arguments(), api)
    original = api.request

    def request(method, path, body=None):
        if method == "DELETE":
            return {}  # what a real 204 now yields — but the row deliberately stays
        return original(method, path, body)

    api.request = request
    with pytest.raises(cli.CliError, match="still lists that connection"):
        cli.run(cli.parser().parse_args(["disconnect", NAME, "--yes"]), api)


def test_disconnect_surfaces_an_unreachable_gateway(environment):
    """A real network failure must NOT be mistaken for a successful delete.

    Issue #5039: the old code swallowed `gateway_unavailable` from the DELETE to
    work around the 204 parsing bug, which meant an genuinely unreachable gateway
    fell through to the readback. With the transport fixed, the swallow is gone
    and the failure surfaces.
    """
    api, _ = environment
    cli.run(arguments(), api)

    def request(method, path, body=None):
        raise cli.CliError("ADP could not be reached or returned an invalid response.", "gateway_unavailable")

    api.request = request
    with pytest.raises(cli.CliError, match="could not be reached"):
        cli.run(cli.parser().parse_args(["disconnect", NAME, "--yes"]), api)


def test_list_shows_only_the_callers_aws_connections(environment, capsys):
    api, _ = environment
    cli.run(arguments(), api)
    result = cli.run(cli.parser().parse_args(["list"]), api)
    assert [row["id"] for row in result["connections"]] == [api.rows[0]["id"]]
    cli.display(result, False, "list")
    output = capsys.readouterr().out
    assert NAME in output and ACCOUNT in output


def test_empty_list_points_at_the_connect_command(environment, capsys):
    api, _ = environment
    cli.display(cli.run(cli.parser().parse_args(["list"]), api), False, "list")
    assert "adp aws connect" in capsys.readouterr().out


def test_json_output_is_one_object_and_carries_no_setup_secret(environment, monkeypatch, capsys):
    api, _ = environment
    monkeypatch.setattr(cli, "Api", lambda: api)
    assert cli.main(["connect", "--account", ACCOUNT, "--yes", "--json"]) == 0
    output = capsys.readouterr().out
    envelope = json.loads(output)
    assert output.strip().count("\n") == 0
    assert envelope["command"] == "aws connect"
    assert envelope["status"] == "verified"
    assert EXTERNAL not in output and USER_TAG not in output


# ---------------------------------------------------------------------------
# Local AWS boundary and installed surface
# ---------------------------------------------------------------------------


def test_aws_failure_redacts_parameter_values(monkeypatch):
    """AWS validation errors quote parameter values, and one of this template's
    parameters is the ExternalId."""
    captured = []

    def run(command, **kwargs):
        captured.append(command)
        return subprocess.CompletedProcess(command, 1, "", f"An error occurred (AccessDenied): secret value {EXTERNAL}")

    monkeypatch.setattr(cli.subprocess, "run", run)
    aws = cli.Aws("fixture-profile", "us-east-1")
    with pytest.raises(cli.CliError, match="AccessDenied") as error:
        aws.call("cloudformation", "create-stack", "--parameters", "file:///private/parameters.json")
    assert EXTERNAL not in str(error.value)
    assert "fixture-profile" in captured[0]


def test_installed_cli_exposes_the_aws_commands_without_authentication(run_adp):
    result = run_adp(["aws", "connect", "--help"])
    assert result.returncode == 0, result.stderr
    assert "--download" in result.stdout and "--resume" in result.stdout and "--role-arn" in result.stdout
    assert "--external-id " not in result.stdout


def test_help_lists_the_aws_commands(run_adp):
    result = run_adp(["help"])
    assert "aws connect" in result.stdout
    assert "aws disconnect" in result.stdout
