#!/usr/bin/env python3
"""Human Activity controls and hosted coding through the canonical Task API."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common

ACTIONS = {"pause", "resume", "steer", "abort"}
STATUSES = {"pending", "delivered", "applied", "cancelled", "rejected", "unknown"}


def invalid(message="Malformed Activity response."):
    raise common.CliError(message, "invalid_response", 5)


def object_response(value):
    if not isinstance(value, dict):
        invalid()
    return value


def identifier(value):
    if not value or len(value) > 128 or any(c in value for c in "/\\\r\n"):
        raise common.CliError("Use a valid run or chain ID.", "invalid_arguments", 2)
    return urllib.parse.quote(value, safe="")


def positive(value):
    number = int(value)
    if not 1 <= number <= 3600:
        raise ValueError("must be between 1 and 3600")
    return number


def parser():
    root = common.Parser(prog="adp agent", description=__doc__)
    sub = root.add_subparsers(dest="action", required=True)
    for action in ["list", "chain", "detail", "status", "ping", "state", "logs", "wait", *sorted(ACTIONS)]:
        p = sub.add_parser(action)
        p.add_argument("--json", action="store_true")
        if action == "chain":
            p.add_argument("chain_id")
        elif action != "list":
            p.add_argument("--run", required=True)
        if action in {"list", "chain", "detail", "status", "logs", "wait"}:
            p.add_argument("--admin", action="store_true", help="Use tenant-admin read API; server authorization still applies")
        if action == "list":
            p.add_argument("--page-size", type=int, choices=range(1, 101), default=20, metavar="1..100")
            p.add_argument("--cursor")
            p.add_argument("--max-pages", type=positive, default=1)
        if action in {"logs", "wait"}:
            p.add_argument("--timeout", type=positive, default=60, help="Whole watch deadline in seconds (1..3600)")
        if action == "logs":
            p.add_argument("--follow", action="store_true", help="Follow live explanations as NDJSON; Ctrl-C only detaches")
            p.add_argument("--last-event-id", help="Opaque cursor printed with each explanation; used only with --follow")
        if action == "wait":
            p.add_argument("--interval", type=positive, default=5)
        if action in ACTIONS:
            p.add_argument("--command-id", required=True, help="Stable UUID; retain for reconciliation/replay")
            p.add_argument("--instruction" if action == "steer" else "--reason", required=True)
            p.add_argument("--expected-generation", type=int, help="Advisory read check; server does not support an atomic generation precondition")
            p.add_argument("--yes", action="store_true")
            p.add_argument("--dry-run", action="store_true")
    trigger = sub.add_parser("trigger", help="Submit a bounded repository coding task through the existing Task API")
    trigger.add_argument("--repo", required=True)
    trigger.add_argument("--issue", required=True, type=int)
    trigger.add_argument("--persona", required=True, choices=["agent-task-claude-developer", "agent-task-codex-developer"])
    trigger.add_argument("--snapshot-file", required=True, help="Repository snapshot JSON; ADP independently verifies its commit, issue and files")
    trigger.add_argument("--instructions-file", required=True, help="UTF-8 issue task instructions")
    trigger.add_argument("--request-id", required=True, help="Stable saved idempotency key")
    trigger.add_argument("--timeout", type=positive, default=120)
    trigger.add_argument("--dry-run", action="store_true")
    trigger.add_argument("--yes", action="store_true")
    trigger.add_argument("--json", action="store_true")
    return root


class Client:
    def __init__(self):
        self.api = common.Api()
        # Pin one human session for the entire command, including reconnection.
        self.token = common.access_token()

    def get(self, path, timeout=30):
        try:
            return object_response(self.api.request("GET", path, token=self.token, timeout=timeout))
        except (http.client.HTTPException, OSError):
            raise common.CliError("Activity read was interrupted; retry the read.", "gateway_unavailable", 4) from None

    def post(self, path, body):
        try:
            return object_response(self.api.request("POST", path, body, token=self.token, timeout=30))
        except (http.client.HTTPException, OSError):
            raise common.CliError(
                "Activity command acknowledgement was interrupted. Reconcile the original command ID.",
                "unknown_mutation_outcome",
                4,
            ) from None

    def raw(self, path, *, timeout, cursor=None):
        headers = {"Authorization": "Bearer " + self.token}
        if cursor:
            headers["Last-Event-ID"] = cursor
        request = urllib.request.Request(self.api.base + path, headers=headers)
        try:
            return self.api.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise common.CliError(
                f"Activity returned HTTP {exc.code}; read access or content may be unavailable.",
                "http_error",
                {401: 2, 403: 3}.get(exc.code, 5),
                status_code=exc.code,
            ) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError):
            raise common.CliError("Activity read unavailable; hosted execution is unchanged.", "gateway_unavailable", 4) from None


def validate_state(value, run):
    object_response(value)
    if (
        value.get("run_id") != run
        or type(value.get("available")) is not bool
        or not isinstance(value.get("capabilities"), dict)
        or not isinstance(value.get("commands"), list)
    ):
        invalid()
    return value


def control(args, client, path):
    try:
        uuid.UUID(args.command_id)
    except ValueError:
        raise common.CliError("--command-id must be a UUID.", "invalid_arguments", 2) from None
    text = args.instruction if args.action == "steer" else args.reason
    if not text.strip() or len(text) > (4000 if args.action == "steer" else 1000):
        raise common.CliError("Provide nonempty text within the documented size bound.", "invalid_arguments", 2)
    if not args.yes and not args.dry_run:
        raise common.CliError("Inspect state, then pass --yes to confirm this command.", "confirmation_required", 2)
    state = validate_state(client.get(path + "/state"), args.run)
    if args.expected_generation is not None and state.get("generation") != args.expected_generation:
        raise common.CliError("Worker generation changed; inspect state before issuing an intent.", "stale_generation", 4)
    if not state["available"] or state["capabilities"].get(args.action) is not True:
        return common.envelope("unavailable", "adp agent " + args.action, state)
    body = {"command_id": args.command_id, "instruction" if args.action == "steer" else "reason": text}
    if args.dry_run:
        return common.envelope("dry_run", "adp agent " + args.action, {"run_id": args.run, "body": body, "generation": state.get("generation")})
    try:
        result = client.post(path + "/" + args.action, body)
    except common.CliError as exc:
        if exc.code not in {"unknown_mutation_outcome", "invalid_response"}:
            raise
        # Read-only reconciliation: never replay a mutation automatically.
        try:
            observed = validate_state(client.get(path + "/state"), args.run)
        except common.CliError:
            observed = None
        result = {"run_id": args.run, "action": args.action, "command_id": args.command_id, "command_status": "unknown", "observed_state": observed}
        if observed and observed.get("generation") == state.get("generation"):
            for ack in observed["commands"]:
                if (
                    isinstance(ack, dict)
                    and ack.get("command_id") == args.command_id
                    and ack.get("action") == args.action
                    and ack.get("status") in STATUSES
                ):
                    # The journal binds ID/action, not this request's payload.
                    # An earlier payload may have applied while this changed
                    # payload conflicted and its conflict response was lost.
                    result["observed_acknowledgement"] = ack
                    result["payload_verified"] = False
                    result["reconciliation_note"] = (
                        "The journal does not expose a payload binding. This acknowledgement is informational; "
                        "the submitted payload outcome remains unknown."
                    )
    if (
        result.get("run_id") != args.run
        or result.get("command_id") != args.command_id
        or result.get("action") != args.action
        or result.get("command_status") not in STATUSES
    ):
        return common.envelope(
            "pending",
            "adp agent " + args.action,
            {"run_id": args.run, "action": args.action, "command_id": args.command_id, "command_status": "unknown"},
            "Acknowledgement identity/status did not match. Read state and reconcile the original command ID; do not issue a new intent.",
        )
    status = result["command_status"]
    outcome = "ok" if status == "applied" else "failed" if status in {"rejected", "cancelled"} else "pending"
    return common.envelope(
        outcome, "adp agent " + args.action, result, "Read adp agent state; delivery does not prove pause quiescence, model comprehension, or exit."
    )


def stream(args, client, path):
    cursor = args.last_event_id
    if cursor and (len(cursor) > 256 or any(c in cursor for c in "\r\n")):
        raise common.CliError("Invalid event cursor.", "invalid_arguments", 2)
    deadline = time.monotonic() + args.timeout
    generation = None
    sequence = 0
    if cursor:
        try:
            run, gen, seq = cursor.rsplit(":", 2)
            if run != args.run:
                raise ValueError
            generation, sequence = int(gen), int(seq)
        except ValueError:
            raise common.CliError("Cursor must belong to this run.", "invalid_arguments", 2) from None
    while time.monotonic() < deadline:
        with client.raw(path + "/events", timeout=min(10, deadline - time.monotonic()), cursor=cursor) as response:
            if response.headers.get_content_type() != "text/event-stream":
                invalid("Expected an explanation event stream.")
            buffer = b""
            while time.monotonic() < deadline:
                raw = getattr(getattr(response, "fp", None), "raw", None)
                sock = getattr(raw, "_sock", None)
                if sock is not None:
                    sock.settimeout(min(10, max(0.01, deadline - time.monotonic())))
                try:
                    chunk = response.read1(8192)
                except (http.client.HTTPException, OSError):
                    return common.envelope(
                        "pending",
                        "adp agent logs",
                        {"detached": True, "last_event_id": cursor},
                        "Stream read was interrupted; reconnect with the last cursor. Hosted execution is unchanged.",
                    )
                if not chunk:
                    break
                buffer += chunk
                buffer = buffer.replace(b"\r\n", b"\n")
                if len(buffer) > 262144:
                    invalid("Explanation frame exceeded the client size bound.")
                while b"\n\n" in buffer:
                    frame, buffer = buffer.split(b"\n\n", 1)
                    fields = {}
                    for line in frame.decode("utf-8").splitlines():
                        if line and not line.startswith(":"):
                            key, _, value = line.partition(":")
                            if key in fields:
                                invalid("Duplicate stream field.")
                            fields[key] = value.lstrip(" ")
                    if not fields:
                        continue
                    kind = fields.get("event")
                    value = object_response(json.loads(fields.get("data", "null")))
                    if kind == "heartbeat":
                        continue
                    if kind == "finished":
                        return common.envelope(
                            "pending",
                            "adp agent logs",
                            {"event": "finished", "detached": True, "last_event_id": cursor},
                            "Gateway ended this subscription. Recheck state and access; this is not an execution outcome.",
                        )
                    if kind == "unavailable":
                        return common.envelope(
                            "unavailable",
                            "adp agent logs",
                            {"event": kind, "data": value, "detached": True, "last_event_id": cursor},
                            "Live feed unavailable; reconnect with the last cursor to recheck access.",
                        )
                    if kind == "reset":
                        # A future or expired cursor is no longer a valid
                        # deduplication bound. Keep the generation guard, but
                        # admit the retained history the server now replays.
                        cursor, sequence = None, 0
                        common.emit(common.envelope("pending", "adp agent logs", {"event": "reset", "data": value, "last_event_id": cursor}), True)
                        continue
                    if (
                        kind not in {"explanation", "terminal"}
                        or value.get("invocation_id") != args.run
                        or value.get("version") != 1
                        or type(value.get("generation")) is not int
                        or type(value.get("sequence")) is not int
                        or value["sequence"] < 1
                    ):
                        invalid("Explanation identity/schema mismatch.")
                    if generation is not None and value["generation"] != generation:
                        invalid("Worker generation changed; inspect state before reconnecting.")
                    generation = value["generation"]
                    expected = f"{args.run}:{generation}:{value['sequence']}"
                    if fields.get("id") != expected:
                        invalid("Explanation cursor mismatch.")
                    if value["sequence"] <= sequence:
                        continue
                    if kind == "explanation" and not isinstance(value.get("payload", {}).get("text"), str):
                        invalid()
                    sequence, cursor = value["sequence"], expected
                    common.emit(common.envelope("ok", "adp agent logs", {"event": kind, "data": value, "last_event_id": cursor}), True)
                    if kind == "terminal":
                        return common.envelope(
                            "ok",
                            "adp agent logs",
                            {"stream_closed": True, "last_event_id": cursor},
                            "Read agent status for the final execution outcome.",
                        )
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    return common.envelope(
        "pending", "adp agent logs", {"detached": True, "last_event_id": cursor}, "Watch deadline reached; hosted execution is unchanged."
    )


def execute(args, client):
    action = args.action
    command = "adp agent " + action
    if action == "trigger" or str(getattr(args, "run", "")).startswith("tsk_"):
        return task_execute(args, client)
    prefix = "/admin" if getattr(args, "admin", False) else "/me"
    base = prefix + "/agent-invocations"
    if action == "list":
        cursor, items, seen = args.cursor, [], set()
        for _ in range(args.max_pages):
            query = {"page_size": args.page_size}
            if cursor:
                query["last_key"] = cursor
            result = client.get(base + "?" + urllib.parse.urlencode(query))
            if not isinstance(result.get("items"), list) or not all(isinstance(i, dict) for i in result["items"]):
                invalid()
            items.extend(result["items"])
            cursor = result.get("last_key")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                invalid("Invalid or repeated pagination cursor.")
            seen.add(cursor)
        return common.envelope("ok", command, {"items": items, "last_key": cursor, "complete": cursor is None})
    if action == "chain":
        result = client.get(base + "/chain/" + identifier(args.chain_id))
        if result.get("correlation_id") != args.chain_id or not isinstance(result.get("items"), list):
            invalid()
        return common.envelope("ok", command, result)
    run = identifier(args.run)
    control_path = "/activity/invocations/" + run + "/agent"
    if action in ACTIONS:
        return control(args, client, control_path)
    if action in {"ping", "state"}:
        result = client.get(control_path + "/" + action)
        if result.get("run_id") != args.run or type(result.get("available")) is not bool:
            invalid()
        return common.envelope("ok" if result["available"] else "unavailable", command, result)
    if action == "logs":
        if args.follow:
            if args.admin:
                raise common.CliError(
                    "Live explanations require the run owner's human session; --admin applies to retained transcript reads only.",
                    "invalid_arguments",
                    2,
                )
            return stream(args, client, control_path)
        if args.last_event_id:
            raise common.CliError("--last-event-id requires --follow.", "invalid_arguments", 2)
        try:
            with client.raw(base + "/" + run + "/transcript", timeout=args.timeout) as response:
                if response.headers.get_content_type() not in {"text/markdown", "text/plain"}:
                    invalid("Expected a Markdown transcript.")
                deadline = time.monotonic() + args.timeout
                parts, size = [], 0
                while size <= 5 * 1024 * 1024:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise common.CliError("Transcript read deadline reached.", "request_timeout", 4)
                    raw = getattr(getattr(response, "fp", None), "raw", None)
                    sock = getattr(raw, "_sock", None)
                    if sock is not None:
                        sock.settimeout(remaining)
                    chunk = response.read1(min(65536, 5 * 1024 * 1024 + 1 - size))
                    if not chunk:
                        break
                    parts.append(chunk)
                    size += len(chunk)
                payload = b"".join(parts)
                if len(payload) > 5 * 1024 * 1024:
                    invalid("Transcript exceeds client size bound.")
                text = payload.decode("utf-8")
                if not text.strip():
                    return common.envelope("unavailable", command, {"transcript_status": "empty"})
                return common.envelope("ok", command, {"transcript_status": "available", "text": text})
        except common.CliError as exc:
            if exc.status_code == 404:
                return common.envelope(
                    "unavailable",
                    command,
                    {"transcript_status": "unavailable"},
                    "The API does not distinguish pending, redacted, expired, missing, or inaccessible content.",
                )
            raise
    deadline = time.monotonic() + getattr(args, "timeout", 30)
    while True:
        result = client.get(base + "/" + run, timeout=min(30, max(0.01, deadline - time.monotonic())))
        if result.get("invocation_id") != args.run or not isinstance(result.get("status"), str):
            invalid()
        if action != "wait":
            return common.envelope("ok", command, result)
        if result["status"] == "complete":
            return common.envelope("ok", command, result)
        if result["status"] in {"failed", "aborted", "rejected", "rate_limited", "no_op", "blocked", "skipped", "budget_stopped"}:
            return common.envelope("failed", command, result)
        if time.monotonic() >= deadline:
            return common.envelope("pending", command, result, "Deadline reached; hosted execution is unchanged.")
        time.sleep(min(args.interval, deadline - time.monotonic()))


def task_execute(args, client):
    helper = common.load_provider("adp-task.py")
    if helper is None:
        raise common.CliError("Task CLI helper is missing; run adp update.", "unavailable", 5)
    command = "adp agent " + args.action
    task = helper.TaskClient(
        {"gateway_url": common.gateway_url()}, common.gateway_url(), human_login=True, deadline=time.monotonic() + getattr(args, "timeout", 120)
    )
    task.token, task.expires = client.token, helper.token_expiry(client.token)
    if getattr(args, "admin", False):
        raise common.CliError("Human Tasks use exact owner access; admin read override is unavailable.", "usage_error", 1)
    if args.action == "trigger":
        return task_trigger(args, helper, task)
    if not re.fullmatch(r"tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", args.run):
        invalid("Malformed Task run ID.")
    if args.action in ACTIONS:
        if args.action not in {"abort", "steer"}:
            return common.envelope(
                "unavailable", command, {"task_id": args.run}, "Task API supports input and cancellation; pause/resume are not implemented."
            )
        if args.expected_generation is not None:
            raise common.CliError("Task commands do not accept the Activity generation flag.", "usage_error", 1)
        uuid.UUID(args.command_id)
        if args.dry_run or not args.yes:
            return common.envelope("dry_run", command, {"task_id": args.run, "command_id": args.command_id})
        result = task.command(
            args.run, "cancel" if args.action == "abort" else "messages", args.command_id, args.reason if args.action == "abort" else args.instruction
        )
        if result.get("task_id") != args.run or result.get("command_id") != args.command_id:
            invalid("Task command acknowledgement mismatch; reconcile the same command ID.")
        return common.envelope("pending", command, result, "Command acceptance is not execution or cancellation confirmation; inspect the same Task.")
    if args.action == "logs" and args.follow:
        code = helper.monitor(
            task, args.run, SimpleNamespace(cursor=args.last_event_id, cursor_file=None, max_events=10000, timeout=args.timeout, json=True)
        )
        return common.envelope("ok" if code == 0 else "failed" if code in {5, 7} else "pending", command, {"task_id": args.run, "monitor_exit": code})
    while True:
        value = task.snapshot(args.run)
        if value.get("task_id") != args.run:
            invalid("Task snapshot identity mismatch.")
        outcome = helper.snapshot_exit(value)
        if args.action != "wait":
            return common.envelope("ok", command, value, "Canonical Task result and events are separate from legacy Activity transcripts.")
        if value["status"] in helper.TERMINAL:
            return common.envelope("ok" if outcome == 0 else "pending" if outcome == 4 else "failed", command, value)
        if time.monotonic() >= task.deadline:
            return common.envelope("pending", command, value, "Wait deadline reached; no cancellation was sent.")
        time.sleep(min(args.interval, max(0, task.deadline - time.monotonic())))


def task_trigger(args, helper, task):
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo)
        or args.issue <= 0
        or not 1 <= len(args.request_id) <= 128
        or any(not 32 <= ord(c) <= 126 for c in args.request_id)
    ):
        raise common.CliError("Use an exact repository, positive issue and printable stable request ID (1..128 characters).", "usage_error", 1)
    with Path(args.snapshot_file).open("rb") as source:
        snapshot_bytes = source.read(262145)
    with Path(args.instructions_file).open() as source:
        instructions = source.read(16001)
    if not 0 < len(snapshot_bytes) <= 262144 or not 0 < len(instructions) <= 16000:
        raise common.CliError("Snapshot limit is256KiB; instructions limit is16000characters.", "usage_error", 1)
    snapshot = json.loads(snapshot_bytes)
    if not isinstance(snapshot, dict) or snapshot.get("repository") != args.repo or snapshot.get("issue") != args.issue:
        raise common.CliError("Snapshot repository/issue does not match the requested target.", "conflict", 4)
    digest = hashlib.sha256(snapshot_bytes).hexdigest()
    intent = {
        "repo": args.repo,
        "issue": args.issue,
        "persona": args.persona,
        "snapshot_sha256": digest,
        "instructions": instructions,
        "gateway": task.gateway,
        "scope": common.authenticated_scope(token=task.token),
    }
    fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
    if args.dry_run or not args.yes:
        return common.envelope(
            "dry_run",
            "adp agent trigger",
            {"repo": args.repo, "issue": args.issue, "persona": args.persona, "snapshot_sha256": digest, "request_id": args.request_id},
            "No artifact uploaded or task submitted. Server repository verification runs at admission.",
        )
    directory = common.private_directory(common.state_dir() / "hosted-tasks")
    key_scope = json.dumps([task.gateway, intent["scope"], args.request_id]).encode()
    path = directory / (hashlib.sha256(key_scope).hexdigest() + ".json")
    with common.file_lock(path.with_suffix(".lock"), "Task request already running"):
        if path.exists():
            saved = common.read_private_json(path)
            if saved.get("fingerprint") != fingerprint:
                raise common.CliError("Request ID belongs to different task inputs or identity.", "conflict", 4)
            if not saved.get("artifact_id"):
                return common.envelope(
                    "pending",
                    "adp agent trigger",
                    {"request_id": args.request_id, "phase": "artifact_upload_unknown"},
                    "No paid task was submitted; artifact upload outcome is unknown.",
                )
        else:
            saved = {"fingerprint": fingerprint}
            common.write_json(path, saved)
            boundary = "adp" + uuid.uuid4().hex
            metadata = {"schema_version": "1.0", "content_type": "application/json", "content_sha256": digest, "content_length": len(snapshot_bytes)}
            payload = (
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\nContent-Type: application/json\r\n\r\n'
                    + json.dumps(metadata)
                    + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="content"; filename="snapshot.json"\r\n'
                    + 'Content-Type: application/json\r\n\r\n'
                ).encode()
                + snapshot_bytes
                + f"\r\n--{boundary}--\r\n".encode()
            )
            with task.open(
                "POST", "/v1/task-artifacts", data=payload, headers={"Content-Type": "multipart/form-data; boundary=" + boundary}
            ) as response:
                uploaded = json.loads(helper.bounded_read(response, task, 65536))
            artifact = uploaded.get("artifact_id")
            if (
                not isinstance(artifact, str)
                or not re.fullmatch(r"art_[0-9a-f-]{36}", artifact)
                or uploaded.get("content_sha256") != digest
                or uploaded.get("content_type") != "application/json"
            ):
                invalid("Artifact acknowledgement mismatch; no task submitted.")
            saved["artifact_id"] = artifact
            common.write_json(path, saved)
        body = {
            "schema_version": "1.0",
            "persona": args.persona,
            "instructions": instructions,
            "inputs": {"repository_snapshot_artifact": saved["artifact_id"]},
            "artifact_ids": [saved["artifact_id"]],
            "external_reference": args.repo + "#" + str(args.issue),
        }
        accepted = task.submit(body, args.request_id)
        if (
            not isinstance(accepted, dict)
            or not re.fullmatch(r"tsk_[0-9a-f-]{36}", str(accepted.get("task_id", "")))
            or accepted.get("status") != "accepted"
        ):
            invalid("Task acceptance response malformed; retry the same request ID and inputs.")
        saved["task_id"] = accepted["task_id"]
        common.write_json(path, saved)
        return common.envelope(
            "pending",
            "adp agent trigger",
            {**accepted, "run_id": accepted["task_id"], "idempotency_key": args.request_id},
            "Use agent status/logs/wait with this Task run ID. Accepted is not completed.",
        )


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
    except common.CliError as exc:
        return common.report_error(exc, "adp agent", "--json" in argv)
    as_json = args.json or (args.action == "logs" and args.follow)
    try:
        return common.emit(execute(args, Client()), as_json)
    except KeyboardInterrupt:
        return common.emit(
            common.envelope(
                "pending",
                "adp agent " + args.action,
                {"detached": True},
                "Client interrupted; no cancellation was sent. Reconcile any submitted command using its original ID.",
            ),
            as_json,
        )
    except (common.CliError, http.client.HTTPException, OSError, ValueError, TypeError, AttributeError) as exc:
        if isinstance(exc, common.CliError):
            result = common.envelope("failed", "adp agent " + args.action)
            result["error"] = {"code": exc.code, "message": str(exc), "http_status": exc.status_code}
            common.emit(result, as_json)
            return exc.exit_code
        return common.report_error(exc, "adp agent " + args.action, as_json)


if __name__ == "__main__":
    sys.exit(main())
