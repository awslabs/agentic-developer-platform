"""Worker-side enforcement for Mode B analysis scripts (issue #5616, #4729).

Before this module, the only thing standing between a queued message and code
execution inside the analysis plane was a line in an agent's prompt asking it
to run ``validate_script.py`` on its own output before uploading. The worker
never checked. Anything that could place a message on the queue, or write an
object where the worker could read it, got code execution with the worker's
cloud identity.

This module moves that decision to the consumer. Three checks, all required,
all performed by the worker:

1. **Registration + integrity** — the digest recorded when the pipeline
   registered the script for this job must match the digest of the object the
   worker actually downloaded. A missing registration is a refusal too, so
   integrity is proven independently of location: being in an approved place
   does not imply being the approved content.
2. **Validation** — the shared validator runs here, in the worker, and its
   verdict blocks execution. It is no longer advice the producer may skip.
3. **Explicit outcome** — every refusal raises with a stable reason code so the
   caller fails the stage loudly and non-retryably, rather than returning empty
   findings that look like "nothing found".

Location authorization is a separate concern handled by ``sample_access``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

REASON_REGISTRATION_MISSING = "script_registration_missing"
REASON_DIGEST_MISMATCH = "script_digest_mismatch"
REASON_VALIDATION_FAILED = "script_validation_failed"
REASON_MANIFEST_UNAVAILABLE = "worker_manifest_unavailable"

# Defaults for the worker image layout (see workers/Dockerfile). Both are read
# at call time, not import time, so the running process reflects its current
# environment rather than whatever was set when the module first loaded.
DEFAULT_WORKER_MANIFEST_PATH = "/opt/worker-manifest.json"
DEFAULT_VALIDATOR_PATH = "/app/skills/stage-3-static/validate_script.py"


def worker_manifest_path() -> str:
    """Path to the build-time manifest baked into the image."""
    return os.environ.get("WORKER_MANIFEST_PATH", DEFAULT_WORKER_MANIFEST_PATH)


def validator_path() -> str:
    """Path to the shared validator.

    It lives with the Stage-3 skill so the agent-side convenience check and the
    worker-side enforcement cannot diverge into two rule sets.
    """
    return os.environ.get("CYBER_VALIDATOR_PATH", DEFAULT_VALIDATOR_PATH)


class ScriptRejected(Exception):
    """A Mode B script is not authorized to execute.

    ``reason`` is a stable code safe to surface to the requester.
    ``violations`` carries validator findings, which describe the script the
    requester supplied (not another tenant's data).
    """

    def __init__(self, reason: str, detail: str = "", violations: list[str] | None = None) -> None:
        self.reason = reason
        self.detail = detail
        self.violations = violations or []
        super().__init__(f"script rejected: {reason}" + (f" ({detail})" if detail else ""))


def sha256_file(path: Path) -> str:
    """Content digest of a downloaded object."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_validator():
    """Import the shared validator as a library.

    Loaded by path because the validator ships inside the Stage-3 skill
    directory rather than as an installed package. Its CLI entry point is
    unchanged, so the agent-side pre-upload check still works as before.
    """
    path = Path(validator_path())
    if not path.is_file():
        raise ScriptRejected(REASON_MANIFEST_UNAVAILABLE, "validator not present in image")
    spec = importlib.util.spec_from_file_location("cyber_validate_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_worker_manifest() -> dict:
    """Read the build-time manifest describing what the image actually has."""
    try:
        with open(worker_manifest_path()) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        # Fail closed: without a manifest the validator cannot decide what is
        # allowed, and guessing would reinstate the hole.
        raise ScriptRejected(
            REASON_MANIFEST_UNAVAILABLE, f"{type(exc).__name__}"
        ) from exc


def registered_digest(body: dict) -> str:
    """The digest the pipeline recorded for this job's script.

    Absence is a refusal, not a skip — an unregistered script is exactly the
    case where an attacker supplies their own.
    """
    value = body.get("script_sha256")
    if not value or not isinstance(value, str):
        raise ScriptRejected(REASON_REGISTRATION_MISSING, "no script_sha256 in job")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
        raise ScriptRejected(REASON_REGISTRATION_MISSING, "script_sha256 malformed")
    return normalized


def verify_script(script_path: Path, body: dict) -> str:
    """Authorize a downloaded script for execution.

    Returns the verified digest. Raises ``ScriptRejected`` if the script was
    not registered for this job, does not match what was registered, or fails
    validation. Callers must treat a raise as "do not execute".
    """
    expected = registered_digest(body)
    actual = sha256_file(script_path)
    if actual != expected:
        # Do not log either digest's source object; the mismatch itself is the
        # actionable fact.
        raise ScriptRejected(REASON_DIGEST_MISMATCH, "content differs from registration")

    validator = load_validator()
    manifest = load_worker_manifest()
    violations = validator.validate_script(str(script_path), manifest)
    if violations:
        raise ScriptRejected(
            REASON_VALIDATION_FAILED,
            f"{len(violations)} violation(s)",
            violations=violations,
        )

    return actual
