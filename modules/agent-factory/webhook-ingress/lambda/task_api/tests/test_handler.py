"""``POST /v1/tasks`` handler behavior.

The through-line of these tests is that a 202 must be earned: it may only be
returned when the gateway confirmed a durable record, and everything else —
refusal, timeout, malformed receipt, disabled route — must be something other
than a 202, with nothing queued.

Criteria exercised: T2-AC01, T2-AC02, T2-AC03, T2-AC05.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from task_api import contract, errors, handler

_VALID_SUBMIT = {
    "schema_version": "1.0",
    "persona": "agent-task-investigator",
    "instructions": "Investigate the checkout-api 503 spike.",
}

_TASK_ID = "tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20"
_RECEIPT = {
    "schema_version": "1.0",
    "task_id": _TASK_ID,
    "invocation_id": "5e7a9c31-4d6f-4813-ba25-9c1e3f5a7d40",
    "status": "accepted",
    "created_at": "2026-09-24T14:42:03Z",
    "deadline_at": "2026-09-24T15:12:03Z",
    "status_url": f"/v1/tasks/{_TASK_ID}",
    "events_url": f"/v1/tasks/{_TASK_ID}/events",
    "request_id": "req-01JC9K2M4P7Q",
    "idempotent_replay": False,
}


def _event(
    *,
    body=None,
    headers=None,
    method="POST",
    resource="/v1/tasks",
    is_base64=False,
):
    merged = {
        "Authorization": "Bearer caller-access-token",
        "Idempotency-Key": "incident-483-investigation",
        "Content-Type": "application/json",
    }
    if headers is not None:
        merged = {**merged, **headers}
        merged = {k: v for k, v in merged.items() if v is not None}
    if body is None:
        body = json.dumps(_VALID_SUBMIT)
    return {
        "resource": resource,
        "httpMethod": method,
        "headers": merged,
        "body": body,
        "isBase64Encoded": is_base64,
        "requestContext": {"requestId": "apigw-request-id"},
    }


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv(contract.ADMISSION_FLAG, "true")


def _body(response):
    return json.loads(response["body"])


# --- acceptance ---------------------------------------------------------


def test_valid_submission_returns_202_with_the_gateway_receipt(enabled):
    with patch("task_api.handler.admit_client.admit", return_value=_RECEIPT) as admit:
        response = handler.handle_task_submit(_event(), None)

    assert response["statusCode"] == 202
    assert _body(response) == _RECEIPT
    # No GitHub field is required of the caller, and none is invented.
    forwarded = admit.call_args.kwargs["submit"]
    assert forwarded == _VALID_SUBMIT
    assert not {"repo", "installation_id", "issue"} & set(forwarded)


def test_the_202_carries_only_identifiers_the_gateway_issued(enabled):
    """The Lambda must not mint a task ID, timestamp or status of its own."""
    with patch("task_api.handler.admit_client.admit", return_value=_RECEIPT):
        body = _body(handler.handle_task_submit(_event(), None))
    for field in ("task_id", "invocation_id", "created_at", "deadline_at", "status"):
        assert body[field] == _RECEIPT[field]


def test_idempotent_replay_returns_the_existing_handle(enabled):
    replay = {**_RECEIPT, "idempotent_replay": True}
    with patch("task_api.handler.admit_client.admit", return_value=replay):
        first = _body(handler.handle_task_submit(_event(), None))
    with patch("task_api.handler.admit_client.admit", return_value=replay):
        second = _body(handler.handle_task_submit(_event(), None))
    assert first["task_id"] == second["task_id"] == _TASK_ID
    assert second["idempotent_replay"] is True


def test_exact_request_bytes_are_forwarded_for_proof_binding(enabled):
    """A re-serialized body would break the proof's byte binding."""
    raw = '{"schema_version":"1.0","persona":"agent-task-investigator",' \
          '"instructions":"Investigate the checkout-api 503 spike."}'
    with patch("task_api.handler.admit_client.admit", return_value=_RECEIPT) as admit:
        handler.handle_task_submit(_event(body=raw), None)
    assert admit.call_args.kwargs["request_body"] == raw.encode()


