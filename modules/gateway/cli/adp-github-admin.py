#!/usr/bin/env python3
"""Administrator GitHub App configuration for this ADP deployment.

Standard library only. Creates a new App through GitHub's manifest flow, or
imports one a GitHub administrator created elsewhere. Credentials travel in
protected files or on stdin, never in process arguments, output or saved state.
"""

from __future__ import annotations

import getpass
import html
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common  # noqa: E402

Api = common.Api
CliError = common.CliError

NAME = "github"
TITLE = "GitHub App"
ORDER = 20

APP = "/admin/connections/github/app"
COMMAND = "admin github"

# GitHub's manifest conversion requires a browser form POST to github.com, so the
# CLI stages that one submission locally instead of inventing a second flow.
HANDOFF_FILE = "github-app-manifest.html"
HANDOFF_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Create the ADP GitHub App</title></head>
<body onload="document.forms[0].submit()">
<p>Submitting the GitHub App manifest to {owner}. Approve the App on GitHub.</p>
<form method="post" action="{post_url}">
<input type="hidden" name="manifest" value="{manifest}">
<input type="hidden" name="state" value="{state}">
<button type="submit">Continue to GitHub</button>
</form>
</body></html>
"""


def read_private_text(path):
    """Read a credential file the caller owns privately; never log its content."""
    try:
        descriptor = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise CliError("Could not read that credential file. Give a regular file you own with permissions 0600.", "unsafe_file") from None
    with os.fdopen(descriptor) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise CliError("Credential files must be regular files owned by you with permissions 0600.", "unsafe_file")
        value = source.read(65536).strip()
    if not value:
        raise CliError("That credential file is empty. Nothing was sent to ADP.", "invalid_input", 1)
    return value


def ask(prompt, secret=False):
    if secret:
        return getpass.getpass(prompt).strip()
    print(prompt, end="", file=sys.stderr, flush=True)
    return input().strip()


def flag(value):
    """Report a tri-state check without turning 'unknown' into 'passed'."""
    return "unknown" if value is None else "ok" if value else "mismatch"


def registration(api):
    return api.request("GET", APP + "/status")


def deployment_checks(api):
    """Admin-scoped deployment health, read from the canonical connections list."""
    try:
        listing = api.request("GET", "/admin/connections")
    except CliError as exc:
        if exc.exit_code in {2, 3}:
            raise
        return [], {}
    return listing.get("connections") or [], listing.get("platform_verification") or {}


def readings(api, app):
    """Report sign-in, repository installation and agent integration separately.

    GitHub exposes no API for an App's configured OAuth callback URL, so stored
    credentials are reported as configured and login stays unverified until a
    real OAuth round trip succeeds.
    """
    connections, checks = deployment_checks(api)
    complete = [
        row
        for row in connections
        if all((row.get("verification") or {}).get(key) for key in ("record_present", "tenant_secret_seeded", "identity_index_row"))
    ]
    sign_in = {
        "state": "configured" if app.get("login_enabled") else "missing",
        "credentials_configured": bool(app.get("login_enabled")),
        "login_verified": False,
        "expected_callback_url": checks.get("expected_callback_url"),
        "app_oauth_settings_url": checks.get("app_oauth_settings_url"),
    }
    repositories = {
        "state": "configured" if app.get("install_ready") and complete else "missing",
        "install_ready": bool(app.get("install_ready")),
        "installations": len(connections),
        "complete_installation_records": len(complete),
    }
    integration = {
        "state": "configured"
        if checks.get("webhook_secret") and all(checks.get(key) for key in ("app_webhook_url_matches", "app_permissions_match", "app_events_match"))
        else "missing",
        "webhook_secret": flag(checks.get("webhook_secret")),
        "webhook_url": flag(checks.get("app_webhook_url_matches")),
        "permissions": flag(checks.get("app_permissions_match")),
        "events": flag(checks.get("app_events_match")),
        "warnings": checks.get("app_config_warnings") or [],
    }
    return {"sign_in": sign_in, "repositories": repositories, "agent_integration": integration}


def next_action_for(app, areas):
    if not app.get("registered"):
        return "Run adp admin github setup to create a GitHub App or import an existing one."
    pending = [name.replace("_", " ") for name, area in areas.items() if area["state"] != "configured"]
    if not pending:
        return "Sign in with GitHub once to prove the OAuth callback works; configuration alone does not."
    return (
        f"Incomplete: {', '.join(pending)}. Run adp admin github revalidate after the GitHub App owner "
        "corrects the App settings, and install the App on the repositories you need."
    )


def describe(api):
    """Read-only status; a configured deployment is never reported as verified."""
    app = registration(api)
    detail = {
        "registered": bool(app.get("registered")),
        "app_id": app.get("app_id"),
        "app_slug": app.get("app_slug"),
        "app_owner_type": app.get("owner_type"),
    }
    if not app.get("registered"):
        return common.envelope("pending", COMMAND + " status", detail, next_action_for(app, {}))
    areas = readings(api, app)
    detail.update(areas)
    ready = all(area["state"] == "configured" for area in areas.values())
    return common.envelope("configured" if ready else "pending", COMMAND + " status", detail, next_action_for(app, areas))


def revalidate(api):
    """Re-read the App's live configuration on GitHub; never repairs by rotating."""
    result = api.request("POST", APP + "/revalidate", {})
    detail = {
        "checked": bool(result.get("checked")),
        "webhook_url": flag(result.get("app_webhook_url_matches")),
        "permissions": flag(result.get("app_permissions_match")),
        "events": flag(result.get("app_events_match")),
        "expected_callback_url": result.get("expected_callback_url"),
        "app_oauth_settings_url": result.get("app_oauth_settings_url"),
        "warnings": result.get("warnings") or [],
    }
    ok = result.get("checked") and all(result.get(key) for key in ("app_webhook_url_matches", "app_permissions_match", "app_events_match"))
    return common.envelope(
        "configured" if ok else "pending",
        COMMAND + " revalidate",
        detail,
        None
        if ok
        else "Ask the GitHub App owner to correct the settings above in the App's GitHub page, then rerun adp admin github revalidate. "
        "ADP does not change a shared App's webhook or credentials for you.",
    )


