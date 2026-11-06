"""One authoritative ledger for admissions, physical attempts and reached stages."""

import math
import threading
import uuid
import secrets
import ssl
from contextlib import contextmanager
import urllib.error
import urllib.parse
import urllib.request
import json
import time
from collections import deque

from campaign_recovery import RecoveryError


class CampaignGuard:
    def __init__(self, limits, stages, clock=time.monotonic):
        self.limits = limits
        self.clock = clock
        self.started = clock()
        self.last_sample = self.started
        self.inflight = 0
        self.peak_inflight = 0
        self.inflight_seconds = 0
        self.lock = threading.Lock()
        self.attempts = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.spend_reserved = 0
        self.outcomes = deque(maxlen=50)
        self.completed = 0
        self.failed = 0
        self.latencies = deque(maxlen=50)
        self.stop_reason = None
        self.signals = []
        self.current = None
        self.stages = [dict(stage, status="not-run", actual_dwell_seconds=0,
                            attempts=0, completed=0, failures=0) for stage in stages]

    def _sample(self):
        current = self.clock()
        self.inflight_seconds += max(0, current - self.last_sample) * self.inflight
        self.last_sample = current

    def start_stage(self, name, achieved_sessions):
        with self.lock:
            stage = next(stage for stage in self.stages if stage["name"] == name)
            if self.current is not None or self.stop_reason:
                return False
            self.current = stage
            stage["status"] = "running"
            stage["achieved_sessions"] = achieved_sessions
            stage["reached_at"] = self.clock() if achieved_sessions >= stage["desired_sessions"] else None
            return True

    def reach_stage(self, achieved_sessions):
        with self.lock:
            if self.current is None or self.stop_reason:
                return False
            self.current["achieved_sessions"] = achieved_sessions
            if achieved_sessions >= self.current["desired_sessions"] and self.current["reached_at"] is None:
                self.current["reached_at"] = self.clock()
            return True

    def admit(self, input_tokens, output_tokens, price):
        with self.lock:
            if self.stop_reason or self.current is None:
                return False
            checks = (
                (self.clock() - self.started >= self.limits["active_seconds"], "active_deadline"),
                (self.attempts + 1 > self.limits["attempts"], "attempt_budget"),
                (self.input_tokens + input_tokens > self.limits["input_tokens"], "input_budget"),
                (self.output_tokens + output_tokens > self.limits["output_tokens"], "output_budget"),
                (self.spend_reserved + price > self.limits["max_spend"], "spend_budget"),
            )
            for exceeded, reason in checks:
                if exceeded:
                    self.stop_reason = reason
                    return False
            self._sample()
            self.inflight += 1
            self.peak_inflight = max(self.peak_inflight, self.inflight)
            self.attempts += 1
            self.current["attempts"] += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.spend_reserved += price
            return True

    def can_dispatch(self):
        with self.lock:
            if self.clock() - self.started >= self.limits["active_seconds"]:
                self.stop_reason = self.stop_reason or "active_deadline"
            return self.current is not None and self.stop_reason is None

    def signal(self, kind):
        if kind not in {"oom", "restart", "telemetry_missing", "operator_abort", "lease_lost", "transition_deadline"}:
            raise ValueError("unknown guard signal")
        with self.lock:
            self.signals.append(kind)
            self.stop_reason = self.stop_reason or kind

    def record(self, result):
        with self.lock:
            self._sample()
            self.inflight = max(0, self.inflight - 1)
            complete = bool(result.get("success") and result.get("complete"))
            self.outcomes.append(complete)
            self.completed += 1
            if not complete:
                self.failed += 1
            if self.current is not None:
                self.current["completed"] += 1
                if not complete:
                    self.current["failures"] += 1
            if complete and result.get("ttft_ms") is not None:
                self.latencies.append(result["ttft_ms"])
            if len(self.outcomes) >= 20 and self.outcomes.count(False) >= 5:
                self.stop_reason = self.stop_reason or "error_burst"
            if len(self.latencies) >= 20:
                ordered = sorted(self.latencies)
                if ordered[math.ceil(len(ordered) * 0.95) - 1] > 90000:
                    self.stop_reason = self.stop_reason or "latency_burst"

    def end_stage(self):
        with self.lock:
            stage = self.current
            stage["actual_dwell_seconds"] = (self.clock() - stage["reached_at"]
                                               if stage["reached_at"] is not None else 0)
            minimum_complete = 0.99 if stage.get("ramp") else 1
            stage["status"] = ("pass" if stage["reached_at"] is not None
                               and stage["actual_dwell_seconds"] >= stage["min_dwell_seconds"]
                               and stage["attempts"] and stage["completed"] == stage["attempts"]
                               and (stage["completed"] - stage["failures"]) / stage["attempts"] >= minimum_complete
                               and not self.stop_reason else "fail")
            if stage["status"] != "pass":
                self.stop_reason = self.stop_reason or "incomplete_stage"
            self.current = None
            return stage["status"] == "pass"

    def report(self):
        with self.lock:
            self._sample()
            stages = [{key: value for key, value in stage.items() if key != "reached_at"}
                      for stage in self.stages]
            mandatory_pass = all(stage["status"] == "pass" for stage in stages if stage["required"])
            status = "pass" if mandatory_pass and not self.stop_reason else "fail"
            if not self.attempts and not self.stop_reason:
                status = "not-run"
            return {"status": status, "stop_reason": self.stop_reason, "signals": list(self.signals),
                    "physical_attempts": self.attempts,
                    "active_seconds": max(0, self.last_sample - self.started),
                    "reserved_input_tokens": self.input_tokens,
                    "reserved_output_tokens": self.output_tokens, "reserved_spend": self.spend_reserved,
                    "completed": self.completed, "outstanding": self.attempts - self.completed,
                    "peak_inflight": self.peak_inflight,
                    "time_weighted_inflight": round(self.inflight_seconds / max(1e-9, self.last_sample - self.started), 3),
                    "failures": self.failed,
                    "stages": stages}


