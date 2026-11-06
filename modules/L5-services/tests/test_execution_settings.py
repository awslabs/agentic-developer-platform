"""Saved execution choices must be complete before any provider/delegation work."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from l5_services.configuration_readiness import assess_readiness, effective_monitoring, execution_input_gaps
from l5_services.run_input import ExecutionLimits, ExecutionSettings


LIMITS = {"deadline_seconds": 3600, "max_provider_attempts": 1, "max_polls": 30, "concurrency": 1,
          "max_cost": "1.00", "currency": "USD", "policy_ref": {
              "tenant_id": "example-tenant", "project_id": "example-project", "source_connection_id": "example-source",
              "record_type": "policy", "record_id": "example-budget", "record_revision": "1"}}


def test_runtime_limits_and_classification_follow_shared_contract():
    schema = json.loads((Path(__file__).resolve().parents[3] / "docs/l5-services/contracts/v1/schemas.json").read_text())["$defs"]
    runtime = ExecutionLimits.model_json_schema()
    assert runtime["required"] == schema["Limits"]["required"]
    assert runtime["additionalProperties"] is False
    for field, expected in schema["Limits"]["properties"].items():
        actual = runtime["properties"][field]
        if field == "policy_ref":
            assert actual["$ref"] == "#/$defs/RecordRef"
        elif field == "currency":
            assert [actual["const"]] == expected["enum"]
        else:
            assert {key: actual[key] for key in expected} == expected
    assert (ExecutionSettings.model_json_schema()["properties"]["classification"]["enum"]
            == schema["LegacyConfigurationInput"]["properties"]["classification"]["enum"])


@pytest.mark.parametrize("service_id", ["security", "well_architected"])
def test_partial_limits_report_each_missing_field_without_mutating_saved_choices(service_id):
    settings = {"enabled": True, "purpose": "Review", "classification": "internal", "limits": {"deadline_seconds": 300},
                "connection_ref": {"record_id": "connection"}, "region": "us-east-1", "source_refs": [{"record_type": "document"}],
                "capabilities": ["threat_model"] if service_id == "security" else ["architecture_review"],
                "well_architected": {"execution_role_ref": {"record_id": "role"}, "source_store_ref": {"record_id": "store"}}}
    before = deepcopy(settings)
    state, gaps, _ = assess_readiness(settings, "configuration", [], delegation_active=True, service_id=service_id)
    assert state == "needs_input"
    assert [gap for gap in gaps if gap.startswith("limits.")] == [
        "limits.max_provider_attempts", "limits.max_polls", "limits.concurrency", "limits.max_cost", "limits.currency", "limits.policy_ref"]
    assert settings == before
    # Incomplete drafts stay editable; they never gain execution authority.
    assert assess_readiness({**settings, "enabled": False}, "configuration", [], delegation_active=True, service_id=service_id)[0] == "disabled"
    complete = {**settings, "limits": LIMITS}
    state, gaps, _ = assess_readiness(complete, "configuration", [], delegation_active=True, service_id=service_id)
    assert not any(gap == "classification" or gap.startswith("limits") for gap in gaps)
    assert state == ("ready" if service_id == "security" else "needs_input")


@pytest.mark.parametrize("field,value", [
    ("deadline_seconds", True), ("deadline_seconds", "3600"), ("deadline_seconds", 59), ("deadline_seconds", 86401),
    ("max_provider_attempts", 0), ("max_provider_attempts", 11), ("max_polls", 0), ("max_polls", 2881),
    ("concurrency", False), ("concurrency", 11), ("currency", "EUR"), ("max_cost", float("nan")),
    ("max_cost", "NaN"), ("max_cost", "Infinity"), ("max_cost", "1e3"), ("max_cost", "-1"),
    ("max_cost", "1.1234567"), ("max_cost", 1), ("policy_ref", {}), ("unknown", True),
])
def test_malformed_limit_values_never_clear_readiness(field, value):
    gaps = execution_input_gaps({"classification": "internal", "limits": {**LIMITS, field: value}})
    assert any(gap == f"limits.{field}" or gap.startswith(f"limits.{field}.") for gap in gaps)


@pytest.mark.parametrize("value", [None, "", "secret", "Internal", True, {}, []])
def test_classification_requires_shared_enum(value):
    assert execution_input_gaps({"classification": value, "limits": LIMITS}) == ["classification"]


@pytest.mark.parametrize("value", [None, [], "limits", True])
def test_malformed_limits_are_actionable_even_with_monitoring(value):
    settings = {"classification": "internal", "limits": value, "monitoring": {"stall_after_seconds": 1200}}
    assert execution_input_gaps(settings) == ["limits"]
    assert effective_monitoring(settings)["stall_after_seconds"] == 1200


@pytest.mark.parametrize("field,value", [("tenant_id", "foreign-tenant"), ("project_id", "foreign-project"), ("record_type", "document")])
def test_budget_reference_must_match_saved_profile_scope_and_policy_kind(field, value):
    settings = {"classification": "internal", "profile_ref": {"tenant_id": "example-tenant", "project_id": "example-project"},
                "limits": {**LIMITS, "policy_ref": {**LIMITS["policy_ref"], field: value}}}
    assert execution_input_gaps(settings) == [f"limits.policy_ref.{field}"]
    assert execution_input_gaps(settings, scope=settings["profile_ref"]) == [f"limits.policy_ref.{field}"]


@pytest.mark.parametrize("value", ["HEAD", "latest", "null", ""])
def test_budget_policy_reference_must_be_immutable(value):
    assert execution_input_gaps({"classification": "internal", "limits": {
        **LIMITS, "policy_ref": {**LIMITS["policy_ref"], "record_revision": value}}}) == ["limits.policy_ref.record_revision"]


def test_zero_cost_bound_is_valid_but_does_not_grant_budget():
    # Canonical Limits allows zero; admission's independent budget authority
    # still decides whether a particular assessment can run.
    assert execution_input_gaps({"classification": "restricted", "limits": {**LIMITS, "max_cost": "0"}}) == []
