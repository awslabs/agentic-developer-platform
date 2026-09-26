#!/usr/bin/env python3
"""Inspect and manage scoped rate-limit overrides."""

from __future__ import annotations

import argparse
import http.client
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common

DIMENSIONS = ("rpm", "tpm", "concurrent_requests")


def limit(value):
    if value == "unset":
        return None
    try:
        number = int(value)
        if str(number) != value or not 1 <= number <= 2147483647:
            raise ValueError
        return number
    except ValueError:
        raise common.CliError("Use an integer 1..2147483647 or unset; zero does not disable enforcement.", "usage_error", 1) from None


def parser():
    root = common.Parser(prog="adp ratelimit")
    areas = root.add_subparsers(dest="area", required=True)
    own = areas.add_parser("ratelimit").add_subparsers(dest="action", required=True)
    admin = areas.add_parser("admin").add_subparsers(dest="admin_area", required=True)
    managed = admin.add_parser("ratelimit").add_subparsers(dest="action", required=True)
    for action in ("me", "list", "show", "set", "delete", "status"):
        p = (own if action == "me" else managed).add_parser(action)
        p.add_argument("--json", action="store_true")
        if action != "me":
            p.add_argument("--org", required=True)
            p.add_argument("--scope", choices=["user", "team", "department", "org"], required=action != "list")
        if action not in {"me", "list"}:
            p.add_argument("--target", required=True)
        if action == "list":
            p.add_argument("--page", type=int, choices=range(1, 10001), default=1)
            p.add_argument("--page-size", type=int, choices=range(1, 101), default=20)
            p.add_argument("--max-pages", type=int, choices=range(1, 101), default=1)
        if action in {"set", "delete"}:
            p.add_argument("--yes", action="store_true")
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--expected-revision")
        if action == "set":
            p.add_argument("--expect-absent", action="store_true")
            for name in DIMENSIONS:
                p.add_argument("--" + name.replace("_", "-"), default=argparse.SUPPRESS)
    return root


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 255 or any(c in value for c in "/\\\r\n"):
        raise common.CliError("Use an exact nonempty ID.", "usage_error", 1)
    return quote(value, safe="")


class Client:
    def __init__(self):
        self.api = common.Api()
        self.token = common.access_token()

    def request(self, method, path, body=None):
        return self.api.request(method, path, body, token=self.token, timeout=30)


def config(value, org, scope, key):
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("org_id") != org or value.get("entity_type") != scope or value.get("entity_id") != key:
        raise common.CliError("Rate-limit target mismatch.", "invalid_response", 5)
    try:
        if datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00")).tzinfo is None:
            raise ValueError
        for name in DIMENSIONS:
            if value[name] is not None and (type(value[name]) is not int or not 0 <= value[name] <= 2147483647):
                raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise common.CliError("Malformed saved limit or revision.", "invalid_response", 5) from None
    return value


def snapshot(value, args):
    if (
        not isinstance(value, dict)
        or value.get("org_id") != args.org
        or value.get("entity_type") != args.scope
        or not isinstance(value.get("entity_id"), str)
        or not value["entity_id"]
        or (args.scope != "user" and value["entity_id"] != args.target)
        or not isinstance(value.get("runtime"), dict)
        or "saved" not in value
    ):
        raise common.CliError("Malformed rate-limit snapshot.", "invalid_response", 5)
    config(value["saved"], args.org, args.scope, value["entity_id"])
    return value


