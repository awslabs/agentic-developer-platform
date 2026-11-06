"""Offline quote compiler checks with explicitly substituted price/source bytes."""

import hashlib
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.agentauth.native_test_effect_contract import EffectCostContract
from src.agentauth.native_test_effect_quotes import COMPONENTS
from src.agentauth.native_test_facade_contract import FacadeEffectQuoteAdapter, InterpreterStepCostContract, NativeFacadeCall
from src.agentauth.native_test_interpreter_registration import (
    INTERPRETER_STEPS,
    InterpreterQuantityProof,
    InterpreterQuoteInputs,
    compile_interpreter_contracts,
)
from src.agentauth.native_test_interpreter_runtime import NativeInterpreterBinding
from src.agentauth.native_test_registration import PricedQuantity, aws_offer_tariff
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical, digest


def retain(evidence, value):
    raw = value if isinstance(value, bytes) else canonical(value).encode()
    key = hashlib.sha256(raw).hexdigest()
    evidence[key] = raw
    return key


@pytest.fixture
def quote_inputs():
    evidence = {}
    source = retain(evidence, b"Explicit offline source substitution; not deployment evidence")
    config = retain(evidence, b"Explicit offline selected configuration substitution")
    binding = NativeInterpreterBinding(
        schema_version="4.0",
        deployment_id="offline-interpreter",
        release_sha256=source,
        source_revision="1" * 40,
        gateway_origin="https://offline.invalid",
        authority_table="offline-authority",
        execution_table="offline-executions",
        facade_role_arn="arn:aws:iam::123456789012:role/offline-facade",
        reconciler_role_arn="arn:aws:iam::123456789012:role/offline-reconciler",
        catalogue={
            "gateway_arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/offline-abcdefghij",
            "gateway_role_arn": "arn:aws:iam::123456789012:role/offline-gateway",
            "target_id": "ABCDEFGHIJ",
            "lambda_arn": "arn:aws:lambda:us-east-1:123456789012:function:offline-wrapper",
            "target_configuration_sha256": config,
        },
        execute_code='print("offline")\n',
        file_path="/tmp/offline.txt",
    )
    price_source = retain(
        evidence,
        {
            "products": {"offline": {"attributes": {"regionCode": "us-east-1"}}},
            "terms": {
                "OnDemand": {
                    "offline": {
                        "offline.term": {
                            "effectiveDate": "2026-01-01T00:00:00Z",
                            "priceDimensions": {
                                "offline.rate": {"unit": "offline-unit", "rateCode": "offline.rate", "pricePerUnit": {"USD": "0.01"}}
                            },
                        }
                    }
                }
            },
        },
    )
    tariff = aws_offer_tariff(
        evidence[price_source], expected_sha256=price_source, sku="offline", term_code="offline.term", rate_code="offline.rate", region="us-east-1"
    )
    quantities = {}
    for step in INTERPRETER_STEPS:
        rows = []
        for component in sorted(COMPONENTS):
            values = {"maximum_quantity": "0", "basis": "unreachable_in_interpreter_scope"}
            if step == "setup" and component == "shared_allocation":
                values.update(maximum_quantity="1", basis="native_window_allocation", allocation_window_seconds=600, configuration_sha256=[config])
            elif step != "setup" and component == "provider_requests":
                values.update(maximum_quantity="1", basis="source_enforced_maximum")
            elif step == "start" and component in {"provider_compute", "cleanup"}:
                values.update(maximum_quantity="1", basis="provider_contract_maximum")
            elif step != "setup" and component == "shared_allocation":
                values.update(basis="covered_by_setup", funded_components=[component])
            elif step not in {"setup", "list", "start"} and component in {"provider_compute", "cleanup"}:
                values.update(basis="covered_by_start", funded_components=[component])
            proof = InterpreterQuantityProof(
                schema_version="1.0",
                release_sha256=source,
                step=step,
                component=component,
                unit=tariff.unit,
                explanation="Explicit offline proof, not a production quantity",
                source_sha256=[source],
                **values,
            )
            rows.append(
                PricedQuantity(
                    component=component, quantity=proof.maximum_quantity, tariff=tariff, enforced_limit_sha256=retain(evidence, proof.model_dump())
                )
            )
        quantities["listing" if step == "list" else step] = rows
    return InterpreterQuoteInputs(schema_version="1.0", binding=binding, not_before=1000, expires_at=2000, **quantities), evidence


