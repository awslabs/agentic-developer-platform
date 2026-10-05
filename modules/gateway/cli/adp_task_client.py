#!/usr/bin/env python3
"""Packaged Task protocol helpers from examples/task-api/client.py.

Native deployment-bound authentication and safe output live in adp-task.py.
"""

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class APIError(RuntimeError):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__(f"Task API HTTP {status}: {body.decode('utf-8', errors='replace')[:4096]}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def endpoint(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise ValueError("An HTTPS endpoint without credentials/query/fragment is required")
    return url.rstrip("/")


class Client:
    def __init__(
        self,
        base_url,
        token=None,
        *,
        token_url=None,
        client_id=None,
        client_secret=None,
        opener=None,
    ):
        self.base = endpoint(base_url)
        self.token, self.expires = token, float("inf") if token else 0
        self.token_url = endpoint(token_url) if token_url else None
        self.client_id, self.client_secret = client_id, client_secret
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def authenticate(self):
        if self.token and time.monotonic() < self.expires:
            return
        if not all((self.token_url, self.client_id, self.client_secret)):
            raise ValueError("Set ADP_TASK_TOKEN or all ADP_TASK_TOKEN_URL, ADP_TASK_CLIENT_ID, ADP_TASK_CLIENT_SECRET")
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        data = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "scope": " ".join("adp-tasks/" + scope for scope in ("submit", "read", "input", "cancel", "artifacts")),
            }
        ).encode()
        request = urllib.request.Request(
            self.token_url,
            data=data,
            headers={
                "Authorization": "Basic " + basic,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with self.opener.open(request, timeout=30) as response:
            result = json.loads(response.read(65537))
        self.token = result["access_token"]
        self.expires = time.monotonic() + max(0, int(result["expires_in"]) - 30)

    def open(self, method, path, data=None, headers=None, *, retry=False, timeout=30):
        self.authenticate()
        supplied = dict(headers or {})
        supplied["Authorization"] = "Bearer " + self.token
        for attempt in range(3 if retry else 1):
            try:
                return self.opener.open(
                    urllib.request.Request(self.base + path, data=data, headers=supplied, method=method),
                    timeout=timeout,
                )
            except urllib.error.HTTPError as error:
                body = error.read(65537)
                if not retry or error.code not in (429, 502, 503, 504) or attempt == 2:
                    raise APIError(error.code, body) from None
                delay = error.headers.get("Retry-After", "")
                time.sleep(min(10, max(0, int(delay))) if delay.isdigit() else 2**attempt)
            except (urllib.error.URLError, TimeoutError):
                if not retry or attempt == 2:
                    raise
                time.sleep(2**attempt)

    def json(self, method, path, body=None, headers=None, *, retry=False):
        headers = {"Content-Type": "application/json", **(headers or {})}
        data = None if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
        with self.open(method, path, data, headers, retry=retry) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("Response exceeds client bound")
            return json.loads(raw)

    def submit(self, body, key):
        if not key:
            raise ValueError("Persist an idempotency key before submitting")
        return self.json("POST", "/v1/tasks", body, {"Idempotency-Key": key}, retry=True)

    def snapshot(self, task_id):
        return self.json("GET", "/v1/tasks/" + segment(task_id), retry=True)

    def command(self, task_id, kind, command_id, text):
        if kind not in ("messages", "cancel"):
            raise ValueError("Unknown command")
        body = {
            "schema_version": "1.0",
            "command_id": command_id,
            "text" if kind == "messages" else "reason": text,
        }
        return self.json("POST", f"/v1/tasks/{segment(task_id)}/{kind}", body, retry=True)

    def events(self, task_id, cursor=None, *, seconds=60, max_events=100):
        if not 0 < seconds <= 600 or not 0 < max_events <= 10000:
            raise ValueError("SSE bounds must be seconds1..600 and events1..10000")
        deadline = time.monotonic() + seconds
        count = 0
        while time.monotonic() < deadline and count < max_events:
            headers = {"Accept": "text/event-stream"}
            if cursor:
                headers["Last-Event-ID"] = cursor
            try:
                with self.open(
                    "GET",
                    f"/v1/tasks/{segment(task_id)}/events",
                    headers=headers,
                    # Leave headroom beyond the server's 15-second heartbeat interval.
                    timeout=min(35, max(0.1, deadline - time.monotonic())),
                ) as response:
                    for event in parse_sse(response, deadline):
                        event_id = event.get("id")
                        if event_id is not None and event_id == cursor:
                            continue
                        if event_id is not None:
                            cursor = event_id
                        count += 1
                        yield event
                        if event_type(event) in ("task.completed", "task.failed", "task.cancelled") or count >= max_events:
                            return
            except (urllib.error.URLError, TimeoutError):
                pass
            if time.monotonic() < deadline:
                time.sleep(min(1, deadline - time.monotonic()))


def event_type(event):
    """Public SSE uses event:event; the persisted kind is data.type."""
    payload = event.get("data")
    return payload.get("type", event.get("event")) if isinstance(payload, dict) else event.get("event")


def segment(value):
    return urllib.parse.quote(value, safe="")


def parse_sse(stream, deadline):
    event, data, size = {}, [], 0
    while time.monotonic() < deadline:
        line = stream.readline(65537)
        if not line:
            return
        size += len(line)
        if len(line) > 65536 or size > 262144:
            raise ValueError("SSE frame exceeds client bound")
        text = line.decode("utf-8").rstrip("\r\n")
        if not text:
            if data:
                event["data"] = json.loads("\n".join(data))
                yield event
            event, data, size = {}, [], 0
        elif not text.startswith(":"):
            field, _, value = text.partition(":")
            value = value.removeprefix(" ")
            if field == "data":
                data.append(value)
            elif field in ("id", "event"):
                event[field] = value