def test_base64_encoded_body_is_decoded_to_its_original_bytes(enabled):
    import base64

    raw = json.dumps(_VALID_SUBMIT).encode()
    event = _event(body=base64.b64encode(raw).decode(), is_base64=True)
    with patch("task_api.handler.admit_client.admit", return_value=_RECEIPT) as admit:
        assert handler.handle_task_submit(event, None)["statusCode"] == 202
    assert admit.call_args.kwargs["request_body"] == raw


# --- credential and forwarding ------------------------------------------


def test_missing_authorization_is_refused_without_forwarding(enabled):
    with patch("task_api.handler.admit_client.admit") as admit:
        response = handler.handle_task_submit(
            _event(headers={"Authorization": None}), None
        )
    assert response["statusCode"] == 401
    assert _body(response)["code"] == "invalid_credential"
    admit.assert_not_called()


@pytest.mark.parametrize(
    "value", ["", "   ", "caller-access-token", "Basic abc", "Bearer", "Bearer   "]
)
def test_malformed_authorization_is_refused_without_forwarding(enabled, value):
    with patch("task_api.handler.admit_client.admit") as admit:
        response = handler.handle_task_submit(
            _event(headers={"Authorization": value}), None
        )
    assert response["statusCode"] == 401
    admit.assert_not_called()


def test_only_the_callers_own_authorization_becomes_the_forwarded_token(enabled):
    """A client-supplied forwarding header must never be trusted as identity.

    Otherwise a caller could authenticate as itself while asking the gateway
    to act on a different, more privileged credential.
    """
    event = _event(
        headers={
            "Authorization": "Bearer genuine-caller-token",
            contract.CALLER_TOKEN_HEADER: "Bearer smuggled-privileged-token",
        }
    )
    with patch("task_api.handler.admit_client.admit", return_value=_RECEIPT) as admit:
        handler.handle_task_submit(event, None)
    assert admit.call_args.kwargs["caller_token"] == "genuine-caller-token"


def test_the_caller_needs_no_worker_or_pod_credential(enabled):
    """T2-AC05: nothing beyond the bearer token is read from the request."""
    with patch("task_api.handler.admit_client.admit", return_value=_RECEIPT) as admit:
        assert handler.handle_task_submit(_event(), None)["statusCode"] == 202
    assert set(admit.call_args.kwargs) == {
        "submit",
        "idempotency_key",
        "caller_token",
        "request_body",
    }


# --- idempotency key ----------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [None, "", "x" * 129, "keyé", "key\n", "key\twith-tab"],
)
def test_absent_or_malformed_idempotency_key_is_refused(enabled, key):
    with patch("task_api.handler.admit_client.admit") as admit:
        response = handler.handle_task_submit(
            _event(headers={"Idempotency-Key": key}), None
        )
    assert response["statusCode"] == 400
    assert _body(response)["code"] == "invalid_request"
    admit.assert_not_called()


def test_idempotency_key_is_forwarded_unchanged(enabled):
    with patch("task_api.handler.admit_client.admit", return_value=_RECEIPT) as admit:
        handler.handle_task_submit(
            _event(headers={"Idempotency-Key": "incident-483"}), None
        )
    assert admit.call_args.kwargs["idempotency_key"] == "incident-483"


def test_header_lookup_is_case_insensitive(enabled):
    """REST v1 preserves header case; HTTP v2 lowercases."""
    event = _event(headers={"Authorization": None, "Idempotency-Key": None})
    event["headers"].update(
        {"AUTHORIZATION": "Bearer t", "idempotency-key": "k"}
    )
    with patch("task_api.handler.admit_client.admit", return_value=_RECEIPT):
        assert handler.handle_task_submit(event, None)["statusCode"] == 202


# --- body refusals ------------------------------------------------------


