#!/usr/bin/env python3
"""GitLab human integration using ADP login and vault credential references."""

from __future__ import annotations

import http.client
import sys
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common

CONTRACT = "gitlab_cli_v1"


def parser():
    root = common.Parser(prog="adp gitlab")
    groups = root.add_subparsers(dest="area", required=True)
    user = groups.add_parser("gitlab").add_subparsers(dest="action", required=True)
    admin = groups.add_parser("admin").add_subparsers(dest="domain", required=True)
    managed = admin.add_parser("gitlab").add_subparsers(dest="action", required=True)
    for group, actions in [(user, ("status", "connect", "disconnect")), (managed, ("status", "configure", "revalidate"))]:
        for action in actions:
            item = group.add_parser(action)
            item.add_argument("--json", action="store_true")
            if group == user or action == "revalidate":
                item.add_argument("--repo", required=action != "status")
            if action in {"connect", "revalidate"} or (group == user and action == "status"):
                item.add_argument(
                    "--credential", required=action in {"connect", "revalidate"}, help="Owned GitLab vault credential ID; never a token"
                )
            if action in {"connect", "disconnect", "configure"}:
                item.add_argument("--operation-id", required=True)
                item.add_argument("--expected-revision", help="Tenant configuration revision from status; omit only for first configuration")
                item.add_argument("--expect-provider-revision", required=True, help="Approved provider revision from status")
                item.add_argument("--dry-run", action="store_true")
                item.add_argument("--yes", action="store_true")
                if action == "configure":
                    item.add_argument("--provider", required=True, help="Deployment-approved provider ID from status")
                else:
                    item.add_argument("--project-id", required=True, type=int, help="Immutable numeric project ID")
    return root


def document(value):
    if not isinstance(value, dict) or value.get("contract") != CONTRACT or not isinstance(value.get("org_id"), str):
        raise common.CliError("GitLab human API unavailable or malformed. Upgrade the gateway and CLI together.", "invalid_response", 5)
    return value


def execute(args, api):
    admin = args.area == "admin"
    command = "adp " + ("admin gitlab " if admin else "gitlab ") + args.action
    base = "/gitlab/admin" if admin else "/gitlab"
    query = {k: v for k, v in {"repo": getattr(args, "repo", None), "credential_id": getattr(args, "credential", None)}.items() if v}
    if args.action in {"status", "revalidate"}:
        value = document(api.request("GET", base + "/" + args.action + ("?" + urlencode(query) if query else "")))
        return common.envelope("ok", command, value, "Verified project access is not proof of SSO, webhook delivery or a successful agent run.")
    try:
        operation = str(UUID(args.operation_id))
    except ValueError:
        raise common.CliError("Use a UUID operation ID and retain it for reconciliation.", "usage_error", 1) from None
    if hasattr(args, "project_id") and args.project_id <= 0:
        raise common.CliError("Project ID must be positive.", "usage_error", 1)
    before = document(api.request("GET", base + "/status"))
    selected_id = args.provider if args.action == "configure" else (before.get("configuration") or {}).get("provider_id")
    matches = [p for p in before.get("providers", []) if isinstance(p, dict) and p.get("id") == selected_id]
    if len(matches) != 1 or matches[0].get("revision") != args.expect_provider_revision:
        raise common.CliError("Approved provider changed or is missing. Read status before retrying.", "conflict", 4)
    body = {"operation_id": operation, "expected_provider_revision": args.expect_provider_revision, "expected_revision": args.expected_revision}
    if args.action == "configure":
        body["provider_id"] = args.provider
    else:
        body.update(repo=args.repo, project_id=args.project_id)
        if args.action == "connect":
            body["credential_id"] = args.credential
    impact = {
        "org_id": before["org_id"],
        "provider": matches[0],
        "request": body,
        "effect": "Change only the managed ADP association. Existing GitLab project, hooks and vault credentials are retained.",
    }
    if args.dry_run:
        return common.envelope("dry_run", command, impact, "Provider ownership and deployment root approval are checked by the server on submission.")
    if not args.yes:
        raise common.CliError("Review --dry-run and pass --yes for this provider/project/revision.", "confirmation_required", 1)
    common.ensure_can_mutate("gitlab.admin.write" if admin else "gitlab.connection.write", request=api.request)
    try:
        result = document(api.request("POST", base + "/" + args.action, body))
        if (
            result.get("org_id") != before["org_id"]
            or result.get("provider_id") != selected_id
            or result.get("operation_id") != operation
            or result.get("action") != args.action
            or result.get("revision") != operation
            or (not admin and (result.get("project_id") != args.project_id or result.get("repo") != args.repo))
        ):
            raise common.CliError("GitLab mutation acknowledgement mismatch.", "invalid_response", 5)
    except common.CliError as exc:
        if exc.exit_code in {2, 3} or exc.status_code in {400, 404, 409, 422}:
            raise
        return common.envelope(
            "pending",
            command,
            {"outcome": "unknown", "operation_id": operation},
            "Inspect status and reconcile the same operation. No automatic retry was sent.",
        )
    except (OSError, http.client.HTTPException, ValueError):
        return common.envelope(
            "pending", command, {"outcome": "unknown", "operation_id": operation}, "Inspect status before retrying the same operation."
        )
    current = result.get("current_revision", operation)
    return common.envelope(
        "ok" if current == operation else "pending",
        command,
        result,
        "Local association state only. Webhook delivery and agent execution require independent live evidence.",
    )


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        return common.emit(execute(args, common.Api()), args.json)
    except (common.CliError, ValueError, TypeError, OSError, http.client.HTTPException) as exc:
        return common.report_error(exc, "adp gitlab", "--json" in argv)
    except KeyboardInterrupt:
        return common.report_error(
            common.CliError("Interrupted. Inspect GitLab status and the original operation before retrying.", "interrupted", 130),
            "adp gitlab",
            "--json" in argv,
        )


if __name__ == "__main__":
    sys.exit(main())
