"""Acquire the assigned run identity before repository execution; refresh live.

Only the pod-bound Kubernetes proof goes to bootstrap. No claimed tenant,
parent, persona or human-root field is exchanged for authority. The gateway
compares the complete queue-envelope digest with its protected dispatch record.
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import botocore.auth
import botocore.awsrequest
import botocore.session
import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger(__name__)
WORKLOAD_TOKEN_ENV = "ADP_WORKLOAD_TOKEN_FILE"
CREDENTIAL_FILE_ENV = "ADP_RUN_CREDENTIAL_FILE"
CONTROL_ENDPOINT_ENV = "ADP_AGENT_CONTROL_ENDPOINT"
WORKLOAD_HEADER = "X-Adp-Workload-Token"
MODEL_POLICY_KEYS_ENV = "ADP_CONTROL_ENVELOPE_KEYS"
MODEL_POLICY_KEYS_FILE_ENV = "ADP_CONTROL_ENVELOPE_KEYS_FILE"
MODEL_POLICY_AUDIENCE = "adp-agent-model-policy"
MODEL_POLICY_ACTION = "resolve_model"
MODEL_POLICY_SCHEMA_VERSION = 1
#: What this worker promises to *honour*, declared to the gateway on bootstrap.
#:
#: Distinct from ``MODEL_POLICY_SCHEMA_VERSION``, which versions the decision
#: payload's shape.  This asserts a capability: "if you hand me an enforcing
#: decision, I will execute on it or fail, and I will not quietly launch on my
#: legacy assignment instead".  The gateway withholds run authority from a client
#: that does not declare it while enforcing, because version skew would otherwise
#: bypass enforcement invisibly.
#:
#: It is a capability assertion only and confers no authority whatsoever: the
#: gateway decides the posture and the model, and declaring a higher number here
#: cannot obtain a decision, relax a gate, or alter what is signed.
MODEL_POLICY_CONTRACT_VERSION = 1
#: The closed posture vocabulary this worker understands.  ``enforcing`` is
#: supported *in source* here so the runtime can be proven end-to-end; every
#: actually configured environment remains ``report_only``, and the flip itself
#: is PMM-09's separate, audited operational change.
MODEL_POLICY_POSTURES = frozenset({"disabled", "report_only", "enforcing"})
ENVELOPE_VERSION = "adpe1"
ENVELOPE_ISSUER = "adp-gateway-control"
MAX_ENVELOPE_TTL_SECONDS = 30
_MAX_TOKEN_BYTES = 8192
_MAX_ENVELOPE_BYTES = 8192
_MAX_KEY_CONFIG_BYTES = 64 * 1024
_MAX_POLICY_FIELD_BYTES = 256
_RESOLUTION_SOURCES = frozenset({"explicit-direct", "principal-mapping", "system-default"})
_REQUIRED_ENVELOPE_CLAIMS = frozenset(
    {
        "iss",
        "aud",
        "alg",
        "kid",
        "tenant_id",
        "principal",
        "target_run_id",
        "target_generation",
        "action",
        "command_id",
        "body_digest",
        "grant_id",
        "revocation_epoch",
        "iat",
        "nbf",
        "exp",
    }
)


class RunIdentityError(Exception):
    """The worker cannot establish or refresh its own authority."""


class WorkOwnershipPending(RunIdentityError):
    """The gateway retained an authorized child behind its active parent."""


class ModelPolicyVerificationError(ValueError):
    """A proposed gateway decision is not authentic or bound to this run."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ModelPolicyReport:
    """Sanitized evidence from bootstrap, and — under ``enforcing`` — an input.

    Under ``disabled`` and ``report_only`` the proposed model is deliberately
    *not* an execution input: the legacy assignment still runs and this is pure
    comparison evidence.  Under ``enforcing`` the verified decision is what the
    harness must launch on, and a run that cannot honour it must fail rather than
    fall back.  Which of those applies is decided by the gateway and read from
    :attr:`posture` — never inferred from this worker's own configuration, which
    an operator could edit.

    ``enforcing`` is supported in source so the path is provable end to end.  No
    configured environment is set to it; PMM-09 owns that operational flip.
    """

    status: Literal["proposed", "unavailable"]
    #: The live posture the gateway verified for this hop.  ``None`` only when the
    #: gateway could not establish one, which is never treated as permissive.
    posture: Literal["disabled", "report_only", "enforcing"] | None = None
    #: Whether the gateway proved the posture against committed platform state.
    posture_verified: bool = False
    reason: str | None = None
    requested_model_id: str | None = None
    resolved_model_id: str | None = None
    resolution_source: str | None = None
    snapshot_digest: str | None = None
    policy_revision: str | None = None
    catalogue_revision: str | None = None
    snapshot_allowlist_policy_revision: str | None = None
    live_allowlist_policy_revision: str | None = None
    allowlist_policy_drift: bool | None = None
    posture_revision: int | None = None
    assertion_key_id: str | None = None

    @property
    def enforced(self) -> bool:
        """True only for a verified enforcing posture with a usable decision.

        Every condition is required, and none may be relaxed:

        * the posture must be ``enforcing`` — not merely "not report_only";
        * it must be *verified*, so an unknown posture never enforces and never
          silently becomes permissive either (the gateway withholds authority for
          that case, and this is the second, independent check);
        * there must be a signed proposal with a resolved model to execute on.

        The caller uses this to decide whether to *substitute* the model.  It is
        not the place that decides whether to *refuse*: an enforcing posture whose
        decision is unusable must stop the run, which is
        :meth:`enforcement_failure`, because returning False here would silently
        hand the run back to its legacy assignment.
        """
        return (
            self.posture == "enforcing"
            and self.posture_verified
            and self.status == "proposed"
            and bool(self.resolved_model_id)
        )

    @property
    def enforcement_failure(self) -> str | None:
        """The refusal reason when enforcing is active but unusable, else None.

        The asymmetry is the point.  Under ``enforcing`` an unavailable or
        unverified decision cannot be downgraded to report-only behaviour by
        exception handling or default substitution: that is exactly the bypass
        PMM-07 has to make impossible.  Under ``disabled``/``report_only`` the
        same unavailable decision is not a failure at all, because the legacy
        assignment is the correct outcome there.
        """
        if self.posture != "enforcing":
            return None
        if not self.posture_verified:
            return "posture_unverified"
        if self.status != "proposed":
            return self.reason or "decision_unavailable"
        if not self.resolved_model_id:
            return "decision_malformed"
        return None

    def effective_model(self, legacy_model: str) -> str:
        """The model this run must actually use.

        Returns the gateway's resolved model only under a verified enforcing
        proposal; otherwise the unchanged legacy assignment.  Callers must consult
        :attr:`enforcement_failure` first — this method deliberately cannot
        express "refuse", so using it alone under a broken enforcing posture would
        preserve legacy behaviour and defeat enforcement.
        """
        if self.enforced:
            assert self.resolved_model_id is not None  # narrowed by ``enforced``
            return self.resolved_model_id
        return legacy_model

    def environment(self, legacy_model: str) -> dict[str, str]:
        """Return policy telemetry for this run.

        Under ``disabled``/``report_only`` this is comparison evidence only and
        ``ANTHROPIC_MODEL`` is untouched.  Under a verified enforcing proposal the
        caller substitutes the model; the variables here still describe what
        happened rather than driving it, and no consumer may treat them as
        authority — they are ordinary environment variables that anything in the
        pod could have written.
        """
        values = {
            # Reported, never assumed.  Hard-coding ``report_only`` here made the
            # telemetry lie under any other posture, and lying in the permissive
            # direction is the worst available default.
            "ADP_MODEL_POLICY_POSTURE": self.posture or "unknown",
            "ADP_MODEL_POLICY_POSTURE_VERIFIED": str(self.posture_verified).lower(),
            "ADP_MODEL_POLICY_ENFORCED": str(self.enforced).lower(),
            "ADP_MODEL_POLICY_STATUS": self.status,
        }
        if self.snapshot_allowlist_policy_revision is not None:
            values["ADP_MODEL_POLICY_SNAPSHOT_ALLOWLIST_REVISION"] = (
                self.snapshot_allowlist_policy_revision
            )
        if self.live_allowlist_policy_revision is not None:
            values["ADP_MODEL_POLICY_LIVE_ALLOWLIST_REVISION"] = self.live_allowlist_policy_revision
        if self.allowlist_policy_drift is not None:
            values["ADP_MODEL_POLICY_ALLOWLIST_DRIFT"] = str(self.allowlist_policy_drift).lower()
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
                "ADP_MODEL_POLICY_ASSERTION_KEY_ID": self.assertion_key_id or "",
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