def test_absent_body_is_refused(enabled):
    event = _event()
    event["body"] = None
    with patch("task_api.handler.admit_client.admit") as admit:
        assert handler.handle_task_submit(event, None)["statusCode"] == 400
    admit.assert_not_called()


def test_forged_tenant_is_refused_without_forwarding(enabled):
    body = json.dumps({**_VALID_SUBMIT, "tenant_id": "another-tenant"})
    with patch("task_api.handler.admit_client.admit") as admit:
        response = handler.handle_task_submit(_event(body=body), None)
    assert response["statusCode"] == 400
    admit.assert_not_called()


def test_non_string_artifact_id_is_a_client_error_without_forwarding(enabled):
    body = dict(_VALID_SUBMIT)
    body["artifact_ids"] = [{"forged": "artifact"}]
    with patch("task_api.handler.admit_client.admit") as admit:
        response = handler.handle_task_submit(_event(body=json.dumps(body)), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["code"] == "invalid_request"
    admit.assert_not_called()


def test_legacy_persona_is_refused_without_forwarding(enabled):
    body = json.dumps({**_VALID_SUBMIT, "persona": "agent-developer"})
    with patch("task_api.handler.admit_client.admit") as admit:
        response = handler.handle_task_submit(_event(body=body), None)
    assert response["statusCode"] == 403
    assert _body(response)["code"] == "disallowed_persona"
    admit.assert_not_called()


def test_oversize_body_is_refused_before_any_forwarding(enabled):
    body = json.dumps(
        {**_VALID_SUBMIT, "inputs": {"padding": "x" * contract.MAX_BODY_BYTES}}
    )
    with patch("task_api.handler.admit_client.admit") as admit:
        response = handler.handle_task_submit(_event(body=body), None)
    assert response["statusCode"] == 413
    assert _body(response)["code"] == "payload_too_large"
    admit.assert_not_called()


def test_a_wrong_method_on_this_resource_is_not_found(enabled):
    response = handler.handle_task_submit(_event(method="DELETE"), None)
    assert response["statusCode"] == 404


# --- the route must not fabricate acceptance ----------------------------


def test_gateway_refusal_is_relayed_not_converted_to_acceptance(enabled):
    refusal = errors.TaskApiError(
        "idempotency_conflict",
        "That idempotency key was used with a different request body.",
        details={"task_id": _TASK_ID, "current_status": "running"},
    )
    with patch("task_api.handler.admit_client.admit", side_effect=refusal):
        response = handler.handle_task_submit(_event(), None)
    assert response["statusCode"] == 409
    body = _body(response)
    assert body["code"] == "idempotency_conflict"
    assert body["details"]["task_id"] == _TASK_ID


def test_unavailable_gateway_cannot_produce_a_202(enabled):
    with patch(
        "task_api.handler.admit_client.admit",
        side_effect=errors.prerequisite_unavailable(),
    ):
        response = handler.handle_task_submit(_event(), None)
    assert response["statusCode"] == 503
    body = _body(response)
    assert body["code"] == "prerequisite_unavailable"
    assert body["retry_after_ms"] >= 0


def test_an_unexpected_fault_cannot_produce_a_202(enabled):
    """T2-AC03: a storage or authority failure is never a false acceptance."""
    with patch(
        "task_api.handler.admit_client.admit", side_effect=RuntimeError("boom")
    ):
        response = handler.handle_task_submit(_event(), None)
    assert response["statusCode"] == 503
    assert _body(response)["code"] == "prerequisite_unavailable"


@pytest.mark.parametrize(
    "mutation",
    [
        {"task_id": "not-a-task-id"},
        {"status": "running"},
        {"status": "completed"},
        {"status_url": "/v1/tasks/tsk_00000000-0000-4000-8000-000000000000"},
        {"events_url": "/v1/tasks/other/events"},
        {"created_at": "2026-09-24 14:42:03"},
        {"created_at": "2026-99-24T14:42:03Z"},
        {"invocation_id": "not-a-uuid"},
        {"schema_version": "2.0"},
        {"unexpected_field": "value"},
    ],
)
def test_a_receipt_the_lambda_cannot_validate_is_not_an_acceptance(enabled, mutation):
    """A malformed receipt might not describe a durable record, so it is not
    relayed as one."""
    with patch(
        "task_api.handler.admit_client.admit", return_value={**_RECEIPT, **mutation}
    ):
        response = handler.handle_task_submit(_event(), None)
    assert response["statusCode"] == 503


def test_a_receipt_missing_a_required_field_is_not_an_acceptance(enabled):
    for field in contract.SUBMIT_RESPONSE_REQUIRED:
        partial = {k: v for k, v in _RECEIPT.items() if k != field}
        with patch("task_api.handler.admit_client.admit", return_value=partial):
            response = handler.handle_task_submit(_event(), None)
        assert response["statusCode"] == 503, f"missing {field} was accepted"


# --- rollout flag -------------------------------------------------------


def test_disabled_admission_accepts_nothing(monkeypatch):
    monkeypatch.delenv(contract.ADMISSION_FLAG, raising=False)
    with patch("task_api.handler.admit_client.admit") as admit:
        response = handler.handle_task_submit(_event(), None)
    assert response["statusCode"] == 503
    assert _body(response)["code"] == "prerequisite_unavailable"
    admit.assert_not_called()


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "TRUE-ish"])
def test_flag_must_be_explicitly_affirmative(monkeypatch, value):
    monkeypatch.setenv(contract.ADMISSION_FLAG, value)
    with patch("task_api.handler.admit_client.admit") as admit:
        assert handler.handle_task_submit(_event(), None)["statusCode"] == 503
    admit.assert_not_called()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes"])
