#!/usr/bin/env python3
"""Scoped research reads and revision-bound proposal review through ADP."""

from __future__ import annotations

import http.client
import json
import sys
import urllib.parse
import uuid
from pathlib import Path

import adp_common as common

BASE = "/superplane/v1/api/v1/research"


def parser():
    root = common.Parser(prog="adp superplane research")
    sub = root.add_subparsers(dest="area", required=True)
    for area in ("findings", "proposal"):
        verbs = sub.add_parser(area).add_subparsers(dest="action", required=True)
        for action in (
            ("list", "show")
            if area == "findings"
            else ("list", "show", "create", "generate", "approve", "reject")
        ):
            p = verbs.add_parser(action)
            p.add_argument("--json", action="store_true")
            if action in ("show", "approve", "reject"):
                p.add_argument("id", type=str)
            if action == "list":
                p.add_argument("--workspace")
                p.add_argument(
                    "--start", help="Inclusive timezone-aware date; requires --end"
                )
                p.add_argument(
                    "--end", help="Exclusive timezone-aware date; maximum 90 days"
                )
                p.add_argument(
                    "--page",
                    type=int,
                    choices=range(1, 10001),
                    default=1,
                    metavar="1..10000",
                )
                p.add_argument(
                    "--page-size",
                    type=int,
                    choices=range(1, 101),
                    default=20,
                    metavar="1..100",
                )
                p.add_argument(
                    "--max-pages",
                    type=int,
                    choices=range(1, 101),
                    default=1,
                    metavar="1..100",
                )
                p.add_argument("--status" if area == "proposal" else "--source")
            if action in ("create", "generate"):
                p.add_argument("--request-file", required=True)
                p.add_argument("--request-id", required=True)
            if action in ("approve", "reject"):
                p.add_argument("--expect-revision", required=True)
                if action == "reject":
                    p.add_argument("--reason", required=True)
            if action in ("create", "generate", "approve", "reject"):
                p.add_argument("--yes", action="store_true")
                p.add_argument("--dry-run", action="store_true")
    for area in ("sources", "stats", "scan"):
        p = sub.add_parser(area)
        p.add_argument("--json", action="store_true")
        if area == "scan":
            p.add_argument("--request-file", required=True)
            p.add_argument("--request-id", required=True)
            p.add_argument("--yes", action="store_true")
            p.add_argument("--dry-run", action="store_true")
    return root


def identifier(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError):
        raise common.CliError("Use a UUID identifier.", "usage_error", 1) from None


def checked(value):
    if not isinstance(value, dict):
        raise common.CliError("Malformed research response.", "invalid_response", 5)
    return value