def serve_permits(guard, secret, host="127.0.0.1", port=0, journal=None,
                  tls_context=None, permit_ttl=2, wall_clock=time.time):
    """A single fail-closed controller admission endpoint for worker processes."""
    import hmac
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    if host not in {"127.0.0.1", "localhost"} and not isinstance(tls_context, ssl.SSLContext):
        raise ValueError("remote permit endpoints require configured TLS")
    if tls_context is not None and tls_context.minimum_version < ssl.TLSVersion.TLSv1_2:
        raise ValueError("permit transport requires TLS 1.2 or newer")
    if (isinstance(permit_ttl, bool) or not isinstance(permit_ttl, (int, float))
            or not math.isfinite(permit_ttl) or not 0 < permit_ttl <= 5):
        raise ValueError("permit lifetime must be finite and at most five seconds")
    issued = {}
    finished = set()
    receipts_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + secret):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048:
                    raise ValueError("invalid body size")
                data = json.loads(self.rfile.read(length))
                if self.path == "/permit":
                    values = (data["input_tokens"], data["output_tokens"], data["price"])
                    if any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) or value <= 0 for value in values):
                        raise ValueError("invalid reservation")
                    attempt_id = data["attempt_id"]
                    if not isinstance(attempt_id, str) or not 0 < len(attempt_id) <= 128:
                        raise ValueError("invalid attempt ID")
                    with receipts_lock:
                        if attempt_id not in issued:
                            admitted = guard.admit(*values)
                            if admitted and journal is not None:
                                try:
                                    journal.record_attempt(attempt_id, *values)
                                except (RecoveryError, OSError):
                                    guard.signal("lease_lost")
                                    admitted = False
                            issued[attempt_id] = {"admitted": admitted, "values": values,
                                                  "expires_at": wall_clock() + permit_ttl,
                                                  "dispatched": False}
                        elif issued[attempt_id]["values"] != values:
                            raise ValueError("reservation changed on retry")
                        grant = issued[attempt_id]
                        outcome = {"admitted": (grant["admitted"] and wall_clock() < grant["expires_at"]
                                                and not grant["dispatched"] and guard.can_dispatch()),
                                   "expires_at": grant["expires_at"]}
                elif self.path == "/dispatch":
                    attempt_id = data["attempt_id"]
                    with receipts_lock:
                        grant = issued.get(attempt_id)
                        authorized = bool(grant and grant["admitted"] and not grant["dispatched"]
                                          and attempt_id not in finished
                                          and wall_clock() < grant["expires_at"]
                                          and guard.can_dispatch())
                        if authorized:
                            grant["dispatched"] = True
                        outcome = {"authorized": authorized,
                                   "expires_at": wall_clock() + min(1, permit_ttl) if authorized else None}
                elif self.path == "/outcome":
                    attempt_id = data["attempt_id"]
                    if not isinstance(data.get("success"), bool) or not isinstance(data.get("complete"), bool):
                        raise ValueError("invalid outcome")
                    with receipts_lock:
                        grant = issued.get(attempt_id)
                        if not grant or not grant["admitted"]:
                            raise ValueError("unknown attempt")
                        if data["success"] and not grant["dispatched"]:
                            raise ValueError("undispatched attempt cannot succeed")
                        if attempt_id not in finished:
                            if journal is not None:
                                journal.record_outcome(attempt_id, data["success"] and data["complete"])
                            guard.record(data)
                            finished.add(attempt_id)
                    outcome = {"recorded": True}
                elif self.path == "/signal":
                    guard.signal(data["kind"])
                    outcome = {"stopped": True}
                else:
                    self.send_error(404)
                    return
            except (ValueError, KeyError, TypeError):
                self.send_error(400)
                return
            response = json.dumps(outcome).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    class PermitServer(ThreadingHTTPServer):
        request_queue_size = 256

    server = PermitServer((host, port), Handler)
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    return server


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


