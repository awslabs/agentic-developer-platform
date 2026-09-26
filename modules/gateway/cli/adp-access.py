#!/usr/bin/env python3
"""Review tenant access and revoke applicable gateway token families."""

from __future__ import annotations

import http.client
import sys
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common


def parser():
    root = common.Parser(prog="adp access|admin access-request|admin session", description=__doc__)
    areas = root.add_subparsers(dest="area", required=True)
    for area, actions in (("access", ("status", "request")), ("access-request", ("list", "show", "approve", "deny")), ("session", ("revoke-user",))):
        group = areas.add_parser(area).add_subparsers(dest="action", required=True)
        for action in actions:
            p = group.add_parser(action)
            p.add_argument("--json", action="store_true")
            if area == "access":
                p.add_argument("--tenant", required=True, help="Requested tenant; does not switch the authenticated workspace")
            if action in {"request", "approve", "deny", "revoke-user"}:
                p.add_argument("--yes", action="store_true")
                p.add_argument("--dry-run", action="store_true")
                p.add_argument("--reason", required=True)
            if action in {"show", "approve", "deny"}:
                p.add_argument("--request", required=True)
            if action in {"approve", "deny"}:
                p.add_argument("--expected-revision", required=True)
                p.add_argument("--expected-role", required=True)
                p.add_argument("--expected-scope", choices=("join_existing", "create_new"), required=True)
                p.add_argument("--operation-id", required=True)
            if action == "list":
                p.add_argument("--limit", type=int, default=50)
                p.add_argument("--cursor", default="")
            if action == "revoke-user":
                p.add_argument("--user", required=True)
                p.add_argument("--org", required=True)
                p.add_argument("--expected-revision", help="Revision from --dry-run")
    return root


def identifier(value):
    if not value or len(value) > 255 or any(c in value for c in "/\\\r\n"):
        raise common.CliError("Use an exact target ID.", "usage_error", 1)
    return quote(value, safe="")


def checked(value, fields):
    if not isinstance(value, dict) or any(not isinstance(value.get(f), str) or not value[f] for f in fields):
        raise common.CliError("Server returned incomplete access metadata.", "invalid_response", 5)
    return value


def mutate(client, path, body):
    operation = (
        "access.self.request" if path == "/access/request" else "auth.session.revoke" if path.startswith("/auth/") else "access.managed.decide"
    )
    common.ensure_can_mutate(operation, request=client.request)
    try:
        return client.request("POST", path, body)
    except common.CliError as exc:
        if exc.status_code is not None and 400 <= exc.status_code < 500:
            raise
        raise common.CliError(
            "Delivery is uncertain. Read the same request or token family before retrying; preserve the operation ID.", "unknown_mutation_outcome", 4
        ) from None
    except (OSError, http.client.HTTPException):
        raise common.CliError("Delivery is uncertain. Reconcile the same request before retrying.", "unknown_mutation_outcome", 4) from None


