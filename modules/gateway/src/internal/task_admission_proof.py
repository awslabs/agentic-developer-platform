"""Request-bound producer proof validation for Task API admission.

The ingress Lambda embeds the exact validated public JSON bytes as the lexical
value of ``submit`` in the frozen internal adapter object. This module extracts
that byte span, verifies the wrapper was emitted in the one supported layout,
and checks that the STS proof's signed binding covers those bytes plus the
forwarded token and idempotency key. The admission route must additionally send
the proof to STS and enforce its ingress-role allowlist before accepting work.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json

PUBLIC_METHOD = "POST"
PUBLIC_ROUTE = "/v1/tasks"
PROOF_BINDING_VERSION = "adp.task.admit.v1"
PROOF_BINDING_HEADER = "x-adp-work-invocation"
_PREFIX = b'{"schema_version":"1.0","submit":'
_MAX_INTERNAL_BODY_BYTES = 96 * 1024
_MAX_PROOF_BYTES = 12000
_JSON_WHITESPACE = " \t\r\n"
_ALLOWED_PROOF_HEADERS = {
    "authorization",
    "content-type",
    "x-amz-date",
    "x-amz-security-token",
    PROOF_BINDING_HEADER,
}
_REQUIRED_FIELDS = {
    "schema_version",
    "submit",
    "idempotency_key",
    "caller_token",
    "producer_proof",
}


class TaskAdmissionProofError(ValueError):
    """The internal request and its producer proof do not agree."""


def binding_digest(*, method: str, route: str, caller_token: str, idempotency_key: str, body: bytes) -> str:
    parts = [
        PROOF_BINDING_VERSION.encode(),
        method.encode(),
        route.encode(),
        hashlib.sha256(caller_token.encode()).hexdigest().encode(),
        idempotency_key.encode(),
        body,
    ]
    encoded = b"".join(len(part).to_bytes(8, "big") + part for part in parts)
    return hashlib.sha256(encoded).hexdigest()


def _json_object(data: bytes) -> dict:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise TaskAdmissionProofError("duplicate internal field")
            result[key] = value
        return result

    def reject_nonfinite(_value):
        raise TaskAdmissionProofError("nonfinite internal number")

    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskAdmissionProofError("invalid internal JSON") from exc
    if not isinstance(value, dict):
        raise TaskAdmissionProofError("internal request must be an object")
    return value


def _encode(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _submit_bytes(raw_body: bytes, payload: dict) -> bytes:
    if not raw_body.startswith(_PREFIX):
        raise TaskAdmissionProofError("unsupported internal wrapper")
    text = raw_body.decode("utf-8")
    lexical_start = len(_PREFIX)
    value_start = lexical_start
    while value_start < len(text) and text[value_start] in _JSON_WHITESPACE:
        value_start += 1
    try:
        submit, value_end = json.JSONDecoder().raw_decode(text, value_start)
    except json.JSONDecodeError as exc:
        raise TaskAdmissionProofError("invalid submit value") from exc
    if not isinstance(submit, dict) or submit != payload["submit"]:
        raise TaskAdmissionProofError("submit value mismatch")
    lexical_end = value_end
    while lexical_end < len(text) and text[lexical_end] in _JSON_WHITESPACE:
        lexical_end += 1
    exact_submit = text[lexical_start:lexical_end].encode()
    expected = b"".join(
        (
            _PREFIX,
            exact_submit,
            b',"idempotency_key":',
            _encode(payload["idempotency_key"]),
            b',"caller_token":',
            _encode(payload["caller_token"]),
            b',"producer_proof":',
            _encode(payload["producer_proof"]),
            b"}",
        )
    )
    if expected != raw_body:
        raise TaskAdmissionProofError("unsupported internal wrapper")
    return exact_submit


def _proof_binding(proof: str) -> str:
    if not proof or len(proof) > _MAX_PROOF_BYTES:
        raise TaskAdmissionProofError("invalid producer proof")
    try:
        headers = _json_object(base64.b64decode(proof, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise TaskAdmissionProofError("invalid producer proof") from exc
    if set(headers) - _ALLOWED_PROOF_HEADERS or any(not isinstance(value, str) for value in headers.values()):
        raise TaskAdmissionProofError("invalid producer proof headers")
    authorization = headers.get("authorization", "")
    try:
        signed_headers = authorization.split("SignedHeaders=", 1)[1].split(",", 1)[0].split(";")
    except IndexError as exc:
        raise TaskAdmissionProofError("invalid signed headers") from exc
    if PROOF_BINDING_HEADER not in signed_headers:
        raise TaskAdmissionProofError("binding header is not signed")
    binding = headers.get(PROOF_BINDING_HEADER)
    if not binding:
        raise TaskAdmissionProofError("missing proof binding")
    return binding


def verify_admission_binding(raw_body: bytes, *, caller_token_header: str, producer_proof_header: str) -> tuple[dict, bytes]:
    """Verify internal wrapper/header agreement and return exact public bytes."""
    if not raw_body or len(raw_body) > _MAX_INTERNAL_BODY_BYTES:
        raise TaskAdmissionProofError("invalid internal body size")
    payload = _json_object(raw_body)
    if set(payload) != _REQUIRED_FIELDS or payload.get("schema_version") != "1.0":
        raise TaskAdmissionProofError("invalid internal request shape")
    for field in ("idempotency_key", "caller_token", "producer_proof"):
        if not isinstance(payload[field], str) or not payload[field]:
            raise TaskAdmissionProofError("invalid internal credential field")
    if payload["caller_token"] != caller_token_header:
        raise TaskAdmissionProofError("caller token header mismatch")
    if payload["producer_proof"] != producer_proof_header:
        raise TaskAdmissionProofError("producer proof header mismatch")
    exact_submit = _submit_bytes(raw_body, payload)
    expected = binding_digest(
        method=PUBLIC_METHOD,
        route=PUBLIC_ROUTE,
        caller_token=caller_token_header,
        idempotency_key=payload["idempotency_key"],
        body=exact_submit,
    )
    if _proof_binding(producer_proof_header) != expected:
        raise TaskAdmissionProofError("producer proof binding mismatch")
    return payload, exact_submit