def execute(args, client):
    command = "adp " + ("ratelimit me" if args.action == "me" else "admin ratelimit " + args.action)
    if args.action == "me":
        value = client.request("GET", "/ratelimits/me")
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("lines"), list)
            or not isinstance(value.get("runtime"), dict)
            or not value.get("org_id")
        ):
            raise common.CliError("Malformed own rate-limit response.", "invalid_response", 5)
        return common.envelope("ok", command, value)
    base = "/admin/organizations/" + identifier(args.org) + "/ratelimit-cli"
    if args.action == "list":
        items = []
        for page in range(args.page, min(10001, args.page + args.max_pages)):
            query = {"page": page, "limit": args.page_size}
            if args.scope:
                query["scope"] = args.scope
            value = client.request("GET", base + "?" + urlencode(query))
            if (
                not isinstance(value, dict)
                or value.get("org_id") != args.org
                or value.get("page") != page
                or value.get("page_size") != args.page_size
                or type(value.get("has_more")) is not bool
                or not isinstance(value.get("items"), list)
                or len(value["items"]) > args.page_size
            ):
                raise common.CliError("Malformed rate-limit page.", "invalid_response", 5)
            for row in value["items"]:
                if (
                    not isinstance(row, dict)
                    or row.get("entity_type") not in {"org", "team", "department", "user", "service_account"}
                    or (args.scope and row["entity_type"] != args.scope)
                ):
                    raise common.CliError("Malformed rate-limit list target.", "invalid_response", 5)
                config(row, args.org, row["entity_type"], row.get("entity_id"))
            items.extend(value["items"])
            if not value["has_more"]:
                break
        return common.envelope("ok", command, {**value, "items": items})
    path = base + "/" + args.scope + "/" + identifier(args.target)
    before = snapshot(client.request("GET", path), args)
    if args.action in {"show", "status"}:
        return common.envelope("ok", command, before)
    changes = {name: limit(getattr(args, name)) for name in DIMENSIONS if hasattr(args, name)} if args.action == "set" else {}
    if args.action == "set" and not changes:
        raise common.CliError("Specify at least one limit dimension.", "usage_error", 1)
    current = before["saved"]
    if args.dry_run:
        return common.envelope(
            "dry_run",
            command,
            {"before": before, "changes": changes, "expected_revision": current["updated_at"] if current else None, "expect_absent": current is None},
        )
    if not args.yes:
        raise common.CliError("Review --dry-run and supply --yes.", "confirmation_required", 1)
    if current is None and args.action == "delete":
        return common.envelope("ok", command, {"deleted": True, "already_absent": True})
    if current:
        if args.expected_revision != current["updated_at"] or getattr(args, "expect_absent", False):
            raise common.CliError("Supply the reviewed --expected-revision; current configuration differs or was not reviewed.", "conflict", 1)
    elif not args.expect_absent or args.expected_revision:
        raise common.CliError("Creation requires --expect-absent.", "usage_error", 1)
    common.ensure_can_mutate("ratelimit.managed.write", request=client.api.request, token=client.token)
    method = "PUT" if args.action == "set" else "DELETE"
    body = {**changes, "expected_revision": args.expected_revision, "expect_absent": args.expect_absent} if method == "PUT" else None
    route = path if method == "PUT" else path + "?" + urlencode({"expected_revision": args.expected_revision})
    unknown = False
    try:
        acknowledgement = client.request(method, route, body)
        if method == "PUT":
            if config(acknowledgement, args.org, args.scope, before["entity_id"]) is None:
                raise common.CliError("Missing acknowledgement", "invalid_response", 5)
        elif (
            not isinstance(acknowledgement, dict)
            or acknowledgement.get("deleted") is not True
            or acknowledgement.get("entity_id") != before["entity_id"]
            or acknowledgement.get("org_id") != args.org
            or acknowledgement.get("entity_type") != args.scope
        ):
            raise common.CliError("Malformed delete acknowledgement", "invalid_response", 5)
    except common.CliError as exc:
        if exc.code not in {"unknown_mutation_outcome", "invalid_response"} and not (exc.status_code and exc.status_code >= 500):
            raise
        unknown = True
    except (OSError, ValueError, http.client.HTTPException):
        unknown = True
    try:
        observed = snapshot(client.request("GET", path), args)
        matches = observed["saved"] is None if method == "DELETE" else not unknown and observed["saved"] == acknowledgement
    except (common.CliError, OSError, ValueError, http.client.HTTPException):
        observed, matches, unknown = None, False, True
    return common.envelope(
        "ok" if matches and not unknown else "pending",
        command,
        {"observed": observed, "readback_matches": matches, "outcome": "unknown" if unknown else "acknowledged"},
        "Saved limits are not enforcement evidence. Counters were preserved. No uncertain mutation was replayed.",
    )


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        if args.action == "set":
            for name in DIMENSIONS:
                if hasattr(args, name):
                    limit(getattr(args, name))
        return common.emit(execute(args, Client()), args.json)
    except (common.CliError, OSError, ValueError, TypeError, http.client.HTTPException) as exc:
        return common.report_error(exc, "adp ratelimit", "--json" in argv)
    except KeyboardInterrupt:
        common.emit(
            common.envelope("pending", "adp ratelimit", {"outcome": "unknown"}, "Interrupted; inspect the same target before retrying."),
            "--json" in argv,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
