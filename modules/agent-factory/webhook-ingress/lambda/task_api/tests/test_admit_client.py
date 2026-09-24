"""Security and failure semantics for the internal admission transport."""

import pytest

from task_api import admit_client, errors


def test_endpoint_accepts_only_execute_api_in_the_configured_shape(monkeypatch):
    monkeypatch.setenv(
        "ADP_TASK_ADMIT_ENDPOINT",
        "https://abc123.execute-api.eu-west-1.amazonaws.com/staging/",
    )
    assert admit_client._endpoint() == (
        "https://abc123.execute-api.eu-west-1.amazonaws.com/staging",
        "eu-west-1",
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://abc123.execute-api.eu-west-1.amazonaws.com/staging",
        "https://gateway.example.com/staging",
        "https://abc123.execute-api.eu-west-1.amazonaws.com/staging?next=evil",
        "https://user@abc123.execute-api.eu-west-1.amazonaws.com/staging",
    ],
)
def test_endpoint_rejects_credential_exfiltration_targets(monkeypatch, endpoint):
    monkeypatch.setenv("ADP_TASK_ADMIT_ENDPOINT", endpoint)
    with pytest.raises(errors.TaskApiError) as caught:
        admit_client._endpoint()
    assert caught.value.code == "prerequisite_unavailable"


def test_binding_is_length_delimited_and_bound_to_exact_request_bytes():
    original = admit_client.binding_digest(
        method="POST",
        route="/v1/tasks",
        caller_token="token-ab",
        idempotency_key="c",
        body=b'{"instructions":"first"}',
    )
    rearranged = admit_client.binding_digest(
        method="POST",
        route="/v1/tasks",
        caller_token="token-a",
        idempotency_key="bc",
        body=b'{"instructions":"first"}',
    )
    changed_body = admit_client.binding_digest(
        method="POST",
        route="/v1/tasks",
        caller_token="token-ab",
        idempotency_key="c",
        body=b'{"instructions":"second"}',
    )

    assert original != rearranged
    assert original != changed_body


def test_gateway_error_text_and_details_cannot_reflect_secrets():
    refusal = admit_client._relay(
        400,
        {
            "code": "invalid_request",
            "message": "Bearer caller-secret",
            "details": {"task_id": "caller-secret"},
        },
    )

    assert refusal.code == "invalid_request"
    assert "caller-secret" not in refusal.message
    assert refusal.details is None


def test_invalid_retry_delay_is_not_relayed():
    refusal = admit_client._relay(
        503,
        {
            "code": "prerequisite_unavailable",
            "message": "unavailable",
            "retry_after_ms": -1,
        },
    )

    assert refusal.retry_after_ms is None
    assert "same Idempotency-Key" in refusal.message


def test_unknown_or_mismatched_gateway_error_fails_closed():
    unknown = admit_client._relay(418, {"code": "teapot", "message": "no"})
    mismatched = admit_client._relay(
        401, {"code": "idempotency_conflict", "message": "no"}
    )

    assert unknown.code == "prerequisite_unavailable"
    assert mismatched.code == "prerequisite_unavailable"
