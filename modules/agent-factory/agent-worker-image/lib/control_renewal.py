"""Coordinate listener credential rotation with the gateway, preserving the journal.

Stage both tokens locally before publishing the new one. A lost response keeps
the same request and token for retry. The previous token has at most 30 seconds
of overlap; expiry or an unavailable lease file fails closed in the listener.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


class ControlRenewal:
    def __init__(
        self, *, run_id, generation, token, expires_at, post=None, directory=None, now=time.time
    ):
        self._run_id = run_id
        self._generation = generation
        self._current = {"epoch": 1, "token": token, "expires_at": expires_at}
        self._pending = None
        self._post = post
        self._now = now
        self._directory = Path(directory or tempfile.mkdtemp(prefix="adp-control-lease-"))
        self.path = self._directory / "lease.json"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    def _write(self, current, previous=None):
        doc = {
            "version": 1,
            "run_id": self._run_id,
            "generation": self._generation,
            "current": current,
        }
        if previous:
            doc.update(previous=previous, staged_at=_iso(self._now()))
        fd, temporary = tempfile.mkstemp(prefix="lease-", dir=self._directory)
        try:
            with os.fdopen(fd, "w") as target:
                json.dump(doc, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def start(self):
        self._write(self._current)
        self._thread = threading.Thread(target=self._run, name="adp-control-renewal", daemon=True)
        self._thread.start()

    def refresh(self):
        with self._lock:
            if self._stop.is_set():
                return
            if self._post is None:
                from lib.status_gateway_client import _post

                post = _post
            else:
                post = self._post
            if (
                self._pending
                and datetime.fromisoformat(self._pending["control_token_expires_at"]).timestamp()
                <= self._now() + 10
            ):
                # A long outage can outlast the staged token. Resolve its exact
                # committed epoch before replacing the expired pending intent.
                state = post(
                    "/control/registration/state", {"control_generation": self._generation}
                )
                epoch = state.get("control_credential_epoch")
                if state.get("control_generation") != self._generation:
                    raise ValueError("control generation changed during renewal")
                if (
                    epoch == self._pending["expected_epoch"] + 1
                    and state.get("rotation_id") == self._pending["rotation_id"]
                ):
                    self._current = {
                        "epoch": epoch,
                        "token": self._pending["control_token"],
                        "expires_at": self._pending["control_token_expires_at"],
                    }
                elif epoch != self._pending["expected_epoch"]:
                    raise ValueError("control rotation was superseded")
                self._pending = None
            if self._pending is None:
                now = self._now()
                current = {
                    "epoch": self._current["epoch"] + 1,
                    "token": secrets.token_urlsafe(32),
                    "expires_at": _iso(now + 3600),
                }
                previous = {**self._current, "valid_until": _iso(now + 30)}
                # Persist before sending: the gateway must never learn a token
                # the live listener cannot yet accept.
                self._write(current, previous)
                self._pending = {
                    "control_generation": self._generation,
                    "expected_epoch": self._current["epoch"],
                    "rotation_id": str(uuid.uuid4()),
                    "control_token": current["token"],
                    "control_token_expires_at": current["expires_at"],
                }
            result = post("/control/registration/renew", dict(self._pending))
            epoch = self._pending["expected_epoch"] + 1
            if (
                result.get("control_generation") != self._generation
                or result.get("control_credential_epoch") != epoch
                or result.get("rotation_id") != self._pending["rotation_id"]
            ):
                raise ValueError("control renewal response does not match the pending rotation")
            self._current = {
                "epoch": epoch,
                "token": self._pending["control_token"],
                "expires_at": self._pending["control_token_expires_at"],
            }
            self._pending = None
            # Keep the staged file: its previous-token deadline never moves on
            # retry or acknowledgement. The next rotation replaces it.

    def _run(self):
        while not self._stop.wait(self._refresh_delay()):
            try:
                self.refresh()
            except Exception:
                logger.warning("Control renewal failed; retry retains the same rotation and expiry")

    def _refresh_delay(self):
        if self._pending is not None:
            return 10
        remaining = datetime.fromisoformat(self._current["expires_at"]).timestamp() - self._now()
        return max(1, min(300, remaining / 2))

    def close(self):
        self._stop.set()
        with self._lock:
            self.path.unlink(missing_ok=True)