def _canonical_policy_json(value: dict) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ModelPolicyVerificationError("decision_malformed") from None


def _decode_urlsafe(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        raise ModelPolicyVerificationError("decision_unverifiable") from None


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ModelPolicyVerificationError("decision_unverifiable")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ModelPolicyVerificationError("decision_unverifiable") from None


def _verification_keys(raw: str) -> dict[str, Ed25519PublicKey]:
    """Parse the same staged JSON/compact public-key formats as the Node worker."""
    keys: dict[str, Ed25519PublicKey] = {}
    if not raw or len(raw.encode("utf-8")) > _MAX_KEY_CONFIG_BYTES:
        return keys
    if raw.lstrip().startswith("{"):
        try:
            entries = json.loads(raw)
        except (TypeError, ValueError):
            return keys
        if not isinstance(entries, dict):
            return keys
        for key_id, pem in entries.items():
            if not isinstance(key_id, str) or not key_id or not isinstance(pem, str):
                continue
            try:
                key = serialization.load_pem_public_key(pem.encode("ascii"))
            except (ValueError, TypeError, UnicodeEncodeError):
                continue
            if isinstance(key, Ed25519PublicKey):
                keys[key_id] = key
        return keys

    for entry in raw.split(","):
        key_id, separator, encoded = entry.partition(":")
        if not separator or not key_id.strip() or not encoded.strip():
            continue
        try:
            decoded = base64.b64decode(encoded.strip(), validate=True)
            if len(decoded) == 32:
                keys[key_id.strip()] = Ed25519PublicKey.from_public_bytes(decoded)
        except (ValueError, TypeError):
            continue
    return keys


def load_model_policy_verification_keys(
    env: dict[str, str] | None = None,
) -> dict[str, Ed25519PublicKey]:
    """Load public verification keys only; a worker never receives a signer."""
    source = env if env is not None else os.environ
    raw = source.get(MODEL_POLICY_KEYS_ENV, "").strip()
    key_file = source.get(MODEL_POLICY_KEYS_FILE_ENV, "").strip()
    if key_file:
        try:
            candidate = Path(key_file).read_bytes()
            if len(candidate) <= _MAX_KEY_CONFIG_BYTES:
                raw = candidate.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            # The staged environment value remains an independently usable
            # rotation source when the projected file is temporarily absent.
            pass
    return _verification_keys(raw)


def _verify_model_policy_assertion(
    token: object,
    *,
    decision_body: bytes,
    invocation_id: str,
    attempt: int,
    tenant_id: str,
    correlation_id: str,
    snapshot_digest: str,
    public_keys: dict[str, Ed25519PublicKey],
    now: datetime | None = None,
) -> str:
    if not isinstance(token, str) or not token or len(token) > _MAX_ENVELOPE_BYTES:
        raise ModelPolicyVerificationError("decision_unavailable")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != ENVELOPE_VERSION:
        raise ModelPolicyVerificationError("decision_unverifiable")
    try:
        body = _decode_urlsafe(parts[1])
        signature = _decode_urlsafe(parts[2])
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError, ModelPolicyVerificationError):
        raise ModelPolicyVerificationError("decision_unverifiable") from None
    if not isinstance(payload, dict) or payload.get("v") != ENVELOPE_VERSION:
        raise ModelPolicyVerificationError("decision_unverifiable")
    if any(payload.get(claim) in (None, "") for claim in _REQUIRED_ENVELOPE_CLAIMS):
        raise ModelPolicyVerificationError("decision_unverifiable")
    string_claims = _REQUIRED_ENVELOPE_CLAIMS - {"target_generation", "revocation_epoch"}
    if any(not isinstance(payload[claim], str) for claim in string_claims):
        raise ModelPolicyVerificationError("decision_unverifiable")
    if any(type(payload[claim]) is not int for claim in ("target_generation", "revocation_epoch")):
        raise ModelPolicyVerificationError("decision_unverifiable")
    if payload["alg"] != "ed25519":
        raise ModelPolicyVerificationError("decision_algorithm_unsupported")
    if payload["iss"] != ENVELOPE_ISSUER:
        raise ModelPolicyVerificationError("decision_untrusted_issuer")
    if payload["aud"] != MODEL_POLICY_AUDIENCE:
        raise ModelPolicyVerificationError("decision_audience_mismatch")
    key = public_keys.get(payload["kid"])
    if key is None:
        raise ModelPolicyVerificationError("decision_unknown_key")
    try:
        key.verify(
            signature,
            ENVELOPE_VERSION.encode("ascii") + b"." + body,
        )
    except InvalidSignature:
        raise ModelPolicyVerificationError("decision_bad_signature") from None
    if (
        payload["target_run_id"] != invocation_id
        or payload["principal"] != f"{invocation_id}#{attempt}"
    ):
        raise ModelPolicyVerificationError("decision_target_mismatch")
    if payload["target_generation"] != attempt:
        raise ModelPolicyVerificationError("decision_generation_mismatch")
    if payload["tenant_id"] != tenant_id:
        raise ModelPolicyVerificationError("decision_cross_tenant")
    if payload["action"] != MODEL_POLICY_ACTION:
        raise ModelPolicyVerificationError("decision_action_mismatch")
    if payload["command_id"] != snapshot_digest:
        raise ModelPolicyVerificationError("decision_snapshot_mismatch")
    if payload.get("chain_id") != correlation_id:
        raise ModelPolicyVerificationError("decision_chain_mismatch")
    if payload["body_digest"] != hashlib.sha256(decision_body).hexdigest():
        raise ModelPolicyVerificationError("decision_altered")
    if payload["revocation_epoch"] < 1:
        raise ModelPolicyVerificationError("decision_unverifiable")
    issued = _parse_timestamp(payload["iat"])
    not_before = _parse_timestamp(payload["nbf"])
    expires = _parse_timestamp(payload["exp"])
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if current >= expires:
        raise ModelPolicyVerificationError("decision_expired")
    if current < not_before or issued > not_before:
        raise ModelPolicyVerificationError("decision_not_yet_valid")
    if (expires - not_before).total_seconds() > MAX_ENVELOPE_TTL_SECONDS:
        raise ModelPolicyVerificationError("decision_validity_too_long")
    return payload["kid"]


