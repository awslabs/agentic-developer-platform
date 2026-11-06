"""Explicit runtime authority for one recovery of an unchanged bootstrap plan."""

import json
from datetime import UTC, datetime

from .identity import OperationRefused

MAX_BOOTSTRAP_RUNTIME_SECONDS = 10800
SETTLED_REAUTHORIZATION_KIND = "bootstrap-runtime-reauthorization-v1"
_FIELDS = {
    "version",
    "kind",
    "original_max_runtime_seconds",
    "max_runtime_seconds",
}
_PROOF_FIELDS = {"runtime_amendment", "runtime_authority_expires_at"}


def runtime_amendment(terms, original_max_runtime_seconds) -> int:
    """Validate a separately reviewed runtime; never change the admitted envelope."""
    if (
        not isinstance(terms, dict)
        or set(terms) != _FIELDS
        or type(terms.get("version")) is not int
        or terms["version"] != 1
        or terms.get("kind") != "bootstrap-runtime-extension-v1"
        or type(original_max_runtime_seconds) is not int
        or original_max_runtime_seconds < 1
        or type(terms.get("original_max_runtime_seconds")) is not int
        or terms["original_max_runtime_seconds"] != original_max_runtime_seconds
        or type(terms.get("max_runtime_seconds")) is not int
        or not max(900, original_max_runtime_seconds)
        < terms["max_runtime_seconds"]
        <= MAX_BOOTSTRAP_RUNTIME_SECONDS
    ):
        raise OperationRefused("explicit bootstrap runtime amendment refused")
    return terms["max_runtime_seconds"]


def settled_runtime_amendment(terms, original_max_runtime_seconds) -> int:
    """A closed three-hour bootstrap may receive a fresh, equal three-hour cap.

    Callers must bind this distinct term to the original settled closure and a
    fresh human decision. Ordinary unstarted-bootstrap extensions stay strict.
    """
    if not isinstance(terms, dict) or terms.get("kind") != SETTLED_REAUTHORIZATION_KIND:
        return runtime_amendment(terms, original_max_runtime_seconds)
    if (
        set(terms) != _FIELDS
        or type(terms.get("version")) is not int
        or terms["version"] != 1
        or type(original_max_runtime_seconds) is not int
        or type(terms.get("original_max_runtime_seconds")) is not int
        or type(terms.get("max_runtime_seconds")) is not int
        or original_max_runtime_seconds != terms["original_max_runtime_seconds"]
        or original_max_runtime_seconds != MAX_BOOTSTRAP_RUNTIME_SECONDS
        or terms["max_runtime_seconds"] != MAX_BOOTSTRAP_RUNTIME_SECONDS
    ):
        raise OperationRefused("settled bootstrap runtime reauthorization refused")
    return MAX_BOOTSTRAP_RUNTIME_SECONDS


def runtime_authorization(proof, original_max_runtime_seconds, recovery_scope):
    """Read only the latest receipt's amendment; absence retains the original cap."""
    if not isinstance(proof, dict):
        raise OperationRefused("bootstrap runtime receipt is malformed")
    if not _PROOF_FIELDS.intersection(proof):
        return original_max_runtime_seconds, None
    if recovery_scope != "unstarted-bootstrap" or not _PROOF_FIELDS <= proof.keys():
        raise OperationRefused("bootstrap runtime authority has another scope")
    terms = proof["runtime_amendment"]
    if isinstance(terms, dict) and terms.get("kind") == SETTLED_REAUTHORIZATION_KIND:
        from .settled_bootstrap import validate

        validate(proof.get("settled_bootstrap"))
        seconds = settled_runtime_amendment(terms, original_max_runtime_seconds)
    else:
        seconds = runtime_amendment(terms, original_max_runtime_seconds)
    try:
        deadline = datetime.fromisoformat(proof["runtime_authority_expires_at"])
        if deadline.tzinfo is None or deadline <= datetime.now(UTC):
            raise ValueError("expired authority")
    except (TypeError, ValueError):
        raise OperationRefused(
            "bootstrap runtime authority expired or invalid"
        ) from None
    return seconds, deadline


async def execution_runtime(connection, paid):
    """Derive the next lease cap from the immutable, owner-validated receipt."""
    original = paid["max_runtime_seconds"]
    receipt = await connection.fetchrow(
        "SELECT * FROM harness_provider_retries WHERE operation_id=$1 "
        "ORDER BY source_fence DESC LIMIT 1",
        paid["operation_id"],
    )
    if receipt is None:
        return original, None
    try:
        proof = json.loads(receipt["proof"])
    except (TypeError, ValueError):
        raise OperationRefused("bootstrap runtime receipt is malformed") from None
    seconds, deadline = runtime_authorization(
        proof, original, receipt["recovery_scope"]
    )
    if deadline is not None and (
        receipt["original_approval_id"] != paid["approval_id"]
        or any(
            receipt[key] != paid[key]
            for key in (
                "operation_id",
                "org_id",
                "workspace_id",
                "plan_digest",
                "requester",
                "approved_by",
            )
        )
    ):
        raise OperationRefused("bootstrap runtime receipt names another admission")
    return seconds, deadline
