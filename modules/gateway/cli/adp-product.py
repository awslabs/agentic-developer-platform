#!/usr/bin/env python3
"""Installed Product commands backed by the gateway's scoped Product API."""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common


def parser():
    root = common.Parser(prog="adp product")
    commands = root.add_subparsers(dest="verb", required=True, parser_class=common.Parser)
    for name in ("list", "create", "show", "history", "update", "lifecycle", "sources", "source-bind", "source-bindings", "source-verify", "source-session", "source-discover", "source-operation", "source-apply"):
        command = commands.add_parser(name)
        command.add_argument("--json", action="store_true")
        if name == "list":
            command.add_argument("--cursor")
            command.add_argument("--limit", type=int, default=100)
        if name == "create":
            command.add_argument("--input")
            command.add_argument("--name")
            command.add_argument("--purpose")
            command.add_argument("--intended-user", action="append")
            command.add_argument("--starting-point")
            selection = command.add_mutually_exclusive_group()
            selection.add_argument("--new-project", action="store_true")
            selection.add_argument("--existing-project")
            command.add_argument("--owner")
            command.add_argument("--team")
        if name in ("show", "history", "update", "lifecycle", "sources", "source-bind", "source-bindings", "source-verify", "source-session", "source-discover", "source-operation", "source-apply"):
            command.add_argument("--product", required=True)
            command.add_argument("--project", required=True)
        if name == "show":
            command.add_argument("--revision", type=int)
        if name == "history":
            command.add_argument("--cursor")
            command.add_argument("--limit", type=int, default=50)
        if name in ("sources", "source-bindings"):
            command.add_argument("--cursor")
            command.add_argument("--limit", type=int, default=50)
        if name == "sources":
            command.add_argument("--connection-id")
            command.add_argument("--connection-kind", choices=("github_installation", "credential"))
            command.add_argument("--resource-cursor")
        if name == "source-bind":
            command.add_argument("--input", required=True)
        if name == "source-verify":
            command.add_argument("--binding", required=True)
        if name in ("source-session", "source-discover", "source-apply"):
            command.add_argument("--session", required=name != "source-session")
        if name == "source-discover":
            command.add_argument("--binding", action="append", required=True)
            command.add_argument("--cursor")
        if name in ("source-operation", "source-apply"):
            command.add_argument("--operation", required=True)
        if name == "update":
            command.add_argument("--input", required=True)
        if name == "lifecycle":
            command.add_argument("--action", choices=("archive", "restore"), required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--yes", action="store_true")
        if name in ("create", "update", "lifecycle", "source-bind", "source-verify", "source-session", "source-discover", "source-apply"):
            command.add_argument("--idempotency-key", required=name != "source-session")
        if name in ("update", "lifecycle", "source-bind", "source-verify", "source-session", "source-discover", "source-apply"):
            command.add_argument("--revision", type=int, required=name != "source-session")
    projects = commands.add_parser("project")
    actions = projects.add_subparsers(dest="project_verb", required=True, parser_class=common.Parser)
    for name in ("list", "bindings", "bind", "update", "unbind"):
        command = actions.add_parser(name)
        command.add_argument("--json", action="store_true")
        if name == "list":
            command.add_argument("--cursor")
            command.add_argument("--limit", type=int, default=100)
        else:
            command.add_argument("--product", required=True)
            if name in ("bindings", "update", "unbind"):
                command.add_argument("--project", required=True)
            if name in ("bind", "update"):
                command.add_argument("--input", required=True)
            if name == "unbind":
                command.add_argument("--yes", action="store_true")
            if name != "bindings":
                command.add_argument("--revision", type=int, required=True)
                command.add_argument("--idempotency-key", required=True)
    budgets = commands.add_parser("budget")
    budget_actions = budgets.add_subparsers(dest="budget_verb", required=True, parser_class=common.Parser)
    show_budget = budget_actions.add_parser("show")
    show_budget.add_argument("--product", required=True)
    show_budget.add_argument("--project", required=True)
    show_budget.add_argument("--json", action="store_true")
    set_budget = budget_actions.add_parser("set")
    set_budget.add_argument("--product", required=True)
    set_budget.add_argument("--project", required=True)
    set_budget.add_argument("--budget-usd", required=True)
    set_budget.add_argument("--reason", required=True)
    set_budget.add_argument("--json", action="store_true")
    return root


def usage(message):
    raise common.CliError(message, "usage_error", 1)


def input_body(source):
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        value = json.loads(text)
    except (OSError, UnicodeError, ValueError):
        usage("--input must be a readable UTF-8 JSON object.")
    if not isinstance(value, dict):
        usage("--input must contain a JSON object.")
    return value


def identifier(value):
    if not isinstance(value, str) or len(value) > 255 or "/" in value or value in {".", ".."} or not re.fullmatch(r"\S(?:.*\S)?", value):
        usage("--product and --project must be exact IDs.")
    return quote(value, safe="")


def budget_amount(value):
    if not re.fullmatch(r"[0-9]{1,8}\.[0-9]{2}", value) or not Decimal("0") < Decimal(value) <= Decimal("99999999.99"):
        raise common.CliError("--budget-usd must be greater than 0, at most 99999999.99, and have exactly two decimal places.", "usage_error", 2)
    return value


def mutation_headers(args):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", args.idempotency_key):
        usage("--idempotency-key must contain 16–128 letters, digits, _ or -.")
    headers = {"Idempotency-Key": args.idempotency_key}
    if args.verb != "create":
        if args.revision is None or not 1 <= args.revision <= 9007199254740991:
            usage("--revision must be a positive profile revision.")
        headers["If-Profile-Revision"] = str(args.revision)
    return headers


def create_body(args):
    if args.input:
        if any((args.name, args.purpose, args.intended_user, args.starting_point, args.new_project, args.existing_project, args.owner, args.team)):
            usage("--input cannot be combined with inline create fields.")
        return input_body(args.input)
    if not all((args.name, args.purpose, args.intended_user, args.starting_point)) or not (args.new_project or args.existing_project):
        usage("Supply --name, --purpose, --intended-user, --starting-point and --new-project or --existing-project.")
    if args.starting_point != "idea" or args.existing_project or args.owner or args.team:
        raise common.CliError(
            "This gateway only supports creating an idea with a new Project; use --input only for the same supported shape.",
            "unsupported_operation",
            5,
        )
    return {
        "name": args.name,
        "purpose": args.purpose,
        "intended_users": args.intended_user,
        "starting_point": args.starting_point,
        "project": {"mode": "create"},
    }


def execute_project(args, client):
    path = "/v1/projects"
    if args.project_verb == "list":
        if not 1 <= args.limit <= 100:
            usage("--limit must be between 1 and 100.")
        query = {"limit": args.limit}
        if args.cursor is not None:
            query["cursor"] = args.cursor
        return client.request("GET", path + "?" + urlencode(query))

    path = "/v1/products/" + identifier(args.product) + "/projects"
    if args.project_verb == "bindings":
        return client.request("GET", path + "/" + identifier(args.project) + "/project-bindings")
    headers = mutation_headers(args)
    operation = "product.project." + args.project_verb
    if args.project_verb == "bind":
        body = input_body(args.input)
        common.ensure_can_mutate(operation, request=client.request)
        return client.request("POST", path, body, extra_headers=headers)
    path += "/" + identifier(args.project)
    if args.project_verb == "update":
        body = input_body(args.input)
        common.ensure_can_mutate(operation, request=client.request)
        return client.request("PATCH", path + "/configuration", body, extra_headers=headers)
    if not args.yes:
        usage("Supply --yes to confirm unlinking this Project without deleting its configuration or assets.")
    common.ensure_can_mutate(operation, request=client.request)
    return client.request("DELETE", path, extra_headers=headers)


def validate_budget_selection(result, args):
    if not isinstance(result, dict) or result.get("product_id") != args.product or result.get("project_id") != args.project:
        raise common.CliError("Project budget response does not match the selected Product and Project.", "invalid_response")


def require_operation(result, args, kind):
    if not isinstance(result, dict) or result.get("id") != args.operation or result.get("kind") != kind or result.get("product_id") != args.product or result.get("project_id") != args.project:
        raise common.CliError("Operation readback does not match the selected Product, Project and operation.", "invalid_response")
    return result


def verify_applied_refs(result, profile):
    if not isinstance(result, dict) or not isinstance(profile, dict):
        raise common.CliError("Saved Profile readback is unavailable.", "invalid_response")
    for proof in result.get("applied", []):
        path = proof.get("paths", [])
        if len(path) != 1 or not path[0].startswith("/configuration/"):
            raise common.CliError("Applied source reference has an invalid Profile path.", "invalid_response")
        section, field = path[0].removeprefix("/configuration/").split("/", 1)
        if proof.get("source_refs") != profile.get("configuration", {}).get(section, {}).get(field):
            raise common.CliError("Saved Profile does not match the applied source reference.", "invalid_response")


def execute(args, client):
    path = "/v1/products"
    command = "product " + args.verb
    if args.verb == "budget":
        command += " " + args.budget_verb
        budget_path = "/v1/projects/" + identifier(args.project) + "/budget"
        if args.budget_verb == "show":
            result = client.request("GET", budget_path)
        else:
            if os.environ.get("SIGV4_PROXY_TARGET"):
                # An agent run behind the SigV4 proxy is not the owner; a Dev Box owner session is (#9640).
                raise common.CliError("Budget set is not available to agent runs.", "usage_error", 2)
            if not args.reason.strip():
                raise common.CliError("--reason must explain the budget change.", "usage_error", 2)
            if len(args.reason) > 500:
                raise common.CliError("--reason must be at most 500 characters.", "usage_error", 2)
            amount = budget_amount(args.budget_usd)
            try:
                validate_budget_selection(client.request("GET", budget_path), args)
                result = client.request(
                    "PUT", budget_path, {"budget_usd": amount},
                    extra_headers={"X-ADP-Budget-Reason-Base64": base64.b64encode(args.reason.encode("utf-8")).decode("ascii")},
                )
            except common.CliError as exc:
                if exc.status_code != 403:
                    raise
                raise common.CliError(exc.server_message or str(exc), exc.code, 3, status_code=403) from None
        validate_budget_selection(result, args)
        result = {**result, "cap_status": "none_metered" if result.get("budget_usd") is None else "capped"}
    elif args.verb == "list":
        if args.limit < 1 or args.limit > 100:
            usage("--limit must be between 1 and 100.")
        query = {"limit": args.limit}
        if args.cursor is not None:
            query["cursor"] = args.cursor
        result = client.request("GET", path + "?" + urlencode(query))
    elif args.verb == "project":
        command += " " + args.project_verb
        result = execute_project(args, client)
    elif args.verb == "create":
        headers = mutation_headers(args)
        body = create_body(args)
        common.ensure_can_mutate("product.create", request=client.request)
        result = client.request("POST", path, body, extra_headers=headers)
    else:
        path += "/" + identifier(args.product) + "/projects/" + identifier(args.project)
        if args.verb == "show":
            if args.revision is not None:
                if not 1 <= args.revision <= 9007199254740991:
                    usage("--revision must be a positive profile revision.")
                path += "/revisions/" + str(args.revision)
            result = client.request("GET", path)
        elif args.verb == "source-bindings":
            if not 1 <= args.limit <= 100:
                usage("--limit must be between 1 and 100.")
            query = {"limit": args.limit}
            if args.cursor is not None:
                query["cursor"] = args.cursor
            result = client.request("GET", path + "/bindings?" + urlencode(query))
        elif args.verb == "sources":
            if not 1 <= args.limit <= 100:
                usage("--limit must be between 1 and 100.")
            if (args.connection_id is None) != (args.connection_kind is None):
                usage("--connection-id and --connection-kind must be supplied together.")
            if args.resource_cursor is not None and args.connection_id is None:
                usage("--resource-cursor requires a selected connection.")
            query = {"limit": args.limit}
            if args.cursor is not None:
                query["cursor"] = args.cursor
            if args.connection_id is not None:
                query["connection_kind"] = args.connection_kind
                query["connection_id"] = identifier(args.connection_id)
            if args.resource_cursor is not None:
                query["resource_cursor"] = args.resource_cursor
            result = client.request("GET", path + "/sources?" + urlencode(query))
            available = {kind for source in result.get("items", []) for kind in source.get("capabilities", [])}
            result["next_prerequisites"] = [
                f"{kind.capitalize()} adapter unavailable for these connections; connect a supported existing source when available."
                for kind, alternatives in (("pipeline", ("pipeline", "ci_job")), ("support", ("support", "incident_service")))
                if not any(option in available for option in alternatives)
            ]
            if args.connection_id is not None:
                for source in result.get("items", []):
                    if source.get("coverage") == "partial":
                        source["next_prerequisite"] = (
                            "Choose only a discovered native ID; other regions and pipeline/support adapters are not covered."
                            if source.get("provider") == "aws" else
                            "Choose only a discovered native ID from the current page; load more pages for additional coverage."
                        )
                    elif source.get("coverage") == "unavailable":
                        source["next_prerequisite"] = (
                            "Verify the selected connection or retry discovery; Product save remains available."
                        )
        elif args.verb == "source-bind":
            headers = mutation_headers(args)
            body = input_body(args.input)
            common.ensure_can_mutate("product.update", request=client.request)
            result = client.request("POST", path + "/bindings", body, extra_headers=headers)
        elif args.verb == "source-verify":
            headers = mutation_headers(args)
            common.ensure_can_mutate("product.update", request=client.request)
            result = client.request("POST", path + "/bindings/" + identifier(args.binding) + "/verify", extra_headers=headers)
        elif args.verb == "source-session":
            session_path = path + "/onboarding-sessions"
            if args.session:
                if args.idempotency_key or args.revision is not None:
                    usage("--revision and --idempotency-key are only for starting a session.")
                result = client.request("GET", session_path + "/" + identifier(args.session))
            else:
                if args.idempotency_key is None or args.revision is None:
                    usage("Starting a session requires --revision and --idempotency-key.")
                headers = mutation_headers(args)
                common.ensure_can_mutate("product.update", request=client.request)
                result = client.request("POST", session_path, {"mode": "direct", "selected_binding_ids": []}, extra_headers=headers)
        elif args.verb == "source-operation":
            result = client.request("GET", path + "/operations/" + identifier(args.operation))
        elif args.verb == "source-discover":
            headers = mutation_headers(args)
            if len(args.binding) > 10 or len(set(args.binding)) != len(args.binding):
                usage("Select between 1 and 10 distinct --binding IDs.")
            session_path = path + "/onboarding-sessions/" + identifier(args.session)
            session = client.request("GET", session_path)
            if session.get("id") != args.session or session.get("revision") != args.revision or session.get("state") not in ("open", "waiting_external"):
                raise common.CliError("Read the current open session and Profile revision before discovery.", "stale_revision")
            for binding in args.binding:
                identifier(binding)
            body = {"binding_ids": args.binding, "adapter": "devlake"}
            if args.cursor is not None:
                body["cursor"] = identifier(args.cursor)
            common.ensure_can_mutate("product.update", request=client.request)
            result = client.request("POST", session_path + "/discoveries", body, extra_headers=headers)
        elif args.verb == "source-apply":
            headers = mutation_headers(args)
            operation_path = path + "/operations/" + identifier(args.operation)
            discovery = require_operation(client.request("GET", operation_path), args, "discoverSources")
            if discovery.get("state") != "succeeded" or discovery.get("coverage") not in ("complete", "partial") or discovery.get("cursor"):
                raise common.CliError("Discovery is incomplete or unavailable; read its operation before applying.", "dependency_pending", 4)
            common.ensure_can_mutate("product.update", request=client.request)
            result = client.request("POST", path + "/onboarding-sessions/" + identifier(args.session) + "/source-enrichment",
                                    {"operation_id": args.operation}, extra_headers=headers)
            profile = client.request("GET", path)
            if profile.get("revision") != result.get("revision"):
                raise common.CliError("Saved Profile revision does not match the apply receipt.", "invalid_response")
            verify_applied_refs(result, profile)
        elif args.verb == "history":
            if not 1 <= args.limit <= 100:
                usage("--limit must be between 1 and 100.")
            query = {"limit": args.limit}
            if args.cursor is not None:
                query["cursor"] = args.cursor
            result = client.request("GET", path + "/changes?" + urlencode(query))
        else:
            headers = mutation_headers(args)
            if args.verb == "update":
                body = input_body(args.input)
                common.ensure_can_mutate("product.update", request=client.request)
                result = client.request("PATCH", path, body, extra_headers=headers)
            else:
                if not args.yes or not args.reason.strip():
                    usage("Supply --reason and --yes to confirm the lifecycle change.")
                common.ensure_can_mutate("product.lifecycle", request=client.request)
                result = client.request("POST", path + "/lifecycle", {"action": args.action, "reason": args.reason}, extra_headers=headers)
    if not isinstance(result, dict):
        raise common.CliError("Malformed Product response.", "invalid_response", 5)
    return common.envelope("ok", command, result)


def emit_budget(result, as_json):
    if as_json:
        return common.emit(result, True)
    detail = result["detail"]
    print(result["command"] + ": " + result["status"])
    print("Project: " + str(detail["project_name"]) + " (" + str(detail["project_id"]) + ")")
    print("Budget: " + ("no cap, metered" if detail["budget_usd"] is None else str(detail["budget_usd"]) + " USD"))
    for field in ("spent_usd", "reserved_usd", "available_usd", "accounting_status"):
        print(field + ": " + str(detail.get(field)))
    print("Flows:")
    for flow in detail.get("flows", []):
        print("  " + str(flow.get("flow_name") or flow.get("flow_id")) + ": " + str(flow.get("spent_usd")) + " USD (" + str(flow.get("run_count")) + " runs)")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        result = execute(args, common.Api())
        if args.verb == "budget":
            return emit_budget(result, args.json)
        return common.emit(result, args.json)
    except (common.CliError, OSError, ValueError, TypeError) as exc:
        if isinstance(exc, common.CliError) and exc.code == "unknown_mutation_outcome":
            exc = common.CliError(
                "Budget write outcome is unknown. Use `adp product budget show` with the same --product and --project "
                "to read the current cap and reconcile the change before retrying."
                if args.verb == "budget" else
                "Product write outcome is unknown. Read the current Product first, then retry only the exact same "
                "command with unchanged --idempotency-key, --revision and input; do not choose a new key.",
                "unknown_mutation_outcome",
                4,
            )
        return common.report_error(exc, "product", "--json" in argv)


if __name__ == "__main__":
    sys.exit(main())