@contextmanager
def running_permits(guard, journal=None):
    secret = secrets.token_urlsafe(32)
    server = serve_permits(guard, secret, journal=journal)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    heartbeat_stop = threading.Event()
    heartbeat_thread = None
    if journal is not None:
        def renew_lease():
            while not heartbeat_stop.wait(min(10, journal.lease_seconds / 3)):
                try:
                    journal.heartbeat()
                except (RecoveryError, OSError):
                    guard.signal("lease_lost")
                    return

        heartbeat_thread = threading.Thread(target=renew_lease, daemon=True)
        heartbeat_thread.start()
    try:
        yield WorkerClient(f"http://127.0.0.1:{server.server_port}", secret)
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class WorkerClient:
    """Fail-closed permit client; a missing reply never authorizes an attempt."""

    def __init__(self, url, secret, timeout=2, ca_file=None, wall_clock=time.time):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        )) or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("worker permit URL must use HTTPS or local loopback")
        self.url = url.rstrip("/")
        self.secret = secret
        self.timeout = timeout
        self.wall_clock = wall_clock
        self.ca_context = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
        self.expires = {}
        self.lost = False

    def _send(self, route, payload):
        request = urllib.request.Request(
            self.url + route, data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + self.secret,
                     "Content-Type": "application/json"})
        try:
            opener = urllib.request.build_opener(NoRedirects, urllib.request.ProxyHandler({}),
                                                 urllib.request.HTTPSHandler(context=self.ca_context))
            with opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except (OSError, ValueError):
            self.lost = True
            return None

    def permit(self, input_tokens, output_tokens, price):
        if self.lost:
            return None
        attempt_id = uuid.uuid4().hex
        result = self._send("/permit", {"attempt_id": attempt_id, "input_tokens": input_tokens,
                                        "output_tokens": output_tokens, "price": price})
        if not result or not result.get("admitted") or self.wall_clock() >= result.get("expires_at", 0):
            return None
        self.expires[attempt_id] = result["expires_at"]
        return attempt_id

    def dispatch(self, attempt_id):
        if self.lost or attempt_id not in self.expires or self.wall_clock() >= self.expires[attempt_id]:
            return False
        result = self._send("/dispatch", {"attempt_id": attempt_id})
        return bool(result and result.get("authorized") and
                    self.wall_clock() < (result.get("expires_at") or 0))

    def finish(self, attempt_id, row):
        if self.lost:
            return False
        result = self._send("/outcome", {"attempt_id": attempt_id,
                                         "success": bool(row.get("success")),
                                         "complete": bool(row.get("complete")),
                                         "ttft_ms": row.get("ttft_ms")})
        return bool(result and result.get("recorded"))
