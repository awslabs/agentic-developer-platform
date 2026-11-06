"""Retain the same sanitized board served by the Claude/Codex live feed."""

import json
import hashlib
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from lib import run_report

logger = logging.getLogger(__name__)


class TaskBoardReporter:
    def __init__(self, path, run_id, interval=10):
        self.path, self.run_id, self.interval = Path(path), run_id, interval
        self._stop = threading.Event()
        self._thread = None
        self._sent = None
        self._pending = None
        self._revision = None
        self._failure = None

    def _diagnostic(self, code):
        self._failure = code
        # No task content, raw upstream response or credential enters diagnostics.
        try:
            target = Path(str(self.path) + ".checkpoint-receipt")
            temporary = Path(str(target) + ".tmp")
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as target_file:
                target_file.write(json.dumps({"code": code, "checkpoint_revision": self._revision}))
            temporary.replace(target)
        except OSError:
            pass

    def _send(self):
        body = self._pending
        if body is None:
            return
        receipt = run_report.request("/task-board", body, timeout=3)
        if receipt.get("recorded") is not True:
            raise ValueError("unacknowledged")
        if "story_checkpoint" in body:
            digest = hashlib.sha256(json.dumps(body["story_checkpoint"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            if receipt.get("checkpoint_revision") != digest:
                raise ValueError("checkpoint unacknowledged")
            self._revision = digest
            self._diagnostic("story_checkpoint_acknowledged")
        self._sent = {key: value for key, value in body.items() if key != "expected_checkpoint_revision"}
        self._pending = None

    def publish(self):
        try:
            # Retry the exact in-flight write before coalescing newer local work.
            # Otherwise a lost acknowledgement would make our CAS parent stale.
            self._send()
            body = None
            display_failed = False
            try:
                if self.path.exists():
                    with self.path.open("rb") as source:
                        raw = source.read(32769)
                    if len(raw) > 32768:
                        raise ValueError("display oversized")
                    event = json.loads(raw)
                    if (not isinstance(event, dict) or event.get("version") != 1
                            or event.get("invocation_id") != self.run_id or event.get("kind") != "explanation"):
                        raise ValueError("display identity")
                    payload = event.get("payload") or {}
                    if payload.get("progress", {}).get("category") != "plan" or not isinstance(payload.get("text"), str):
                        raise ValueError("display shape")
                    if (type(event.get("generation")) is not int or event["generation"] < 1
                            or type(event.get("sequence")) is not int or event["sequence"] < 1
                            or not 1 <= len(payload["text"]) <= 16384
                            or not isinstance(event.get("timestamp"), str)):
                        raise ValueError("display contract")
                    observed = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
                    if observed.tzinfo is None or observed.utcoffset() is None:
                        raise ValueError("display timestamp")
                    body = {"generation": event["generation"], "sequence": event["sequence"],
                            "observed_at": event["timestamp"], "text": payload["text"]}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                body = None
                display_failed = True
                # A broken/oversized public render cannot discard a valid private delta.
            private = Path(str(self.path) + ".checkpoint")
            if private.exists():
                with private.open("rb") as source:
                    raw = source.read(128 * 1024 + 1)
                if len(raw) > 128 * 1024:
                    raise ValueError("checkpoint oversized")
                checkpoint = json.loads(raw)
                if checkpoint.get("version") != 1 or checkpoint.get("invocation_id") != self.run_id or checkpoint["story_checkpoint"]["scope"]["run_id"] != self.run_id:
                    raise ValueError("checkpoint identity")
                if type(checkpoint.get("sequence")) is not int or checkpoint["sequence"] < 1:
                    raise ValueError("checkpoint sequence")
                if body is None:
                    # The private path works even when public rendering is too large.
                    body = {"generation": 1, "sequence": checkpoint["sequence"],
                        "observed_at": datetime.now(UTC).isoformat(),
                        "text": "Structured story progress is retained separately; no partial completion total is shown."}
                    if self._sent and self._sent.get("story_checkpoint") == checkpoint["story_checkpoint"]:
                        return True
                body["story_checkpoint"] = checkpoint["story_checkpoint"]
                if self._revision is None:
                    self._revision = checkpoint.get("parent_revision")
            if body is None:
                if display_failed:
                    raise ValueError("display unavailable")
                return True
            if body == self._sent:
                return True
            self._pending = {**body, **({"expected_checkpoint_revision": self._revision} if "story_checkpoint" in body else {})}
            if len(json.dumps(self._pending).encode()) > 128 * 1024:
                self._pending = None
                raise ValueError("envelope oversized")
            self._send()
            return True
        except Exception:
            self._diagnostic("story_checkpoint_persistence_unconfirmed")
            logger.warning("story_checkpoint_persistence_unconfirmed: only the last acknowledged checkpoint is durable; retrying the same write")
            return False

    def _run(self):
        while not self._stop.wait(self.interval):
            self.publish()

    def __enter__(self):
        if run_report.enabled():
            self._thread = threading.Thread(target=self._run, name="task-board-report", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_):
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
            # Flush before terminal reporting closes this assignment.
            if not self.publish():
                logger.warning("story_checkpoint_final_flush_failed: preserve this run's activity; read repair_tasks before resuming because recent task changes may not be durable")
