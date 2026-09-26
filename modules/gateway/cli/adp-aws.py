#!/usr/bin/env python3
"""Connect your own AWS account to ADP from the command line.

Standard library only. Local AWS credentials stay local: ADP is sent the account
id and the role ARN to assume, never an access key or a session token. The
connection's ExternalId travels in private files, never in process arguments.

This is the personal read-only connection — the same records, role template and
endpoints the Credentials page uses, so a connection made here is managed there
and the other way round. It does not route anyone's Bedrock model calls; that is a
separate, separately authorized decision.
"""

from __future__ import annotations

import base64
import getpass
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.parse
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common  # noqa: E402

CREDENTIALS = "/auth/credentials"
CONNECT = CREDENTIALS + "/aws"
#: Exactly the files the setup read packages. A package with anything else in it
#: is not one we know how to apply, so nothing gets provisioned from it.
PACKAGE_FILES = {"template.yaml", "parameters.json", "README.md"}
#: The state file this helper owns (contract §3.6). Progress and identifiers only.
NAME = "aws"

# Shared auth, refresh, URL validation and error handling belong to CLI-00.
Api = common.Api
CliError = common.CliError


class Aws:
    """The local AWS CLI, used only against the user's own account.

    Nothing this class produces is sent to ADP. It exists so the common case —
    "I can create roles in my own account" — is one command instead of a console
    detour, and so the account is checked before anything is created.
    """

    def __init__(self, profile, region):
        self.profile = profile
        self.region = region

    def call(self, *args, missing_ok=False, wait=False):
        command = ["aws", *(["--profile", self.profile] if self.profile else []), "--region", self.region, "--output", "json", *args]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=1860 if wait else 120,
                env={**os.environ, "AWS_PAGER": "", "AWS_CLI_AUTO_PROMPT": "off"},
            )
        except FileNotFoundError as exc:
            raise CliError(
                "Install AWS CLI v2 to create the role here, or use --download to hand provisioning to an AWS administrator.",
                "aws_cli_missing",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CliError("The AWS operation timed out. The stack may still be running; rerun the same connect command to resume.") from exc
        if result.returncode:
            if missing_ok and "(ValidationError)" in result.stderr and "does not exist" in result.stderr:
                return None
            # Do not echo AWS stderr: validation errors can quote parameter values,
            # and one of this template's parameters is the ExternalId.
            match = re.search(r"An error occurred \(([A-Za-z0-9]+)\)", result.stderr)
            code = match.group(1) if match else "command_failed"
            raise CliError(f"AWS {args[0]} {args[1]} failed ({code}). Check the AWS profile and stack status; the ADP connection was not changed.")
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def check_account(self, account_id):
        """Refuse to provision into an account other than the one being connected.

        --profile only selects which local credentials to use. If those belong to
        somebody else's account, creating the role there would put a trust policy
        naming this ADP user into an account they did not mean to touch.
        """
        identity = self.call("sts", "get-caller-identity")
        if identity.get("Account") != account_id:
            raise CliError(
                f"Those AWS credentials belong to account {identity.get('Account')}, not {account_id}. Nothing was created.",
                "account_mismatch",
            )
        return identity["Account"]


def segment(value):
    return urllib.parse.quote(value, safe="")


def connections(api):
    """The caller's own AWS connections, from the endpoint the UI list uses."""
    return [row for row in api.request("GET", CREDENTIALS + "?scope=user") if row["service"] == "aws" and row["credential_type"] == "aws_role"]


def status_of(row):
    return (row.get("scopes") or {}).get("status") or "pending"


def account_of(row):
    return (row.get("scopes") or {}).get("account_id")


def resolve_connection(api, value):
    """Find one connection by ADP id or by the name the user gave it."""
    rows = connections(api)
    matches = [row for row in rows if row["id"] == value] or [row for row in rows if row["label"].casefold() == value.casefold()]
    if len(matches) != 1:
        raise CliError(f"Connection is missing or ambiguous: {value}. Use its exact ADP ID from adp aws list.", "not_found")
    return matches[0]


def external_id(args):
    """Read the ExternalId for an existing role from a private file, stdin or a
    hidden prompt — never from a command argument.

    A role that trusts ADP without the confused-deputy guard is legitimate, but
    saying so has to be deliberate: silently registering no ExternalId would
    weaken the connection without the user noticing.
    """
    if args.no_external_id:
        return None
    if args.external_id_file:
        value = common.read_private_json(args.external_id_file).get("external_id")
    elif args.external_id_stdin:
        try:
            value = json.loads(sys.stdin.read()).get("external_id")
        except (ValueError, AttributeError) as exc:
            raise CliError('Provide {"external_id": "…"} as a JSON object on stdin.', "usage_error", 1) from exc
    elif sys.stdin.isatty():
        value = getpass.getpass("ExternalId the role requires (Enter if it requires none): ").strip() or None
        if value is None:
            raise CliError("Pass --no-external-id to register a role whose trust policy has no ExternalId condition.", "usage_error", 1)
    else:
        raise CliError(
            "Supply the role's ExternalId with --external-id-file or --external-id-stdin, or pass --no-external-id if it requires none.",
            "usage_error",
            1,
        )
    if not isinstance(value, str) or not value.strip():
        raise CliError('The ExternalId source must contain a non-empty "external_id" string.', "usage_error", 1)
    return value.strip()


def setup_package(api, connection, account_id):
    """Fetch a saved connection's own setup material and check it describes it.

    The gateway rebuilds this from the connection's stored ExternalId, so a
    resumed or handed-off setup provisions the role ADP will actually assume. The
    checks here are against the connection we asked about, so a mixed-up response
    cannot cause a role to be created in the wrong account.
    """
    setup = api.request("GET", f"{CONNECT}/{segment(connection['id'])}/setup")
    expected_role = f"arn:aws:iam::{account_id}:role/ADP-Agent-{connection['label']}"
    if setup["credential_id"] != connection["id"] or setup["account_id"] != account_id or setup["role_arn"] != expected_role:
        raise CliError("Setup details do not match the selected connection. Nothing was created.")
    try:
        encoded = setup["download_base64"]
        if len(encoded) > 4_000_000:
            raise ValueError("Oversized package")
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(encoded, validate=True))) as bundle:
            if set(bundle.namelist()) != PACKAGE_FILES or len(bundle.infolist()) != len(PACKAGE_FILES):
                raise ValueError("Unexpected files")
            if sum(info.file_size for info in bundle.infolist()) > 2_000_000:
                raise ValueError("Oversized files")
            files = {name: bundle.read(name) for name in PACKAGE_FILES}
        parameters = json.loads(files["parameters.json"])
        if not isinstance(parameters, list) or any(not isinstance(item.get("ParameterValue"), str) for item in parameters):
            raise ValueError("Invalid parameters")
        values = {item["ParameterKey"]: item["ParameterValue"] for item in parameters}
        # UserSessionTag is what pins this role to one ADP user; a package without
        # it is not the personal template and must not be applied as if it were.
        if values.get("Nickname") != connection["label"] or not values.get("ExternalId") or not values.get("UserSessionTag"):
            raise ValueError("Wrong role parameters")
    except (ValueError, KeyError, AttributeError, zipfile.BadZipFile) as exc:
        raise CliError("The setup package is invalid. Nothing was created.") from exc
    return setup, files