def change_proof(inputs, evidence, selected_step, selected_component, **changes):
    rows = getattr(inputs, "listing" if selected_step == "list" else selected_step)
    index = next(i for i, row in enumerate(rows) if row.component == selected_component)
    original = rows[index]
    proof = json.loads(evidence[original.enforced_limit_sha256])
    proof.update(changes)
    rows[index] = original.model_copy(
        update={
            "quantity": proof["maximum_quantity"],
            "enforced_limit_sha256": retain(evidence, proof),
        }
    )


def test_compiles_seven_original_effects_once_and_keeps_allocations_distinct(quote_inputs):
    inputs, evidence = quote_inputs
    result = compile_interpreter_contracts(inputs, evidence=evidence)
    assert result["maximum_original_smoke_usd"] == "0.090000"
    assert list(result["effect_totals_usd"]) == list(INTERPRETER_STEPS)
    assert result["effect_totals_usd"]["start"] == "0.030000"
    assert len(result["contracts"]) == len(result["effects"]) == 7
    assert result["registered"] is result["deployment_attested"] is result["observed_usage"] is False
    assert result["binding_sha256"] == digest(inputs.binding.model_dump())
    assert "native_window_allocation" in result["quantity_bases"]["setup"]
    assert "covered_by_start" in result["quantity_bases"]["file"]
    for registration in result["effects"]:
        contract = result["contracts"][registration["contract_id"]]
        assert registration["contract_sha256"] == digest(contract)
        assert {row["component"] for row in contract["components"]} == COMPONENTS
        assert (contract["session_seconds"], contract["cleanup_seconds"], contract["cleanup_attempts"]) == (300, 600, 2)
    assert [(e["action"], e["tool"]) for e in result["effects"]] == [
        ("setup", "code_interpreter.start"),
        ("list", "code_interpreter.start"),
        ("call", "code_interpreter.start"),
        ("call", "code_interpreter.execute"),
        ("recover", "code_interpreter.execute"),
        ("call", "code_interpreter.result"),
        ("call", "code_interpreter.file"),
    ]


@pytest.mark.parametrize("step", INTERPRETER_STEPS[1:])
def test_compiled_contract_uses_actual_facade_payload_normalization(quote_inputs, step):
    inputs, evidence = quote_inputs
    compiled = compile_interpreter_contracts(inputs, evidence=evidence)
    registration = next(row for row in compiled["effects"] if row["effect_key"] == "native-effect:interpreter-" + step)
    contract = InterpreterStepCostContract.model_validate(compiled["contracts"][registration["contract_id"]])
    tool = "execute" if step == "execute_status" else step
    arguments = {}
    if step not in {"list", "start"}:
        arguments["session_id"] = "a" * 64
    if step in {"execute", "execute_status"}:
        arguments.update(code=inputs.binding.execute_code, language="python")
    elif step == "result":
        arguments["provider_id"] = "owned-task"
    elif step == "file":
        arguments["path"] = inputs.binding.file_path
    body = canonical(
        {
            "schema_version": "1.0",
            "action": "list" if step == "list" else "recover" if step == "execute_status" else "call",
            "tool": None if step == "list" else "code_interpreter___" + tool,
            "arguments": arguments,
            "request_id": None if step == "list" else "11111111-1111-4111-8111-111111111111",
            "execution_id": None if step in {"list", "start"} else "22222222-2222-4222-8222-222222222222",
            "ttl_seconds": 300 if step == "start" else None,
        }
    ).encode()
    assert NativeFacadeCall.parse(body).approved_digest() == contract.request_sha256
    adapter = object.__new__(FacadeEffectQuoteAdapter)
    assert adapter._request_permitted(body, contract)
    if step in {"execute", "execute_status", "file"}:
        changed = json.loads(body)
        changed["arguments"]["path" if step == "file" else "code"] += "changed"
        assert not adapter._request_permitted(canonical(changed).encode(), contract)


@pytest.mark.parametrize(
    "fault", ["price_bytes", "proof_bytes", "source_bytes", "configuration_bytes", "price", "quantity", "release", "missing_component", "duplicate"]
)
def test_changed_or_incomplete_evidence_never_compiles(quote_inputs, fault):
    inputs, evidence = quote_inputs
    row = inputs.start[0]
    if fault == "price_bytes":
        evidence[row.tariff.source_sha256] = b"changed"
    elif fault == "proof_bytes":
        evidence[row.enforced_limit_sha256] = b"changed"
    elif fault in {"source_bytes", "configuration_bytes"}:
        key = inputs.binding.release_sha256 if fault == "source_bytes" else inputs.binding.catalogue.target_configuration_sha256
        evidence[key] = b"changed"
    elif fault == "price":
        inputs.start[0] = row.model_copy(update={"tariff": row.tariff.model_copy(update={"usd_per_unit": "0.0001"})})
    elif fault == "quantity":
        inputs.start[0] = row.model_copy(update={"quantity": "999"})
    elif fault == "release":
        inputs = inputs.model_copy(update={"binding": inputs.binding.model_copy(update={"release_sha256": "e" * 64})})
    elif fault == "missing_component":
        inputs.start.pop()
    else:
        inputs.start.append(row)
    with pytest.raises((AdmissionRefusedError, ValidationError)):
        compile_interpreter_contracts(inputs, evidence=evidence)


