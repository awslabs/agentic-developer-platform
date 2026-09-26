#!/usr/bin/env python3
"""Native administrator login and guided setup. Provider workflows live separately."""

import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common  # noqa: E402

PROVIDERS = [("bedrock", "Model access", "adp-bedrock.py"), ("github", "GitHub App", "adp-github-admin.py")]


def provider_step(provider, action, context, name):
    try:
        result = getattr(provider, action)(context)
        if not isinstance(result, dict) or result.get("status") not in {"configured", "verified", "pending", "failed", "unavailable"}:
            raise ValueError("Invalid provider result")
        return result
    except Exception as exc:
        # Providers must not stop other setup steps or expose raw SDK responses.
        if not isinstance(exc, common.CliError):
            exc = common.CliError("Provider could not complete this step. Update the CLI and retry.", "provider_failed")
        result = common.envelope("failed", f"admin {name}", next_action="Check this provider and rerun adp admin setup.")
        result["error"] = {"code": exc.code, "message": str(exc)}
        return result


def parser():
    root = common.Parser(prog="adp admin", description="Administer this ADP deployment.")
    commands = root.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Sign in with your Cognito administrator account")
    source = login.add_mutually_exclusive_group()
    source.add_argument("--credentials-file", metavar="FILE", help="Private 0600 JSON file for scripted login")
    source.add_argument("--credentials-stdin", action="store_true", help="Read credentials as JSON from standard input")
    login.add_argument("--json", action="store_true")
    setup = commands.add_parser("setup", help="Check and resume first-time administrator setup")
    setup.add_argument("--org", help="ADP organization ID or exact name")
    setup.add_argument("--dry-run", action="store_true", help="Check setup without changing configuration")
    setup.add_argument("--yes", action="store_true", help="Approve provider changes with explicit inputs")
    setup.add_argument("--json", action="store_true")
    if Path(__file__).with_name("adp-bedrock.py").is_file():
        commands.add_parser("bedrock", help="Connect and verify Bedrock destinations; inspect routing")
    if Path(__file__).with_name("adp-github-admin.py").is_file():
        commands.add_parser("github", help="Configure this deployment's GitHub App; check sign-in and repository access")
    for area in ("service-account", "agent", "service-principal"):
        commands.add_parser(area, help="Explicit machine identity lifecycle")
    commands.add_parser("usage", help="Inspect managed usage with --org and explicit UTC bounds")
    return root


def login(args, client):
    credentials = {}
    if args.credentials_file:
        credentials = common.read_private_json(args.credentials_file)
    elif args.credentials_stdin:
        credentials = json.loads(sys.stdin.read(32768))
    elif not sys.stdin.isatty():
        raise common.CliError("Use --credentials-file or --credentials-stdin for non-interactive login.", "authentication_required", 2)

    def value(key, prompt, secret=True):
        if key in credentials:
            result = credentials[key]
        elif sys.stdin.isatty() and not (args.credentials_file or args.credentials_stdin):
            if secret:
                result = getpass.getpass(prompt)
            else:
                print(prompt, end="", file=sys.stderr, flush=True)
                result = input()
        else:
            raise common.CliError(f"Login requires {key}; supply it through protected input and retry.", "authentication_required", 2)
        if not isinstance(result, str) or not result:
            raise common.CliError("Missing login input.", "authentication_required", 2)
        return result

    username = value("username", "Cognito username: ", False)
    result = client.request("POST", "/auth/cli/password", {"username": username, "password": value("password", "Password: ")}, authenticated=False)
    for _ in range(5):
        if "access_token" in result:
            verified = client.request("GET", "/auth/cli/admin-session", token=result["access_token"])
            common.save_session(result)
            return common.envelope("verified", "admin login", verified, "Run adp admin setup.")
        challenge = result.get("challenge")
        if challenge not in {"NEW_PASSWORD_REQUIRED", "SMS_MFA", "SOFTWARE_TOKEN_MFA"}:
            return common.envelope(
                "pending", "admin login", next_action="Complete account verification or MFA enrollment in the browser, then rerun adp admin login."
            )
        if challenge == "NEW_PASSWORD_REQUIRED":
            responses = {"NEW_PASSWORD": value("new_password", "New password: ")}
            for attribute in result.get("required_attributes", []):
                responses[attribute] = value(attribute, f"{attribute.removeprefix('userAttributes.')}: ")
        else:
            key = "SMS_MFA_CODE" if challenge == "SMS_MFA" else "SOFTWARE_TOKEN_MFA_CODE"
            responses = {key: value(key.lower(), "MFA code: ")}
        result = client.request("POST", "/auth/cli/challenge", {"continuation": result["continuation"], "responses": responses}, authenticated=False)
    raise common.CliError("Too many authentication challenges. Restart adp admin login.")


