#!/usr/bin/env python3
"""Read bounded own/managed usage and redacted inference log metadata."""

from __future__ import annotations

import csv
import http.client
import io
import json
import sys
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common


def parser():
    root = common.Parser(prog="adp")
    top = root.add_subparsers(dest="area", required=True)
    usage = top.add_parser("usage").add_subparsers(dest="view", required=True)
    logs = top.add_parser("logs").add_subparsers(dest="view", required=True)
    admin = top.add_parser("admin").add_subparsers(dest="admin_area", required=True)
    managed = admin.add_parser("usage").add_subparsers(dest="view", required=True)
    for sub, names, is_admin in [
        (usage, ["summary", "timeline", "models", "requests", "request"], False),
        (logs, ["list", "show", "export"], False),
        (managed, ["summary", "users", "departments", "requests"], True),
    ]:
        for name in names:
            p = sub.add_parser(name)
            p.add_argument("--json", action="store_true")
            p.add_argument("--start", required=True, help="Inclusive timezone-aware ISO8601 start")
            p.add_argument("--end", required=True, help="Exclusive timezone-aware ISO8601 end; maximum 90-day range")
            p.add_argument("--run", help="Activity invocation ID; owner/tenant checked by the server")
            if is_admin:
                p.add_argument("--org", required=True, help="Exact managed organization ID")
            if name == "request":
                p.add_argument("request_id")
            else:
                p.add_argument("--request-id", required=name == "show")
            if name in {"requests", "request", "list", "show", "export"}:
                p.add_argument("--cursor", help="Resume using the same scope, time and request/run filters")
                p.add_argument("--page-size", type=int, choices=range(1, 101), default=50, metavar="1..100")
                p.add_argument("--max-pages", type=int, choices=range(1, 101), default=1, metavar="1..100")
            if name == "export":
                p.add_argument("--format", choices=["json", "ndjson", "csv"], default="json")
    return root


def bounds(args):
    try:
        start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None or not 0 < (end - start).total_seconds() <= 90 * 86400:
            raise ValueError
        return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()  # noqa: UP017 -- Python 3.9 CLI
    except ValueError:
        raise common.CliError("Use timezone-aware ISO8601 [start,end) bounds within 90 days.", "usage_error", 1) from None


def checked(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("scope"), dict)
        or not isinstance(value.get("items"), list)
        or type(value.get("complete")) is not bool
    ):
        raise common.CliError("Malformed usage response; upgrade the gateway if necessary.", "invalid_response", 5)
    if not all(isinstance(row, dict) for row in value["items"]):
        raise common.CliError("Malformed usage records.", "invalid_response", 5)
    for row in value["items"]:
        cost = row.get("cost")
        if not isinstance(cost, dict) or cost.get("status") not in {"estimated", "lower_bound", "unknown"} or cost.get("currency") != "USD":
            raise common.CliError("Malformed cost semantics.", "invalid_response", 5)
        amount = cost.get("amount")
        if amount is not None:
            try:
                if not isinstance(amount, str) or not Decimal(amount).is_finite() or Decimal(amount) < 0:
                    raise ValueError
            except (ValueError, InvalidOperation):
                raise common.CliError("Cost must be a finite nonnegative decimal string.", "invalid_response", 5) from None
    return value


