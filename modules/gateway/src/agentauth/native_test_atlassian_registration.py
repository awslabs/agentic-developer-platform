"""Compile exact Atlassian test operations from retained price/quantity proof."""

from decimal import Decimal
from typing import Literal

from pydantic import Field

from src.agentauth.native_test_atlassian_runtime import NativeAtlassianBinding, atlassian_coverage
from src.agentauth.native_test_effect_contract import EffectCostContract
from src.agentauth.native_test_effect_quotes import CostBound, component_total
from src.agentauth.native_test_enrollment import Contract, EffectRegistration
from src.agentauth.native_test_facade_contract import FacadeEffectCostContract, NativeFacadeCall
from src.agentauth.native_test_registration import PricedQuantity, _terms
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical, digest


class AtlassianPricedCall(Contract):
    tool: str = Field(pattern=r"^atlassian___[a-z_]+$")
    arguments: dict
    quantities: list[PricedQuantity] = Field(min_length=1, max_length=128)


class AtlassianQuoteInputs(Contract):
    schema_version: Literal["2.0"]
    binding: NativeAtlassianBinding
    not_before: int = Field(gt=0)
    expires_at: int = Field(gt=0)
    setup: list[PricedQuantity] = Field(min_length=1, max_length=128)
    listing: list[PricedQuantity] = Field(min_length=1, max_length=128)
    calls: list[AtlassianPricedCall] = Field(min_length=1, max_length=16)


def compile_atlassian_contracts(inputs, *, evidence):
    if type(inputs) is not AtlassianQuoteInputs or inputs.expires_at <= inputs.not_before:
        raise AdmissionRefusedError("native_test_quote_input_refused")
    records = []
    for call in inputs.calls:
        parsed = NativeFacadeCall.parse(
            canonical(
                {
                    "schema_version": "1.0",
                    "action": "call",
                    "tool": call.tool,
                    "arguments": call.arguments,
                    "request_id": "00000000-0000-4000-8000-000000000000",
                    "execution_id": None,
                    "ttl_seconds": None,
                }
            ).encode()
        )
        request_sha256 = parsed.approved_digest()
        records.append(("call", "atlassian-call-" + request_sha256[:24], call.tool.replace("___", ".", 1), request_sha256, call.quantities))
    if len({record[3] for record in records}) != len(records):
        raise AdmissionRefusedError("native_test_duplicate_call_contract")
    setup_key = "native-effect:atlassian-setup"
    first_tool = records[0][2]
    records = [
        ("setup", "atlassian-setup", first_tool, digest({"effect_key": setup_key, "phase": "test_setup"}), inputs.setup),
        ("list", "atlassian-list", first_tool, digest({"request_binding": "facade_list_v1", "max_pages": 1}), inputs.listing),
        *records,
    ]
    contracts, registrations, totals = {}, [], {}
    for action, name, tool, request_sha256, rows in records:
        terms = _terms(rows, evidence, action=action, binding=inputs.binding)
        values = {
            "schema_version": "1.0",
            "state": "active",
            "scope": "model_tool_setup_cleanup",
            "effect_key": "native-effect:" + name,
            "request_sha256": request_sha256,
            "source_sha256": inputs.binding.release_sha256,
            "pricing_revision": digest([row.tariff.model_dump() for row in rows]),
            "not_before": inputs.not_before,
            "expires_at": inputs.expires_at,
            "quote_seconds": 60,
            "session_seconds": 300,
            "cleanup_seconds": 600,
            "cleanup_attempts": 2,
            "components": terms,
        }
        contract = (
            EffectCostContract(**values)
            if action == "setup"
            else FacadeEffectCostContract(
                **values,
                request_binding="facade_call_v1",
                max_call_bytes=65536,
                max_response_bytes=262144,
                transport_attempts=1,
                claim_attempts=1,
            )
        )
        total = component_total(tuple(CostBound(**term.model_dump()) for term in terms))
        if total <= 0:
            raise AdmissionRefusedError("native_test_quote_cost_unavailable")
        contract_sha256 = digest(contract.model_dump())
        contract_id = name + "-" + contract_sha256[:24]
        contracts[contract_id], totals[name] = contract.model_dump(), str(total)
        registrations.append(
            EffectRegistration(
                effect_key=values["effect_key"],
                action=action,
                tool=tool,
                contract_id=contract_id,
                contract_sha256=contract_sha256,
                source_sha256=inputs.binding.release_sha256,
            )
        )
    complete = sum((Decimal(value) for value in totals.values()), Decimal(0))
    if complete > 20:
        raise AdmissionRefusedError("native_test_complete_contract_exceeds_cap")
    return {
        "contracts": contracts,
        "effects": [registration.model_dump() for registration in registrations],
        "effect_coverage_digest": atlassian_coverage(inputs.binding),
        "maximum_original_smoke_usd": str(complete),
        "effect_totals_usd": totals,
        "evidence_sha256": sorted(evidence),
    }
