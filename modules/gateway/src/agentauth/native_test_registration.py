"""Compile reviewed quantities and published tariffs into protected contracts.

This module is an operator-side compiler/registrar, never a gateway route. It
does not create a fixture user, credentials, consent, money or a native run.
Quantity evidence must bind the reviewed deployment release. Missing prices or
limits cannot be replaced by the $20 cap or remaining hierarchy headroom.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from src.agentauth.native_test_atlassian_runtime import NativeAtlassianBinding
from src.agentauth.native_test_configuration import CONFIGURATION_KIND, NativeTestAllInDeployment, deployment_key
from src.agentauth.native_test_effect_contract import EffectContractTerm, EffectCostContract
from src.agentauth.native_test_effect_quotes import COMPONENTS, CostBound, amount, component_total
from src.agentauth.native_test_enrollment import Contract, Digest, EffectRegistration, FixtureDesignation, Name
from src.agentauth.native_test_facade_contract import FacadeEffectCostContract
from src.agentauth.native_test_stateless_runtime import NativeStatelessBinding, stateless_coverage
from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest


class PublishedTariff(Contract):
    sku: Name
    term_code: Name
    region: str = Field(min_length=1, max_length=128)
    geography_attribute: Literal["regionCode", "location", "fromRegionCode", "fromLocation"] = "regionCode"
    unit: str = Field(min_length=1, max_length=128)
    usd_per_unit: str
    source_sha256: Digest
    rate_code: str = Field(min_length=1, max_length=255)
    effective_date: str = Field(min_length=1, max_length=64)


def aws_offer_tariff(raw, *, expected_sha256, sku, term_code, rate_code, region, geography_attribute="regionCode"):
    """Select an exact published USD rate from retained AWS offer bytes."""
    import json

    if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise AdmissionRefusedError("native_test_tariff_source_changed")
    try:
        offer = json.loads(raw)
        offer = offer.get("response", offer)
        if "PriceList" in offer:
            if offer.get("NextToken"):
                raise ValueError
            offer = {"products": [json.loads(entry) if isinstance(entry, str) else entry for entry in offer["PriceList"]], "truncated": False}
        if isinstance(offer.get("products"), list):
            matches = [entry for entry in offer["products"] if entry.get("product", {}).get("sku") == sku]
            if len(matches) != 1 or offer.get("truncated") is not False:
                raise ValueError
            product = matches[0]["product"]
            term = matches[0]["terms"]["OnDemand"][term_code]
        else:
            product = offer["products"][sku]
            term = offer["terms"]["OnDemand"][sku][term_code]
        rate = term["priceDimensions"][rate_code]
        value = rate["pricePerUnit"]["USD"]
        if (
            geography_attribute not in {"regionCode", "location", "fromRegionCode", "fromLocation"}
            or product["attributes"][geography_attribute] != region
            or rate["rateCode"] != rate_code
            or amount(value) <= 0
            or amount(value)
            < max(amount(dimension["pricePerUnit"]["USD"]) for dimension in term["priceDimensions"].values() if dimension["unit"] == rate["unit"])
        ):
            raise ValueError
        return PublishedTariff(
            sku=sku,
            term_code=term_code,
            region=region,
            geography_attribute=geography_attribute,
            unit=rate["unit"],
            usd_per_unit=value,
            source_sha256=expected_sha256,
            rate_code=rate_code,
            effective_date=term["effectiveDate"],
        )
    except (KeyError, ValueError, TypeError):
        raise AdmissionRefusedError("native_test_tariff_unavailable") from None


class PricedQuantity(Contract):
    component: str
    quantity: str
    tariff: PublishedTariff
    # Digest of the exact retained, reviewed bound proof. This must not be the
    # tariff digest: price evidence alone cannot establish a maximum quantity.
    enforced_limit_sha256: Digest


class EnforcedQuantityProof(Contract):
    schema_version: Literal["1.0"]
    release_sha256: Digest
    action: Literal["setup", "list", "call"]
    component: str
    unit: str
    maximum_quantity: str
    basis: Literal["source_enforced_maximum", "native_window_allocation", "unreachable_in_stateless_scope"]
    explanation: str = Field(min_length=1, max_length=4096)
    source_sha256: list[Digest] = Field(min_length=1, max_length=64)


class StatelessQuoteInputs(Contract):
    schema_version: Literal["1.0"]
    binding: NativeStatelessBinding
    # No query default. The approving owner selects the exact fixture payload.
    query: str = Field(min_length=1, max_length=200)
    not_before: int = Field(gt=0)
    expires_at: int = Field(gt=0)
    # Every component, including a justified zero, is explicit for each effect.
    setup: list[PricedQuantity] = Field(min_length=1, max_length=128)
    listing: list[PricedQuantity] = Field(min_length=1, max_length=128)
    search: list[PricedQuantity] = Field(min_length=1, max_length=128)


def _terms(rows, evidence, *, action, binding):
    terms = []
    if {row.component for row in rows} != COMPONENTS:
        raise AdmissionRefusedError("native_test_quote_components_incomplete")
    for row in rows:
        if row.enforced_limit_sha256 == row.tariff.source_sha256:
            raise AdmissionRefusedError("native_test_quantity_evidence_unavailable")
        for proof in (row.enforced_limit_sha256, row.tariff.source_sha256):
            raw = evidence.get(proof)
            if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != proof:
                raise AdmissionRefusedError("native_test_quote_evidence_unavailable")
        selected = aws_offer_tariff(
            evidence[row.tariff.source_sha256],
            expected_sha256=row.tariff.source_sha256,
            sku=row.tariff.sku,
            term_code=row.tariff.term_code,
            rate_code=row.tariff.rate_code,
            region=row.tariff.region,
            geography_attribute=row.tariff.geography_attribute,
        )
        if selected != row.tariff:
            raise AdmissionRefusedError("native_test_tariff_selection_changed")
        proof = EnforcedQuantityProof.model_validate_json(evidence[row.enforced_limit_sha256])
        if (
            proof.release_sha256 != binding.release_sha256
            or proof.action != action
            or proof.component != row.component
            or proof.unit != row.tariff.unit
            or amount(proof.maximum_quantity) != amount(row.quantity)
            or (proof.basis == "unreachable_in_stateless_scope" and amount(row.quantity) != 0)
            or any(
                type(evidence.get(source)) is not bytes or hashlib.sha256(evidence[source]).hexdigest() != source for source in proof.source_sha256
            )
        ):
            raise AdmissionRefusedError("native_test_quantity_binding_changed")
        term = CostBound(
            component=row.component,
            unit=row.tariff.unit,
            quantity=row.quantity,
            usd_per_unit=row.tariff.usd_per_unit,
            pricing_evidence_sha256=row.tariff.source_sha256,
            enforced_limit_sha256=row.enforced_limit_sha256,
        )
        terms.append(EffectContractTerm(**term.__dict__))
    return terms


def compile_stateless_contracts(inputs, *, evidence):
    if type(inputs) is not StatelessQuoteInputs or not inputs.query.strip() or inputs.expires_at <= inputs.not_before:
        raise AdmissionRefusedError("native_test_quote_input_refused")
    contracts, registrations, totals = {}, [], {}
    bodies = {
        "setup": digest({"effect_key": "native-effect:stateless-setup", "phase": "test_setup"}),
        "list": digest({"request_binding": "facade_list_v1", "max_pages": 1}),
        "call": digest(
            {
                "request_binding": "facade_call_v1",
                "tool": "websearch___search",
                "arguments": {"query": inputs.query},
                "execution_binding": "new",
                "ttl_seconds": None,
            }
        ),
    }
    for action, rows in (("setup", inputs.setup), ("list", inputs.listing), ("call", inputs.search)):
        effect_key = "native-effect:stateless-" + ("setup" if action == "setup" else action)
        terms = _terms(rows, evidence, action=action, binding=inputs.binding)
        values = {
            "schema_version": "1.0",
            "state": "active",
            "scope": "model_tool_setup_cleanup",
            "effect_key": effect_key,
            "request_sha256": bodies[action],
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
        if action == "setup":
            contract = EffectCostContract(**values)
        else:
            contract = FacadeEffectCostContract(
                **values, request_binding="facade_call_v1", max_call_bytes=65536, max_response_bytes=262144, transport_attempts=1, claim_attempts=1
            )
        total = component_total(tuple(CostBound(**term.model_dump()) for term in terms))
        if total <= 0:
            raise AdmissionRefusedError("native_test_quote_cost_unavailable")
        contract_sha256 = digest(contract.model_dump())
        contract_id = "stateless-" + action + "-" + contract_sha256[:24]
        contracts[contract_id], totals[action] = contract.model_dump(), str(total)
        registrations.append(
            EffectRegistration(
                effect_key=effect_key,
                action=action,
                tool="websearch.search",
                contract_id=contract_id,
                contract_sha256=contract_sha256,
                source_sha256=inputs.binding.release_sha256,
            )
        )
    complete_bound = Decimal(totals["setup"]) + Decimal(totals["call"]) + 2 * Decimal(totals["list"])
    if complete_bound > 20:
        raise AdmissionRefusedError("native_test_complete_contract_exceeds_cap")
    return {
        "contracts": contracts,
        "effects": [registration.model_dump() for registration in registrations],
        "effect_coverage_digest": stateless_coverage(inputs.binding),
        "maximum_original_smoke_usd": str(complete_bound),
        "effect_totals_usd": totals,
        "query_sha256": hashlib.sha256(inputs.query.encode()).hexdigest(),
        "evidence_sha256": sorted(evidence),
    }


class NativeRegistrationPlan(Contract):
    schema_version: Literal["2.0"]
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: Name
    registration_role_arn: str = Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    table: Name
    binding: NativeStatelessBinding | NativeAtlassianBinding
    designation: FixtureDesignation
    deployment: NativeTestAllInDeployment
    contracts: dict[str, dict]
    maximum_original_smoke_usd: str


def runtime_environment(plan):
    """Exact protected ConfigMap overlay, applied with the reviewed image."""
    return {
        "ADP_NATIVE_TEST_AUTHORITY_TABLE": plan.binding.authority_table,
        "ADP_NATIVE_TEST_DEPLOYMENT_ID": plan.binding.deployment_id,
        "ADP_NATIVE_TEST_TOOL_BINDING": plan.binding.model_dump_json(),
        "ADP_TOOLS_FACADE_ROLE_ARN": plan.binding.facade_role_arn,
        "ADP_TOOLS_RECONCILER_ROLE_ARN": plan.binding.reconciler_role_arn,
        "ADP_TOOLS_GATEWAY_ROLE_ARN": plan.binding.catalogue.gateway_role_arn,
    }


def registration_plan(*, inputs, evidence, designation, deployment, account_id, region, registration_role_arn):
    from src.agentauth.native_test_atlassian_registration import AtlassianQuoteInputs, compile_atlassian_contracts

    atlassian = type(inputs) is AtlassianQuoteInputs
    compiled = (compile_atlassian_contracts if atlassian else compile_stateless_contracts)(inputs, evidence=evidence)
    tools = sorted({call.tool.replace("___", ".", 1) for call in inputs.calls}) if atlassian else ["websearch.search"]
    if (
        type(designation) is not FixtureDesignation
        or type(deployment) is not NativeTestAllInDeployment
        or designation.deployment_id != inputs.binding.deployment_id
        or designation.tenant_id != deployment.tenant_id
        or designation.state != "active"
        or deployment.deployment_id != inputs.binding.deployment_id
        or deployment.state != "active"
        or (not atlassian and (deployment.allowed_models or deployment.allowed_routes))
        or (
            atlassian
            and (
                len(deployment.allowed_models) > 2
                or len(deployment.allowed_routes) > 2
                or bool(deployment.allowed_models) != bool(deployment.allowed_routes)
            )
        )
        or sorted(deployment.allowed_tools) != tools
        or deployment.expires_at > inputs.expires_at
        or deployment.effect_coverage_digest != compiled["effect_coverage_digest"]
        or inputs.binding.catalogue.account_id != account_id
        or inputs.binding.catalogue.region != region
        or [effect.model_dump() for effect in deployment.effects] != compiled["effects"]
        or any(arn.split(":")[4] != account_id for arn in (registration_role_arn, inputs.binding.facade_role_arn, inputs.binding.reconciler_role_arn))
        or registration_role_arn in (inputs.binding.facade_role_arn, inputs.binding.reconciler_role_arn)
    ):
        raise AdmissionRefusedError("native_test_registration_binding_refused")
    return NativeRegistrationPlan(
        schema_version="2.0",
        account_id=account_id,
        region=region,
        registration_role_arn=registration_role_arn,
        table=inputs.binding.authority_table,
        binding=inputs.binding,
        designation=designation,
        deployment=deployment,
        contracts=compiled["contracts"],
        maximum_original_smoke_usd=compiled["maximum_original_smoke_usd"],
    )


def register_initial_plan(plan, *, expected_plan_sha256, store, sts_client, now=None):
    """One explicit operator call; initial conditional writes, never replacement.

    The caller supplies one-attempt clients and the already reviewed plan digest.
    Lost acknowledgement is reconciled read-only by the operator, never retried.
    The protected table policy remains the authority for allowed writers.
    """
    from src.agentauth.native_test_enrollment import NativeTestStore
    from src.auth.agent_registry import parse_assumed_role_arn

    now = now or datetime.now(UTC)
    if (
        type(plan) is not NativeRegistrationPlan
        or digest(plan.model_dump()) != expected_plan_sha256
        or type(store) is not NativeTestStore
        or store.table != plan.table
        or plan.deployment.expires_at <= now.timestamp()
        or plan.table != plan.binding.authority_table
    ):
        raise AdmissionRefusedError("native_test_registration_plan_refused")
    for client in (store.client, sts_client):
        if client.meta.config.retries.get("total_max_attempts") != 1:
            raise AdmissionRefusedError("native_test_registration_retry_refused")
    identity = sts_client.get_caller_identity()
    if identity.get("Account") != plan.account_id or parse_assumed_role_arn(identity.get("Arn", "")) != plan.registration_role_arn:
        raise AdmissionRefusedError("native_test_registration_role_refused")
    writes = [
        store.put(store.fixture_key(plan.designation.subject_id), "native_test_fixture_v1", plan.designation.model_dump()),
        store.put(deployment_key(plan.binding.deployment_id), CONFIGURATION_KIND, plan.deployment.model_dump()),
    ]
    for contract_id, document in plan.contracts.items():
        writes.append(
            store.put(
                {"pk": {"S": "TOOLS_TEST_DEPLOYMENT#" + plan.binding.deployment_id}, "sk": {"S": "EFFECT_CONTRACT#" + contract_id}},
                "native_test_effect_contract_v1",
                document,
            )
        )
    store.transaction(writes)
    # This is registration acknowledgement, never user approval or paid smoke.
    return {"schema_version": "1.0", "plan_sha256": expected_plan_sha256, "registered": True, "governance_complete": False}
