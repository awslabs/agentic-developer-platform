#!/usr/bin/env python3
"""Submit, observe and abort tasks as a registered Task service principal."""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common
import adp_task_client as protocol

CliError = common.CliError
TERMINAL = {"completed": 0, "failed": 5, "cancelled": 7}


def private_credentials(path):
    try:
        if Path(path).stat().st_size > 65536:
            raise ValueError
        value = common.read_private_json(path)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, CliError):
        raise CliError(
            "Use an owned, mode-0600 Task credential JSON file. "
            "Ask your administrator to register this Task client; adp login alone does not grant access.",
            "task_credentials_required",
            2,
        ) from None


def safe_endpoint(url):
    parsed = urllib.parse.urlsplit(url)
    # Keep the shared CLI's explicit loopback fixture convention.
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError
        return url.rstrip("/")
    return protocol.endpoint(url)


def token_expiry(token):
    try:
        encoded = token.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))["exp"])
    except (ValueError, KeyError, IndexError, TypeError):
        return float("inf")


class DeadlineReader:
    """Limit every socket read by the remaining whole-command deadline.

    read1 performs at most one underlying read; unlike read/readline it cannot
    reset a socket timeout repeatedly while a peer slowly dribbles one frame.
    """

    def __init__(self, response, client):
        self.response, self.client, self.buffer = response, client, b""

    def chunk(self, size=65536):
        remaining = self.client.remaining(35)
        raw = getattr(getattr(self.response, "fp", None), "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        reader = getattr(self.response, "read1", self.response.read)
        return reader(size)

    def readline(self, size):
        while b"\n" not in self.buffer and len(self.buffer) < size:
            chunk = self.chunk(min(8192, size - len(self.buffer)))
            if not chunk:
                break
            self.buffer += chunk
        length = self.buffer.find(b"\n") + 1
        length = min(size, length if length else len(self.buffer))
        line, self.buffer = self.buffer[:length], self.buffer[length:]
        return line


def bounded_read(response, client, limit):
    reader = DeadlineReader(response, client)
    parts, size = [], 0
    while size <= limit:
        chunk = reader.chunk(min(65536, limit + 1 - size))
        if not chunk:
            return b"".join(parts)
        parts.append(chunk)
        size += len(chunk)
    raise CliError("Response exceeds the client byte bound.", "invalid_response", 3)


class TaskClient(protocol.Client):
    """Reuse Task protocol helpers with deployment-bound auth and finite retries."""

    def __init__(self, credentials, gateway, *, token_file=None, scopes=None, opener=None, deadline=None):
        self.credentials = credentials
        self.token_file = token_file
        self.gateway = gateway.rstrip("/")
        self._validate_binding(credentials)
        self.base = safe_endpoint(credentials.get("task_api_url", self.gateway))
        if urllib.parse.urlsplit(self.base)[:2] != urllib.parse.urlsplit(self.gateway)[:2]:
            raise CliError("Task API URL must use the selected deployment's origin.", "deployment_mismatch", 1)
        self.token_url = safe_endpoint(credentials["token_url"]) if credentials.get("token_url") else None
        self.token, self.expires = None, 0
        self.scopes = credentials.get("scopes") or scopes or ["adp-tasks/read"]
        if (
            not isinstance(self.scopes, list)
            or not self.scopes
            or not all(scope in {"adp-tasks/" + name for name in ("submit", "read", "cancel", "input", "artifacts")} for scope in self.scopes)
        ):
            raise CliError("Configure Task OAuth scopes as a list of adp-tasks scope names.", "invalid_credentials", 2)
        self.opener = opener or urllib.request.build_opener(common.NoRedirect())
        self.deadline = deadline if deadline is not None else time.monotonic() + 120

    def _validate_binding(self, credentials):
        if credentials.get("gateway_url", "").rstrip("/") != self.gateway:
            raise CliError(
                "Task credentials belong to another deployment, or lack gateway_url. Use that deployment's Task credentials.",
                "deployment_mismatch",
                2,
            )

    def remaining(self, maximum=30):
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise CliError("Local wait expired; remote task was not aborted. Resume with task status or monitor.", "timeout", 4)
        return min(maximum, seconds)

    def pause(self, seconds):
        time.sleep(min(seconds, self.remaining()))

    def authenticate(self, force=False):
        if not force and self.token and time.time() < self.expires - 10:
            return
        if self.token_file:
            credentials = private_credentials(self.token_file)
            self._validate_binding(credentials)
            token = credentials.get("access_token")
            expires = credentials.get("expires_at", token_expiry(token or ""))
        else:
            if not self.token_url or not self.credentials.get("client_id") or not self.credentials.get("client_secret"):
                raise CliError(
                    "Configure a registered Task service client with token_url, client_id and client_secret. "
                    "adp login alone does not grant Task API access.",
                    "task_credentials_required",
                    2,
                )
            basic = base64.b64encode((self.credentials["client_id"] + ":" + self.credentials["client_secret"]).encode()).decode()
            request = urllib.request.Request(
                self.token_url,
                data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": " ".join(self.scopes)}).encode(),
                headers={"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with self.opener.open(request, timeout=self.remaining()) as response:
                    raw = bounded_read(response, self, 65536)
                    if len(raw) > 65536:
                        raise ValueError
                    result = json.loads(raw)
                token, expires = result["access_token"], time.time() + float(result["expires_in"])
            except urllib.error.HTTPError as error:
                error.close()
                raise CliError(
                    "OAuth authentication failed. Check the registered Task client and granted scopes.", "authentication_failed", 2
                ) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                raise CliError("OAuth transport failed; no task was submitted by authentication.", "transport_failure", 3) from None
        if not isinstance(token, str) or not token or any(c.isspace() for c in token) or float(expires) <= time.time():
            raise CliError("Task token is missing or expired. Refresh the private token file or use client credentials.", "authentication_failed", 2)
        self.token, self.expires = token, float(expires)

    def open(self, method, path, data=None, headers=None, *, retry=False, timeout=30):
        refreshed = False
        attempt = 0
        while True:
            self.authenticate()
            supplied = {**(headers or {}), "Authorization": "Bearer " + self.token}
            try:
                return self.opener.open(
                    urllib.request.Request(self.base + path, data=data, headers=supplied, method=method), timeout=self.remaining(timeout)
                )
            except urllib.error.HTTPError as error:
                status = error.code
                error.close()
                if status == 401 and not refreshed:
                    refreshed = True
                    self.authenticate(force=True)
                    continue
                if retry and status in (429, 502, 503, 504) and attempt < 2:
                    self.pause(2**attempt)
                    attempt += 1
                    continue
                if status in (401, 403):
                    raise CliError(
                        "Task access denied. Ask your administrator to register this service principal "
                        "and grant the required Task scopes/persona policy.",
                        "task_access_denied",
                        2,
                        status_code=status,
                    ) from None
                if status == 410:
                    raise CliError(
                        "Event history expired. Read task status, then explicitly choose a new --cursor; continuous history cannot be recovered.",
                        "history_expired",
                        6,
                    ) from None
                if status == 409:
                    raise CliError(
                        "Task request conflicts with existing state or reused identity. "
                        "Reuse the original body/key or command ID; inspect task status.",
                        "task_conflict",
                        5,
                    ) from None
                raise CliError(
                    f"Task API returned HTTP {status}. Check deployment readiness or the request schema; no response body is logged.",
                    "task_http_error",
                    3 if status >= 500 or status == 429 else 5,
                    status_code=status,
                ) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if not retry or attempt >= 2:
                    raise CliError(
                        "Task transport failed; outcome may be unknown. Retry the identical request/key or abort command ID, or inspect task status.",
                        "transport_failure",
                        3,
                    ) from None
                self.pause(2**attempt)
                attempt += 1

    def json(self, method, path, body=None, headers=None, *, retry=False):
        data = None if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        # Serialize once; retries after both header and body failures use these bytes.
        for attempt in range(3 if retry else 1):
            try:
                with self.open(method, path, data, {"Content-Type": "application/json", **(headers or {})}) as response:
                    result = json.loads(bounded_read(response, self, 2 * 1024 * 1024))
                    if not isinstance(result, dict):
                        raise CliError("Expected a Task API response object.", "invalid_response", 3)
                    return result
            except CliError as error:
                if error.exit_code != 3 or not retry or attempt == 2:
                    raise
                self.pause(2**attempt)
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead, json.JSONDecodeError):
                if not retry or attempt == 2:
                    raise CliError(
                        "Response transport failed or returned invalid JSON; outcome is unknown. Retry the identical request/key or command ID.",
                        "transport_failure",
                        3,
                    ) from None
                self.pause(2**attempt)


def output(kind, payload, as_json):
    if as_json:
        print(json.dumps({"type": kind, "data": payload}, ensure_ascii=True), flush=True)
    else:
        print(kind + ": " + json.dumps(payload, ensure_ascii=True), flush=True)


def snapshot_exit(snapshot):
    if snapshot.get("status") not in {"accepted", "queued", "running", "waiting_for_input", "cancel_requested", *TERMINAL}:
        raise CliError("Task API returned an unknown or missing task status; completion cannot be determined.", "invalid_response", 3)
    if snapshot.get("status") == "cancelled":
        error = snapshot.get("error") or {}
        if error.get("child_exit_confirmed") is not True or error.get("recovery_required") is not False or snapshot.get("recovery_required"):
            return 4
        return 7
    return TERMINAL.get(snapshot.get("status"), 0)


def cursor_value(value, task_id):
    if value is not None and not re.fullmatch(re.escape(task_id) + r":[1-9][0-9]*", value):
        raise CliError("Cursor must belong to this task and have a positive event sequence.", "invalid_cursor", 1)
    return value


def monitor(client, task_id, args):
    cursor = cursor_value(args.cursor, task_id)
    if args.cursor_file and Path(args.cursor_file).exists() and cursor is None:
        saved = common.read_private_json(args.cursor_file)
        if saved.get("gateway_url") != client.gateway or saved.get("task_id") != task_id:
            raise CliError("Cursor file belongs to another deployment or task.", "cursor_mismatch", 1)
        cursor = cursor_value(saved.get("cursor"), task_id)
    handled = 0
    reconnects = 0
    try:
        while handled < args.max_events and reconnects < 50:
            snap = client.snapshot(task_id)
            output("snapshot", snap, args.json)
            terminal_cursor = cursor_value(snap.get("latest_event_cursor"), task_id)
            if snap.get("status") in TERMINAL and (terminal_cursor is None or cursor == terminal_cursor):
                return snapshot_exit(snap)
            headers = {"Accept": "text/event-stream"}
            if cursor:
                headers["Last-Event-ID"] = cursor
            try:
                with client.open(
                    "GET", "/v1/tasks/" + protocol.segment(task_id) + "/events", headers=headers, retry=True, timeout=client.remaining(35)
                ) as response:
                    for event in protocol.parse_sse(DeadlineReader(response, client), client.deadline):
                        event_id = cursor_value(event.get("id"), task_id)
                        if event_id and cursor and int(event_id.rsplit(":", 1)[1]) <= int(cursor.rsplit(":", 1)[1]):
                            continue
                        if event_id and cursor and int(event_id.rsplit(":", 1)[1]) != int(cursor.rsplit(":", 1)[1]) + 1:
                            raise CliError("Event sequence has a gap. Read task status and explicitly choose a new resume cursor.", "history_gap", 6)
                        kind = protocol.event_type(event)
                        if kind == "history.gap":
                            raise CliError(
                                "The server reported a history gap. Read task status and explicitly choose a new resume cursor.", "history_gap", 6
                            )
                        output("event", event, args.json)
                        if event_id:
                            cursor = event_id
                            if args.cursor_file:
                                common.write_json(args.cursor_file, {"gateway_url": client.gateway, "task_id": task_id, "cursor": cursor})
                        handled += 1
                        if snap.get("status") in TERMINAL and cursor == terminal_cursor:
                            return snapshot_exit(snap)
                        if kind in ("task.completed", "task.failed", "task.cancelled"):
                            snap = client.snapshot(task_id)
                            output("snapshot", snap, args.json)
                            terminal_cursor = cursor_value(snap.get("latest_event_cursor"), task_id)
                            if terminal_cursor is None or cursor == terminal_cursor:
                                return snapshot_exit(snap) if snap.get("status") in TERMINAL else 4
                            break
                        if handled >= args.max_events:
                            break
            except BrokenPipeError:
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead):
                pass
            reconnects += 1
            client.pause(min(1, client.remaining()))
        return 4
    finally:
        output("resume", {"task_id": task_id, "cursor": cursor, "remote_abort_requested": False}, args.json)


def parser():
    root = common.Parser(prog="adp task", description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    for verb in ("submit", "status", "monitor", "abort"):
        cmd = commands.add_parser(verb)
        cmd.add_argument("request_file" if verb == "submit" else "task_id")
        cmd.add_argument("--credentials", default=os.environ.get("ADP_TASK_CREDENTIALS_FILE"))
        cmd.add_argument("--token-file", default=os.environ.get("ADP_TASK_TOKEN_FILE"))
        cmd.add_argument("--json", action="store_true", help="Emit one JSON object per line")
        cmd.add_argument("--timeout", "--seconds", type=float, default=120, help="Total local deadline in seconds (1..3600)")
        if verb in ("submit", "abort"):
            cmd.add_argument("--wait", action="store_true")
        if verb in ("submit", "abort", "monitor"):
            cmd.add_argument("--cursor")
            cmd.add_argument("--cursor-file")
            cmd.add_argument("--max-events", type=int, default=10000)
        if verb == "submit":
            cmd.add_argument("--key", required=True)
        if verb == "abort":
            cmd.add_argument("--command-id", required=True)
            cmd.add_argument("--reason", required=True)
            cmd.add_argument("--yes", action="store_true", help="Confirm the durable remote cancellation request")
    return root


def main(argv=None):
    args = None
    try:
        args = parser().parse_args(argv)
        if not 1 <= args.timeout <= 3600 or not 1 <= getattr(args, "max_events", 1) <= 10000:
            raise CliError("Use timeout 1..3600 seconds and max-events 1..10000.", "usage_error", 1)
        if args.command == "abort":
            try:
                uuid.UUID(args.command_id)
            except ValueError:
                raise CliError("Use a valid saved UUID for --command-id.", "usage_error", 1) from None
            if not args.yes:
                raise CliError(
                    "Abort requests remote cancellation. Rerun with --yes to confirm; Ctrl-C only stops this CLI.", "confirmation_required", 1
                )
        gateway = common.gateway_url()
        if args.credentials and args.token_file:
            raise CliError("Choose --credentials or --token-file, not both.", "usage_error", 1)
        path = args.token_file or args.credentials or common.config_path().parent / "task-credentials.json"
        credentials = private_credentials(path)
        scopes = ["adp-tasks/" + ({"status": "read", "monitor": "read", "abort": "cancel", "submit": "submit"}[args.command])]
        if getattr(args, "wait", False) and "adp-tasks/read" not in scopes:
            scopes.append("adp-tasks/read")
        client = TaskClient(credentials, gateway, token_file=args.token_file, scopes=scopes, deadline=time.monotonic() + args.timeout)
        if args.command == "submit":
            with Path(args.request_file).open("rb") as source:
                raw = source.read(1048577)
            if len(raw) > 1048576:
                raise CliError("Request exceeds the 1 MiB local bound.", "usage_error", 1)
            body = json.loads(raw)
            if not isinstance(body, dict) or not args.key or len(args.key) > 256:
                raise CliError("Supply a request object and durable idempotency key (1..256 characters).", "usage_error", 1)
            result = client.submit(body, args.key)
            output("submitted", result, args.json)
            return monitor(client, result["task_id"], args) if args.wait else 0
        if args.command == "status":
            result = client.snapshot(args.task_id)
            output("snapshot", result, args.json)
            return snapshot_exit(result)
        if args.command == "abort":
            result = client.command(args.task_id, "cancel", args.command_id, args.reason)
            output("abort_receipt", {"receipt": result, "terminal_cancellation_confirmed": False}, args.json)
            if not args.wait:
                return 4
            outcome = monitor(client, args.task_id, args)
            if outcome == 0:
                raise CliError("Task completed before cancellation was confirmed. Completion is not a successful abort.", "abort_raced_completion", 5)
            if outcome == 7:
                output("abort_confirmed", {"task_id": args.task_id, "terminal_cancellation_confirmed": True}, args.json)
            return outcome
        return monitor(client, args.task_id, args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 3
    except (CliError, OSError, ValueError, KeyError, TypeError) as error:
        return common.report_error(error, "task " + (args.command if args else ""), bool(args and args.json))


if __name__ == "__main__":
    raise SystemExit(main())
