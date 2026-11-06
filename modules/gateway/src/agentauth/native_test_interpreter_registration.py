"""Offline cost-contract compiler for one original Interpreter journey.

This consumes reviewed quantities and retained official prices. It cannot attest
a deployment, grant operator consent, register authority, or invoke a provider.
Shared native-window allocations are explicit; they are not observed AWS usage.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Literal

from pydantic import Field

from src.agentauth.native_test_effect_contract import EffectContractTerm, EffectCostContract
from src.agentauth.native_test_effect_quotes import COMPONENTS, CostBound, amount, component_total
from src.agentauth.native_test_enrollment import Contract, Digest, EffectRegistration
from src.agentauth.native_test_facade_contract import InterpreterStepCostContract
from src.agentauth.native_test_interpreter_runtime import NativeInterpreterBinding, interpreter_coverage, interpreter_request_digests
from src.agentauth.native_test_registration import PricedQuantity, aws_offer_tariff
from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest

InterpreterStep = Literal["setup", "list", "start", "execute", "execute_status", "result", "file"]
INTERPRETER_STEPS = ("setup", "list", "start", "execute", "execute_status", "result", "file")
REGISTRATION_TOOLS = {
    "setup": "code_interpreter.start",
    "list": "code_interpreter.start",
    "start": "code_interpreter.start",
    "execute": "code_interpreter.execute",
    "execute_status": "code_interpreter.execute",
    "result": "code_interpreter.result",
    "file": "code_interpreter.file",
}


class InterpreterQuantityProof(Contract):
    """Reviewed quantity scope, independently bound from its price evidence."""

    schema_version: Literal["1.0"]
    release_sha256: Digest
    step: InterpreterStep
    component: str
    unit: str
    maximum_quantity: str
    basis: Literal[
        "source_enforced_maximum",
        "provider_contract_maximum",
        "native_window_allocation",
        "unreachable_in_interpreter_scope",
        "covered_by_setup",
        "covered_by_start",
    ]
    explanation: str = Field(min_length=1, max_length=4096)
    source_sha256: list[Digest] = Field(min_length=1, max_length=64)
    configuration_sha256: list[Digest] = Field(default_factory=list, max_length=64)
    allocation_window_seconds: Literal[600] | None = None
    # A zero incremental child charge must name the already-funded components.
    # Cleanup can be funded by several components in the original start quote.
    funded_components: list[str] = Field(default_factory=list, max_length=17)


class InterpreterQuoteInputs(Contract):
    schema_version: Literal["1.0"]
    binding: NativeInterpreterBinding
    not_before: int = Field(gt=0)
    expires_at: int = Field(gt=0)
    setup: list[PricedQuantity] = Field(min_length=17, max_length=128)
    listing: list[PricedQuantity] = Field(min_length=17, max_length=128)
    start: list[PricedQuantity] = Field(min_length=17, max_length=128)
    execute: list[PricedQuantity] = Field(min_length=17, max_length=128)
    execute_status: list[PricedQuantity] = Field(min_length=17, max_length=128)
    result: list[PricedQuantity] = Field(min_length=17, max_length=128)
    file: list[PricedQuantity] = Field(min_length=17, max_length=128)


def _retained(evidence, sha256):
    raw = evidence.get(sha256)
    if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != sha256:
        raise AdmissionRefusedError("native_test_quote_evidence_unavailable")
    return raw


def _proof(row, evidence, *, step, release_sha256):
    if row.enforced_limit_sha256 == row.tariff.source_sha256:
        raise AdmissionRefusedError("native_test_quantity_evidence_unavailable")
    proof = InterpreterQuantityProof.model_validate_json(_retained(evidence, row.enforced_limit_sha256))
    quantity = amount(row.quantity)
    if (
        proof.release_sha256 != release_sha256
        or proof.step != step
        or proof.component != row.component
        or proof.unit != row.tariff.unit
        or amount(proof.maximum_quantity) != quantity
        or any(component not in COMPONENTS for component in proof.funded_components)
        or len(set(proof.funded_components)) != len(proof.funded_components)
    ):
        raise AdmissionRefusedError("native_test_quantity_binding_changed")
    for source in proof.source_sha256 + proof.configuration_sha256:
        _retained(evidence, source)
    if proof.basis == "native_window_allocation":
        if step != "setup" or proof.allocation_window_seconds != 600 or not proof.configuration_sha256 or proof.funded_components:
            raise AdmissionRefusedError("native_test_window_allocation_refused")
    elif proof.allocation_window_seconds is not None:
        raise AdmissionRefusedError("native_test_window_allocation_refused")
    if proof.basis in {"covered_by_setup", "covered_by_start"}:
        owner = proof.basis.removeprefix("covered_by_")
        if quantity != 0 or not proof.funded_components or step == owner or step == "setup" or owner == "start" and step == "list":
            raise AdmissionRefusedError("native_test_quantity_funding_refused")
    elif proof.funded_components:
        raise AdmissionRefusedError("native_test_quantity_funding_refused")
    if proof.basis == "unreachable_in_interpreter_scope" and quantity != 0:
        raise AdmissionRefusedError("native_test_quantity_binding_changed")
    if row.component in {"model_setup", "sqs"} and (quantity != 0 or proof.basis != "unreachable_in_interpreter_scope"):
        raise AdmissionRefusedError("native_test_interpreter_scope_refused")
    return proof


def _terms(rows, evidence, *, step, binding):
    if {row.component for row in rows} != COMPONENTS:
        raise AdmissionRefusedError("native_test_quote_components_incomplete")
    terms, proofs, seen = [], [], set()
    for row in rows:
        key = (row.component, row.tariff.source_sha256, row.tariff.rate_code, row.enforced_limit_sha256)
        if key in seen:
            raise AdmissionRefusedError("native_test_duplicate_quantity_refused")
        seen.add(key)
        selected = aws_offer_tariff(
            _retained(evidence, row.tariff.source_sha256),
            expected_sha256=row.tariff.source_sha256,
            sku=row.tariff.sku,
            term_code=row.tariff.term_code,
            rate_code=row.tariff.rate_code,
            region=row.tariff.region,
            geography_attribute=row.tariff.geography_attribute,
        )
        if selected != row.tariff:
            raise AdmissionRefusedError("native_test_tariff_selection_changed")
        proofs.append(_proof(row, evidence, step=step, release_sha256=binding.release_sha256))
        terms.append(
            EffectContractTerm(
                **CostBound(
                    component=row.component,
                    unit=row.tariff.unit,
                    quantity=row.quantity,
                    usd_per_unit=row.tariff.usd_per_unit,
                    pricing_evidence_sha256=row.tariff.source_sha256,
                    enforced_limit_sha256=row.enforced_limit_sha256,
                ).__dict__
            )
        )
    return terms, proofs


def compile_interpreter_contracts(inputs, *, evidence):
    """Compile seven effects once, retaining root-funded lifetime and cleanup."""
    if type(inputs) is not InterpreterQuoteInputs or inputs.expires_at <= inputs.not_before:
        raise AdmissionRefusedError("native_test_quote_input_refused")
    # Revalidate nested frozen models too: model_copy/list mutation must not
    # bypass the selected binding or minimum component cardinality checks.
    inputs = InterpreterQuoteInputs.model_validate(inputs.model_dump())
    rows = {step: getattr(inputs, "listing" if step == "list" else step) for step in INTERPRETER_STEPS}
    terms, proofs = {}, {}
    for step, quantities in rows.items():
        terms[step], proofs[step] = _terms(quantities, evidence, step=step, binding=inputs.binding)
    costs = {
        step: {
            component: component_total(tuple(CostBound(**term.model_dump()) for term in values if term.component == component))
            for component in COMPONENTS
        }
        for step, values in terms.items()
    }
    if costs["start"]["provider_compute"] <= 0 or costs["start"]["cleanup"] <= 0:
        raise AdmissionRefusedError("native_test_original_lifetime_cleanup_unfunded")
    for step, values in proofs.items():
        for proof in values:
            if proof.basis in {"covered_by_setup", "covered_by_start"}:
                owner = proof.basis.removeprefix("covered_by_")
                if any(costs[owner][component] <= 0 for component in proof.funded_components):
                    raise AdmissionRefusedError("native_test_quantity_funding_refused")
    bodies = {
        "setup": digest({"effect_key": "native-effect:interpreter-setup", "phase": "test_setup"}),
        **interpreter_request_digests(inputs.binding),
    }
    contracts, registrations, totals = {}, [], {}
    for step in INTERPRETER_STEPS:
        effect_key = "native-effect:interpreter-" + step
        values = {
            "schema_version": "1.0",
            "state": "active",
            "scope": "model_tool_setup_cleanup",
            "effect_key": effect_key,
            "request_sha256": bodies[step],
            "source_sha256": inputs.binding.release_sha256,
            "pricing_revision": digest([row.tariff.model_dump() for row in rows[step]]),
            "not_before": inputs.not_before,
            "expires_at": inputs.expires_at,
            "quote_seconds": 60,
            "session_seconds": 300,
            "cleanup_seconds": 600,
            "cleanup_attempts": 2,
            "components": terms[step],
        }
        if step == "setup":
            contract = EffectCostContract(**values)
        else:
            contract = InterpreterStepCostContract(
                **values,
                request_binding="facade_call_v1",
                max_call_bytes=65536,
                max_response_bytes=262144,
                transport_attempts=1,
                claim_attempts=1,
                interpreter_step=step,
                max_step_requests=1,
            )
        total = sum(costs[step].values(), Decimal(0))
        if total <= 0:
            raise AdmissionRefusedError("native_test_quote_cost_unavailable")
        contract_sha256 = digest(contract.model_dump())
        identifier = "interpreter-" + step + "-" + contract_sha256[:24]
        contracts[identifier], totals[step] = contract.model_dump(), str(total)
        registrations.append(
            EffectRegistration(
                effect_key=effect_key,
                action=step if step in {"setup", "list"} else "recover" if step == "execute_status" else "call",
                tool=REGISTRATION_TOOLS[step],
                contract_id=identifier,
                contract_sha256=contract_sha256,
                source_sha256=inputs.binding.release_sha256,
            )
        )
    complete = sum((Decimal(value) for value in totals.values()), Decimal(0))
    if complete > 20:
        raise AdmissionRefusedError("native_test_complete_contract_exceeds_cap")
    return {
        "contracts": contracts,
        "effects": [row.model_dump() for row in registrations],
        "effect_coverage_digest": interpreter_coverage(inputs.binding),
        "maximum_original_smoke_usd": str(complete),
        "effect_totals_usd": totals,
        "binding_sha256": digest(inputs.binding.model_dump()),
        "execute_code_sha256": hashlib.sha256(inputs.binding.execute_code.encode()).hexdigest(),
        "file_path_sha256": hashlib.sha256(inputs.binding.file_path.encode()).hexdigest(),
        "quantity_bases": {step: sorted({proof.basis for proof in values}) for step, values in proofs.items()},
        "evidence_sha256": sorted(evidence),
        "observed_usage": False,
        "deployment_attested": False,
        "registered": False,
    }