@pytest.mark.parametrize(
    "change",
    [
        {"step": "execute"},
        {"unit": "wrong-unit"},
        {"maximum_quantity": "NaN"},
        {"basis": "unreachable_in_stateless_scope"},
        {"basis": "unreachable_in_interpreter_scope", "maximum_quantity": "1"},
    ],
)
def test_interpreter_proof_is_strict_and_cannot_borrow_stateless_zero(quote_inputs, change):
    inputs, evidence = quote_inputs
    change_proof(inputs, evidence, "start", "network", **change)
    with pytest.raises((AdmissionRefusedError, ValidationError)):
        compile_interpreter_contracts(inputs, evidence=evidence)


@pytest.mark.parametrize(
    "changes",
    [
        {"allocation_window_seconds": None},
        {"allocation_window_seconds": 601},
        {"configuration_sha256": []},
        {"funded_components": ["shared_allocation"]},
    ],
)
def test_window_allocation_requires_original_window_and_selected_configuration(quote_inputs, changes):
    inputs, evidence = quote_inputs
    change_proof(inputs, evidence, "setup", "shared_allocation", **changes)
    with pytest.raises((AdmissionRefusedError, ValidationError)):
        compile_interpreter_contracts(inputs, evidence=evidence)


@pytest.mark.parametrize(
    "step,component,changes",
    [
        ("start", "provider_compute", {"maximum_quantity": "0"}),
        ("start", "cleanup", {"maximum_quantity": "0"}),
        ("setup", "shared_allocation", {"maximum_quantity": "0"}),
        ("file", "provider_compute", {"funded_components": ["model_setup"]}),
        ("file", "provider_compute", {"funded_components": []}),
        ("file", "provider_compute", {"maximum_quantity": "1"}),
        ("setup", "runner", {"basis": "covered_by_setup", "funded_components": ["shared_allocation"]}),
        ("list", "provider_compute", {"basis": "covered_by_start", "funded_components": ["provider_compute"]}),
    ],
)
def test_lifetime_cleanup_and_zero_incremental_terms_need_original_funding(quote_inputs, step, component, changes):
    inputs, evidence = quote_inputs
    change_proof(inputs, evidence, step, component, **changes)
    with pytest.raises(AdmissionRefusedError):
        compile_interpreter_contracts(inputs, evidence=evidence)


@pytest.mark.parametrize("component", ["model_setup", "sqs"])
def test_operator_interpreter_does_not_admit_model_or_browser_queue_work(quote_inputs, component):
    inputs, evidence = quote_inputs
    change_proof(inputs, evidence, "execute", component, maximum_quantity="1", basis="source_enforced_maximum")
    with pytest.raises(AdmissionRefusedError, match="scope_refused"):
        compile_interpreter_contracts(inputs, evidence=evidence)


def test_cap_applies_to_sum_of_all_seven_effects_with_per_term_rounding(quote_inputs):
    inputs, evidence = quote_inputs
    change_proof(inputs, evidence, "setup", "shared_allocation", maximum_quantity="1992")
    assert Decimal(compile_interpreter_contracts(inputs, evidence=evidence)["maximum_original_smoke_usd"]) == 20
    change_proof(inputs, evidence, "setup", "shared_allocation", maximum_quantity="1992.0001")
    with pytest.raises(AdmissionRefusedError, match="exceeds_cap"):
        compile_interpreter_contracts(inputs, evidence=evidence)


def test_setup_digest_and_result_are_deterministic_without_attestation(quote_inputs):
    inputs, evidence = quote_inputs
    first = compile_interpreter_contracts(inputs, evidence=evidence)
    assert first == compile_interpreter_contracts(inputs, evidence=dict(reversed(list(evidence.items()))))
    registration = first["effects"][0]
    setup = EffectCostContract.model_validate(first["contracts"][registration["contract_id"]])
    assert setup.request_sha256 == digest({"effect_key": registration["effect_key"], "phase": "test_setup"})
    assert first["evidence_sha256"] == sorted(evidence)
