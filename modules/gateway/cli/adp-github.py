#!/usr/bin/env python3
"""Connect an authorized GitHub repository to ADP as an ordinary user.

Standard library only. Reuses the platform's existing GitHub App: an ordinary
user is never asked to create an App or supply its private key, client secret or
webhook secret. Tokens stay in memory, owned by the shared transport.

Repository selection happens on GitHub's own installation screen, inside the
user's browser — ADP cannot choose it for them. So ``--repo`` is the *requested*
target, never proof of anything, and this helper verifies access to that exact
repository afterwards against the live repository list.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common  # noqa: E402

# Shared auth, refresh, URL validation, error mapping and output belong to CLI-00.
Api = common.Api
CliError = common.CliError

NAME = "github"
CONNECTIONS = "/admin/connections"
INSTALL_START = CONNECTIONS + "/github/install-start"

# owner/name as GitHub accepts it. Rejecting the shape locally is a UX nicety;
# authorization is always the server's decision.
_SEGMENT = r"[A-Za-z0-9._-]{1,100}"
_REPO = re.compile(rf"^({_SEGMENT})/({_SEGMENT})$")


def parse_repo(value):
    """Split ``owner/name``, rejecting anything that is not exactly that."""
    match = _REPO.match((value or "").strip())
    if not match:
        raise CliError("Use --repo owner/name, for example --repo SOPHOS-IT/project.", "usage_error", 1)
    return match.group(1), match.group(2)


def platform_app_missing(exc):
    """Whether a failure means this deployment has no GitHub App configured yet.

    install-start answers 503 with a prose detail (not a reason code) when the
    App slug cannot be resolved, so the HTTP status is the only signal available
    to a client. The shared transport puts it in the message verbatim and does
    not expose it as a field; matching the message keeps this story out of a
    shared file that another story owns (see the PR's coordination note).
    """
    return "HTTP 503" in str(exc)


def connections(api, org=None):
    """Every GitHub connection the SERVER decided this caller may see.

    ``org`` only filters that already-authorized set. It cannot widen it, and it
    cannot move an installation between tenants — those APIs take no tenant
    argument at all, and the server scopes the response to the caller's own
    memberships.
    """
    rows = api.request("GET", CONNECTIONS).get("connections") or []
    if org:
        rows = [row for row in rows if org in {row.get("tenant_id"), row.get("tenant_name")}]
    return rows


def repositories_of(connection):
    return [name for name in (connection.get("repositories") or []) if isinstance(name, str)]


def grants_repo(connection, owner, repo):
    """Whether this installation's repository list contains that exact repository.

    Compared case-insensitively because GitHub treats owner and repository names
    that way. A *different* repository in the list never satisfies the request.
    """
    wanted = f"{owner}/{repo}".casefold()
    return any(name.casefold() == wanted for name in repositories_of(connection))


def repositories_proven(connection):
    """Tri-state: was the repository list read live from GitHub just now?

    ``None`` means the gateway could not reach GitHub and served its stored
    snapshot instead. A snapshot is configuration, not proof — so it must not be
    reported as verified access (issue #5184; the field is #4016's convention).
    """
    verification = connection.get("verification") or {}
    return verification.get("repositories_live")


def owner_matches(connection, owner):
    return (connection.get("account_login") or "").casefold() == owner.casefold()


def detail_of(connection, owner=None, repo=None):
    """Stable, scriptable, secret-free description of one connection."""
    detail = {
        "installation_id": connection.get("installation_id"),
        "account_login": connection.get("account_login"),
        "account_type": connection.get("account_type"),
        "repository_selection": connection.get("repository_selection"),
        "repository_count": connection.get("repository_count"),
        "repositories_verified_live": repositories_proven(connection),
        "can_manage": connection.get("can_manage"),
        "tenant_id": connection.get("tenant_id"),
        "manage_url": connection.get("manage_url") or connection.get("configure_url"),
    }
    if owner and repo:
        detail["requested_repository"] = f"{owner}/{repo}"
        detail["repository_access"] = repository_access(connection, owner, repo)
    return detail


def repository_access(connection, owner, repo):
    """How well access to the requested repository is established.

    ``verified``   — a live read from GitHub lists this exact repository.
    ``unproven``   — it is listed, but only in a stored snapshot.
    ``not_granted`` — a live read does not list it.
    ``unknown``    — could not be determined either way.
    """
    listed, live = grants_repo(connection, owner, repo), repositories_proven(connection)
    if listed:
        return "verified" if live else "unproven"
    return "not_granted" if live else "unknown"


def agent_integration(connection):
    """Whether the tenant plumbing an agent run needs is in place.

    Deliberately NOT called "ready": these are configuration checks. Nothing
    here observes an agent actually running, so this must never be reported as a
    passing agent run.
    """
    verification = connection.get("verification") or {}
    checks = {
        "tenant_credentials_seeded": verification.get("tenant_secret_seeded"),
        "webhook_routing_row": verification.get("identity_index_row"),
        "platform_record_present": verification.get("record_present"),
    }
    if any(value is False for value in checks.values()):
        state = "incomplete"
    elif all(value is True for value in checks.values()):
        state = "configured"
    else:
        state = "unknown"
    return {
        "state": state,
        "checks": checks,
        "agent_run_observed": False,
        "note": "Configuration only. No agent run was executed, so this does not prove an agent can complete work.",
    }


def pending_request(api, owner, repo):
    """The saved request for this repository on this gateway, if any.

    A hint for resuming and for reporting the pending target — never proof, and
    never a credential.
    """
    saved = common.read_state(NAME)
    if saved.get("gateway_url") == api.base and saved.get("requested_repository") == f"{owner}/{repo}":
        return saved
    return {}


def save_request(api, owner, repo, install_url):
    common.write_state(
        NAME,
        {
            "gateway_url": api.base,
            "requested_repository": f"{owner}/{repo}",
            "install_url": install_url,
            "awaiting": "github_installation",
        },
    )


def clear_request(api, owner, repo):
    if pending_request(api, owner, repo):
        common.write_state(NAME, {})


def approval_next_action(owner, repo, install_url):
    return (
        f"Complete the installation for {owner}/{repo} at {install_url} and select that repository. "
        f"If {owner} is an organization you do not own, GitHub sends your request to its owners for approval. "
        f"Rerun adp github connect --repo {owner}/{repo} (or adp github status --repo {owner}/{repo}) to resume."
    )


def connect(args, api):
    owner, repo = parse_repo(args.repo)
    existing = connections(api, args.org)

    # Reuse first: an installation that already grants this exact repository is
    # the answer. Starting a second install would create a duplicate for no gain.
    for connection in existing:
        if grants_repo(connection, owner, repo):
            clear_request(api, owner, repo)
            detail = detail_of(connection, owner, repo)
            detail["reused_existing_installation"] = True
            if detail["repository_access"] == "verified":
                return common.envelope("verified", "github connect", detail)
            return common.envelope(
                "configured",
                "github connect",
                detail,
                f"{owner}/{repo} is recorded on installation {connection.get('installation_id')}, but ADP could not "
                "confirm it with GitHub just now. Rerun adp github status --repo "
                f"{owner}/{repo} to confirm before relying on it.",
            )

    # An installation on the right owner that does not include this repository is
    # a repository-selection problem, not a new install. Send them to GitHub's
    # repository management page for the installation they already authorized.
    for connection in existing:
        if owner_matches(connection, owner):
            detail = detail_of(connection, owner, repo)
            detail["reused_existing_installation"] = True
            if args.dry_run:
                return common.envelope("pending", "github connect", detail, "Would ask GitHub to add the repository to this existing installation.")
            manage_url = detail["manage_url"]
            if args.no_browser:
                print(manage_url, file=sys.stderr)
            else:
                common.open_browser(manage_url)
            return common.envelope(
                "pending",
                "github connect",
                detail,
                f"Add {owner}/{repo} to the existing ADP installation at {manage_url}, then rerun "
                f"adp github connect --repo {owner}/{repo}. "
                f"If you cannot change it, an owner of {owner} must.",
            )

    if args.dry_run:
        return common.envelope(
            "pending",
            "github connect",
            {"requested_repository": f"{owner}/{repo}"},
            "Would start a GitHub App installation using this deployment's existing app. No app would be created.",
        )

    saved = pending_request(api, owner, repo)
    try:
        started = api.request("POST", INSTALL_START, {})
    except CliError as exc:
        if platform_app_missing(exc):
            return unavailable_envelope("github connect", owner, repo)
        # A previously saved request is still the resumable state; the transport
        # already explained the failure.
        if saved.get("install_url"):
            return common.envelope(
                "pending",
                "github connect",
                {"requested_repository": f"{owner}/{repo}"},
                approval_next_action(owner, repo, saved["install_url"]),
            )
        raise

    install_url = started.get("install_url") or ""
    if not install_url:
        raise CliError("ADP did not return a GitHub installation URL. Nothing was installed.", "invalid_response")
    save_request(api, owner, repo, install_url)

    # The URL is printed before any browser attempt, so an SSH session or a
    # headless machine can still finish by hand. Never wait for a callback that
    # cannot arrive here — GitHub redirects a BROWSER, not this process.
    if args.no_browser:
        print(install_url, file=sys.stderr)
    else:
        common.open_browser(install_url)

    return common.envelope(
        "pending",
        "github connect",
        {"requested_repository": f"{owner}/{repo}", "install_url": install_url},
        approval_next_action(owner, repo, install_url),
    )


def unavailable_envelope(command, owner=None, repo=None):
    detail = {"requested_repository": f"{owner}/{repo}"} if owner and repo else {}
    detail["platform_github_app"] = "not_configured"
    return common.envelope(
        "unavailable",
        command,
        detail,
        "This ADP deployment has no GitHub App yet. An ADP platform administrator must set one up "
        "(Settings > Connections, or adp admin github). Ordinary users cannot and should not create it — "
        "it needs the platform's own app credentials.",
    )


def status(args, api):
    owner = repo = None
    if args.repo:
        owner, repo = parse_repo(args.repo)

    # A successful authenticated read is what proves the session works. A local
    # token file only proves a file exists.
    rows = connections(api, args.org)
    detail = {"signed_in": True, "connection_count": len(rows)}

    if not owner:
        detail["connections"] = [detail_of(connection) for connection in rows]
        detail["agent_integration"] = [
            {"installation_id": connection.get("installation_id"), **agent_integration(connection)} for connection in rows
        ]
        if not rows:
            return common.envelope(
                "pending",
                "github status",
                detail,
                "No repository is connected yet. Run adp github connect --repo owner/name.",
            )
        return common.envelope("configured", "github status", detail)

    detail["requested_repository"] = f"{owner}/{repo}"
    for connection in rows:
        if grants_repo(connection, owner, repo):
            detail.update(detail_of(connection, owner, repo))
            detail["agent_integration"] = agent_integration(connection)
            if detail["repository_access"] == "verified":
                return common.envelope("verified", "github status", detail)
            return common.envelope(
                "configured",
                "github status",
                detail,
                f"ADP could not confirm {owner}/{repo} with GitHub just now; this is its last recorded state. Retry shortly.",
            )

    for connection in rows:
        if owner_matches(connection, owner):
            detail.update(detail_of(connection, owner, repo))
            detail["agent_integration"] = agent_integration(connection)
            return common.envelope(
                "pending",
                "github status",
                detail,
                f"{owner} is connected, but {owner}/{repo} is not among its authorized repositories. "
                f"Add it at {detail['manage_url']}, or run adp github connect --repo {owner}/{repo}.",
            )

    saved = pending_request(api, owner, repo)
    if saved.get("install_url"):
        return common.envelope("pending", "github status", detail, approval_next_action(owner, repo, saved["install_url"]))
    return common.envelope(
        "pending",
        "github status",
        detail,
        f"{owner}/{repo} is not connected. Run adp github connect --repo {owner}/{repo}.",
    )


def parser():
    root = common.Parser(prog="adp github", description="Connect an authorized GitHub repository to ADP.")
    commands = root.add_subparsers(dest="command", required=True)

    connect_command = commands.add_parser("connect", help="Connect a repository using this deployment's existing GitHub App")
    connect_command.add_argument("--repo", required=True, metavar="OWNER/NAME", help="The repository to connect, for example SOPHOS-IT/project")
    connect_command.add_argument("--org", help="Filter to one ADP organization you already belong to (never widens access)")
    connect_command.add_argument("--no-browser", action="store_true", help="Print the approval URL instead of opening a browser")
    connect_command.add_argument("--dry-run", action="store_true", help="Show what would happen without changing anything")
    connect_command.add_argument("--yes", action="store_true", help="Approve without a prompt, for scripts")
    connect_command.add_argument("--json", action="store_true", help="Print machine-readable output")

    status_command = commands.add_parser("status", help="Show sign-in, repository access and agent integration state")
    status_command.add_argument("--repo", metavar="OWNER/NAME", help="Report on one repository")
    status_command.add_argument("--org", help="Filter to one ADP organization you already belong to")
    status_command.add_argument("--json", action="store_true", help="Print machine-readable output")
    return root


def run(args, api):
    return connect(args, api) if args.command == "connect" else status(args, api)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    command = "github " + (argv[0] if argv and not argv[0].startswith("-") else "status")
    try:
        args = parser().parse_args(argv)
        return common.emit(run(args, Api()), args.json)
    except (CliError, OSError, ValueError, KeyError, TypeError) as exc:
        return common.report_error(exc, command, as_json)
    except KeyboardInterrupt:
        return common.report_error(
            CliError("Interrupted. Nothing was installed by this command; rerun it to resume.", "interrupted", 130),
            command,
            as_json,
        )


if __name__ == "__main__":
    sys.exit(main())
