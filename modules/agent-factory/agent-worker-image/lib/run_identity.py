"""Acquire the assigned run identity before repository execution; refresh live.

Only the pod-bound Kubernetes proof goes to bootstrap. No claimed tenant,
parent, persona or human-root field is exchanged for authority. The gateway
compares the complete queue-envelope digest with its protected dispatch record.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import botocore.auth
import botocore.awsrequest
import botocore.session
import requests

logger = logging.getLogger(__name__)
WORKLOAD_TOKEN_ENV = "ADP_WORKLOAD_TOKEN_FILE"
CREDENTIAL_FILE_ENV = "ADP_RUN_CREDENTIAL_FILE"
CONTROL_ENDPOINT_ENV = "ADP_AGENT_CONTROL_ENDPOINT"
WORKLOAD_HEADER = "X-Adp-Workload-Token"
_MAX_TOKEN_BYTES = 8192


class RunIdentityError(Exception):
    """The worker cannot establish or refresh its own authority."""


def read_workload_token() -> str:
    try:
        with open(os.environ[WORKLOAD_TOKEN_ENV], "rb") as source:
            raw = source.read(_MAX_TOKEN_BYTES + 1)
        token = raw.decode("ascii").strip()
        if not token or len(raw) > _MAX_TOKEN_BYTES or any(ord(c) <= 32 for c in token):
            raise ValueError("invalid workload token")
        return token
    except (OSError, KeyError, ValueError):
        raise RunIdentityError("projected workload token unavailable") from None


class RunIdentitySession:
    def __init__(self, *, envelope: dict, directory: Path | None = None) -> None:
        base = os.environ.get(CONTROL_ENDPOINT_ENV, "").rstrip("/")
        parsed = urlparse(base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise RunIdentityError("agent authority endpoint is not configured")
        self._url = base + "/bootstrap"
        self._invocation_id = envelope["message_id"]
        self._digest = hashlib.sha256(
            json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        self._directory = directory or Path(tempfile.mkdtemp(prefix="adp-run-identity-"))
        self.credential_path = self._directory / "credential"
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._attempt: int | None = None

    def _request(self) -> dict:
        session = botocore.session.get_session()
        from adp_trigger.transport_identity import worker_credentials

        credentials = worker_credentials(session)
        if credentials is None:
            raise RunIdentityError("worker transport identity unavailable")
        data = json.dumps(
            {"invocation_id": self._invocation_id, "envelope_digest": self._digest}
        ).encode()
        signed = botocore.awsrequest.AWSRequest(
            method="POST",
            url=self._url,
            data=data,
            headers={"Content-Type": "application/json", WORKLOAD_HEADER: read_workload_token()},
        )
        botocore.auth.SigV4Auth(
            credentials.get_frozen_credentials(),
            "execute-api",
            os.environ.get("AWS_REGION", "us-east-1"),
        ).add_auth(signed)
        try:
            with requests.Session() as http:
                http.trust_env = False
                with http.post(
                    self._url,
                    data=data,
                    headers=dict(signed.headers),
                    timeout=10,
                    allow_redirects=False,
                    stream=True,
                ) as response:
                    if response.status_code != 200:
                        raise RunIdentityError("gateway refused run identity")
                    raw = response.raw.read(8193, decode_content=True)
                    if len(raw) > 8192:
                        raise RunIdentityError("invalid identity response")
                    return json.loads(raw)
        except (requests.RequestException, ValueError, OSError):
            raise RunIdentityError("run identity service unavailable") from None

    def refresh(self) -> None:
        result = self._request()
        token = result.get("credential")
        attempt = result.get("attempt")
        if (
            result.get("invocation_id") != self._invocation_id
            or type(attempt) is not int
            or attempt < 1
            or (self._attempt is not None and attempt != self._attempt)
            or not isinstance(token, str)
            or not token.startswith("adpr1.")
            or len(token) > 4096
            or any(ord(c) <= 32 or ord(c) >= 127 for c in token)
        ):
            raise RunIdentityError("invalid run identity response")
        with self._write_lock:
            if self._stop.is_set():
                return
            fd, temporary = tempfile.mkstemp(prefix="credential-", dir=self._directory)
            try:
                with os.fdopen(fd, "w", encoding="ascii") as target:
                    target.write(token + "\n")
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, self.credential_path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            self._attempt = attempt

    def start(self) -> None:
        self.refresh()  # Fail before repository execution if bootstrap refused.
        os.environ[CREDENTIAL_FILE_ENV] = str(self.credential_path)
        self._thread = threading.Thread(
            target=self._renew, name="adp-run-identity-refresh", daemon=True
        )
        self._thread.start()
        atexit.register(self.close)

    def _renew(self) -> None:
        while not self._stop.wait(300):
            try:
                self.refresh()
            except Exception:
                # Never log HTTP exceptions, response bodies or file contents.
                # Keep the existing short-lived credential; after its expiry
                # the gateway refuses it until a verified refresh succeeds.
                logger.warning(
                    "Run identity refresh failed; existing credential retains its original expiry"
                )

    def close(self) -> None:
        self._stop.set()
        with self._write_lock:
            self.credential_path.unlink(missing_ok=True)


def bootstrap_run_identity(envelope: dict) -> RunIdentitySession | None:
    claims_required = envelope.get("work_claim_required") is True or os.environ.get("ADP_WORK_CLAIMS_ENABLED", "false").lower() == "true"
    if os.environ.get("ADP_AGENT_AUTHORITY_ENABLED", "false").lower() != "true":
        if claims_required:
            raise RunIdentityError("Work ownership requires protected worker identity")
        return None
    identity = RunIdentitySession(envelope=envelope)
    identity.start()
    return identity