def owner_choice(args, interactive):
    """Keep ADP organization context and GitHub App ownership explicitly separate."""
    if args.owner == "user":
        print("The App will be owned by your personal GitHub account, not an organization.", file=sys.stderr)
        return "user", None
    github_org = args.github_org
    if not github_org and interactive:
        suggestion = f" (ADP organization is {args.org})" if args.org else ""
        print(f"ADP organization context is separate from GitHub App ownership{suggestion}.", file=sys.stderr)
        github_org = ask("GitHub organization that will own the App: ")
    if not github_org:
        raise CliError(
            "Give the GitHub organization with --github-org, or --owner user for a personal App. --org is the ADP organization.", "usage_error", 1
        )
    return "org", github_org


def handoff(api, result, owner):
    """Stage the one browser step GitHub requires, then leave the run resumable."""
    # The selected deployment's state dir, not a hardcoded one (Issue #5413). This
    # page carries a state nonce minted by ONE gateway; writing it to a shared path
    # would let a second deployment's handoff overwrite it, and the user would
    # complete the browser step against a nonce the gateway they are talking to
    # never issued.
    page = common.private_directory(common.state_dir()) / HANDOFF_FILE
    # The page carries a single-use state nonce, so it is created 0600 rather
    # than written first and tightened afterwards.
    descriptor = os.open(page, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as target:
        target.write(
            HANDOFF_PAGE.format(
                owner=html.escape(owner),
                post_url=html.escape(result["post_url"], quote=True),
                manifest=html.escape(json.dumps(result["manifest"]), quote=True),
                state=html.escape(result["state"], quote=True),
            )
        )
    common.write_state(
        NAME, {"gateway_url": api.base, "stage": "awaiting_github_approval", "owner": owner, "app_name": result.get("suggested_app_name")}
    )
    print(f"Open this page and approve the App on GitHub: {page}", file=sys.stderr)
    common.open_browser(page.as_uri())
    return common.envelope(
        "pending",
        COMMAND + " setup",
        {"stage": "awaiting_github_approval", "app_owner": owner, "suggested_app_name": result.get("suggested_app_name"), "approval_page": str(page)},
        f"A GitHub owner of {owner} must approve the App's permissions on GitHub using the page above. "
        "Then rerun adp admin github setup to continue; nothing is created until they approve.",
    )


def create_app(api, args, interactive):
    """Reuse an existing registration; otherwise start the manifest flow once."""
    owner_type, github_org = owner_choice(args, interactive)
    if args.dry_run:
        return common.envelope(
            "pending",
            COMMAND + " setup",
            {"would_create": True, "owner_type": owner_type, "app_owner": github_org or "your personal GitHub account", "app_name": args.app_name},
            "Rerun without --dry-run to generate the App manifest. Nothing was created.",
        )
    # The owner is confirmed out loud: a personal App silently standing in for an
    # organization-owned one is the mistake that is expensive to undo later.
    if interactive and not args.yes:
        target = f"the GitHub organization {github_org}" if github_org else "your personal GitHub account"
        if ask(f"Create a {args.visibility} GitHub App owned by {target}? [y/N] ").lower() != "y":
            return common.envelope(
                "pending", COMMAND + " setup", {"owner_type": owner_type}, "Nothing was created. Rerun setup with the owner you intended."
            )
    body = {"owner_type": owner_type, "org": github_org, "visibility": args.visibility}
    if args.app_name:
        body["app_name"] = args.app_name
    result = api.request("POST", APP + "/register-start", body)
    if result.get("status") == "already_registered":
        print("This deployment already has a GitHub App; reusing it instead of creating another.", file=sys.stderr)
        return describe(api)
    if not (result.get("manifest") and result.get("post_url") and result.get("state")):
        raise CliError("ADP could not prepare a GitHub App manifest. No App was created.", "manifest_unavailable")
    return handoff(api, result, github_org or "your personal GitHub account")


def existing_credentials(args, interactive):
    """Collect an external administrator's App credentials through protected input."""
    supplied = {}
    if args.credentials_file:
        supplied = common.read_private_json(args.credentials_file)
    elif args.credentials_stdin:
        supplied = json.loads(sys.stdin.read(65536))
    elif not interactive:
        raise CliError("Use --credentials-file or --credentials-stdin to import an App without prompts.", "usage_error", 1)
    if not isinstance(supplied, dict):
        raise CliError("Credential input must be a JSON object.", "invalid_input", 1)

    def value(key, prompt, secret=False, required=False):
        result = supplied.get(key, "") if supplied else (ask(prompt, secret) if interactive else "")
        if not isinstance(result, str):
            raise CliError(f"Credential value {key} must be text.", "invalid_input", 1)
        if required and not result:
            raise CliError(f"Importing an existing App requires {key}. Supply it through protected input.", "invalid_input", 1)
        return result

    app_id = value("app_id", "GitHub App ID: ", required=True)
    # The key never travels through a prompt or an argument: it is read from a
    # file the caller owns privately, or from the supplied JSON object.
    key_file = value("private_key_file", "Private key .pem file from the GitHub App owner: ")
    private_key = read_private_text(key_file) if key_file else (supplied.get("private_key") or "")
    if not isinstance(private_key, str):
        raise CliError("Credential value private_key must be text.", "invalid_input", 1)
    if not private_key:
        raise CliError("Importing an existing App requires its private key from a protected 0600 file.", "invalid_input", 1)
    return {
        "app_id": app_id,
        "private_key": private_key,
        "client_id": value("client_id", "OAuth client ID (Enter to leave GitHub sign-in off for now): "),
        "client_secret": value("client_secret", "OAuth client secret: ", secret=True),
        "webhook_secret": value("webhook_secret", "Webhook secret: ", secret=True),
    }


def import_app(api, args, interactive):
    """Import an App another GitHub administrator created, without repointing one in use."""
    credentials = existing_credentials(args, interactive)
    current = registration(api)
    if current.get("registered") and current.get("app_id") and str(current["app_id"]) != credentials["app_id"]:
        raise CliError(
            f"This deployment already uses GitHub App {current['app_id']}; importing {credentials['app_id']} would repoint it and break "
            "its existing consumers. Disconnect the current App deliberately in Settings → Connections first. Nothing was changed.",
            "app_conflict",
        )
    if args.dry_run:
        return common.envelope(
            "pending",
            COMMAND + " setup",
            {"would_import_app_id": credentials["app_id"], "reimport": bool(current.get("registered"))},
            "Rerun without --dry-run to import these credentials. Nothing was changed.",
        )
    result = api.request("POST", APP + "/register-manual", credentials)
    detail = {
        "registered": bool(result.get("registered")),
        "app_id": result.get("app_id"),
        "app_slug": result.get("app_slug"),
        "sign_in_credentials_configured": bool(result.get("login_enabled")),
        "warnings": result.get("warnings") or [],
    }
    if not result.get("registered"):
        raise CliError("ADP did not store the App credentials. Check the App ID and private key, then retry.", "registration_failed")
    if not result.get("login_enabled"):
        return common.envelope(
            "pending",
            COMMAND + " setup",
            detail,
            "GitHub sign-in stays off until the App's OAuth client ID and secret are imported. Ask the GitHub App owner for them, "
            "then rerun adp admin github setup --existing with the same App ID.",
        )
    return common.envelope(
        "configured",
        COMMAND + " setup",
        detail,
        "Credentials are stored, not proven. Sign in with GitHub once, and install the App on the repositories ADP should reach.",
    )


def setup(api, args, interactive):
    """Resume pending setup instead of recreating an App that already exists."""
    current = registration(api)
    mode = "existing" if args.existing else "new" if args.new else None
    if current.get("registered") and not args.existing:
        if mode == "new":
            print(f"GitHub App {current.get('app_id')} is already registered; reporting its status instead of creating another.", file=sys.stderr)
        return describe(api)
    if mode is None:
        if not interactive:
            raise CliError("Choose --new or --existing for a non-interactive setup.", "usage_error", 1)
        saved = common.read_state(NAME)
        if saved.get("gateway_url") == api.base and saved.get("stage") == "awaiting_github_approval":
            print(f"A GitHub App approval for {saved.get('owner')} is still pending.", file=sys.stderr)
        answer = ask("Create a new GitHub App, or connect an existing one? [new/existing]: ").lower()
        if answer not in {"new", "existing"}:
            raise CliError("Answer new or existing, then retry setup.", "usage_error", 1)
        mode = answer
    return import_app(api, args, interactive) if mode == "existing" else create_app(api, args, interactive)


def parser():
    root = common.Parser(prog="adp admin github", description="Configure this deployment's GitHub App: sign-in, webhooks and repository access.")
    commands = root.add_subparsers(dest="command", required=True)

    def shared(sub):
        sub.add_argument("--json", action="store_true", help="Print one JSON result object on stdout")
        return sub

    configure = shared(commands.add_parser("setup", help="Create a GitHub App, or connect one an existing GitHub administrator created"))
    choice = configure.add_mutually_exclusive_group()
    choice.add_argument("--new", action="store_true", help="Create a new GitHub App through GitHub's manifest flow")
    choice.add_argument("--existing", action="store_true", help="Connect an App already created on GitHub")
    configure.add_argument("--github-org", help="GitHub organization that owns the App (not the ADP organization)")
    configure.add_argument(
        "--owner", choices=["org", "user"], default="org", help="Create the App under a GitHub organization or your personal account"
    )
    configure.add_argument("--app-name", help="GitHub App name; GitHub requires it to be unique across all of GitHub")
    configure.add_argument("--visibility", choices=["private", "public"], default="private", help="Who may install the App (default private)")
    source = configure.add_mutually_exclusive_group()
    source.add_argument("--credentials-file", metavar="FILE", help="Private 0600 JSON file holding existing App credentials")
    source.add_argument("--credentials-stdin", action="store_true", help="Read existing App credentials as JSON from standard input")
    configure.add_argument("--org", help="ADP organization ID or exact name")
    configure.add_argument("--dry-run", action="store_true", help="Report what would change without configuring anything")
    configure.add_argument("--yes", action="store_true", help="Skip confirmation prompts; required credentials are still prompted for")
    shared(commands.add_parser("status", help="Report sign-in, repository installation and agent integration separately"))
    shared(commands.add_parser("revalidate", help="Re-read the App's live configuration on GitHub"))
    return root


def run(args, api, interactive=None):
    interactive = sys.stdin.isatty() and not args.json if interactive is None else interactive
    if args.command == "status":
        return describe(api)
    if args.command == "revalidate":
        return revalidate(api)
    return setup(api, args, interactive)


def status(ctx):
    """Wizard provider: read-only, and never prompts."""
    return describe(ctx["api"])


def configure(ctx):
    """Wizard provider: resumable setup owned entirely by this helper."""
    args = parser().parse_args(["setup"])
    args.org = ctx.get("org")
    args.yes = bool(ctx.get("yes"))
    args.dry_run = bool(ctx.get("dry_run"))
    if args.dry_run or not ctx.get("interactive"):
        return describe(ctx["api"])
    return setup(ctx["api"], args, True)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    try:
        args = parser().parse_args(argv)
        result = run(args, Api())
        code = common.emit(result, args.json)
        return 0 if getattr(args, "dry_run", False) and code == 4 else code
    except (CliError, OSError, ValueError, KeyError, TypeError) as exc:
        return common.report_error(exc, COMMAND, as_json)
    except (KeyboardInterrupt, EOFError):
        return common.report_error(
            CliError("Interrupted. Run adp admin github status to check what is configured.", "interrupted", 130), COMMAND, as_json
        )


if __name__ == "__main__":
    sys.exit(main())