def execute(args, api):
    action = getattr(args, "action", args.area)
    command = (
        "adp superplane research "
        + args.area
        + (" " + action if hasattr(args, "action") else "")
    )
    if action in {"scan", "generate"}:
        return common.envelope(
            "unavailable",
            command,
            {"reason": "durable_bounded_research_unavailable"},
            "This backend has no durable bounded scan/generation request contract. No work was submitted.",
        )
    token = common.access_token()

    def request(method, path, body=None):
        return checked(api.request(method, BASE + path, body, token=token, timeout=30))

    group = "proposals" if args.area == "proposal" else args.area
    if action in {"sources", "stats"}:
        value = request("GET", "/" + group)
        required = "sources" if action == "sources" else "total_findings"
        if required not in value:
            raise common.CliError("Malformed research response.", "invalid_response", 5)
        return common.envelope("ok", command, value)
    if action == "list":
        query = {"page": args.page, "page_size": args.page_size}
        for name in ("workspace", "status", "source"):
            val = getattr(args, name, None)
            if val:
                query["workspace_id" if name == "workspace" else name] = (
                    identifier(val) if name == "workspace" else val
                )
        if args.start or args.end:
            from datetime import datetime, timedelta

            try:
                start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
                end = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
                if (
                    start.tzinfo is None
                    or end.tzinfo is None
                    or not timedelta(0) < end - start <= timedelta(days=90)
                ):
                    raise ValueError
            except (ValueError, AttributeError):
                raise common.CliError(
                    "Provide both timezone-aware start/end within 90 days.",
                    "usage_error",
                    1,
                ) from None
            query.update(start=start.isoformat(), end=end.isoformat())
        items, seen = [], set()
        for _ in range(args.max_pages):
            value = request("GET", "/" + group + "?" + urllib.parse.urlencode(query))
            if (
                value.get("page") != query["page"]
                or value.get("page_size") != args.page_size
                or type(value.get("total")) is not int
                or value["total"] < 0
                or not isinstance(value.get("items"), list)
                or len(value["items"]) > args.page_size
            ):
                raise common.CliError("Malformed research page.", "invalid_response", 5)
            for row in value["items"]:
                row = checked(row)
                key = identifier(row.get("id"))
                if key in seen:
                    raise common.CliError(
                        "Research changed during pagination; restart the read.",
                        "invalid_response",
                        5,
                    )
                seen.add(key)
                items.append(row)
            complete = query["page"] * args.page_size >= value["total"]
            if complete:
                break
            if not value["items"]:
                raise common.CliError(
                    "Incomplete empty research page.", "invalid_response", 5
                )
            query["page"] += 1
        value.update(
            items=items,
            complete=complete,
            next_page=None if complete else query["page"],
            snapshot=False,
        )
        return common.envelope(
            "ok",
            command,
            value,
            "Offset pages are not a frozen snapshot; empty findings do not prove a scan ran.",
        )
    if action == "show":
        key = identifier(args.id)
        value = request("GET", "/" + group + "/" + key)
        if value.get("id") != key:
            raise common.CliError("Research identity mismatch.", "invalid_response", 5)
        return common.envelope("ok", command, value)
    if not args.yes and not args.dry_run:
        raise common.CliError(
            "Review the exact effect and pass --yes.", "confirmation_required", 1
        )
    if action == "create":
        path = Path(args.request_file)
        if path.stat().st_size > 65536:
            raise common.CliError("Proposal input exceeds 64 KiB.", "usage_error", 1)
        body = checked(json.loads(path.read_text()))
        if "request_id" in body:
            raise common.CliError(
                "Supply request identity only with --request-id.", "usage_error", 1
            )
        body["request_id"] = identifier(args.request_id)
        route, method = "/proposals", "POST"
    else:
        import re

        if not re.fullmatch(r"[0-9a-f]{64}", args.expect_revision):
            raise common.CliError(
                "Use the exact revision from proposal show.", "usage_error", 1
            )
        body = {"expected_revision": args.expect_revision}
        if action == "reject":
            if not args.reason.strip() or len(args.reason) > 4000:
                raise common.CliError(
                    "Use a nonempty reason of at most 4000 characters.",
                    "usage_error",
                    1,
                )
            body["reason"] = args.reason
        route, method = "/proposals/" + identifier(args.id) + "/" + action, "PATCH"
    if args.dry_run:
        return common.envelope(
            "dry_run",
            command,
            {
                "method": method,
                "path": BASE + route,
                "body": body,
                "effect": "Approval queues the reviewed experiment under existing domain execution policy."
                if action == "approve"
                else action,
            },
        )
    # An old server could ignore the new fields. Require explicit support before
    # any write; compatibility is server capability, never inferred from --yes.
    support = request("GET", "/cli-support")
    if support.get("proposal_contract") != "revision-idempotency-v1":
        return common.envelope("unavailable", command, support)
    common.ensure_can_mutate(
        "superplane.workspace.write", request=api.request, token=token
    )
    try:
        value = request(method, route, body)
    except common.CliError as exc:
        if exc.code != "invalid_response":
            raise
        raise common.CliError(
            "Proposal acknowledgement is malformed; reconcile the original request.",
            "unknown_mutation_outcome",
            4,
        ) from None
    except (http.client.HTTPException, OSError):
        raise common.CliError(
            "Outcome unknown; retain the same request ID/revision and inspect proposals before retrying.",
            "unknown_mutation_outcome",
            4,
        ) from None
    if (
        not isinstance(value.get("id"), str)
        or not isinstance(value.get("revision"), str)
        or len(value["revision"]) != 64
        or (action != "create" and value["id"] != identifier(args.id))
        or value.get("status")
        not in {
            "proposed",
            "approved",
            "rejected",
            "in_progress",
            "completed",
            "failed",
        }
    ):
        raise common.CliError(
            "Proposal acknowledgement is incomplete; reconcile the original request.",
            "unknown_mutation_outcome",
            4,
        )
    return common.envelope(
        "ok",
        command,
        value,
        "Proposal state is not proof of experiment completion or deployment.",
    )


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        return common.emit(execute(args, common.Api()), args.json)
    except (
        common.CliError,
        ValueError,
        TypeError,
        OSError,
        http.client.HTTPException,
    ) as exc:
        return common.report_error(exc, "adp superplane research", "--json" in argv)
    except KeyboardInterrupt:
        return common.report_error(
            common.CliError(
                "Interrupted; a submitted mutation may have completed. Reconcile its request ID or reviewed proposal before retrying.",
                "interrupted",
                130,
            ),
            "adp superplane research",
            "--json" in argv,
        )


if __name__ == "__main__":
    sys.exit(main())
