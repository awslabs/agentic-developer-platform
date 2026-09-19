#!/usr/bin/env python3
"""Scriptable Bedrock destination setup using ADP login and a local AWS profile.

Standard library only. Tokens stay in memory; AWS credentials never go to ADP.
CloudFormation parameters travel in private files, never process arguments.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common  # noqa: E402

ROUTING = "/admin/bedrock-routing"
PACKAGE_FILES = {"template.yaml", "parameters.json", "README.md"}


# Shared auth, refresh, URL validation and error handling belong to CLI-00.
Api = common.Api
CliError = common.CliError


class Aws:
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
            raise CliError("Install AWS CLI v2 to provision a role. Use --download to hand provisioning to an AWS administrator.") from exc
        except subprocess.TimeoutExpired as exc:
            raise CliError("The AWS operation timed out. The stack may still be running; rerun the same connect command to resume.") from exc
        if result.returncode:
            if missing_ok and "(ValidationError)" in result.stderr and "does not exist" in result.stderr:
                return None
            # Do not echo AWS stderr: validation errors can include parameter values.
            match = re.search(r"An error occurred \(([A-Za-z0-9]+)\)", result.stderr)
            code = match.group(1) if match else "command_failed"
            raise CliError(f"AWS {args[0]} {args[1]} failed ({code}). Check the AWS profile and stack status; assignment was not changed.")
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def check_account(self, account_id):
        identity = self.call("sts", "get-caller-identity")
        if identity.get("Account") != account_id:
            raise CliError(f"AWS profile belongs to account {identity.get('Account')}, expected {account_id}. Nothing was provisioned.")
        return identity["Account"]


def segment(value):
    return urllib.parse.quote(value, safe="")


def pages(api, path):
    rows = []
    for page in range(1, 1001):
        result = api.request("GET", path + f"?page={page}&page_size=100")
        rows.extend(result["items"])
        if not result.get("has_more") and len(rows) >= result.get("total", len(rows)):
            return rows
        if not result["items"]:
            break
    raise CliError("ADP returned an incomplete list. Retry; no scope was selected.")


def resolve(rows, value, fields, kind):
    ids = [row for row in rows if row["id"] == value]
    matches = ids or [row for row in rows if any(str(row.get(field) or "").casefold() == value.casefold() for field in fields)]
    if len(matches) != 1:
        raise CliError(f"{kind} is missing or ambiguous: {value}. Use its exact ADP ID.")
    return matches[0]


def organization(api, value):
    return resolve(pages(api, "/admin/organizations"), value, ["name"], "Organization")


def scope_for(api, args, org=None):
    if args.scope == "org":
        if not args.org or args.team or args.user:
            raise CliError("An organization rule requires --org and no --team or --user.")
        org = org or organization(api, args.org)
        return {"scope_type": "org", "scope_id_org": org["id"], "path": "org:" + segment(org["id"])}
    if args.scope == "team":
        if not args.org or not args.team or args.user:
            raise CliError("A team rule requires --org and --team, with no --user.")
        org = org or organization(api, args.org)
        team = resolve(pages(api, f"/admin/organizations/{segment(org['id'])}/teams"), args.team, ["name"], "Team")
        return {
            "scope_type": "team",
            "scope_id_org": org["id"],
            "scope_id_team": team["id"],
            "path": f"team:{segment(org['id'])}:{segment(team['id'])}",
        }
    if not args.user or args.team:
        raise CliError("A user rule requires --user (email, GitHub username or ADP user ID), with no --team.")
    user = resolve(pages(api, "/admin/users"), args.user, ["email", "github_username"], "User")
    return {"scope_type": "user", "scope_id_user": user["id"], "path": "user:" + segment(user["id"])}


def destinations(api):
    return api.request("GET", ROUTING + "/destinations")


def destination_by_id(api, destination_id):
    return resolve(destinations(api), destination_id, [], "Destination")


def current_mapping(api, scope):
    keys = [key for key in scope if key != "path"]
    existing = next((row for row in api.request("GET", ROUTING + "/mappings") if all(row.get(key) == scope[key] for key in keys)), None)
    return existing["destination_id"] if existing else None


def confirm(args, plan):
    if args.dry_run:
        return False
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise CliError("Non-interactive changes require --yes. Use --dry-run to inspect the plan first.")
    print(f"Connect AWS account {plan['account_id']} to {plan['applies_to']}.", file=sys.stderr)
    if plan["previous_destination_id"] and plan["previous_destination_id"] != plan["destination_id"]:
        print("This replaces the existing rule at that scope.", file=sys.stderr)
    print("Continue? Type yes: ", end="", file=sys.stderr, flush=True)
    if input().strip() != "yes":
        raise CliError("Cancelled; no changes made.")
    return True


def find_prepared(api, org, args):
    matches = [
        row for row in destinations(api) if row["owner_org_id"] == org["id"] and row["account_id"] == args.account_id and row["label"] == args.name
    ]
    if len(matches) > 1:
        raise CliError("Multiple matching destinations exist. Resolve the duplicate destinations in Model Access before retrying.")
    if matches and (matches[0]["region"] != args.region or matches[0].get("connection_id")):
        raise CliError(
            "The matching destination has a different region or uses an existing connection. "
            "Choose another --name or use the connection through Model Access."
        )
    return matches[0] if matches else None


def register(api, org, args, existing):
    if existing:
        return existing
    result = api.request(
        "POST",
        ROUTING + "/destinations",
        {
            "source": "new_account",
            "account_id": args.account_id,
            "label": args.name,
            "link_to_org_id": org["id"],
            "region": args.region,
        },
    )
    destination = result["destination"]
    print(f"Saved pending destination {destination['id']}; rerun the same command if setup is interrupted.", file=sys.stderr)
    return destination


def setup_package(api, destination):
    setup = api.request("GET", f"{ROUTING}/destinations/{segment(destination['id'])}/setup")
    expected_role = f"arn:aws:iam::{destination['account_id']}:role/ADP-Agent-{destination['label']}"
    if setup["account_id"] != destination["account_id"] or setup["role_arn"] != expected_role or setup["region"] != destination["region"]:
        raise CliError("Setup details do not match the selected destination. Nothing was provisioned.")
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
        if values.get("Nickname") != destination["label"] or not values.get("ExternalId"):
            raise ValueError("Wrong role parameters")
    except (ValueError, KeyError, AttributeError, zipfile.BadZipFile) as exc:
        raise CliError("The setup package is invalid. Nothing was provisioned.") from exc
    return setup, files


def private_directory(path):
    path = Path(path).absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise CliError("Use a private output directory owned by you with permissions 0700.")
    return path


def write_files(directory, files):
    for name, content in files.items():
        fd, temporary = tempfile.mkstemp(prefix=".adp-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
            os.replace(temporary, directory / name)
        finally:
            Path(temporary).unlink(missing_ok=True)


def verify(api, destination_id):
    result = api.request("POST", f"{ROUTING}/destinations/{segment(destination_id)}/verify")
    if not result["verified"]:
        reason = result.get("reason") or "not_verified"
        reason = reason if re.fullmatch(r"[a-z_]{1,80}", reason) else "not_verified"
        raise CliError(f"Destination verification failed ({reason}). No routing rule was assigned.")
    return result["destination"]


def provision(api, aws, destination):
    aws.check_account(destination["account_id"])
    setup, files = setup_package(api, destination)
    stack_name = "ADP-Agent-" + destination["label"]
    if not re.fullmatch(r"ADP-Agent-[A-Za-z0-9-]{1,54}", stack_name):
        raise CliError("This role needs manual provisioning; its name is not supported by the routing template.")
    described = aws.call("cloudformation", "describe-stacks", "--stack-name", stack_name, missing_ok=True)
    if described:
        stack = described["Stacks"][0]
        status = stack["StackStatus"]
        if status == "CREATE_IN_PROGRESS":
            print(f"Waiting for the role in AWS account {destination['account_id']}…", file=sys.stderr)
            aws.call("cloudformation", "wait", "stack-create-complete", "--stack-name", stack_name, wait=True)
        elif status not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
            raise CliError(f"Stack {stack_name} is {status}. Resolve it in AWS before retrying; ADP will not replace an existing stack.")
    else:
        print(f"Creating the role in AWS account {destination['account_id']}…", file=sys.stderr)
        with tempfile.TemporaryDirectory(prefix="adp-bedrock-") as temporary:
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
        raise CliError("The existing stack does not output the expected role ARN. Nothing was assigned.")
    return verify(api, destination["id"])


def assign(api, destination, scope, previous):
    # Check again after provisioning; another administrator may have changed it.
    if current_mapping(api, scope) not in {previous, destination["id"]}:
        raise CliError("The routing rule changed during setup. Rerun connect to review the latest rule.")
    return api.request("PUT", ROUTING + "/mappings/" + scope["path"], {"destination_id": destination["id"]})


def parser():
    root = common.Parser(prog="adp admin bedrock", description="Connect an AWS account and route Bedrock usage to it.")
    commands = root.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="Show available destinations")
    listing.add_argument("--org", help="Organization ID or exact name")
    listing.add_argument("--json", action="store_true")
    connect = commands.add_parser("connect", help="Create the role, verify it and assign routing")
    connect.add_argument("--account", dest="account_id", help="12-digit AWS account ID")
    connect.add_argument("--destination", help="Reuse an existing destination ID; verify and assign without provisioning")
    connect.add_argument("--org", help="Organization ID or exact name")
    scope = connect.add_mutually_exclusive_group()
    scope.add_argument("--team", help="Route a team within --org instead of the whole organization")
    scope.add_argument("--user", help="Route one user (email, GitHub username or ADP ID)")
    connect.add_argument("--profile", dest="aws_profile", help="Local AWS profile (otherwise use the AWS CLI's current credentials)")
    connect.add_argument("--download", dest="output_dir", metavar="DIRECTORY", help="Download for an AWS administrator; do not provision or assign")
    connect.add_argument("--resume", metavar="DIRECTORY", help="Verify and assign a downloaded setup after the administrator creates the role")
    connect.add_argument("--name", help="Optional role nickname; generated from the organization by default")
    connect.add_argument("--region", default="us-east-1")
    connect.add_argument("--yes", action="store_true", help="Approve changes without a prompt, for scripts")
    connect.add_argument("--dry-run", action="store_true", help="Show the plan without creating or changing anything")
    connect.add_argument("--json", action="store_true", help="Print machine-readable output")
    verification = commands.add_parser("verify", help="Verify an existing destination through ADP")
    verification.add_argument("destination", help="Destination ID")
    verification.add_argument("--json", action="store_true")
    selection = commands.add_parser("status", help="Show the effective routing rule and its source")
    selection.add_argument("--user", help="Inspect another user (requires administrator authority)")
    selection.add_argument("--json", action="store_true")
    return root


def resume_details(api, args):
    if any((args.account_id, args.org, args.team, args.user, args.name, args.aws_profile, args.output_dir, args.destination)):
        raise CliError("--resume uses the account and routing scope saved in the download directory. No other setup options are needed.")
    metadata = common.read_private_json(Path(args.resume) / "destination.json")
    # Whose setup this is, checked before any ADP or AWS call (Issue #5413): a
    # routing rule assigned from another deployment's handoff would point a whole
    # organization's Bedrock traffic at an account nobody chose here.
    common.check_handoff_deployment(metadata, "Bedrock setup")
    if metadata["gateway_url"] != api.base:
        raise CliError("This setup belongs to a different ADP gateway. Sign in to that gateway before resuming.")
    destination = destination_by_id(api, metadata["destination_id"])
    if destination["account_id"] != metadata["account_id"] or destination["owner_org_id"] != metadata["organization_id"]:
        raise CliError("The saved account or organization no longer matches this destination.")
    args.account_id = destination["account_id"]
    args.org = metadata["organization_id"]
    args.name = destination["label"]
    args.region = destination["region"]
    scope = metadata["scope"]
    if scope.get("scope_type") not in {"org", "team", "user"}:
        raise CliError("The saved routing scope is invalid. Download the setup again.")
    if scope["scope_type"] == "team" and not scope.get("scope_id_team"):
        raise CliError("The saved team is missing. Download the setup again.")
    if scope["scope_type"] == "user" and not scope.get("scope_id_user"):
        raise CliError("The saved user is missing. Download the setup again.")
    args.saved_scope = scope
    args.team = scope.get("scope_id_team")
    args.user = scope.get("scope_id_user")
    return destination


def run(args, api):
    if args.command == "list":
        org = organization(api, args.org) if args.org else None
        return {"destinations": [row for row in destinations(api) if org is None or row["owner_org_id"] in {None, org["id"]}]}
    if args.command == "status":
        if args.user:
            user = resolve(pages(api, "/admin/users"), args.user, ["email", "github_username"], "User")
            effective = api.request("GET", ROUTING + "/effective/" + segment(user["id"]))
        else:
            effective = api.request("GET", "/me/bedrock-routing/selection")["effective"]
        return {"effective": effective, "verification": "configured_route_only", "applies_to": "personal and user-owned cloud calls"}
    if args.command == "verify":
        destination = destination_by_id(api, args.destination)
        verified = verify(api, destination["id"])
        return {"destination_id": verified["id"], "account_id": verified["account_id"], "verified": True, "assigned": False}
    destination = resume_details(api, args) if args.resume else None
    if args.destination:
        if args.output_dir or args.aws_profile or args.name:
            raise CliError("--destination reuses a role; do not combine it with --download, --profile or --name.")
        destination = destination_by_id(api, args.destination)
        if args.account_id and args.account_id != destination["account_id"]:
            raise CliError("The destination belongs to a different AWS account. Nothing was changed.")
        args.account_id = destination["account_id"]
        args.region = destination["region"]
        args.name = destination["label"]
    args.org = args.org or common.organization_context(None, api)

    if not args.account_id or not args.org:
        raise CliError("Use --account and --org, or --resume with a downloaded setup directory.")
    if not re.fullmatch(r"[0-9]{12}", args.account_id):
        raise CliError("--account must contain exactly 12 digits.")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", args.region):
        raise CliError("Invalid AWS region.")
    org = organization(api, args.org)
    if not args.name:
        slug = re.sub(r"[^a-z0-9]+", "-", org["name"].lower()).strip("-")[:30]
        suffix = hashlib.sha256(org["id"].encode()).hexdigest()[:8]
        args.name = f"bedrock-{slug}-{suffix}"
    if not args.destination and not re.fullmatch(r"[A-Za-z0-9-]{1,54}", args.name):
        raise CliError("--name must contain 1–54 letters, numbers or hyphens.")
    destination = destination or find_prepared(api, org, args)
    args.scope = "user" if args.user else "team" if args.team else "org"
    scope = scope_for(api, args, org)
    if args.resume and scope != args.saved_scope:
        raise CliError("The saved routing scope no longer matches ADP. Download the setup again.")
    previous = current_mapping(api, scope)
    aws = None
    if not args.output_dir and not args.resume and not args.destination:
        aws = Aws(args.aws_profile, args.region)
        aws.check_account(args.account_id)
    applies_to = f"organization {org['name']}"
    if args.team:
        applies_to = f"team {args.team} in {org['name']}"
    if args.user:
        applies_to = f"user {args.user}"
    plan = {
        "action": "download" if args.output_dir else "verify_and_assign" if args.resume else "connect",
        "account_id": args.account_id,
        "role_name": "ADP-Agent-" + args.name,
        "region": args.region,
        "destination_id": destination["id"] if destination else None,
        "organization_id": org["id"],
        "applies_to": applies_to,
        "scope": scope,
        "previous_destination_id": previous,
        "dry_run": args.dry_run,
    }
    if args.dry_run:
        return plan
    confirm(args, plan)
    directory = private_directory(args.output_dir) if args.output_dir else None
    if directory and (directory / "destination.json").exists():
        saved = json.loads((directory / "destination.json").read_text())
        if saved.get("gateway_url") != api.base or not destination or saved.get("destination_id") != destination["id"]:
            raise CliError("That directory contains another destination's setup. Choose a different --download directory.")
    destination = register(api, org, args, destination)
    if directory:
        setup, files = setup_package(api, destination)
        metadata = {
            "gateway_url": api.base,
            # Which deployment this handoff belongs to (Issue #5413).
            **common.deployment_stamp(),
            "destination_id": destination["id"],
            "account_id": setup["account_id"],
            "role_arn": setup["role_arn"],
            "region": setup["region"],
            "organization_id": org["id"],
            "scope": scope,
        }
        files["destination.json"] = (json.dumps(metadata, indent=2) + "\n").encode()
        write_files(directory, files)
        return {**plan, "destination_id": destination["id"], "output_dir": str(directory), "assigned": False}
    destination = provision(api, aws, destination) if aws else verify(api, destination["id"])
    mapping = assign(api, destination, scope, previous)
    return {**plan, "destination_id": destination["id"], "verified": True, "assigned": True, "mapping": mapping}


def result_envelope(result, command):
    status = "verified" if result.get("verified") else "pending" if result.get("output_dir") else "configured"
    next_action = None
    if result.get("output_dir"):
        import shlex

        next_action = (
            "Ask your AWS administrator to apply the downloaded template, then run "
            f"adp admin bedrock connect --resume {shlex.quote(result['output_dir'])} --yes."
        )
    return common.envelope(status, "admin bedrock " + command, result, next_action)


def display(result, as_json, command="connect"):
    if as_json:
        print(json.dumps(result_envelope(result, command)))
    elif "effective" in result:
        route = result["effective"]
        print(f"Bedrock account: {route.get('account_id') or 'platform default (account not reported)'}")
        print(f"Rule: {route['rung']} | Destination: {route.get('destination_label') or 'platform default'}")
        print("Applies to personal and user-owned cloud calls. This is the configured route; no inference was run.")
    elif command == "verify":
        print(f"Verified destination {result['destination_id']} in AWS account {result['account_id']}. No routing rule was assigned.")
    elif "destinations" in result:
        for row in result["destinations"]:
            status = "verified" if row["usable_for_routing"] else "pending verification"
            print(f"{row['label']}  {row['account_id']}  {status}  ({row['id']})")
        if not result["destinations"]:
            print("No destinations yet. Use adp admin bedrock connect to add one.")
    elif result["dry_run"]:
        print(f"Would {result['action'].replace('_', ' ')} AWS account {result['account_id']} for {result['applies_to']}.")
        print(f"Role: {result['role_name']} ({result['region']}). No changes made.")
        if result["previous_destination_id"]:
            print(f"Current rule: {result['previous_destination_id']}")
    elif result.get("output_dir"):
        import shlex

        print(f"Setup downloaded to {result['output_dir']}. Give template.yaml, parameters.json and README.md to your AWS administrator.")
        print("Once the role is created, run:")
        print(f"  adp admin bedrock connect --resume {shlex.quote(result['output_dir'])}")
    else:
        print(f"Connected AWS account {result['account_id']} to {result['applies_to']}.")
        print("Routing applies within about a minute. User rules take priority over team and organization rules.")


NAME = "bedrock"
TITLE = "Model access"
ORDER = 10


def status(ctx):
    """Read the canonical organization mapping; never mistake saved state for proof."""
    api = ctx["api"]
    if not ctx.get("org"):
        return common.envelope("pending", "admin bedrock", next_action="Select an ADP organization with adp admin setup --org NAME.")
    org = organization(api, ctx["org"])
    args = parser().parse_args(["connect", "--org", org["id"]])
    args.scope = "org"
    scope = scope_for(api, args, org)
    selected = current_mapping(api, scope)
    if selected:
        destination = destination_by_id(api, selected)
        if destination["usable_for_routing"]:
            return common.envelope(
                "configured",
                "admin bedrock",
                {
                    "account_id": destination["account_id"],
                    "destination_id": selected,
                    "scope": scope,
                    "verification": "stored_destination_verification",
                },
            )
    return common.envelope(
        "pending",
        "admin bedrock",
        {"organization_id": org["id"]},
        "Run adp admin bedrock connect --org " + org["id"] + " --account ACCOUNT, or select an existing --destination ID.",
    )


def configure(ctx):
    """The provider owns prompts and handoff state; the wizard only orchestrates."""
    current = status(ctx)
    if current["status"] != "pending" or not ctx.get("interactive") or not ctx.get("org"):
        return current
    api = ctx["api"]
    org = organization(api, ctx["org"])
    saved = common.read_state(NAME)
    if saved.get("gateway_url") == api.base and saved.get("org_id") == org["id"] and saved.get("download_dir"):
        print("A downloaded setup is pending. Has the AWS administrator applied it? [y/N] ", end="", file=sys.stderr)
        if input().strip().lower() == "y":
            args = parser().parse_args(["connect", "--resume", saved["download_dir"], "--yes"])
            return result_envelope(run(args, api), "connect")
        return common.envelope("pending", "admin bedrock", next_action="Complete the saved AWS handoff, then rerun adp admin setup.")
    available = [row for row in destinations(api) if row["owner_org_id"] in {None, org["id"]}]
    if available:
        print("Existing destinations:", file=sys.stderr)
        for row in available:
            print(f"  {row['id']}  {row['label']}  {row['account_id']}", file=sys.stderr)
        print("Destination ID to reuse (Enter to connect a new account): ", end="", file=sys.stderr)
        chosen = input().strip()
        if chosen:
            args = parser().parse_args(["connect", "--destination", chosen, "--org", org["id"]])
            return result_envelope(run(args, api), "connect")
    print("AWS account ID (or press Enter to skip): ", end="", file=sys.stderr)
    account = input().strip()
    if not account:
        return current
    print("Create the role here, or download for an AWS administrator? [create/download]: ", end="", file=sys.stderr)
    mode = input().strip().lower()
    argv = ["connect", "--account", account, "--org", org["id"]]
    if mode == "download":
        print("Private download directory: ", end="", file=sys.stderr)
        directory = str(Path(input().strip()).expanduser().absolute())
        argv += ["--download", directory]
    elif mode == "create":
        print("AWS profile (Enter for current credentials): ", end="", file=sys.stderr)
        profile = input().strip()
        if profile:
            argv += ["--profile", profile]
    else:
        raise CliError("Choose create or download, then retry setup.")
    args = parser().parse_args(argv)
    result = run(args, api)
    if result.get("output_dir"):
        common.write_state(NAME, {"gateway_url": api.base, "org_id": org["id"], "download_dir": result["output_dir"]})
    return result_envelope(result, "connect")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    try:
        args = parser().parse_args(argv)
        result = run(args, Api())
        display(result, args.json, args.command)
        return 4 if result.get("output_dir") else 0
    except (CliError, OSError, ValueError, KeyError, TypeError) as exc:
        return common.report_error(exc, "admin bedrock", as_json)
    except KeyboardInterrupt:
        return common.report_error(
            CliError("Interrupted. A stack may still be running; rerun the same command to resume.", "interrupted", 130), "admin bedrock", as_json
        )


if __name__ == "__main__":
    sys.exit(main())
