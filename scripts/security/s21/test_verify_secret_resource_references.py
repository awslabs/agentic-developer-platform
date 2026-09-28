"""Reference classification requires a whole identifier and its bound use."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "reference_verifier",
    Path(__file__).with_name("verify_secret_resource_references.py"),
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)

# Synthetic account/resource identifiers, never resolved.
REFERENCE = "arn:aws:secretsmanager:us-east-1:123:secret:synthetic-resource"


def test_explicit_api_identifier_argument():
    source = f"client.get_secret_value(SecretId={REFERENCE!r})"
    assert verifier.arn_service(REFERENCE) == "secretsmanager"
    assert verifier.context_proofs(source, REFERENCE, 1)[0]["field"] == "SecretId"


def test_bound_reference_variable():
    source = f"REFERENCE = {REFERENCE!r}\noptions = {{'KEY_SECRET_ARN': REFERENCE}}"
    proof = verifier.context_proofs(source, REFERENCE, 1)[0]
    assert proof["variable"] == "REFERENCE" and proof["field"] == "KEY_SECRET_ARN"


def test_reference_used_as_credential_payload_is_not_accepted():
    source = f"create_credential(password={REFERENCE!r})"
    with pytest.raises(AssertionError, match="reference field"):
        verifier.context_proofs(source, REFERENCE, 1)


def test_unconsumed_arn_named_variable_is_not_accepted():
    source = f"SECRET_ARN = {REFERENCE!r}\nprint(SECRET_ARN)"
    with pytest.raises(AssertionError, match="reference field"):
        verifier.context_proofs(source, REFERENCE, 1)


def test_shadowed_variable_cannot_supply_other_binding_proof():
    source = (
        f"SECRET_ARN = {REFERENCE!r}\n"
        "def unrelated():\n"
        "    SECRET_ARN = 'other-value'\n"
        "    return dict(secret_arn=SECRET_ARN)\n"
    )
    with pytest.raises(AssertionError, match="reference field"):
        verifier.context_proofs(source, REFERENCE, 1)


def test_concatenated_reference_prefix_is_not_a_complete_literal():
    source = f"secret_arn = {REFERENCE!r} + payload"
    with pytest.raises(AssertionError, match="reference field"):
        verifier.context_proofs(source, REFERENCE, 1)


@pytest.mark.parametrize(
    "candidate",
    [
        "arn:secret",
        "arn:aws:secretsmanager:us-east-1:${account}:secret:x",
        "arn:aws:kms:us-east-1:123:key/synthetic",
        REFERENCE + " payload",
    ],
)
def test_unsupported_or_incomplete_identifier_is_not_accepted(candidate):
    with pytest.raises(AssertionError):
        verifier.arn_service(candidate)