def write_files(directory, files):
    for name, content in files.items():
        fd, temporary = tempfile.mkstemp(prefix=".adp-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
            os.replace(temporary, directory / name)
        finally:
            Path(temporary).unlink(missing_ok=True)


def verify(api, connection_id):
    """Prove the connection works now, rather than replaying a stored verdict.

    ``fresh`` is what separates this from the connect flow's idempotent check: the
    question a person asks with this command is whether the role is usable today,
    and a role an administrator deleted last week would still answer yes from
    cache.
    """
    result = api.request("POST", CONNECT + "/verify", {"credential_id": connection_id, "fresh": True})
    if result["status"] != "verified":
        reason = result.get("reason") or "Verification failed."
        raise CliError(f"ADP could not assume the role. {reason}", "not_verified")
    return result


def provision(api, aws, connection, account_id):
    """Create the role in the user's account, then have ADP prove it works."""
    aws.check_account(account_id)
    setup, files = setup_package(api, connection, account_id)
    stack_name = "ADP-Agent-" + connection["label"]
    described = aws.call("cloudformation", "describe-stacks", "--stack-name", stack_name, missing_ok=True)
    if described:
        stack = described["Stacks"][0]
        if stack["StackStatus"] == "CREATE_IN_PROGRESS":
            print(f"Waiting for the role in AWS account {account_id}…", file=sys.stderr)
            aws.call("cloudformation", "wait", "stack-create-complete", "--stack-name", stack_name, wait=True)
        elif stack["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
            raise CliError(
                f"Stack {stack_name} is {stack['StackStatus']}. Resolve it in AWS before retrying; ADP will not replace an existing stack."
            )
    else:
        print(f"Creating the role in AWS account {account_id}…", file=sys.stderr)
        with tempfile.TemporaryDirectory(prefix="adp-aws-") as temporary:
            directory = Path(temporary)
            write_files(directory, files)
            aws.call(
                "cloudformation",
                "create-stack",
                "--stack-name",
                stack_name,
                "--template-body",
                "file://" + str(directory / "template.yaml"),
                "--parameters",
                "file://" + str(directory / "parameters.json"),
                "--capabilities",
                "CAPABILITY_NAMED_IAM",
            )
            aws.call("cloudformation", "wait", "stack-create-complete", "--stack-name", stack_name, wait=True)
    final = aws.call("cloudformation", "describe-stacks", "--stack-name", stack_name)["Stacks"][0]
    outputs = {row["OutputKey"]: row["OutputValue"] for row in final.get("Outputs", [])}
    if outputs.get("RoleArn") != setup["role_arn"]:
        raise CliError("The existing stack does not output the expected role ARN. The connection was left unverified.")
    return verify(api, connection["id"])


def parser():
    root = common.Parser(prog="adp aws", description="Connect your own AWS account to ADP.")
    commands = root.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="Show your AWS connections")
    listing.add_argument("--json", action="store_true")

    connect = commands.add_parser("connect", help="Connect an AWS account, or resume an interrupted setup")
    connect.add_argument("--account", dest="account_id", help="12-digit AWS account ID")
    connect.add_argument("--name", help="Your name for this connection; defaults to personal-ACCOUNT")
    connect.add_argument("--region", default="us-east-1", help="AWS region used for the role's setup and assume calls")
    connect.add_argument("--profile", dest="aws_profile", help="Local AWS profile to create the role with (selects local credentials only)")
    connect.add_argument("--role-arn", help="Register an IAM role that already exists instead of creating one")
    secret = connect.add_mutually_exclusive_group()
    secret.add_argument("--external-id-file", metavar="PATH", help='Private 0600 file holding {"external_id": "…"} for an existing role')
    secret.add_argument("--external-id-stdin", action="store_true", help="Read that JSON object from stdin")
    secret.add_argument("--no-external-id", action="store_true", help="The existing role's trust policy has no ExternalId condition")
    connect.add_argument("--download", dest="output_dir", metavar="DIRECTORY", help="Save the setup for an AWS administrator; create nothing")
    connect.add_argument("--resume", metavar="DIRECTORY", help="Verify a downloaded setup after the administrator applied it")
    connect.add_argument("--yes", action="store_true", help="Approve without a prompt, for scripts")
    connect.add_argument("--dry-run", action="store_true", help="Show what would happen without creating or changing anything")
    connect.add_argument("--json", action="store_true", help="Print machine-readable output")

    verification = commands.add_parser("verify", help="Check that a connection still works right now")
    verification.add_argument("connection", help="Connection ID or name")
    verification.add_argument("--yes", action="store_true", help="Approve updating stored verification evidence without a prompt")
    verification.add_argument("--dry-run", action="store_true", help="Show which verification evidence would be refreshed without probing or writing")
    verification.add_argument("--json", action="store_true")

    removal = commands.add_parser("disconnect", help="Remove an ADP connection; leaves the AWS role in place")
    removal.add_argument("connection", help="Connection ID or name")
    removal.add_argument("--yes", action="store_true", help="Approve without a prompt, for scripts")
    removal.add_argument("--dry-run", action="store_true", help="Show what would be removed without removing it")
    removal.add_argument("--json", action="store_true")
    return root


def confirm(args, message, extra=None):
    if args.dry_run:
        return False
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise CliError("Non-interactive changes require --yes. Use --dry-run to inspect the plan first.", "confirmation_required", 1)
    print(message, file=sys.stderr)
    if extra:
        print(extra, file=sys.stderr)
    print("Continue? Type yes: ", end="", file=sys.stderr, flush=True)
    if input().strip() != "yes":
        raise CliError("Cancelled; nothing was changed.", "cancelled")
    return True


def resume_details(api, args):
    """Recover the saved account, gateway and connection from a download directory.

    This path exists for the user who cannot create IAM resources, so it must work
    with no local AWS credentials at all — it reads the saved identifiers and asks
    ADP to assume the role. The gateway check is here because the same directory
    presented to a different deployment would name a connection id that belongs to
    somebody else's records.
    """
    if any((args.account_id, args.name, args.aws_profile, args.output_dir, args.role_arn)):
        raise CliError(
            "--resume uses the account and connection saved in the download directory. No other setup options are needed.", "usage_error", 1
        )
    metadata = common.read_private_json(Path(args.resume) / "connection.json")
    # Whose setup this is, checked before any ADP or AWS call (Issue #5413). Two
    # deployments can share a gateway URL as aliases, so identity is checked by the
    # stable id the handoff recorded, not by the URL alone.
    common.check_handoff_deployment(metadata, "AWS setup")
    if metadata.get("gateway_url") != api.base:
        raise CliError("This setup belongs to a different ADP gateway. Sign in to that gateway before resuming.")
    rows = [row for row in connections(api) if row["id"] == metadata["credential_id"]]
    if not rows:
        raise CliError("That saved connection no longer exists in ADP. Run adp aws connect --account ACCOUNT to start again.", "not_found")
    connection = rows[0]
    if account_of(connection) != metadata["account_id"]:
        raise CliError("The saved account no longer matches this connection. Download the setup again.")
    args.account_id = metadata["account_id"]
    args.name = connection["label"]
    args.region = metadata.get("region", args.region)
    return connection


def existing_for(api, args):
    """Reuse the caller's own pending or verified connection instead of a duplicate.

    An interrupted `connect` leaves a saved connection behind. Rerunning the same
    command has to find it — otherwise the second attempt creates a second record
    for one account, and the role that eventually appears matches only one of them.
    Owner scoping is the server's; this list is already only the caller's.
    """
    matches = [row for row in connections(api) if row["label"] == args.name]
    if not matches:
        return None
    connection = matches[0]
    if account_of(connection) not in {None, args.account_id}:
        raise CliError(
            f"You already have a connection named {args.name} for AWS account {account_of(connection)}. Choose another --name.",
            "duplicate_name",
        )
    return connection


def run(args, api):
    mutation_capability = {
        "connect": "connections.aws.write",
        "disconnect": "connections.aws.write",
        "verify": "connections.aws.verify.write",
    }.get(args.command)
    if mutation_capability and not args.dry_run:
        common.ensure_can_mutate(mutation_capability, request=api.request)
    if args.command == "list":
        saved = common.read_state(NAME)
        pending = saved.get("download_dir") if saved.get("gateway_url") == api.base else None
        return {
            "connections": [
                {"id": row["id"], "name": row["label"], "account_id": account_of(row), "status": status_of(row)} for row in connections(api)
            ],
            "pending_handoff": pending,
        }

    if args.command == "verify":
        connection = resolve_connection(api, args.connection)
        plan = {
            "action": "refresh_verification_evidence",
            "connection_id": connection["id"],
            "name": connection["label"],
            "account_id": account_of(connection),
            "dry_run": args.dry_run,
            "effect": "The probe may mark a previously verified connection unavailable.",
        }
        if args.dry_run:
            return plan
        confirm(
            args,
            f"Probe {connection['label']} now and replace its stored verification evidence?",
            "A failed probe can mark a previously verified connection unavailable.",
        )
        result = verify(api, connection["id"])
        return {
            "connection_id": connection["id"],
            "name": connection["label"],
            "account_id": account_of(connection),
            "verified": True,
            "routing_capable": result.get("routing_capable"),
        }

    if args.command == "disconnect":
        return disconnect(api, args)

    return connect(api, args)


def connect(api, args):
    connection = resume_details(api, args) if args.resume else None
    if args.resume and args.role_arn:
        raise CliError("--resume verifies a downloaded setup; it does not register a different role.", "usage_error", 1)
    if args.role_arn and (args.output_dir or args.aws_profile):
        raise CliError("--role-arn registers a role that already exists, so there is nothing to download or create.", "usage_error", 1)
    if args.output_dir and args.resume:
        raise CliError("Use --download to save a setup, then --resume to finish it.", "usage_error", 1)

    if not args.account_id:
        raise CliError("Use --account with a 12-digit AWS account ID, or --resume with a downloaded setup directory.", "usage_error", 1)
    if not re.fullmatch(r"[0-9]{12}", args.account_id):
        raise CliError("--account must contain exactly 12 digits.", "usage_error", 1)
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", args.region):
        raise CliError("Invalid AWS region.", "usage_error", 1)
    args.name = args.name or f"personal-{args.account_id}"
    # The name becomes the role and stack name, so the template's own limits apply.
    if not re.fullmatch(r"[A-Za-z0-9-]{1,54}", args.name):
        raise CliError("--name must contain 1–54 letters, numbers or hyphens.", "usage_error", 1)
    if args.role_arn and not re.fullmatch(r"arn:aws(?:-[a-z]+)*:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}", args.role_arn):
        raise CliError("--role-arn must be an IAM role ARN, for example arn:aws:iam::123456789012:role/MyRole.", "usage_error", 1)
    if args.role_arn and args.role_arn.split(":")[4] != args.account_id:
        raise CliError("--role-arn belongs to a different AWS account than --account. Nothing was registered.", "account_mismatch")

    connection = connection or existing_for(api, args)
    action = "register_existing_role" if args.role_arn else "download" if args.output_dir else "verify" if args.resume else "create_role"
    plan = {
        "action": action,
        "account_id": args.account_id,
        "name": args.name,
        "role_arn": args.role_arn or f"arn:aws:iam::{args.account_id}:role/ADP-Agent-{args.name}",
        "region": args.region,
        "connection_id": connection["id"] if connection else None,
        "reusing": bool(connection),
        "grants": "read-only access to that account for your own ADP work",
        "dry_run": args.dry_run,
    }
    if args.dry_run:
        return plan

    aws = None
    if action == "create_role":
        # Check the account before anything is saved, so a mistyped --account or
        # the wrong --profile fails without leaving a record behind.
        aws = Aws(args.aws_profile, args.region)
        aws.check_account(args.account_id)

    confirm(
        args,
        f"Connect AWS account {args.account_id} to your ADP account as {args.name}.",
        "ADP will be able to read that account on your behalf. Shared Bedrock routing is not affected.",
    )

    directory = common.private_directory(args.output_dir) if args.output_dir else None
    if directory and (directory / "connection.json").exists():
        saved = common.read_private_json(directory / "connection.json")
        if saved.get("gateway_url") != api.base or not connection or saved.get("credential_id") != connection["id"]:
            raise CliError("That directory holds another connection's setup. Choose a different --download directory.")

    if args.role_arn:
        return register_existing(api, args, plan)

    if connection is None:
        created = api.request("POST", CONNECT + "/connect", {"nickname": args.name, "account_id": args.account_id})
        connection = {"id": created["credential_id"], "label": args.name, "scopes": {"account_id": args.account_id, "status": "pending"}}
        print(f"Saved pending connection {connection['id']}; rerun the same command if setup is interrupted.", file=sys.stderr)

    if directory:
        setup, files = setup_package(api, connection, args.account_id)
        files["connection.json"] = (
            json.dumps(
                {
                    "gateway_url": api.base,
                    # Which deployment this handoff belongs to (Issue #5413), so the
                    # resume that comes back later can refuse to apply it elsewhere.
                    **common.deployment_stamp(),
                    "credential_id": connection["id"],
                    "account_id": setup["account_id"],
                    "role_arn": setup["role_arn"],
                    "region": setup["region"],
                },
                indent=2,
            )
            + "\n"
        ).encode()
        write_files(directory, files)
        # Remember the handoff so an interrupted setup is discoverable later. The
        # directory holds the ExternalId; the state file holds only its path.
        common.write_state(NAME, {"gateway_url": api.base, "credential_id": connection["id"], "download_dir": str(directory)})
        return {**plan, "connection_id": connection["id"], "output_dir": str(directory), "verified": False}

    result = provision(api, aws, connection, args.account_id) if aws else verify(api, connection["id"])
    if args.resume:
        common.write_state(NAME, {})
    return {**plan, "connection_id": connection["id"], "verified": True, "routing_capable": result.get("routing_capable")}


def register_existing(api, args, plan):
    """Register a role the account already has, then prove ADP can assume it.

    Registering is not verifying: the import call records the role, and the verify
    call is what decides whether the connection is usable. Reporting it any other
    way would mark a mistyped ARN as working.
    """
    body = {"nickname": args.name, "account_id": args.account_id, "role_arn": args.role_arn, "default_region": args.region}
    secret = external_id(args)
    if secret:
        body["external_id"] = secret
    result = api.request("POST", CONNECT + "/import", body)
    if result["reused"]:
        print(f"Reusing existing connection {result['credential_id']} for that role.", file=sys.stderr)
    verified = verify(api, result["credential_id"])
    return {
        **plan,
        "connection_id": result["credential_id"],
        "reusing": result["reused"],
        "verified": True,
        "routing_capable": verified.get("routing_capable"),
    }


def disconnect(api, args):
    connection = resolve_connection(api, args.connection)
    plan = {
        "action": "disconnect",
        "connection_id": connection["id"],
        "name": connection["label"],
        "account_id": account_of(connection),
        "removes": "ADP's saved connection and its stored role reference",
        "keeps": "the IAM role, its policies and the CloudFormation stack in your AWS account",
        "dry_run": args.dry_run,
    }
    if args.dry_run:
        return plan
    confirm(
        args,
        f"Disconnect {connection['label']} (AWS account {account_of(connection)}) from ADP.",
        "ADP stops using that account and anything an administrator pointed at this connection stops working. "
        "The IAM role stays in AWS — delete its CloudFormation stack yourself if you want it gone.",
    )
    # A successful delete answers 204 with no body. The shared transport used to
    # report that as "gateway_unavailable", so this call had to swallow that one
    # code and let the readback below settle it; adp_common now recognises an
    # empty 204, so a genuine unreachable gateway is again allowed to surface
    # here instead of being mistaken for success (Issue #5039).
    api.request("DELETE", f"{CREDENTIALS}/{segment(connection['id'])}")
    # The readback stays: it confirms ADP really dropped the connection.
    if any(row["id"] == connection["id"] for row in connections(api)):
        raise CliError("ADP still lists that connection. It was not disconnected; check its status before retrying.")
    return {**plan, "disconnected": True}


def result_envelope(result, command):
    status = "verified" if result.get("verified") else "pending" if result.get("output_dir") else "configured"
    next_action = None
    if result.get("output_dir"):
        next_action = (
            "Ask your AWS administrator to apply the downloaded template, then run "
            f"adp aws connect --resume {shlex.quote(result['output_dir'])} --yes."
        )
    return common.envelope(status, "aws " + command, result, next_action)


def display(result, as_json, command):
    if as_json:
        print(json.dumps(result_envelope(result, command)))
        return
    if "connections" in result:
        for row in result["connections"]:
            print(f"{row['name']}  {row['account_id'] or 'account unknown'}  {row['status']}  ({row['id']})")
        if not result["connections"]:
            print("No AWS connections yet. Use adp aws connect --account ACCOUNT to add one.")
        if result.get("pending_handoff"):
            print(f"A downloaded setup is waiting in {result['pending_handoff']}. Finish it with adp aws connect --resume.")
    elif result.get("dry_run"):
        if result["action"] == "refresh_verification_evidence":
            print(
                f"Would probe {result['name']} (AWS account {result['account_id']}) and replace its stored verification evidence. No probe sent."
            )
        elif result["action"] == "disconnect":
            print(f"Would disconnect {result['name']} (AWS account {result['account_id']}) from ADP. No changes made.")
            print(f"Would remove {result['removes']}. Would keep {result['keeps']}.")
        else:
            print(f"Would {result['action'].replace('_', ' ')} for AWS account {result['account_id']} as {result['name']}. No changes made.")
            print(f"Role: {result['role_arn']} ({result['region']}). Grants {result['grants']}.")
            if result["reusing"]:
                print(f"Would reuse your existing connection {result['connection_id']}.")
    elif result.get("disconnected"):
        print(f"Disconnected {result['name']} from ADP. The IAM role in AWS account {result['account_id']} was left in place.")
        print("Delete its CloudFormation stack in AWS if you no longer want the role.")
    elif result.get("output_dir"):
        print(f"Setup saved to {result['output_dir']}. Give template.yaml, parameters.json and README.md to your AWS administrator.")
        print("That directory contains this connection's ExternalId — share it only with them.")
        print("Once the role exists, run:")
        print(f"  adp aws connect --resume {shlex.quote(result['output_dir'])}")
    elif command == "verify":
        print(f"Connection {result['name']} works: ADP assumed the role in AWS account {result['account_id']} just now.")
    else:
        print(f"Connected AWS account {result['account_id']} as {result['name']} and verified it.")
        print("ADP can now read that account for your own work. Shared Bedrock routing is unchanged.")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    try:
        args = parser().parse_args(argv)
        result = run(args, Api())
        display(result, args.json, args.command)
        return 4 if result.get("output_dir") else 0
    except (CliError, OSError, ValueError, KeyError, TypeError) as exc:
        return common.report_error(exc, "aws", as_json)
    except KeyboardInterrupt:
        return common.report_error(
            CliError("Interrupted. A stack may still be running; rerun the same command to resume.", "interrupted", 130), "aws", as_json
        )


if __name__ == "__main__":
    sys.exit(main())