def parse_model_policy_report(
    value: object,
    *,
    invocation_id: str,
    attempt: int = 1,
    tenant_id: str | None = None,
    correlation_id: str | None = None,
    public_keys: dict[str, Ed25519PublicKey] | None = None,
    now: datetime | None = None,
) -> ModelPolicyReport:
    """Verify a bootstrap policy response and reduce it to a usable report.

    Accepts all three postures.  ``report_only`` and ``disabled`` yield evidence
    the caller ignores for execution; ``enforcing`` yields a decision the caller
    must execute on or refuse.  An unrecognised posture is rejected outright
    rather than coerced to the permissive value.

    A verified posture with no consumable decision is still returned (with
    ``status="unavailable"``), because the *caller's* correct behaviour depends on
    the posture it failed under: legacy assignment under report_only, refusal
    under enforcing.  Discarding the posture here would force that decision to be
    guessed from local configuration.
    """
    if not isinstance(value, dict):
        raise ValueError("invalid model-policy response")
    posture = value.get("posture")
    verified = value.get("posture_verified")
    if posture is not None and posture not in MODEL_POLICY_POSTURES:
        raise ValueError("invalid model-policy response")
    if type(verified) is not bool:
        # Absent or non-boolean: an older gateway that cannot prove the posture.
        # Treated as unverified, never as verified-permissive.
        verified = False
    if posture is None and verified:
        # "Verified" with no posture is self-contradictory; trust neither half.
        raise ValueError("invalid model-policy response")
    status = value.get("status")
    if status == "unavailable":
        evidence = value.get("evidence")
        snapshot_allowlist_revision = None
        live_allowlist_revision = None
        allowlist_drift = None
        if evidence is not None:
            if not isinstance(evidence, dict):
                raise ValueError("invalid model-policy evidence")
            snapshot_allowlist_revision = _safe_policy_text(
                evidence.get("snapshot_allowlist_policy_revision")
            )
            live_allowlist_revision = _safe_policy_text(
                evidence.get("live_allowlist_policy_revision")
            )
            allowlist_drift = evidence.get("allowlist_policy_drift")
            if type(allowlist_drift) is not bool:
                raise ValueError("invalid model-policy evidence")
        return ModelPolicyReport(
            status="unavailable",
            posture=posture,
            posture_verified=verified,
            reason=_safe_policy_text(value.get("reason")),
            snapshot_allowlist_policy_revision=snapshot_allowlist_revision,
            live_allowlist_policy_revision=live_allowlist_revision,
            allowlist_policy_drift=allowlist_drift,
        )
    if status != "proposed" or not isinstance(value.get("decision"), dict):
        raise ModelPolicyVerificationError("decision_malformed")
    decision = value["decision"]
    resolved = _safe_policy_text(decision.get("resolved_model_id"))
    requested = _safe_policy_text(decision.get("requested_model_id"), optional=True)
    source = _safe_policy_text(decision.get("resolution_source"))
    digest = _safe_policy_text(decision.get("snapshot_digest"))
    policy_revision = _safe_policy_text(decision.get("policy_revision"))
    catalogue_revision = _safe_policy_text(decision.get("catalogue_revision"))
    snapshot_allowlist_revision = _safe_policy_text(
        decision.get("snapshot_allowlist_policy_revision")
    )
    live_allowlist_revision = _safe_policy_text(decision.get("live_allowlist_policy_revision"))
    allowlist_drift = decision.get("allowlist_policy_drift")
    posture_revision = decision.get("posture_revision")
    decision_tenant = _safe_policy_text(decision.get("tenant_id"))
    decision_chain = _safe_policy_text(decision.get("correlation_id"))
    if (
        decision.get("invocation_id") != invocation_id
        # The signed decision's own posture must match the envelope's. They are
        # produced together by the gateway, so disagreement means the response was
        # assembled from mismatched parts and neither half can be trusted.
        or decision.get("runtime_posture") != posture
        or source not in _RESOLUTION_SOURCES
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
        or type(posture_revision) is not int
        or posture_revision < 1
        or type(allowlist_drift) is not bool
        or not isinstance(value.get("assertion"), str)
        or not value["assertion"].startswith("adpe1.")
    ):
        raise ModelPolicyVerificationError("decision_malformed")
    if tenant_id is not None and decision_tenant != tenant_id:
        raise ModelPolicyVerificationError("decision_cross_tenant")
    if correlation_id is not None and decision_chain != correlation_id:
        raise ModelPolicyVerificationError("decision_chain_mismatch")
    key_id = _verify_model_policy_assertion(
        value.get("assertion"),
        decision_body=_canonical_policy_json(decision),
        invocation_id=invocation_id,
        attempt=attempt,
        tenant_id=tenant_id or decision_tenant,
        correlation_id=correlation_id or decision_chain,
        snapshot_digest=digest,
        public_keys=public_keys or {},
        now=now,
    )
    if decision.get("schema_version") != MODEL_POLICY_SCHEMA_VERSION:
        raise ModelPolicyVerificationError("snapshot_unsupported_revision")
    return ModelPolicyReport(
        status="proposed",
        posture=posture,
        posture_verified=verified,
        requested_model_id=requested,
        resolved_model_id=resolved,
        resolution_source=source,
        snapshot_digest=digest,
        policy_revision=policy_revision,
        catalogue_revision=catalogue_revision,
        snapshot_allowlist_policy_revision=snapshot_allowlist_revision,
        live_allowlist_policy_revision=live_allowlist_revision,
        allowlist_policy_drift=allowlist_drift,
        posture_revision=posture_revision,
        assertion_key_id=key_id,
    )