def setup(args, client):
    try:
        actor = client.request("GET", "/auth/cli/admin-session")
    except common.CliError as exc:
        if exc.exit_code != 2 or args.dry_run or args.json or not sys.stdin.isatty():
            raise
        print("Sign in with your Cognito administrator account to continue setup.", file=sys.stderr)
        result = login(parser().parse_args(["login"]), client)
        if result["status"] != "verified":
            return result
        actor = result["detail"]
    context = {
        "api": client,
        "org": args.org or os.environ.get("ADP_ORG") or actor.get("org_id"),
        "interactive": sys.stdin.isatty() and not args.json,
        "yes": args.yes,
        "dry_run": args.dry_run,
    }
    steps = []
    loaded = []
    for name, title, filename in PROVIDERS:
        provider = common.load_provider(filename)
        loaded.append(provider)
        result = (
            provider_step(provider, "status", context, name)
            if provider
            else common.envelope(
                "unavailable", f"admin {name}", next_action=f"{title} setup is not included in this CLI build. Update when that provider is released."
            )
        )
        steps.append({"name": name, "title": title, **result})
    if not args.json:
        for step in steps:
            print(f"{step['title']}: {step['status']}", file=sys.stderr)
    for index, provider in enumerate(loaded):
        if provider and steps[index]["status"] == "pending" and not args.dry_run:
            steps[index].update(provider_step(provider, "configure", context, steps[index]["name"]))
    statuses = {step["status"] for step in steps}
    status = "failed" if "failed" in statuses else "pending" if statuses & {"pending", "unavailable"} else "configured"
    result = common.envelope(
        status, "admin setup", {"steps": steps}, "Resume with adp admin setup after completing the pending actions." if status == "pending" else None
    )
    if not args.dry_run:
        common.write_state("admin", {"gateway_url": client.base, "steps": [{"name": step["name"], "status": step["status"]} for step in steps]})
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    try:
        if argv and argv[0] in {"service-account", "agent", "service-principal"}:
            module = common.load_provider("adp-machine.py")
            if not module:
                raise common.CliError("Machine identity helper is missing. Run adp update.", "provider_unavailable", 4)
            return module.main(argv)
        if argv and argv[0] == "usage":
            module = common.load_provider("adp-usage.py")
            if not module:
                raise common.CliError("Usage helper is missing. Run adp update.", "provider_unavailable", 4)
            return module.main(["admin", "usage", *argv[1:]])
        # Area registration is explicit and only exposed when its helper ships.
        areas = {"bedrock": ("adp-bedrock.py", "Model access setup"), "github": ("adp-github-admin.py", "GitHub App setup")}
        if argv and argv[0] in areas:
            filename, title = areas[argv[0]]
            module = common.load_provider(filename)
            if not module:
                raise common.CliError(f"{title} is not installed. Run adp update.", "provider_unavailable", 4)
            return module.main(argv[1:])
        args = parser().parse_args(argv)
        client = common.Api()
        result = login(args, client) if args.command == "login" else setup(args, client)
        code = common.emit(result, as_json)
        return 0 if getattr(args, "dry_run", False) and code == 4 else code
    except (common.CliError, OSError, ValueError, KeyError, TypeError) as exc:
        return common.report_error(exc, "admin", as_json)
    except (KeyboardInterrupt, EOFError):
        return common.report_error(common.CliError("Interrupted. Run adp admin setup to check progress.", "interrupted", 130), "admin", as_json)


if __name__ == "__main__":
    sys.exit(main())