def execute(args, client):
    command = "adp " + ("admin " if args.area != "access" else "") + args.area + " " + args.action
    if args.area == "access":
        tenant = identifier(args.tenant)
        before = checked(client.request("GET", "/access/status?target_tenant=" + tenant), ("status", "tenant_id"))
        if before["tenant_id"] != args.tenant:
            raise common.CliError("Server returned a different target tenant.", "invalid_response", 5)
        if args.action == "status":
            return common.envelope("ok", command, before)
        if args.dry_run or not args.yes:
            return common.envelope(
                "preview",
                command,
                {"current": before, "effect": "Submit a pending membership request. Authentication alone grants no spend eligibility."},
            )
        result = mutate(client, "/access/request", {"target_tenant": args.tenant, "motivation": args.reason})
        if (
            not isinstance(result, dict)
            or result.get("tenant_id") != args.tenant
            or result.get("status") != "pending"
            or not result.get("request_id")
        ):
            raise common.CliError("Request outcome is incomplete; inspect access status before retrying.", "unknown_mutation_outcome", 4)
        return common.envelope("pending", command, result)
    if args.area == "session":
        path = "/auth/admin/revoke-user-tokens/" + identifier(args.user)
        before = checked(client.request("GET", path + "/review?org=" + identifier(args.org)), ("user_id", "org", "revision", "effect"))
        if before["user_id"] != args.user or before["org"] != args.org:
            raise common.CliError("Server returned a different session target.", "invalid_response", 5)
        if args.dry_run or not args.yes:
            return common.envelope("preview", command, before)
        if not args.expected_revision or args.expected_revision != before["revision"]:
            raise common.CliError("Review token families with --dry-run and supply that --expected-revision.", "revision_conflict", 4)
        result = mutate(client, path + "/revision", {"org": args.org, "reason": args.reason, "expected_revision": args.expected_revision})
        if (
            not isinstance(result, dict)
            or result.get("user_id") != args.user
            or result.get("org") != args.org
            or type(result.get("tokens_revoked")) is not int
            or not isinstance(result.get("effect"), str)
        ):
            raise common.CliError("Revocation outcome is incomplete; inspect this token family before retrying.", "unknown_mutation_outcome", 4)
        return common.envelope("ok", command, result)
    if args.action == "list":
        if not 1 <= args.limit <= 200:
            raise common.CliError("Use --limit between 1 and 200.", "usage_error", 1)
        result = client.request("GET", "/admin/access-requests/review?limit=" + str(args.limit) + "&after=" + quote(args.cursor, safe=""))
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("items"), list)
            or (result.get("next_cursor") is not None and not isinstance(result["next_cursor"], str))
        ):
            raise common.CliError("Malformed access-request page.", "invalid_response", 5)
        for row in result["items"]:
            checked(row, ("id", "requester", "target_tenant", "status", "revision", "proposed_role", "requested_scope"))
        return common.envelope("ok", command, result)
    path = "/admin/access-requests/" + identifier(args.request)
    before = checked(
        client.request("GET", path + "/review"), ("id", "requester", "target_tenant", "status", "revision", "proposed_role", "requested_scope")
    )
    if before["id"] != args.request:
        raise common.CliError("Server returned a different request.", "invalid_response", 5)
    if args.action == "show":
        return common.envelope("ok", command, before)
    UUID(args.operation_id)
    if args.dry_run or not args.yes:
        return common.envelope("preview", command, before)
    # Server owns stale/replay decisions. Send the explicitly reviewed values,
    # including on identical retry after a lost acknowledgement.
    result = mutate(
        client,
        path + "/" + args.action + "/revision",
        {
            "operation_id": args.operation_id,
            "expected_revision": args.expected_revision,
            "expected_role": args.expected_role,
            "expected_scope": args.expected_scope,
            "decision_note": args.reason,
        },
    )
    if (
        not isinstance(result, dict)
        or result.get("request_id") != args.request
        or result.get("tenant_id") != before["target_tenant"]
        or (args.action == "approve" and result.get("granted_role") != args.expected_role)
        or result.get("operation_id") != args.operation_id
        or result.get("status") != ("approved" if args.action == "approve" else "denied")
    ):
        raise common.CliError("Decision acknowledgement is incomplete. Read this request and retain its operation ID.", "unknown_mutation_outcome", 4)
    return common.envelope("ok", command, result)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    try:
        result = execute(parser().parse_args(argv), common.Api())
        common.emit(result, as_json)
        return 4 if result["status"] == "pending" else 0
    except common.CliError as exc:
        result = common.envelope("failed", "adp access")
        result["error"] = {"code": exc.code, "message": str(exc), "http_status": exc.status_code}
        common.emit(result, as_json)
        return exc.exit_code
    except KeyboardInterrupt:
        common.emit(
            common.envelope("pending", "adp access", {"outcome": "unknown", "message": "Interrupted; reconcile the same request or token family."}),
            as_json,
        )
        return 130
    except (ValueError, TypeError, OSError, http.client.HTTPException):
        common.emit(
            common.envelope("failed", "adp access", {"error": "Invalid input or response; inspect the same target before retrying."}), as_json
        )
        return 5


if __name__ == "__main__":
    sys.exit(main())
