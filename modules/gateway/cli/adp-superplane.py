#!/usr/bin/env python3
"""Superplane operational commands, reached as `adp superplane <verb>` (Issue #5039).

Standard library only, like every other `adp` area helper. The point of this file
is what it does NOT have: a credential store. The upstream Superplane CLI wrote a
plaintext ``token`` into ``~/.superplane/config.yaml``, so a developer laptop held
a second long-lived secret outside ADP's vault and outside its revocation path.
Here the only credential path is the ADP session that ``adp login`` already
established — ``adp_common.access_token()`` fetches it per request and nothing is
written to disk by this helper except the *non-secret* current-workspace pointer.

Three treatments, deliberately not uniform:

* **reused** — account, aws-onboard, node, quota, workspace, cost, events, deploy
  keep their upstream request shapes but run against the domain API through the
  shared ADP transport, which authorizes server-side.
* **redirected** — org and user perform NO administration. ADP owns organization,
  user and SSO administration; porting those verbs would rebuild the second
  administration surface the design retires. They report where to go instead.
* **replaced** — the upstream ``vault`` commands become ``provider``, which posts
  the value to ADP's vault API and keeps only the returned credential *id* as
  domain metadata.

Provider values are read from a hidden prompt or explicit stdin. ``--api-key
VALUE`` is refused: a secret in argv lands in shell history and in every process
listing on the machine. Request bodies are never traced for the same reason.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common  # noqa: E402

Api = common.Api
CliError = common.CliError

NAME = "superplane"

# The domain API is mounted behind the same gateway as every other ADP API, so
# the ADP session authorizes it and this helper needs no endpoint of its own.
#
# This is the base the design proposes — "/api/superplane/v1 on the existing ADP
# origin" (design §7 line 448) — expressed relative to the /api that
# adp_common.gateway_url() already appends. The earlier "/domain-apps/superplane"
# matched neither the design nor anything mounted in this repo (Issue #5039).
#
# No route serves this prefix yet: modules/domain-apps/superplane/ deliberately
# has no api/ directory, because domain routes live beside the Superplane API
# upstream (design §3 line 171, enforced by
# tests/features/test_superplane_registration.py::test_no_api_directory). Every
# path below is therefore a PROPOSED contract, as design §7 line 452 states of
# its own table — the first live call is the first confirmation.
API_BASE = "/superplane/v1"

# ADP's own vault. Provider values go here and nowhere else; the response carries
# an id, never the value and never a Secrets Manager ARN.
VAULT_CREDENTIALS = "/auth/credentials"

# Non-secret CLI state: which workspace the operational verbs default to. Lives
# in ADP's private state directory (0600, owner-checked by adp_common) rather
# than a new dotfile, and holds no token — that is the whole point of this unit.
STATE = "superplane"

# Argument names that would put a secret in argv. Checked before argparse so the
# error names the real problem instead of "unrecognized arguments".
SECRET_FLAGS = ("--api-key", "--token", "--secret", "--oauth-token", "--password")

# Upstream `account onboard --provider aws` created five IRSA roles and a
# cross-account SQS ingest role, and registered their ARNs (reference
# commands/aws_onboard.py). Those two responsibilities have NO successor stated in
# the design note or the accepted requirements, and nothing in ADP creates them
# today. Reporting them is not pedantry: an operator who reads "ok" from this verb
# would otherwise assume a complete onboarding, and the missing pieces only
# surface when a workload cannot assume its service-account role.
#
# Named here rather than silently dropped, so `aws-onboard register` states what
# it did NOT do. Resolving them is contract work outside this unit.
ONBOARDING_UNRESOLVED = [
    "IRSA service-account roles (upstream created 5) have no stated owner; the "
    "upstream trust policy was itself a placeholder pending an EKS OIDC provider.",
    "The cross-account SQS ingest role (upstream SuperplaneIngestAccess) has no stated owner.",
    "ADP's connect role grants ReadOnlyAccess, which does not cover the EKS/EC2/IAM "
    "actions the upstream control-plane role held. Whether the domain API needs a "
    "wider grant is unresolved.",
]

# Verbs ADP owns. Listing them flat alongside the operational verbs would imply
# this CLI administers organizations and users, which it must not.
REDIRECTED = {
    "org": (
        "/settings/organization",
        "Organization, quota-policy and SSO administration is ADP's, not this CLI's.",
    ),
    "user": (
        "/settings/users",
        "User, invitation and role administration is ADP's, not this CLI's.",
    ),
}

CANCELLED = (
    "Cancelled locally. This did NOT cancel work already accepted by the domain API "
    "and did NOT release any provider resource — check 'adp superplane events' and "
    "your provider console before assuming anything stopped or stopped billing."
)


def progress(message):
    """Progress and warnings go to stderr, so stdout stays parseable."""
    print(message, file=sys.stderr)


class LazyApi:
    """Resolve the gateway on the FIRST request, not at startup.

    Some verbs are purely local — `workspace use` only records which workspace
    later commands default to. Building the transport eagerly made those verbs
    demand a configured gateway and fail with "reinstall the CLI", which is both
    wrong and unactionable. It also masked argument errors: a missing workspace
    selection reported a gateway problem instead of the usage error it is.
    """

    def __init__(self):
        self._api = None

    def request(self, method, path, body=None, **kwargs):
        if self._api is None:
            self._api = Api()
        return self._api.request(method, path, body, **kwargs)


def segment(value):
    """One path segment, matching adp-aws.py and adp-bedrock.py.

    `safe=""` rather than quote()'s default `safe="/"`: a name carrying a slash
    must not be able to add a path segment. Every value here is trusted CLI input,
    so this is the house pattern rather than a fix.
    """
    return urllib.parse.quote(value, safe="")


def query(path, params):
    """Append a query string, dropping unset values."""
    pairs = {key: value for key, value in params.items() if value is not None}
    return path + ("?" + urllib.parse.urlencode(pairs) if pairs else "")


def reject_secret_arguments(argv):
    """Refuse a provider value supplied on the command line.

    Rejected rather than accepted-and-warned: by the time a warning prints, the
    value is already in the shell's history file and was already visible in the
    process list to every other user on the machine. There is no way to un-leak
    it, so the only safe answer is to not accept it at all.
    """
    for argument in argv:
        name = argument.split("=", 1)[0]
        if name in SECRET_FLAGS:
            raise CliError(
                f"{name} on the command line would leak the value into your shell history and "
                "process list. Omit it to be prompted, or pipe the value in with --stdin.",
                "secret_in_argv",
                1,
            )


def current_workspace(explicit=None):
    """The workspace an operational verb applies to."""
    workspace = explicit or common.read_state(STATE).get("workspace")
    if not workspace:
        raise CliError(
            "No workspace selected. Run adp superplane workspace use <name>, or pass --workspace.",
            "workspace_not_selected",
            1,
        )
    return workspace


def read_provider_value(from_stdin, prompt):
    """Read a provider secret from an explicit pipe or a hidden prompt."""
    if from_stdin:
        # read(), not readline(): --type config_file takes a multi-line JSON key,
        # and readline() sent the vault just "{" while still reporting ok
        # (Issue #5039). Strip only the trailing newline the shell/heredoc adds
        # — interior newlines are part of the credential, and leading whitespace
        # can be significant (e.g. an indented PEM block), so only the trailing
        # run of newlines goes.
        value = sys.stdin.read().rstrip("\r\n")
    else:
        if not sys.stdin.isatty():
            raise CliError(
                "No terminal available for a hidden prompt. Pipe the value in with --stdin.",
                "usage_error",
                1,
            )
        value = getpass.getpass(f"{prompt}: ").strip()
    # Checked on the stripped form so a pipe carrying only blank lines is still
    # "empty", but the value RETURNED keeps its interior and leading whitespace.
    if not value.strip():
        raise CliError("No value supplied; nothing was stored.", "usage_error", 1)
    return value


# --- reused: workspace -------------------------------------------------------


def workspace(args, api):
    if args.subcommand == "use":
        # Purely local, and deliberately not validated against the API: selecting
        # a context is not an authorization decision. The next call carries the
        # session and the server decides.
        common.write_state(STATE, dict(common.read_state(STATE), workspace=args.name))
        return common.envelope("ok", "superplane workspace use", {"workspace": args.name}, "adp superplane workspace describe")

    if args.subcommand == "create":
        if args.isolation == "research" and not args.account:
            raise CliError("Research workspaces need --account <name>.", "usage_error", 1)
        body = {"name": args.name, "isolation_mode": args.isolation}
        for key, value in (
            ("account", args.account),
            ("budget_max_daily_usd", args.budget_daily),
            ("budget_max_gpus", args.budget_gpus),
        ):
            if value is not None:
                body[key] = value
        progress(f"Creating workspace {args.name}...")
        created = api.request("POST", API_BASE + "/workspaces", body)
        return common.envelope("ok", "superplane workspace create", created)

    if args.subcommand == "list":
        result = api.request("GET", API_BASE + "/workspaces")
        return common.envelope("ok", "superplane workspace list", {"workspaces": result.get("workspaces") or []})

    name = current_workspace(getattr(args, "workspace", None))
    if args.subcommand == "kubeconfig":
        result = api.request("GET", f"{API_BASE}/workspaces/{segment(name)}/kubeconfig")
        return common.envelope("ok", "superplane workspace kubeconfig", {"kubeconfig": result.get("kubeconfig", "")})

    result = api.request("GET", f"{API_BASE}/workspaces/{segment(name)}")
    return common.envelope("ok", "superplane workspace describe", result)


# --- reused: node, cost, events, deploy, quota -------------------------------


def node(args, api):
    name = current_workspace(args.workspace)
    result = api.request("GET", f"{API_BASE}/workspaces/{segment(name)}/nodes")
    return common.envelope("ok", "superplane node list", {"nodes": result.get("nodes") or []})


def cost(args, api):
    path = query(API_BASE + "/cost/summary", {"workspace": args.workspace or common.read_state(STATE).get("workspace")})
    return common.envelope("ok", "superplane cost", api.request("GET", path))


def events(args, api):
    path = query(
        API_BASE + "/events",
        {"workspace": args.workspace or common.read_state(STATE).get("workspace"), "limit": args.limit},
    )
    result = api.request("GET", path)
    return common.envelope("ok", "superplane events", {"events": result.get("events") or []})


def deploy(args, api):
    name = current_workspace(getattr(args, "workspace", None))
    base = f"{API_BASE}/workspaces/{segment(name)}/deployments"

    if args.subcommand == "create":
        body = {"model": args.model, "precision": args.precision}
        if args.name:
            body["name"] = args.name
        progress(f"Requesting deployment of {args.model} in {name}...")
        return common.envelope("ok", "superplane deploy create", api.request("POST", base, body))

    if args.subcommand == "delete":
        progress(f"Deleting deployment {args.name}...")
        api.request("DELETE", f"{base}/{segment(args.name)}")
        return common.envelope("ok", "superplane deploy delete", {"deleted": args.name})

    result = api.request("GET", base)
    return common.envelope("ok", "superplane deploy list", {"deployments": result.get("deployments") or []})


def quota(args, api):
    # Org-level quota is a policy decision ADP owns, so this helper only ever
    # addresses a workspace's quota. `org` is redirected for the same reason.
    name = current_workspace(args.workspace)
    path = f"{API_BASE}/workspaces/{segment(name)}/quota"

    if args.subcommand == "show":
        return common.envelope("ok", "superplane quota show", api.request("GET", path))

    body = {}
    for key, value in (
        ("max_gpus", args.max_gpus),
        ("max_cost_per_day", args.max_cost_per_day),
        ("max_nodes", args.max_nodes),
    ):
        if value is not None:
            body[key] = value
    if args.allowed_clouds is not None:
        body["allowed_clouds"] = [item.strip() for item in args.allowed_clouds.split(",") if item.strip()]
    if not body:
        raise CliError(
            "Set at least one of --max-gpus, --max-cost-per-day, --max-nodes or --allowed-clouds.",
            "usage_error",
            1,
        )
    return common.envelope("ok", "superplane quota set", api.request("PATCH", path, body))


# --- reused: account and AWS onboarding --------------------------------------


def account(args, api):
    if args.subcommand == "list":
        result = api.request("GET", API_BASE + "/accounts")
        return common.envelope("ok", "superplane account list", {"accounts": result.get("accounts") or []})

    if args.subcommand == "delete":
        progress(f"Deregistering account {args.account_id}...")
        api.request("DELETE", f"{API_BASE}/accounts/{segment(args.account_id)}")
        return common.envelope("ok", "superplane account delete", {"deregistered": args.account_id})

    body = {"name": args.name, "provider": args.provider}
    if args.account_id:
        body["account_id"] = args.account_id
    progress(f"Registering {args.provider} account {args.name}...")
    registered = api.request("POST", API_BASE + "/accounts", body)
    return common.envelope(
        "ok",
        "superplane account onboard",
        registered,
        "Add its credential with adp superplane provider add.",
    )


def aws_onboard(args, api):
    """Register an AWS account ADP has already connected.

    Upstream `account onboard --provider aws` did two separable things: it CREATED
    AWS resources under the user's own profile with boto3, then REGISTERED the
    resulting identifiers with `POST /accounts`. Only the second half belongs
    here. ADP already owns AWS account connection — `adp aws connect` creates the
    role from a CloudFormation template with a server-generated ExternalId
    (src/auth/aws_connect_routes.py, src/auth/cfn_templates/aws_role_v1.yaml) —
    and design §2 puts credential authorization and binding on the ADP side, so
    re-creating roles here would fork that flow and rebuild a second onboarding
    surface.

    So this verb registers a REFERENCE. Per design §7 lines 487-502 the domain API
    accepts references only, keyed on the vault credential id that
    `adp aws connect` returns; the CLI holds no authority of its own and the
    server authorizes the reference against the caller's session.

    Two responsibilities from the upstream flow have NO established owner and are
    deliberately not reimplemented here — see `unresolved` in the payload and
    ONBOARDING_UNRESOLVED below.
    """
    connection = args.credential_id
    progress(f"Registering AWS account {args.account_id} with Superplane...")
    # Only non-secret metadata and the vault credential id cross over. No role
    # ARN is minted here and no ExternalId is generated here: both belong to the
    # ADP connection this id points at.
    body = {
        "account_id": args.account_id,
        "provider": "aws",
        "vault_credential_id": connection,
    }
    if args.name:
        body["name"] = args.name
    registered = api.request("POST", API_BASE + "/accounts", body)
    return common.envelope(
        "ok",
        "superplane aws-onboard register",
        {**(registered if isinstance(registered, dict) else {}), "unresolved": ONBOARDING_UNRESOLVED},
        "Confirm with adp superplane account list.",
    )


# --- replaced: provider credentials go to ADP's vault ------------------------


def provider_delete(args, api):
    """Remove the domain metadata, then the vault credential it points at.

    The two deletes are separate requests and cannot be atomic, so the only
    honest design is to make the second one RESUMABLE and to report which half
    happened. Order matters: the vault credential is the secret, so it must not
    be left behind silently. Domain metadata goes first because it only ever
    held the id — losing it leaves a real credential the user can still retry
    on, whereas the reverse leaves a domain row pointing at nothing.

    A retry after a partial failure must finish the job, so an already-gone
    domain row (404) is treated as "that half is done" and the vault delete
    still runs. Only the domain 404 is tolerated: any other domain failure, and
    EVERY vault failure, stays visible and non-zero (Issue #5039).
    """
    credential_id = segment(args.credential_id)
    progress(f"Removing provider credential {args.credential_id}...")

    metadata = "deleted"
    try:
        api.request("DELETE", f"{API_BASE}/providers/{credential_id}")
    except CliError as exc:
        if exc.code != "not_found":
            raise
        # Resumes a run that died between the two deletes.
        metadata = "already_absent"
        progress("Domain metadata was already gone; continuing to the vault credential.")

    progress("Deleting the credential from ADP's vault...")
    try:
        api.request("DELETE", f"{VAULT_CREDENTIALS}/{credential_id}")
    except CliError as exc:
        vault = "already_absent" if exc.code == "not_found" else None
        if vault is None:
            # The secret may still exist. Say so, and say what to do about it —
            # the domain row is gone, so `provider list` will no longer show it.
            raise CliError(
                f"Domain metadata for {args.credential_id} was {metadata}, but deleting the vault "
                f"credential failed: {exc}. The stored secret may still exist and will no longer "
                "appear in adp superplane provider list. Retry this command, or remove it with "
                "adp aws/vault credential tooling.",
                "provider_delete_incomplete",
                5,
            ) from None
    else:
        vault = "deleted"

    return common.envelope(
        "ok",
        "superplane provider delete",
        {"deleted": args.credential_id, "domain_metadata": metadata, "vault_credential": vault},
    )


def provider(args, api):
    if args.subcommand == "list":
        result = api.request("GET", API_BASE + "/providers")
        return common.envelope("ok", "superplane provider list", {"providers": result.get("providers") or []})

    if args.subcommand == "delete":
        return provider_delete(args, api)

    value = read_provider_value(args.stdin, f"{args.provider} {args.type.replace('_', ' ')}")

    # Straight to ADP's vault. The value is in this process only for the length
    # of this call, is never written to disk here, and is never logged: the
    # request body is not traced, and only `stored` (metadata) is ever printed.
    progress(f"Storing the {args.provider} credential in ADP's vault...")
    stored = api.request(
        "POST",
        VAULT_CREDENTIALS,
        {
            "service": args.provider,
            "label": args.name,
            "credential_type": args.type,
            "value": value,
            "scope_hint": "user",
        },
    )
    # Drops this function's reference so the value cannot be picked up by anything
    # added below. NOT a memory scrub: CPython keeps the string object alive until
    # it is collected, and nothing here can guarantee otherwise. The real
    # protections are that it never reaches argv, disk, a log, or output.
    del value

    credential_id = stored.get("id")
    if not credential_id:
        raise CliError("ADP's vault did not return a credential id; nothing was registered.", "vault_no_id")

    # Only the id crosses into domain metadata — never the value, never an ARN.
    progress("Registering the credential id with Superplane...")
    registered = api.request(
        "POST",
        API_BASE + "/providers",
        {"name": args.name, "provider": args.provider, "credential_id": credential_id},
    )
    return common.envelope(
        "ok",
        "superplane provider add",
        {"credential_id": credential_id, "name": args.name, "provider": args.provider, "registered": registered},
        "Verify with adp superplane provider list.",
    )


# --- redirected: ADP owns organization and user administration ---------------


def redirect(verb):
    """Report where the administration lives. Performs no administration.

    Returns `unavailable` (exit 4), not `ok`: a script that pipes `adp superplane
    org update ...` must not read a zero exit as "the change was applied".
    """
    path, reason = REDIRECTED[verb]
    return common.envelope(
        "unavailable",
        f"superplane {verb}",
        {"redirected_to": path, "reason": reason, "performed": "nothing"},
        f"Use ADP: {path} in the console, or the ADP admin API. This CLI performs no {verb} administration.",
    )


# --- parsing -----------------------------------------------------------------


def parser():
    root = common.Parser(prog="adp superplane", description="Superplane workloads, using your existing ADP login.")
    commands = root.add_subparsers(dest="command", required=True)

    # --json belongs on the leaf, so `adp superplane workspace list --json` works
    # (a root-only flag would have to precede the subcommand). A shared parent
    # parser is argparse's own idiom for that and keeps one definition.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true", help="Print machine-readable JSON on stdout")

    def leaf(subcommands, name, **kwargs):
        return subcommands.add_parser(name, parents=[shared], **kwargs)

    workspace_command = commands.add_parser("workspace", help="Create, list, select and describe workspaces")
    workspace_subcommands = workspace_command.add_subparsers(dest="subcommand", required=True)
    create_workspace = leaf(workspace_subcommands, "create", help="Create a workspace")
    create_workspace.add_argument("--name", required=True)
    create_workspace.add_argument("--isolation", default="dedicated", choices=("dedicated", "namespace", "research"))
    create_workspace.add_argument("--account", help="AWS account name or id (required for research isolation)")
    create_workspace.add_argument("--budget-daily", type=float, dest="budget_daily", help="Daily spend cap in USD")
    create_workspace.add_argument("--budget-gpus", type=int, dest="budget_gpus", help="Maximum GPUs")
    leaf(workspace_subcommands, "list", help="List workspaces")
    use_workspace = leaf(workspace_subcommands, "use", help="Select the workspace later verbs default to")
    use_workspace.add_argument("name")
    describe_workspace = leaf(workspace_subcommands, "describe", help="Describe the selected workspace")
    describe_workspace.add_argument("--workspace")
    kubeconfig = leaf(workspace_subcommands, "kubeconfig", help="Print kubectl access for the workspace")
    kubeconfig.add_argument("--workspace")

    node_command = commands.add_parser("node", parents=[shared], help="List nodes in a workspace")
    node_command.add_argument("--workspace")

    quota_command = commands.add_parser("quota", help="Show or set a workspace's quota")
    quota_subcommands = quota_command.add_subparsers(dest="subcommand", required=True)
    show_quota = leaf(quota_subcommands, "show", help="Show the workspace quota")
    show_quota.add_argument("--workspace")
    set_quota = leaf(quota_subcommands, "set", help="Set the workspace quota")
    set_quota.add_argument("--workspace")
    set_quota.add_argument("--max-gpus", type=int, dest="max_gpus")
    set_quota.add_argument("--max-cost-per-day", type=float, dest="max_cost_per_day")
    set_quota.add_argument("--max-nodes", type=int, dest="max_nodes")
    set_quota.add_argument("--allowed-clouds", dest="allowed_clouds", help="Comma-separated, for example aws,lambda")

    cost_command = commands.add_parser("cost", parents=[shared], help="Show a cost summary")
    cost_command.add_argument("--workspace")

    events_command = commands.add_parser("events", parents=[shared], help="Show recent events")
    events_command.add_argument("--workspace")
    events_command.add_argument("--limit", type=int, default=50)

    deploy_command = commands.add_parser("deploy", help="Create, list and delete model deployments")
    deploy_subcommands = deploy_command.add_subparsers(dest="subcommand", required=True)
    create_deploy = leaf(deploy_subcommands, "create", help="Deploy a model")
    create_deploy.add_argument("--model", required=True)
    create_deploy.add_argument("--precision", default="fp16", choices=("fp8", "fp16", "bf16"))
    create_deploy.add_argument("--name", help="Deployment name (generated when omitted)")
    create_deploy.add_argument("--workspace")
    list_deploy = leaf(deploy_subcommands, "list", help="List deployments")
    list_deploy.add_argument("--workspace")
    delete_deploy = leaf(deploy_subcommands, "delete", help="Delete a deployment")
    delete_deploy.add_argument("--name", required=True)
    delete_deploy.add_argument("--workspace")

    account_command = commands.add_parser("account", help="Register, list and deregister cloud accounts")
    account_subcommands = account_command.add_subparsers(dest="subcommand", required=True)
    onboard_account = leaf(account_subcommands, "onboard", help="Register a cloud account")
    onboard_account.add_argument("--name", required=True)
    onboard_account.add_argument("--provider", required=True)
    onboard_account.add_argument("--account-id", dest="account_id")
    leaf(account_subcommands, "list", help="List registered accounts")
    delete_account = leaf(account_subcommands, "delete", help="Deregister an account")
    delete_account.add_argument("account_id")

    # No `plan` subcommand: it called a GET /aws/onboarding-plan that exists
    # neither upstream nor in the design's endpoint table (Issue #5039). The
    # connection itself is created by `adp aws connect`, which is where the
    # CloudFormation template and the role already live.
    aws_command = commands.add_parser("aws-onboard", help="Register an AWS account ADP has already connected")
    aws_subcommands = aws_command.add_subparsers(dest="subcommand", required=True)
    register_aws = leaf(aws_subcommands, "register", help="Register an AWS account already connected to ADP")
    register_aws.add_argument("--account-id", dest="account_id", required=True)
    # The vault credential id `adp aws connect` returned — the handle `adp aws`
    # itself uses. Required: registering an account with no reference to a
    # connection is what produced an unverifiable registration before.
    register_aws.add_argument(
        "--credential-id",
        dest="credential_id",
        required=True,
        help="The ADP credential id from adp aws connect (see adp aws list)",
    )
    register_aws.add_argument("--name", help="Account name as Superplane should record it")

    provider_command = commands.add_parser("provider", help="Provider credentials, stored in ADP's vault")
    provider_subcommands = provider_command.add_subparsers(dest="subcommand", required=True)
    add_provider = leaf(provider_subcommands, "add", help="Store a provider credential in ADP's vault (prompted, never on the command line)")
    add_provider.add_argument("--name", required=True, help="Label for this credential")
    add_provider.add_argument("--provider", required=True, help="Provider, for example nebius or lambda")
    add_provider.add_argument(
        "--type",
        default="api_key",
        choices=("api_key", "oauth_token", "bearer", "basic_auth", "config_file"),
        help="Credential type as ADP's vault records it",
    )
    add_provider.add_argument("--stdin", action="store_true", help="Read the value from stdin instead of prompting")
    leaf(provider_subcommands, "list", help="List provider credentials registered with Superplane")
    delete_provider = leaf(provider_subcommands, "delete", help="Remove a provider credential")
    delete_provider.add_argument("credential_id")

    # Listed so the redirect is discoverable in `--help`. Their arguments are
    # never parsed — main() intercepts these verbs first (see there for why).
    for verb in REDIRECTED:
        commands.add_parser(verb, add_help=False, help=f"Redirected: ADP administers {verb}s")

    return root


HANDLERS = {
    "workspace": workspace,
    "node": node,
    "quota": quota,
    "cost": cost,
    "events": events,
    "deploy": deploy,
    "account": account,
    "aws-onboard": aws_onboard,
    "provider": provider,
}


def run(args, api):
    if args.command in REDIRECTED:
        return redirect(args.command)
    return HANDLERS[args.command](args, api)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    command = "superplane " + (argv[0] if argv and not argv[0].startswith("-") else "")
    try:
        # Before argparse: a rejected secret flag must be reported as the leak it
        # is, not as an unrecognized argument.
        reject_secret_arguments(argv)
        # Also before argparse, and before any gateway or token is resolved: a
        # redirected verb performs nothing, so neither its arguments nor the
        # user's session are relevant. Parsing them would mean either rebuilding
        # the upstream org/user flag surface — the administration surface the
        # design retires — or failing valid upstream invocations with a usage
        # error instead of telling the caller where the administration actually
        # lives.
        if argv and argv[0] in REDIRECTED:
            return common.emit(redirect(argv[0]), as_json)
        args = parser().parse_args(argv)
        return common.emit(run(args, LazyApi()), args.json)
    except (CliError, OSError, ValueError, KeyError, TypeError) as exc:
        return common.report_error(exc, command, as_json)
    except (KeyboardInterrupt, EOFError):
        return common.report_error(CliError(CANCELLED, "interrupted", 130), command, as_json)


if __name__ == "__main__":
    sys.exit(main())
