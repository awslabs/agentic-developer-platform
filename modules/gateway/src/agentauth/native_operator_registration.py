"""Offline operator Interpreter plan and explicit initial protected registration.

The registrar writes one deployment and its seven compiled cost contracts. It
never writes a fixture designation, user, consent, approval, capability or run.
Source/deployment attestation and review of the exact plan digest remain with
the operator; successful registration is not authorization for a paid effect.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import Field, ValidationError, model_validator

from src.agentauth.native_test_configuration import CONFIGURATION_KIND, NativeOperatorDeployment, deployment_key
from src.agentauth.native_test_effect_contract import EffectCostContract
from src.agentauth.native_test_effect_quotes import COMPONENTS, CostBound, amount, component_total, sha256
from src.agentauth.native_test_enrollment import Contract, NativeTestStore
from src.agentauth.native_test_facade_contract import InterpreterStepCostContract
from src.agentauth.native_test_interpreter_registration import INTERPRETER_STEPS, REGISTRATION_TOOLS, compile_interpreter_contracts
from src.agentauth.native_test_interpreter_runtime import NativeInterpreterBinding, interpreter_coverage, interpreter_request_digests
from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest
from src.auth.agent_registry import parse_assumed_role_arn


class NativeOperatorRegistrationPlan(Contract):
    schema_version: Literal["1.0"]
    producer: Literal["operator_local"]
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: str = Field(pattern=r"^[a-z0-9-]{1,20}$")
    registration_role_arn: str = Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    table: str = Field(pattern=r"^[A-Za-z0-9_.-]{3,255}$")
    binding: NativeInterpreterBinding
    deployment: NativeOperatorDeployment
    contracts: dict[str, dict] = Field(min_length=7, max_length=7)
    maximum_original_smoke_usd: str

    @model_validator(mode="after")
    def exact_operator_contract(self):
        binding, deployment = self.binding, self.deployment
        runtime_roles = (binding.facade_role_arn, binding.reconciler_role_arn, binding.catalogue.gateway_role_arn)
        if (
            self.table != binding.authority_table
            or binding.catalogue.account_id != self.account_id
            or binding.catalogue.region != self.region
            or any(arn.split(":")[4] != self.account_id for arn in (self.registration_role_arn, *runtime_roles))
            or self.registration_role_arn in runtime_roles
            or deployment.state != "active"
            or deployment.deployment_id != binding.deployment_id
            or deployment.release_sha256 != binding.release_sha256
            or deployment.source_revision != binding.source_revision
            or deployment.gateway_origin != binding.gateway_origin
            or deployment.contract_sha256 != digest(binding.model_dump())
            or deployment.effect_coverage_digest != interpreter_coverage(binding)
        ):
            raise ValueError("operator registration binding changed")
        requests = {
            "setup": digest({"effect_key": "native-effect:interpreter-setup", "phase": "test_setup"}),
            **interpreter_request_digests(binding),
        }
        if set(self.contracts) != {effect.contract_id for effect in deployment.effects}:
            raise ValueError("exact seven operator contracts required")
        total, horizons = Decimal(0), set()
        for step, effect in zip(INTERPRETER_STEPS, deployment.effects, strict=True):
            document = self.contracts[effect.contract_id]
            contract_type = EffectCostContract if step == "setup" else InterpreterStepCostContract
            contract = contract_type.model_validate(document)
            contract_hash = digest(document)
            terms = tuple(CostBound(**term.model_dump()) for term in contract.components)
            cost = component_total(terms)
            if (
                contract.model_dump() != document
                or effect.effect_key != "native-effect:interpreter-" + step
                or effect.action != (step if step in {"setup", "list"} else "recover" if step == "execute_status" else "call")
                or effect.tool != REGISTRATION_TOOLS[step]
                or effect.contract_id != "interpreter-" + step + "-" + contract_hash[:24]
                or effect.contract_sha256 != contract_hash
                or effect.source_sha256 != binding.release_sha256
                or contract.effect_key != effect.effect_key
                or contract.source_sha256 != binding.release_sha256
                or contract.request_sha256 != requests[step]
                or not sha256(contract.pricing_revision)
                or contract.not_before >= contract.expires_at
                or deployment.expires_at > contract.expires_at
                or contract.not_before >= deployment.expires_at
                or (contract.quote_seconds, contract.session_seconds, contract.cleanup_seconds, contract.cleanup_attempts) != (60, 300, 600, 2)
                or step != "setup"
                and contract.interpreter_step != step
                or {term.component for term in terms} != COMPONENTS
                or any(term.component in {"model_setup", "sqs"} and amount(term.quantity) != 0 for term in terms)
                or step == "start"
                and any(
                    component_total(tuple(term for term in terms if term.component == component)) <= 0
                    for component in ("provider_compute", "cleanup")
                )
                or cost <= 0
            ):
                raise ValueError("compiled operator effect changed")
            horizons.add((contract.not_before, contract.expires_at))
            total += cost
        if len(horizons) != 1 or total > 20 or amount(self.maximum_original_smoke_usd) != total:
            raise ValueError("complete original operator quote required")
        return self


def _validated_plan(plan):
    try:
        if type(plan) is not NativeOperatorRegistrationPlan:
            raise ValueError
        # Frozen models contain mutable lists/maps; take a revalidated snapshot
        # rather than trusting model_copy or sharing the caller's nested state.
        return NativeOperatorRegistrationPlan.model_validate(plan.model_dump())
    except (ValidationError, ValueError, TypeError, KeyError):
        raise AdmissionRefusedError("native_operator_registration_plan_refused") from None


def registration_plan(*, inputs, evidence, deployment, account_id, region, registration_role_arn):
    compiled = compile_interpreter_contracts(inputs, evidence=evidence)
    if type(deployment) is not NativeOperatorDeployment or [effect.model_dump() for effect in deployment.effects] != compiled["effects"]:
        raise AdmissionRefusedError("native_operator_registration_binding_refused")
    try:
        return NativeOperatorRegistrationPlan(
            schema_version="1.0",
            producer="operator_local",
            account_id=account_id,
            region=region,
            registration_role_arn=registration_role_arn,
            table=inputs.binding.authority_table,
            binding=inputs.binding.model_dump(),
            deployment=deployment.model_dump(),
            contracts=compiled["contracts"],
            maximum_original_smoke_usd=compiled["maximum_original_smoke_usd"],
        )
    except (ValidationError, ValueError, TypeError, KeyError):
        raise AdmissionRefusedError("native_operator_registration_binding_refused") from None


def runtime_environment(plan, *, current_environment):
    """Operator-only overlay; the caller supplies the existing ConfigMap data.

    Do not silently replace a generic/native or different operator binding.
    This returns source configuration, not attestation that it was installed.
    """
    plan = _validated_plan(plan)
    binding_json = plan.binding.model_dump_json()
    protected = {
        "ADP_NATIVE_TEST_AUTHORITY_TABLE": plan.table,
        "ADP_NATIVE_TEST_DEPLOYMENT_ID": plan.binding.deployment_id,
        "ADP_OPERATOR_TOOL_BINDING": binding_json,
        "ADP_TOOLS_FACADE_ROLE_ARN": plan.binding.facade_role_arn,
        "ADP_TOOLS_RECONCILER_ROLE_ARN": plan.binding.reconciler_role_arn,
        "ADP_TOOLS_GATEWAY_ROLE_ARN": plan.binding.catalogue.gateway_role_arn,
    }
    if (
        type(current_environment) is not dict
        or any(type(key) is not str or type(value) is not str for key, value in current_environment.items())
        or current_environment.get("ADP_NATIVE_TEST_TOOL_BINDING")
        or any(current_environment.get(key, "") not in {"", value} for key, value in protected.items())
    ):
        raise AdmissionRefusedError("native_operator_runtime_binding_conflict")
    return {**current_environment, **protected}


def register_initial_plan(plan, *, expected_plan_sha256, store, sts_client, clock=None):
    """One transaction with eight initial-only puts and no replacement path.

    A conflict or lost acknowledgement is unconfirmed, never replayed or read
    back as success. The operator must reconcile an ambiguous result read-only.
    The protected table policy remains the authority for allowed writers.
    """
    plan = _validated_plan(plan)
    clock = clock or (lambda: datetime.now(UTC))

    def require_current():
        now = clock().timestamp()
        if (
            digest(plan.model_dump()) != expected_plan_sha256
            or type(store) is not NativeTestStore
            or store.table != plan.table
            or plan.deployment.expires_at <= now
            or any(not document["not_before"] <= now < document["expires_at"] for document in plan.contracts.values())
        ):
            raise AdmissionRefusedError("native_operator_registration_plan_refused")

    require_current()
    for client in (store.client, sts_client):
        retries = client.meta.config.retries
        if not isinstance(retries, dict) or retries.get("total_max_attempts") != 1:
            raise AdmissionRefusedError("native_operator_registration_retry_refused")
        if client.meta.region_name != plan.region:
            raise AdmissionRefusedError("native_operator_registration_region_refused")
    try:
        identity = sts_client.get_caller_identity()
    except (BotoCoreError, ClientError):
        raise AdmissionRefusedError("native_operator_registration_identity_unavailable") from None
    if identity.get("Account") != plan.account_id or parse_assumed_role_arn(identity.get("Arn", "")) != plan.registration_role_arn:
        raise AdmissionRefusedError("native_operator_registration_role_refused")
    writes = [store.put(deployment_key(plan.binding.deployment_id), CONFIGURATION_KIND, plan.deployment.model_dump())]
    for contract_id, document in plan.contracts.items():
        writes.append(
            store.put(
                {"pk": {"S": "TOOLS_TEST_DEPLOYMENT#" + plan.binding.deployment_id}, "sk": {"S": "EFFECT_CONTRACT#" + contract_id}},
                "native_test_effect_contract_v1",
                document,
            )
        )
    # Identity lookup can be delayed. Do not register an expired reviewed plan.
    require_current()
    store.transaction(writes)
    return {
        "schema_version": "1.0",
        "producer": "operator_local",
        "plan_sha256": expected_plan_sha256,
        "registered": True,
        "governance_complete": False,
    }
