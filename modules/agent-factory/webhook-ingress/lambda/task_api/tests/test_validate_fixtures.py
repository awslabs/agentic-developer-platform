"""Edge validation against the T0-supplied submit fixtures, consumed unchanged.

The fixtures are the frozen contract's own positive and negative cases. Using
them rather than hand-written equivalents is what makes this evidence mean
something: a limit quietly widened in the implementation shows up here as a
negative fixture that stopped being refused.

Criteria exercised: T2-AC01 (valid caller shape accepted), T2-AC02 (malformed,
oversize, forged-tenant and legacy-persona submissions refused).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from task_api import contract, errors, validate

_FIXTURES = (
    Path(__file__).resolve().parents[6]
    / "docs"
    / "task-api"
    / "contracts"
    / "v1"
    / "fixtures"
)

pytestmark = pytest.mark.skipif(
    not _FIXTURES.is_dir(),
    reason="contract fixtures are repository docs, absent from the deployed package",
)


def _fixture(kind: str, name: str) -> dict:
    return json.loads((_FIXTURES / kind / f"{name}.json").read_text())


def _instance(fixture: dict) -> dict:
    """Strip the self-describing ``$fixture`` block, applying any recipe."""
    body = {k: v for k, v in fixture.items() if k != "$fixture"}
    for step in fixture["$fixture"].get("materialize", []):
        pointer = step["json_pointer"].lstrip("/").split("/")
        target = body
        for part in pointer[:-1]:
            target = target.setdefault(part, {})
        target[pointer[-1]] = step["value"] * step["count"]
    return body


def _raw_bytes(fixture: dict) -> bytes:
    """Recover the exact request bytes a transport-stage fixture encodes."""
    block = fixture["$fixture"]
    if "raw_instance_base64" in fixture:
        return base64.b64decode(fixture["raw_instance_base64"])
    if "raw_instance" in fixture:
        return fixture["raw_instance"].encode()
    if block.get("validation_stage") == "encoded_size":
        return json.dumps(_instance(fixture), separators=(",", ":")).encode()
    raise AssertionError("fixture carries no transport-stage bytes")


# --- valid fixtures must be accepted ------------------------------------


@pytest.mark.parametrize("name", ["submit-request", "submit-request-full"])
def test_valid_submit_request_is_accepted(name):
    body = _instance(_fixture("valid", name))
    assert validate.submit_request(body) is body


@pytest.mark.parametrize(
    "name", ["submit-response", "submit-response-idempotent-replay"]
)
def test_valid_submit_response_is_accepted(name):
    receipt = _instance(_fixture("valid", name))
    assert validate.submit_response(receipt) is receipt


def test_replay_fixture_is_distinguishable_from_a_fresh_acceptance():
    """A retry must be reported as a replay, not as newly created work."""
    fresh = _instance(_fixture("valid", "submit-response"))
    replay = _instance(_fixture("valid", "submit-response-idempotent-replay"))
    assert fresh["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    # The handle a retry receives is the one already issued (T2-AC03).
    assert replay["task_id"] == fresh["task_id"]
    assert replay["invocation_id"] == fresh["invocation_id"]


# --- transport-stage negative fixtures ----------------------------------


def test_oversize_encoded_request_is_refused_as_payload_too_large():
    raw = _raw_bytes(_fixture("invalid", "submit-request-body-byte-limit"))
    assert len(raw) > contract.MAX_BODY_BYTES
    with pytest.raises(errors.TaskApiError) as caught:
        validate.decode_body(raw)
    assert caught.value.code == "payload_too_large"
    assert caught.value.status == 413
    assert caught.value.details == {
        "limit_name": "submit_body.max_body_bytes",
        "limit_value": 65536,
    }


@pytest.mark.parametrize(
    "name",
    [
        "submit-request-duplicate-field",
        "submit-request-invalid-utf8",
        "submit-request-nonfinite-number",
    ],
)
def test_undecodable_request_is_refused_before_schema_validation(name):
    raw = _raw_bytes(_fixture("invalid", name))
    with pytest.raises(errors.TaskApiError) as caught:
        validate.decode_body(raw)
    assert caught.value.code == "invalid_request"
    assert caught.value.status == 400


def test_duplicate_field_cannot_silently_pick_an_interpretation():
    """Either interpretation would be a guess, so the request is refused."""
    fixture = _fixture("invalid", "submit-request-duplicate-field")
    raw = _raw_bytes(fixture)
    assert b"first interpretation" in raw and b"second interpretation" in raw
    with pytest.raises(errors.TaskApiError):
        validate.decode_body(raw)


# --- shape-stage negative fixtures --------------------------------------


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("submit-request-body-too-large", "invalid_request"),
        ("submit-request-caller-supplied-limits", "invalid_request"),
        ("submit-request-caller-supplied-model", "invalid_request"),
        ("submit-request-caller-supplied-tenant", "invalid_request"),
        ("submit-request-legacy-persona", "disallowed_persona"),
        ("submit-request-too-many-artifacts", "invalid_request"),
    ],
)
def test_invalid_submit_request_is_refused(name, code):
    body = _instance(_fixture("invalid", name))
    with pytest.raises(errors.TaskApiError) as caught:
        validate.submit_request(body)
    assert caught.value.code == code


def test_legacy_github_persona_does_not_fall_through_to_an_agent():
    """A GitHub persona reaching the task API is refused, never dispatched."""
    body = _instance(_fixture("invalid", "submit-request-legacy-persona"))
    assert body["persona"] == "agent-developer"
    with pytest.raises(errors.TaskApiError) as caught:
        validate.submit_request(body)
    assert caught.value.code == "disallowed_persona"
    assert caught.value.status == 403


def test_forged_tenant_is_refused_rather_than_ignored():
    """Silently dropping a forged field would let a caller believe it worked."""
    body = _instance(_fixture("invalid", "submit-request-caller-supplied-tenant"))
    assert "tenant_id" in body
    with pytest.raises(errors.TaskApiError) as caught:
        validate.submit_request(body)
    assert caught.value.code == "invalid_request"
    assert "tenant_id" in caught.value.message


def test_error_fixtures_match_the_shapes_this_module_emits():
    """The emitted refusals are the contract's own error fixtures."""
    too_large = _instance(_fixture("valid", "error-payload-too-large"))
    emitted = errors.payload_too_large(
        "submit_body.max_body_bytes", contract.MAX_BODY_BYTES
    ).body(too_large["request_id"])
    assert emitted["code"] == too_large["code"]
    assert emitted["http_status"] == too_large["http_status"]
    assert emitted["details"] == too_large["details"]
