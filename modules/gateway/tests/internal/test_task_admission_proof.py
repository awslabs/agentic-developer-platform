"""Verifier-side tests for exact Task API producer-proof binding."""

import base64
import json

import pytest

from src.internal.task_admission_proof import (
    PROOF_BINDING_HEADER,
    TaskAdmissionProofError,
    binding_digest,
    verify_admission_binding,
)


def _proof(*, request_body: bytes, caller_token: str, idempotency_key: str) -> str:
    binding = binding_digest(
        method="POST",
        route="/v1/tasks",
        caller_token=caller_token,
        idempotency_key=idempotency_key,
        body=request_body,
    )
    headers = {
        "authorization": (
            "AWS4-HMAC-SHA256 Credential=fixture,"
            "SignedHeaders=content-type;x-adp-work-invocation;x-amz-date,"
            "Signature=fixture"
        ),
        "content-type": "application/x-www-form-urlencoded",
        "x-amz-date": "20260924T120000Z",
        PROOF_BINDING_HEADER: binding,
    }
    return base64.b64encode(
        json.dumps(headers, sort_keys=True, separators=(",", ":")).encode()
    ).decode()


def _payload(
    *, request_body: bytes, caller_token: str, idempotency_key: str, proof: str
) -> bytes:
    def encode(value):
        return json.dumps(value, separators=(",", ":")).encode()
    return b"".join(
        (
            b'{"schema_version":"1.0","submit":',
            request_body,
            b',"idempotency_key":',
            encode(idempotency_key),
            b',"caller_token":',
            encode(caller_token),
            b',"producer_proof":',
            encode(proof),
            b"}",
        )
    )


def _valid():
    request_body = (
        b' \n{ "schema_version":"1.0", "persona":"agent-task-investigator",'
        b' "instructions":"Investigate" }\t'
    )
    caller_token = "token-a"
    idempotency_key = "key-a"
    proof = _proof(
        request_body=request_body,
        caller_token=caller_token,
        idempotency_key=idempotency_key,
    )
    return request_body, caller_token, idempotency_key, proof


def test_verifier_recovers_the_exact_public_request_bytes():
    request_body, caller_token, idempotency_key, proof = _valid()
    raw = _payload(
        request_body=request_body,
        caller_token=caller_token,
        idempotency_key=idempotency_key,
        proof=proof,
    )

    payload, exact = verify_admission_binding(
        raw,
        caller_token_header=caller_token,
        producer_proof_header=proof,
    )

    assert exact == request_body
    assert payload["submit"]["instructions"] == "Investigate"


@pytest.mark.parametrize("mutation", ["body", "ordering", "token", "key"])
def test_verifier_rejects_every_request_binding_mutation(mutation):
    request_body, caller_token, idempotency_key, proof = _valid()
    if mutation == "body":
        request_body = request_body.replace(b"Investigate", b"Exfiltrate")
    elif mutation == "ordering":
        request_body = (
            b'{"instructions":"Investigate","persona":"agent-task-investigator",'
            b'"schema_version":"1.0"}'
        )
    elif mutation == "token":
        caller_token = "token-b"
    else:
        idempotency_key = "key-b"
    raw = _payload(
        request_body=request_body,
        caller_token=caller_token,
        idempotency_key=idempotency_key,
        proof=proof,
    )

    with pytest.raises(TaskAdmissionProofError, match="binding mismatch"):
        verify_admission_binding(
            raw,
            caller_token_header=caller_token,
            producer_proof_header=proof,
        )


def test_verifier_rejects_header_and_body_credential_disagreement():
    request_body, caller_token, idempotency_key, proof = _valid()
    raw = _payload(
        request_body=request_body,
        caller_token=caller_token,
        idempotency_key=idempotency_key,
        proof=proof,
    )

    with pytest.raises(TaskAdmissionProofError, match="token header mismatch"):
        verify_admission_binding(
            raw,
            caller_token_header="token-b",
            producer_proof_header=proof,
        )
    with pytest.raises(TaskAdmissionProofError, match="proof header mismatch"):
        verify_admission_binding(
            raw,
            caller_token_header=caller_token,
            producer_proof_header=proof + "changed",
        )


def test_verifier_rejects_noncanonical_wrapper_that_drops_byte_provenance():
    request_body, caller_token, idempotency_key, proof = _valid()
    reserialized = json.dumps(
        {
            "schema_version": "1.0",
            "submit": json.loads(request_body),
            "idempotency_key": idempotency_key,
            "caller_token": caller_token,
            "producer_proof": proof,
        }
    ).encode()

    with pytest.raises(TaskAdmissionProofError, match="wrapper"):
        verify_admission_binding(
            reserialized,
            caller_token_header=caller_token,
            producer_proof_header=proof,
        )


def test_binding_matches_the_cross_component_vector():
    assert binding_digest(
        method="POST",
        route="/v1/tasks",
        caller_token="token-a",
        idempotency_key="key-a",
        body=b'{"schema_version":"1.0","instructions":"Investigate"}',
    ) == "fa39c63ae897cca037b7b0d622d66a969777500b9b80ef5b80a79b5c553082f3"