def execute(args, client):
    start, end = bounds(args)
    view = "requests" if args.view in {"requests", "request", "list", "show", "export"} else args.view
    if args.area == "admin":
        if not args.org or any(c in args.org for c in "/\\\r\n"):
            raise common.CliError("Use an exact organization ID.", "usage_error", 1)
        prefix = "/usage/managed/" + urllib.parse.quote(args.org, safe="")
    else:
        prefix = "/usage/me"
    query = {"start": start, "end": end}
    if args.request_id:
        query["request_id"] = args.request_id
    if args.run:
        query["run_id"] = args.run
    cursor = getattr(args, "cursor", None)
    rows, seen, seen_ids = [], set(), set()
    if cursor:
        seen.add(cursor)
    scope = None
    for _ in range(getattr(args, "max_pages", 1)):
        if view == "requests":
            query["limit"] = args.page_size
            if cursor:
                query["cursor"] = cursor
        result = checked(client.request("GET", prefix + "/" + view + "?" + urllib.parse.urlencode(query), timeout=30))
        if scope is not None and result["scope"] != scope:
            raise common.CliError("Usage scope changed between pages.", "invalid_response", 5)
        scope = result["scope"]
        if scope.get("kind") != ("managed" if args.area == "admin" else "own") or (args.area == "admin" and scope.get("org_id") != args.org):
            raise common.CliError("Usage response scope mismatch.", "invalid_response", 5)
        if view == "requests":
            for row in result["items"]:
                row_id = row.get("id")
                if not isinstance(row_id, str) or not row_id or row_id in seen_ids:
                    raise common.CliError("Missing or repeated usage record identity.", "invalid_response", 5)
                seen_ids.add(row_id)
        rows.extend(result["items"])
        cursor = result.get("next_cursor")
        if result["complete"]:
            if cursor:
                raise common.CliError("Complete usage page unexpectedly has a cursor.", "invalid_response", 5)
            break
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise common.CliError("Missing or repeated usage cursor.", "invalid_response", 5)
        seen.add(cursor)
    result["items"] = rows
    result["next_cursor"] = cursor
    command = "adp " + ("admin usage" if args.area == "admin" else args.area) + " " + args.view
    if args.view in {"request", "show"} and not rows:
        return common.envelope(
            "unavailable", command, result, "No visible record in this window: missing, inaccessible, expired or delayed are indistinguishable."
        )
    status = "pending" if args.view == "export" and not result["complete"] else "ok"
    return common.envelope(
        status, command, result, "Continue with next_cursor and identical bounds; records may arrive or settle later." if cursor else None
    )


CSV_FIELDS = [
    "id",
    "timestamp",
    "request_id",
    "org_id",
    "user_id",
    "model",
    "input_tokens",
    "output_tokens",
    "status_code",
    "invocation_id",
    "chain_id",
    "root_human_id",
    "cost_status",
    "cost_amount",
    "currency",
    "settlement",
]


def csv_cell(value):
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        text = "'" + text
    return text


def emit_export(result, mode):
    detail = result["detail"]
    metadata = {key: value for key, value in detail.items() if key != "items"}
    if mode == "json":
        return common.emit(result, True)
    if mode == "ndjson":
        for row in detail["items"]:
            print(json.dumps({"type": "record", "scope": detail["scope"], "record": row}))
        print(json.dumps({"type": "continuation", "status": result["status"], "detail": metadata}))
    else:
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in detail["items"]:
            cost = row.get("cost", {})
            flat = {
                **row,
                "cost_status": cost.get("status"),
                "cost_amount": cost.get("amount"),
                "currency": cost.get("currency"),
                "settlement": cost.get("settlement"),
            }
            writer.writerow({name: csv_cell(flat.get(name)) for name in CSV_FIELDS})
        print(output.getvalue(), end="")
        print(json.dumps({"type": "continuation", "status": result["status"], "detail": metadata}), file=sys.stderr)
    return 4 if result["status"] == "pending" else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        if args.view == "export" and args.json and args.format != "json":
            raise common.CliError("--json cannot be combined with a different export format.", "usage_error", 1)
        # Validate the window before reading operator authentication/configuration.
        bounds(args)
        result = execute(args, common.Api())
        if args.view == "export":
            return emit_export(result, args.format)
        return common.emit(result, args.json)
    except (common.CliError, ValueError, OSError, TypeError, KeyError, http.client.HTTPException) as exc:
        return common.report_error(exc, "adp usage/logs", "--json" in argv)
    except KeyboardInterrupt:
        return common.report_error(
            common.CliError("Read interrupted; no remote state changed.", "interrupted", 130), "adp usage/logs", "--json" in argv
        )


if __name__ == "__main__":
    sys.exit(main())
