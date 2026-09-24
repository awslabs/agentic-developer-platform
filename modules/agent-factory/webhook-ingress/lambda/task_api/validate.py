"""Edge validation for ``POST /v1/tasks``.

Ordered so that the cheapest transport check runs first and nothing expensive
is spent on a request that cannot be admitted:

1. transport bytes   — exact request bytes, UTF-8 validity, 65536-byte ceiling
2. JSON decode       — duplicate object names and nonfinite numbers refused
                       before schema validation so parser choice cannot
                       silently change the accepted request or its digest
3. request shape     — required/unknown fields, types, contract limits and the
                       ``agent-task-*`` persona namespace

None of these stages resolves identity. Who the caller is and what it may do
is decided by the gateway from the verified credential; this module only
refuses requests that are malformed, oversize, or that try to select platform
state a caller may not choose.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import math

from . import contract, errors


def request_bytes(event: dict) -> bytes:
    """Recover the exact request bytes from a REST API v1 proxy event.

    The raw bytes matter beyond convenience: the producer proof is bound to
    them, so a re-serialized body would break the binding.
    """
    raw = event.get("body")
    if raw is None:
        raise errors.invalid_request("A JSON request body is required.")
    if event.get("isBase64Encoded"):
        if not isinstance(raw, str):
            raise errors.invalid_request("The request body could not be decoded.")
        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise errors.invalid_request(
                "The request body could not be decoded."
            ) from None
    if isinstance(raw, bytes):
        return raw
    if not isinstance(raw, str):
        raise errors.invalid_request("The request body could not be decoded.")
    return raw.encode("utf-8", "surrogateescape")


def decode_body(data: bytes) -> dict:
    """Decode the submit body, refusing the shapes the design forbids."""
    if len(data) > contract.MAX_BODY_BYTES:
        raise errors.payload_too_large(
            "submit_body.max_body_bytes", contract.MAX_BODY_BYTES
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise errors.invalid_request("The request body must be valid UTF-8.") from None

    def _no_duplicates(pairs):
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                raise ValueError(f"duplicate field: {key}")
            seen.add(key)
        return dict(pairs)

    def _no_nonfinite(token):
        raise ValueError(f"nonfinite number: {token}")

    def _finite_float(token):
        value = float(token)
        if not math.isfinite(value):
            raise ValueError("nonfinite number")
        return value

    try:
        decoded = json.loads(
            text, object_pairs_hook=_no_duplicates, parse_constant=_no_nonfinite,
            parse_float=_finite_float,
        )
    except ValueError:
        # The caller's body is never echoed back; only the fixed reason.
        raise errors.invalid_request(
            "The request body must be a single JSON object with unique field "
            "names and finite numbers."
        ) from None
    if not isinstance(decoded, dict):
        raise errors.invalid_request("The request body must be a JSON object.")
    return decoded


def idempotency_key(headers: dict) -> str:
    """Require a well-formed ``Idempotency-Key``.

    The key is what makes a retry safe, so an absent or malformed key is
    refused rather than defaulted — a generated key would turn a lost response
    into a second task.
    """
    key = headers.get(contract.IDEMPOTENCY_KEY_HEADER)
    if not isinstance(key, str) or not key:
        raise errors.invalid_request("An Idempotency-Key header is required.")
    if not (
        contract.IDEMPOTENCY_KEY_MIN_CHARACTERS
        <= len(key)
        <= contract.IDEMPOTENCY_KEY_MAX_CHARACTERS
    ):
        raise errors.invalid_request(
            "Idempotency-Key must be 1 to 128 printable ASCII characters."
        )
    if not contract.IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise errors.invalid_request(
            "Idempotency-Key must be 1 to 128 printable ASCII characters."
        )
    return key


def bearer_token(headers: dict) -> str:
    """Extract the caller's bearer token from its own Authorization header.

    Only this header is trusted as the token source. A caller-supplied
    forwarding header is never read here, so a client cannot present one
    credential to the gateway and a different one to this Lambda.
    """
    value = headers.get("authorization")
    if not isinstance(value, str) or not value.strip():
        raise errors.invalid_credential()
    scheme, _, token = value.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise errors.invalid_credential()
    return token


def _require_string(body: dict, field: str, *, max_characters: int) -> str:
    value = body[field]
    if not isinstance(value, str):
        raise errors.invalid_request(f"{field} must be a string.")
    if len(value) > max_characters:
        raise errors.invalid_request(
            f"{field} exceeds its permitted length.",
            limit_name=f"submit_body.max_{field}_characters",
            limit_value=max_characters,
        )
    return value


def submit_request(body: dict) -> dict:
    """Validate the submit body against the frozen public contract."""
    forbidden = sorted(set(body) & contract.CALLER_FORBIDDEN_FIELDS)
    if forbidden:
        # Named separately from the generic unknown-field refusal: these are
        # not typos, they are attempts to choose platform-owned state.
        raise errors.invalid_request(
            "A task request may not select ownership or execution parameters."
        )
    unknown = sorted(set(body) - contract.SUBMIT_ALLOWED_FIELDS)
    if unknown:
        raise errors.invalid_request("The task request contains unknown fields.")
    missing = [f for f in contract.SUBMIT_REQUIRED_FIELDS if f not in body]
    if missing:
        raise errors.invalid_request(
            "Missing required field(s): " + ", ".join(missing) + "."
        )

    if body["schema_version"] != contract.SCHEMA_VERSION:
        raise errors.invalid_request("schema_version must be \"1.0\".")

    persona = body["persona"]
    if not isinstance(persona, str):
        raise errors.disallowed_persona("persona must be a string.")
    if len(persona) > contract.MAX_PERSONA_CHARACTERS or not (
        contract.PERSONA_PATTERN.fullmatch(persona)
    ):
        # An unrecognised persona is refused here rather than allowed to fall
        # through to a general-purpose GitHub agent with no task contract.
        raise errors.disallowed_persona(
            "persona must name a registered agent-task-* task persona."
        )

    instructions = _require_string(
        body, "instructions", max_characters=contract.MAX_INSTRUCTIONS_CHARACTERS
    )
    if not instructions:
        raise errors.invalid_request("instructions must not be empty.")

    if "inputs" in body and not isinstance(body["inputs"], dict):
        raise errors.invalid_request("inputs must be a JSON object.")

    if "external_reference" in body:
        _require_string(
            body,
            "external_reference",
            max_characters=contract.MAX_EXTERNAL_REFERENCE_CHARACTERS,
        )

    if "artifact_ids" in body:
        ids = body["artifact_ids"]
        if not isinstance(ids, list):
            raise errors.invalid_request("artifact_ids must be an array.")
        if len(ids) > contract.MAX_INPUT_ARTIFACTS:
            raise errors.invalid_request(
                "artifact_ids exceeds the permitted number of input artifacts.",
                limit_name="artifacts.max_input_artifacts",
                limit_value=contract.MAX_INPUT_ARTIFACTS,
            )
        for value in ids:
            if not isinstance(value, str) or not contract.ARTIFACT_ID_PATTERN.fullmatch(
                value
            ):
                raise errors.invalid_request(
                    "artifact_ids entries must be artifact identifiers."
                )
        if len(set(ids)) != len(ids):
            raise errors.invalid_request("artifact_ids must be unique.")

    if "acceptance_criteria" in body:
        criteria = body["acceptance_criteria"]
        if not isinstance(criteria, list):
            raise errors.invalid_request("acceptance_criteria must be an array.")
        if len(criteria) > contract.MAX_ACCEPTANCE_CRITERIA_COUNT:
            raise errors.invalid_request(
                "acceptance_criteria exceeds the permitted number of entries.",
                limit_name="submit_body.max_acceptance_criteria_count",
                limit_value=contract.MAX_ACCEPTANCE_CRITERIA_COUNT,
            )
        for value in criteria:
            if not isinstance(value, str) or not value:
                raise errors.invalid_request(
                    "acceptance_criteria entries must be nonempty strings."
                )
            if len(value) > contract.MAX_ACCEPTANCE_CRITERION_CHARACTERS:
                raise errors.invalid_request(
                    "An acceptance_criteria entry exceeds its permitted length.",
                    limit_name="submit_body.max_acceptance_criterion_characters",
                    limit_value=contract.MAX_ACCEPTANCE_CRITERION_CHARACTERS,
                )

    return body


def submit_response(receipt: object) -> dict:
    """Accept a gateway receipt only if it is a complete acceptance.

    A 202 must mean the task is durably recorded. Anything this function
    cannot fully validate becomes an unavailable-prerequisite failure rather
    than a false acceptance (T2-AC03).
    """
    if not isinstance(receipt, dict):
        raise errors.prerequisite_unavailable()
    if set(receipt) - contract.SUBMIT_RESPONSE_ALLOWED:
        raise errors.prerequisite_unavailable()
    if any(field not in receipt for field in contract.SUBMIT_RESPONSE_REQUIRED):
        raise errors.prerequisite_unavailable()
    if receipt["schema_version"] != contract.SCHEMA_VERSION:
        raise errors.prerequisite_unavailable()

    task_id = receipt["task_id"]
    if not isinstance(task_id, str) or not contract.TASK_ID_PATTERN.fullmatch(task_id):
        raise errors.prerequisite_unavailable()
    invocation_id = receipt["invocation_id"]
    if not isinstance(invocation_id, str) or not contract.UUID4_PATTERN.fullmatch(
        invocation_id
    ):
        raise errors.prerequisite_unavailable()
    if receipt["status"] not in contract.SUBMIT_RESPONSE_STATUSES:
        raise errors.prerequisite_unavailable()
    for field in ("created_at", "deadline_at"):
        value = receipt[field]
        if not isinstance(value, str) or not contract.RFC3339_UTC_PATTERN.fullmatch(
            value
        ):
            raise errors.prerequisite_unavailable()
        try:
            dt.datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
        except ValueError:
            raise errors.prerequisite_unavailable() from None
    # The URLs must address the task the gateway says it recorded, so a
    # caller cannot be handed a pointer to someone else's task.
    if receipt["status_url"] != f"/v1/tasks/{task_id}":
        raise errors.prerequisite_unavailable()
    if receipt["events_url"] != f"/v1/tasks/{task_id}/events":
        raise errors.prerequisite_unavailable()
    request_id = receipt["request_id"]
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise errors.prerequisite_unavailable()
    if "idempotent_replay" in receipt and not isinstance(
        receipt["idempotent_replay"], bool
    ):
        raise errors.prerequisite_unavailable()
    return receipt