def _unconsumable_report(policy: object, *, reason: str) -> ModelPolicyReport:
    """Build the report for a response that failed verification.

    The posture is salvaged from the raw response even though verification
    failed, and that asymmetry is deliberate and safe:

    * it can only cause a **refusal**, never an execution.  ``status`` is
      ``unavailable``, so :attr:`ModelPolicyReport.enforced` is False and no model
      can be substituted from an unverified response;
    * dropping it would be unsafe in the other direction.  A malformed decision
      under a claimed ``enforcing`` posture would look like "not enforcing", and
      the run would quietly continue on its legacy model — the precise bypass
      this work exists to close.

    So an unverifiable claim of ``enforcing`` stops the run, and an unverifiable
    claim of ``report_only`` cannot grant anything it did not already have. Any
    unrecognised value is dropped rather than guessed at, which also lands on
    refusal only if the gateway separately proved a posture.
    """
    posture = policy.get("posture") if isinstance(policy, dict) else None
    if posture not in MODEL_POLICY_POSTURES:
        posture = None
    return ModelPolicyReport(
        status="unavailable",
        posture=posture,
        # Never verified here by construction: verification is what just failed.
        posture_verified=False,
        reason=reason,
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
        self._tenant_id = envelope.get("tenant_id")
        correlation = envelope.get("correlation")
        self._correlation_id = (
            correlation.get("correlation_id") if isinstance(correlation, dict) else None
        )
        if not all(
            isinstance(item, str) and item
            for item in (self._invocation_id, self._tenant_id, self._correlation_id)
        ):
            raise RunIdentityError("protected model-policy binding unavailable")
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
            {
                "invocation_id": self._invocation_id,
                "envelope_digest": self._digest,
                # Declares only that this worker will honour an enforcing decision
                # rather than launching on its legacy assignment. It carries no
                # authority: it cannot select a model, relax a gate or influence
                # what the gateway signs. An older gateway ignores the field.
                "model_policy_contract": MODEL_POLICY_CONTRACT_VERSION,
            }
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
                        attempt=attempt,
                        tenant_id=self._tenant_id,
                        correlation_id=self._correlation_id,
                        public_keys=load_model_policy_verification_keys(),
                    )
                except ModelPolicyVerificationError as exc:
                    self.model_policy_report = _unconsumable_report(policy, reason=exc.reason)
                except ValueError:
                    self.model_policy_report = _unconsumable_report(
                        policy, reason="decision_malformed"
                    )
                # The message must describe what actually happened. A report-only
                # warning that implies the run was blocked before inference is a
                # false statement about enforcement, and an operator reading it
                # would conclude the gate works when nothing was gated.
                report = self.model_policy_report
                failure = report.enforcement_failure
                if failure is not None:
                    logger.error(
                        "Enforcing model policy cannot be satisfied; run must not proceed "
                        "on its legacy model (reason=%s)",
                        failure,
                    )
                elif report.enforced:
                    logger.info(
                        "Enforcing model policy: launching on the gateway-decided model"
                    )
                elif report.status == "proposed":
                    logger.info(
                        "Model-policy decision received and recorded; posture=%s leaves the "
                        "existing model assignment in effect (nothing was blocked)",
                        report.posture or "unknown",
                    )
                else:
                    logger.warning(
                        "Model-policy decision unavailable (posture=%s, reason=%s); the "
                        "existing model assignment stays in effect and no inference was "
                        "blocked",
                        report.posture or "unknown",
                        report.reason,
                    )
                # A transient missing key/snapshot may recover on the next
                # refresh. Pin only an authenticated proposal, or the explicit
                # mixed-version compatibility outcome, for this worker run.
                #
                # An enforcing failure is never pinned: it must be re-evaluated on
                # every refresh so an operator's rollback to report_only, or a
                # recovered decision, is picked up instead of the run staying
                # latched to a stale refusal.
                self._model_policy_reported = failure is None and (
                    report.status == "proposed"
                    or report.reason == "snapshot_unsupported_revision"
                )
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
