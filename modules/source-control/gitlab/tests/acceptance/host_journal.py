#!/usr/bin/env python3
"""Retain trusted Docker start observations on the disposable job host.

The private journal survives short-lived containers; the same safe records go
to the EC2 console for read-only provider collection. No job or AWS credentials
are needed. Missing events, failed inspections and truncated console histories
remain incomplete evidence, never successful confinement or absence evidence.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time
import uuid
from urllib.parse import urlencode

import host_observation as host

PREFIX = "ADP_GITLAB_HOST_V1 "


def source_hash():
    return hashlib.sha256(Path(__file__).read_bytes() + b"\0" + Path(host.__file__).read_bytes()).hexdigest()


class Journal:
    def __init__(self, output, console, boot_id, *, clock=time.time_ns, stream_id=None):
        host.require(re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot_id), "boot_id_invalid")
        self.output, self.console, self.boot_id, self.clock = output, console, boot_id, clock
        self.stream_id = stream_id or str(uuid.uuid4())
        self.sequence, self.lock = 0, threading.Lock()
        self.source = source_hash()

    def emit(self, kind, data):
        with self.lock:
            self.sequence += 1
            host.require(self.sequence <= 2048, "observation_bound_exceeded")
            record = {"schema_version": 1, "boot_id": self.boot_id, "stream_id": self.stream_id, "sequence": self.sequence,
                      "observer_sha256": self.source, "observed_at_unix_ns": self.clock(),
                      "kind": kind, "data": data}
            raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            host.require(len(raw) <= 12288, "observation_record_too_large")
            self.output.write(raw + b"\n")
            self.output.flush()
            os.fsync(self.output.fileno())
            self.console.write(b"\n" + PREFIX.encode() + base64.b64encode(raw) + b"\n")
            self.console.flush()
            return record


def event_record(event, journal, *, observe=host.observation):
    """Project only native numeric correlation labels, never event attributes."""
    if event.get("Type") != "container" or event.get("Action") not in {"start", "die", "destroy"}:
        return
    actor = event.get("Actor", {})
    labels = actor.get("Attributes", {})
    if labels.get(host.PREFIX + "managed") != "true":
        return
    container = actor.get("ID", "")
    host.require(re.fullmatch(r"[0-9a-f]{64}", container), "event_container_invalid")
    expected = {}
    for label, field in (("job.id", "job_id"), ("project.id", "project_id"), ("pipeline.id", "pipeline_id")):
        value = labels.get(host.PREFIX + label, "")
        host.require(re.fullmatch(r"[1-9][0-9]*", value), "event_correlation_missing")
        expected[field] = int(value)
    expected["source_revision"] = labels.get(host.PREFIX + "job.sha", "")
    host.require(re.fullmatch(r"[0-9a-f]{40}", expected["source_revision"]), "event_source_missing")
    host.require(type(event.get("timeNano")) is int and event["timeNano"] > 0, "event_time_missing")
    journal.emit("container_event", {**expected, "container_id": container, "action": event["Action"],
                                    "docker_time_unix_ns": event["timeNano"]})
    if event["Action"] == "start":
        try:
            result = observe(container, expected)
        except Exception:
            journal.emit("observation_incomplete", {**expected, "container_id": container,
                                                   "reason": "running_kernel_observation_unavailable"})
        else:
            journal.emit("container_observation", result)


def notify_ready():
    address = os.environ.get("NOTIFY_SOCKET")
    host.require(address and (address.startswith("/") or address.startswith("@")), "notify_socket_missing")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
        connection.connect("\0" + address[1:] if address.startswith("@") else address)
        connection.sendall(b"READY=1")


def watch(journal, *, ready=notify_ready, startup=host.startup_identity):
    # sshd depends on this service becoming ready. Docker has started, but the
    # manager cannot yet connect and create its first container.
    connection = host.DockerConnection()
    stopped = threading.Event()
    try:
        since = time.time_ns() // 1_000_000_000
        connection.request("GET", "/containers/json?all=true")
        response = connection.getresponse()
        host.require(response.status == 200 and json.loads(response.read(1024 * 1024)) == [],
                     "observer_did_not_start_before_containers")
        connection.close()
        connection = host.DockerConnection()
        query = urlencode({"since": str(since), "filters": json.dumps({"type": ["container"],
                            "event": ["start", "die", "destroy"]}, separators=(",", ":"))})
        connection.request("GET", "/events?" + query)
        response = connection.getresponse()
        host.require(response.status == 200, "docker_event_stream_unavailable")
        connection.sock.settimeout(None)
        journal.emit("stream_started", {"docker_since_unix_seconds": since,
                                      "executor_ready": False, **startup()})
        ready()

        def heartbeat():
            while not stopped.wait(30):
                try:
                    journal.emit("heartbeat", {"executor_ready": Path("/run/adp-job-ready").exists()})
                except Exception:
                    # Failed durable retention must stop readiness, including
                    # when no new Docker event would wake the reader.
                    os._exit(1)

        threading.Thread(target=heartbeat, daemon=True).start()
        while True:
            line = response.readline(128 * 1024 + 1)
            host.require(line and len(line) <= 128 * 1024, "docker_event_stream_incomplete")
            event_record(json.loads(line), journal)
    finally:
        stopped.set()
        connection.close()


def main():
    host.require(os.geteuid() == 0, "trusted_host_root_required")
    directory = Path("/var/lib/adp-job-observations")
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    host.require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
                 "private_root_journal_required")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    # A restart creates a separate retained stream; it cannot erase a gap in
    # the first stream or masquerade as coverage from boot.
    path = directory / (boot + "-" + str(time.time_ns()) + ".jsonl")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output, open("/dev/console", "wb", buffering=0) as console:
        journal = Journal(output, console, boot)
        try:
            watch(journal)
        except Exception:
            journal.emit("stream_incomplete", {"reason": "trusted_observation_stream_unavailable"})
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
