"""Exact operator compiler wiring; synthetic prices do not authorize live use."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.agentauth.native_test_atlassian_registration import AtlassianPricedCall, AtlassianQuoteInputs, compile_atlassian_contracts
from src.agentauth.native_test_atlassian_runtime import NativeAtlassianBinding, NativeAtlassianCatalogueBinding
from src.agentauth.native_test_facade_contract import FacadeEffectCostContract, FacadeEffectQuoteAdapter
from src.agentauth.native_test_registration import NativeRegistrationPlan, registration_plan, runtime_environment
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical
from tests.agentauth import test_native_test_registration as existing
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401

admission, sessions, native, all_in = existing.admission, existing.sessions, existing.native, existing.all_in


def inputs():
    old, evidence = existing.inputs_for(
        SimpleNamespace(
            store=SimpleNamespace(table="offline-authority"),
            fixture=SimpleNamespace(clock=[datetime(2026, 1, 1, tzinfo=UTC)]),
        )
    )
    binding = NativeAtlassianBinding(
        **{key: value for key, value in old.binding.model_dump().items() if key not in {"schema_version", "catalogue"}},
        schema_version="5.0",
        catalogue=NativeAtlassianCatalogueBinding(
            gateway_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/offline-gateway",
            gateway_role_arn="arn:aws:iam::123456789012:role/offline-gateway",
            target_id="abcdefghij",
            catalogue_table="offline-catalogue",
            target_binding_sha256="a" * 64,
            tools_sha256="b" * 64,
        ),
    )
    return AtlassianQuoteInputs(
        schema_version="2.0",
        binding=binding,
        not_before=old.not_before,
        expires_at=old.expires_at,
        setup=old.setup,
        listing=old.listing,
        calls=[AtlassianPricedCall(tool="atlassian___create_jira_issue", arguments={"cloudId": "owned", "summary": "☃"}, quantities=old.search)],
    ), evidence


def test_compiled_write_quote_binds_exact_payload_and_keeps_source_evidence():
    value, evidence = inputs()
    compiled = compile_atlassian_contracts(value, evidence=evidence)
    effect = next(row for row in compiled["effects"] if row["action"] == "call")
    contract = FacadeEffectCostContract.model_validate(compiled["contracts"][effect["contract_id"]])
    adapter = object.__new__(FacadeEffectQuoteAdapter)
    body = {
        "schema_version": "1.0",
        "action": "call",
        "tool": value.calls[0].tool,
        "arguments": value.calls[0].arguments,
        "request_id": "00000000-0000-4000-8000-000000000099",
        "execution_id": None,
        "ttl_seconds": None,
    }
    assert adapter._request_permitted(canonical(body).encode(), contract)
    assert not adapter._request_permitted(canonical({**body, "arguments": {"cloudId": "foreign"}}).encode(), contract)
    assert compiled["maximum_original_smoke_usd"] == "3.000000"
    assert compiled["evidence_sha256"] == sorted(evidence)


@pytest.mark.parametrize("damage", ["duplicate", "missing_evidence", "unknown_tool", "missing_site"])
def test_registration_cannot_fill_missing_authority_or_proof(damage):
    value, evidence = inputs()
    if damage == "duplicate":
        value = value.model_copy(update={"calls": value.calls * 2})
    elif damage == "missing_evidence":
        evidence = {}
    else:
        call = value.calls[0].model_copy(update={"tool": "atlassian___other"} if damage == "unknown_tool" else {"arguments": {}})
        value = value.model_copy(update={"calls": [call]})
    with pytest.raises(AdmissionRefusedError):
        compile_atlassian_contracts(value, evidence=evidence)


async def test_existing_registrar_serializes_atlassian_binding_without_installing_it(all_in):
    value, evidence = inputs()
    compiled = compile_atlassian_contracts(value, evidence=evidence)
    deployment = all_in.configuration.model_copy(
        update={
            "allowed_models": [],
            "allowed_routes": [],
            "allowed_tools": ["atlassian.create_jira_issue"],
            "effect_coverage_digest": compiled["effect_coverage_digest"],
            "expires_at": value.expires_at,
        }
    )
    deployment = type(deployment).model_validate({**deployment.model_dump(), "effects": compiled["effects"]})
    plan = registration_plan(
        inputs=value,
        evidence=evidence,
        designation=all_in.f.designation,
        deployment=deployment,
        account_id="123456789012",
        region="us-east-1",
        registration_role_arn="arn:aws:iam::123456789012:role/offline-registrar",
    )
    restored = NativeRegistrationPlan.model_validate_json(plan.model_dump_json())
    assert type(restored.binding) is NativeAtlassianBinding
    assert json.loads(runtime_environment(restored)["ADP_NATIVE_TEST_TOOL_BINDING"])["schema_version"] == "5.0"
