"""The Lambda's transcribed contract must match the frozen contract files.

``task_api/contract.py`` mirrors values the Lambda cannot read at runtime. If
this test fails, the mirror drifted from the accepted design and the drift —
not this test — is the defect. Silently widening a limit in the mirror is
exactly the failure mode these checks exist to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from task_api import contract

_CONTRACTS = (
    Path(__file__).resolve().parents[6] / "docs" / "task-api" / "contracts" / "v1"
)
_DESIGN_REVISION = "b5761a4a2502aceaa9133afef552b567a19cb46e"

pytestmark = pytest.mark.skipif(
    not _CONTRACTS.is_dir(),
    reason="contract files are repository docs, absent from the deployed package",
)


def _load(name: str) -> dict:
    return json.loads((_CONTRACTS / name).read_text())


def test_submit_body_limits_match_contract():
    limits = _load("limits.json")["submit_body"]
    assert contract.MAX_BODY_BYTES == limits["max_body_bytes"]
    assert contract.MAX_INSTRUCTIONS_CHARACTERS == limits["max_instructions_characters"]
    assert (
        contract.MAX_EXTERNAL_REFERENCE_CHARACTERS
        == limits["max_external_reference_characters"]
    )
    assert (
        contract.MAX_ACCEPTANCE_CRITERIA_COUNT
        == limits["max_acceptance_criteria_count"]
    )
    assert (
        contract.MAX_ACCEPTANCE_CRITERION_CHARACTERS
        == limits["max_acceptance_criterion_characters"]
    )
    assert (
        contract.IDEMPOTENCY_KEY_MIN_CHARACTERS
        == limits["idempotency_key_min_characters"]
    )
    assert (
        contract.IDEMPOTENCY_KEY_MAX_CHARACTERS
        == limits["idempotency_key_max_characters"]
    )


def test_artifact_limit_matches_contract():
    assert (
        contract.MAX_INPUT_ARTIFACTS
        == _load("limits.json")["artifacts"]["max_input_artifacts"]
    )


def test_submit_request_fields_match_schema():
    submit = _load("schemas/public-api.schema.json")["$defs"]["submit_request"]
    assert submit["additionalProperties"] is False
    assert list(submit["required"]) == list(contract.SUBMIT_REQUIRED_FIELDS)
    assert set(submit["properties"]) == contract.SUBMIT_ALLOWED_FIELDS


def test_submit_response_fields_match_schema():
    response = _load("schemas/public-api.schema.json")["$defs"]["submit_response"]
    assert set(response["required"]) == set(contract.SUBMIT_RESPONSE_REQUIRED)
    assert set(response["properties"]) == contract.SUBMIT_RESPONSE_ALLOWED
    assert set(response["properties"]["status"]["enum"]) == (
        contract.SUBMIT_RESPONSE_STATUSES
    )


def test_persona_pattern_matches_schema():
    persona = _load("schemas/common.schema.json")["$defs"]["persona"]
    assert contract.PERSONA_PATTERN.pattern == persona["pattern"]
    assert contract.MAX_PERSONA_CHARACTERS == persona["maxLength"]


def test_error_codes_and_statuses_are_contract_codes():
    errors_schema = _load("schemas/errors.schema.json")["$defs"]
    permitted_codes = set(errors_schema["error_code"]["enum"])
    permitted_statuses = set(errors_schema["http_status"]["enum"])
    assert set(contract.ERROR_STATUS) <= permitted_codes
    assert set(contract.ERROR_STATUS.values()) <= permitted_statuses


def test_error_detail_fields_match_schema():
    detail = _load("schemas/errors.schema.json")["$defs"]["error_response"][
        "properties"
    ]["details"]
    assert detail["additionalProperties"] is False
    assert set(detail["properties"]) == contract.ERROR_DETAIL_FIELDS


def test_route_scope_and_ownership_match_contract():
    identity = _load("identity-and-lifecycle.json")
    submit_route = next(
        r for r in identity["public_routes"] if r["route"] == "POST /v1/tasks"
    )
    assert submit_route["owner"] == "T2"
    assert submit_route["scope"] == contract.SUBMIT_SCOPE
    admit_route = next(
        r for r in identity["internal_routes"] if r["contract_key"] == "admit"
    )
    assert admit_route["route"] == contract.ADMIT_ROUTE
    assert admit_route["caller"] == "ingress lambda"


def test_caller_forbidden_fields_cover_contract_rejections():
    rejected = set(
        _load("identity-and-lifecycle.json")["canonical_principal"][
            "caller_supplied_fields_rejected"
        ]
    )
    assert rejected <= contract.CALLER_FORBIDDEN_FIELDS


def test_proof_binding_components_match_contract():
    digests = _load("identity-and-lifecycle.json")["digests"]
    assert digests["producer_proof_binding"] == [
        "method",
        "route",
        "original token digest",
        "idempotency key",
        "exact request bytes",
    ]
    assert digests["hash"] == "SHA-256"


def test_design_revision_is_the_accepted_one():
    manifest = json.loads(
        (_CONTRACTS.parent.parent / "evaluation-manifest.json").read_text()
    )
    assert manifest["design_revision"] == _DESIGN_REVISION
