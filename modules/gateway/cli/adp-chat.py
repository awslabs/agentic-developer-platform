#!/usr/bin/env python3
"""Hosted conversation discovery and bounded owned history (#5640)."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common


def parser():
    root = common.Parser(prog="adp chat")
    commands = root.add_subparsers(dest="action", required=True)
    for action in ("status", "start", "resume", "list", "show", "watch", "export"):
        p = commands.add_parser(action)
        p.add_argument("--json", action="store_true")
        if action in {"show", "watch", "export"}:
            p.add_argument("--session", required=True)
        if action == "list":
            p.add_argument("--page", type=int, choices=range(1, 101), default=1, metavar="1..100")
            p.add_argument("--page-size", type=int, choices=range(1, 101), default=20, metavar="1..100")
        if action == "watch":
            p.add_argument("--task-id", required=True, help="Exact task whose final response is awaited")
            p.add_argument("--timeout", type=int, choices=range(1, 301), default=60, metavar="1..300")
            p.add_argument("--interval", type=int, choices=range(1, 31), default=3, metavar="1..30")
        if action == "export":
            p.add_argument("--output", required=True, help="New transcript file inside an owned private directory")
        if action == "start":
            p.add_argument("--persona", required=True)
            p.add_argument("--message-file", required=True)
        if action == "resume":
            p.add_argument("session_id")
            p.add_argument("--answer-file", required=True)
        if action in {"start", "resume"}:
            p.add_argument("--request-id", required=True)
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--yes", action="store_true")
    return root


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise common.CliError("Use an exact session/task ID.", "usage_error", 1)
    return quote(value, safe="")


class Client:
    def __init__(self):
        self.api = common.Api()
        self.token = common.access_token()

    def get(self, path):
        return self.api.request("GET", path, token=self.token, timeout=30)


def invalid():
    raise common.CliError("Chat response did not preserve the requested identity or bounded schema.", "invalid_response", 4)


def scope(value, expected=None):
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k] for k in ("tenant_id", "user_id")):
        invalid()
    if expected and any(value[k] != expected[k] for k in ("tenant_id", "user_id")):
        invalid()


def session(value, expected, sid=None):
    scope(value, expected)
    identifier(value.get("session_id"))
    if sid and value["session_id"] != sid:
        invalid()
    if type(value.get("expires_at")) is not int or value["expires_at"] <= time.time():
        raise common.CliError("Conversation retention expired.", "history_expired", 4)
    if value.get("status") not in {"idle", "pending"} or value.get("answer_completion_verified") is not False:
        invalid()
    if value.get("redaction") != "known-secret-patterns" or type(value.get("truncated")) is not bool:
        invalid()
    messages, tasks = value.get("messages"), value.get("pending_task_ids")
    if not isinstance(messages, list) or len(messages) > 100 or not isinstance(tasks, list) or len(tasks) > 1000:
        invalid()
    for task in tasks:
        identifier(task)
    total = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            invalid()
        content = message.get("content")
        if not isinstance(content, str) or len(content) > 10000 or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", content):
            invalid()
        total += len(content)
        if message.get("task_id"):
            identifier(message["task_id"])
    if total > 100000:
        invalid()
    return value


def execute(args, client):
    command = "adp chat " + args.action
    capabilities = client.get("/chat/capabilities")
    scope(capabilities)
    if type(capabilities.get("general_turns_supported")) is not bool or not isinstance(capabilities.get("authorized_personas"), list):
        invalid()
    if args.action == "status":
        return common.envelope("ok", command, capabilities)
    if args.action in {"start", "resume"}:
        # No general human admission contract exists yet. Never silently reinterpret
        # this as refinement or dispatch through worker-only Task credentials.
        raise common.CliError(
            "General hosted chat admission is unavailable. Use adp chat status; no message was read or dispatched.", "unavailable", 4
        )
    if capabilities.get("enabled") is not True or capabilities.get("history_configured") is not True:
        raise common.CliError("Hosted chat history is unavailable on this deployment.", "unavailable", 4)
    if args.action == "list":
        result = client.get("/chat/sessions?" + urlencode({"page": args.page, "limit": args.page_size}))
        scope(result, capabilities)
        if not isinstance(result.get("items"), list) or len(result["items"]) > args.page_size:
            invalid()
        for item in result["items"]:
            session(item, capabilities)
        if result.get("next_page") not in {None, args.page + 1} or type(result.get("scan_limit_reached")) is not bool:
            invalid()
        return common.envelope("ok", command, result)
    sid = identifier(args.session)
    if args.action == "watch":
        identifier(args.task_id)
    deadline = time.monotonic() + (args.timeout if args.action == "watch" else 0)
    while True:
        result = session(client.get("/chat/sessions/" + sid), capabilities, args.session)
        if args.action != "watch":
            break
        matched = [m for m in result["messages"] if m["role"] == "assistant" and m.get("task_id") == args.task_id]
        if matched:
            result["matched_task_id"] = args.task_id
            result["response_observed"] = True
            # Response handler append is a final response, not websocket ephemera.
            result["answer_completion_verified"] = not result["truncated"]
            break
        if time.monotonic() >= deadline:
            result["matched_task_id"] = args.task_id
            result["response_observed"] = False
            return common.envelope(
                "pending", command, result, "No final response for this exact task was observed. No new turn or cancellation was sent."
            )
        time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
    if args.action == "export":
        output = Path(args.output).absolute()
        common.private_directory(output.parent)
        # O_EXCL prevents accidental overwrite, symlink following, or replacement
        # of a live config/token file supplied by mistake.
        import json
        import os

        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as target:
            json.dump(result, target, indent=2)
            target.write("\n")
        result = {"session_id": args.session, "output": str(output), "truncated": result["truncated"], "expires_at": result["expires_at"]}
    return common.envelope("ok", command, result)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        return common.emit(execute(parser().parse_args(argv), Client()), "--json" in argv)
    except (common.CliError, OSError, ValueError, TypeError) as exc:
        return common.report_error(exc, "adp chat", "--json" in argv)
    except KeyboardInterrupt:
        return common.emit(common.envelope("pending", "adp chat", {}, "Watch interrupted; no cancellation or new turn sent."), "--json" in argv)


if __name__ == "__main__":
    sys.exit(main())
