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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
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
_MAX_POLICY_FIELD_BYTES = 256
_RESOLUTION_SOURCES = frozenset({"explicit-direct", "principal-mapping", "system-default"})


class RunIdentityError(Exception):
    """The worker cannot establish or refresh its own authority."""


class WorkOwnershipPending(RunIdentityError):
    """The gateway retained an authorized child behind its active parent."""


@dataclass(frozen=True)
class ModelPolicyReport:
    """Sanitized, non-authoritative report-only evidence from bootstrap.

    The proposed model is intentionally not an execution input.  The gateway
    response is authenticated transport, but worker-side signature verification
    and enforcing consumption belong to the later PMM-06/PMM-09 gate.
    """

    status: Literal["proposed", "unavailable"]
    reason: str | None = None
    requested_model_id: str | None = None
    resolved_model_id: str | None = None
    resolution_source: str | None = None
    snapshot_digest: str | None = None
    policy_revision: str | None = None
    catalogue_revision: str | None = None
    posture_revision: int | None = None

    def environment(self, legacy_model: str) -> dict[str, str]:
        """Return comparison telemetry without changing ``ANTHROPIC_MODEL``."""
        values = {
            "ADP_MODEL_POLICY_POSTURE": "report_only",
            "ADP_MODEL_POLICY_STATUS": self.status,
        }
        if self.status == "unavailable":
            values["ADP_MODEL_POLICY_REASON"] = self.reason or "unknown"
            return values
        values.update(
            {
                "ADP_MODEL_POLICY_PROPOSED_MODEL": self.resolved_model_id or "",
                "ADP_MODEL_POLICY_RESOLUTION_SOURCE": self.resolution_source or "",
                "ADP_MODEL_POLICY_SNAPSHOT_DIGEST": self.snapshot_digest or "",
                "ADP_MODEL_POLICY_POLICY_REVISION": self.policy_revision or "",
                "ADP_MODEL_POLICY_CATALOGUE_REVISION": self.catalogue_revision or "",
                "ADP_MODEL_POLICY_POSTURE_REVISION": str(self.posture_revision),
                "ADP_MODEL_POLICY_LEGACY_MODEL": legacy_model,
                "ADP_MODEL_POLICY_MATCH": str(self.resolved_model_id == legacy_model).lower(),
            }
        )
        if self.requested_model_id:
            values["ADP_MODEL_POLICY_REQUESTED_MODEL"] = self.requested_model_id
        return values


def _safe_policy_text(value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_POLICY_FIELD_BYTES
        or any(ord(char) < 32 or ord(char) > 126 for char in value)
    ):
        raise ValueError("invalid model-policy field")
    return value


def parse_model_policy_report(value: object, *, invocation_id: str) -> ModelPolicyReport:
    """Strictly reduce a bootstrap policy response to report-only telemetry."""
    if not isinstance(value, dict) or value.get("posture") != "report_only":
        raise ValueError("invalid model-policy response")
    status = value.get("status")
    if status == "unavailable":
        return ModelPolicyReport(
            status="unavailable",
            reason=_safe_policy_text(value.get("reason")),
        )
    if status != "proposed" or not isinstance(value.get("decision"), dict):
        raise ValueError("invalid model-policy response")
    decision = value["decision"]
    resolved = _safe_policy_text(decision.get("resolved_model_id"))
    requested = _safe_policy_text(decision.get("requested_model_id"), optional=True)
    source = _safe_policy_text(decision.get("resolution_source"))
    digest = _safe_policy_text(decision.get("snapshot_digest"))
    policy_revision = _safe_policy_text(decision.get("policy_revision"))
    catalogue_revision = _safe_policy_text(decision.get("catalogue_revision"))
    posture_revision = decision.get("posture_revision")
    if (
        decision.get("invocation_id") != invocation_id
        or decision.get("runtime_posture") != "report_only"
        or source not in _RESOLUTION_SOURCES
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
        or type(posture_revision) is not int
        or posture_revision < 1
        or not isinstance(value.get("assertion"), str)
        or not value["assertion"].startswith("adpe1.")
    ):
        raise ValueError("invalid model-policy response")
    return ModelPolicyReport(
        status="proposed",
        requested_model_id=requested,
        resolved_model_id=resolved,
        resolution_source=source,
        snapshot_digest=digest,
        policy_revision=policy_revision,
        catalogue_revision=catalogue_revision,
        posture_revision=posture_revision,
    )


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
        self._model_policy_reported = False
        self.model_policy_report: ModelPolicyReport | None = None

    def _request(self) -> dict:
        session = botocore.session.get_session()
        from adp_trigger.transport_identity import gateway_signing_region, worker_credentials

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
            gateway_signing_region(self._url),
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
                    if response.status_code == 425:
                        raise WorkOwnershipPending("waiting for exclusive work ownership")
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
        # PMM-06 report-only compatibility: the gateway may return a signed
        # proposed decision, but this worker must keep the legacy assignment
        # until PMM-09 enables enforcement.  Reporting the ignored proposal is
        # intentional evidence; silently accepting or silently dropping it
        # would make mixed-version rollout impossible to audit.
        if not self._model_policy_reported:
            policy = result.get("model_policy")
            # Absence is expected while an older gateway is still serving a
            # mixed-version rollout.  Keep looking on refresh so a newly
            # upgraded gateway can still emit comparison evidence.
            if policy is not None:
                try:
                    self.model_policy_report = parse_model_policy_report(
                        policy,
                        invocation_id=self._invocation_id,
                    )
                except ValueError:
                    self.model_policy_report = ModelPolicyReport(
                        status="unavailable",
                        reason="invalid_gateway_report",
                    )
                if self.model_policy_report.status == "proposed":
                    logger.info("Model-policy decision received and ignored by report-only worker")
                else:
                    logger.warning(
                        "Model-policy decision unavailable in report-only mode (reason=%s)",
                        self.model_policy_report.reason,
                    )
                self._model_policy_reported = True
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
        deadline = time.monotonic() + 1800
        while True:
            try:
                self.refresh()
                break
            except WorkOwnershipPending:
                if time.monotonic() >= deadline or self._stop.wait(10):
                    raise RunIdentityError("work ownership startup deadline exceeded") from None
                logger.info("Authorized child is waiting for its parent to release work ownership")
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
    claims_required = (
        envelope.get("work_claim_required") is True
        or os.environ.get("ADP_WORK_CLAIMS_ENABLED", "false").lower() == "true"
    )
    if os.environ.get("ADP_AGENT_AUTHORITY_ENABLED", "false").lower() != "true":
        if claims_required:
            raise RunIdentityError("Work ownership requires protected worker identity")
        return None
    identity = RunIdentitySession(envelope=envelope)
    identity.start()
    return identity