def test_affirmative_flag_values_enable_the_route(monkeypatch, value):
    monkeypatch.setenv(contract.ADMISSION_FLAG, value)
    with patch("task_api.handler.admit_client.admit", return_value=_RECEIPT):
        assert handler.handle_task_submit(_event(), None)["statusCode"] == 202


# --- responses never leak the caller's secrets --------------------------


@pytest.mark.parametrize(
    "case",
    [
        {"headers": {"Authorization": "Bearer super-secret-token"}},
        {"body": json.dumps({**_VALID_SUBMIT, "tenant_id": "t"})},
        {"headers": {"Idempotency-Key": "k" * 200}},
    ],
)
def test_no_response_body_echoes_the_caller_token(enabled, case):
    with patch(
        "task_api.handler.admit_client.admit",
        side_effect=errors.prerequisite_unavailable(),
    ):
        response = handler.handle_task_submit(_event(**case), None)
    assert "super-secret-token" not in response["body"]
    assert "Bearer" not in response["body"]


def test_every_error_body_matches_the_contract_error_shape(enabled):
    cases = [
        _event(headers={"Authorization": None}),
        _event(headers={"Idempotency-Key": None}),
        _event(body=json.dumps({**_VALID_SUBMIT, "persona": "agent-developer"})),
    ]
    for event in cases:
        body = _body(handler.handle_task_submit(event, None))
        assert set(body) <= {
            "schema_version",
            "code",
            "message",
            "request_id",
            "retry_after_ms",
            "http_status",
            "details",
        }
        assert body["schema_version"] == "1.0"
        assert body["code"] in contract.ERROR_STATUS
        assert body["http_status"] == contract.ERROR_STATUS[body["code"]]
        assert 1 <= len(body["message"]) <= contract.MAX_SAFE_MESSAGE_CHARACTERS
        assert body["request_id"] == "apigw-request-id"


@pytest.mark.parametrize("literal", ["1e400", "-1e400"])
def test_overflowing_json_number_is_rejected_before_admission(enabled, literal):
    raw = (
        '{"schema_version":"1.0","persona":"agent-task-investigator","instructions":"inspect","inputs":{"number":'
        + literal
        + "}}"
    )
    with patch("task_api.handler.admit_client.admit") as admit:
        response = handler.handle_task_submit(_event(body=raw), None)
    assert response["statusCode"] == 400
    admit.assert_not_called()
