"""Protected registration for the model slice of native test admission.

The gateway can read and condition-check this document. It has no writer for
deployment registration or fixture designation. This is not all-in test budget
coverage or independent attestation of a deployed image.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, ValidationError, model_validator

from src.agentauth.native_test_enrollment import (
    ApprovedJob,
    Contract,
    Digest,
    EffectRegistration,
    Name,
    OperatorLocalApproval,
    Sha,
    Uuid,
    is_effect_approval,
)
from src.agentauth.test_run_admission_store import AdmissionRefusedError

CONFIGURATION_KIND = "native_test_deployment_v1"
COVERAGE_CONTRACT = "adp-native-test-paid-model-routes/v1"


class NativeTestDeployment(Contract):
    deployment_id: Name
    tenant_id: Name
    revision: Uuid
    state: Literal["active", "revoked"]
    scope: Literal["model_only"]
    cap_usd: Literal["20"]
    coverage_contract: Literal["adp-native-test-paid-model-routes/v1"]
    coverage_digest: Digest
    expires_at: int = Field(gt=0)
    jobs: list[ApprovedJob] = Field(min_length=1, max_length=32)
    allowed_models: list[Name] = Field(min_length=1, max_length=16)
    allowed_tools: list[Name] = Field(min_length=1, max_length=32)
    allowed_routes: list[Literal["/v1/messages", "/openai/v1/responses"]] = Field(min_length=1, max_length=2)


class NativeTestAllInDeployment(NativeTestDeployment):
    scope: Literal["all_in"]
    allowed_models: list[Name] = Field(max_length=16)
    allowed_routes: list[Literal["/v1/messages", "/openai/v1/responses"]] = Field(max_length=2)
    effect_coverage_contract: Literal["adp-native-test-tool-lifecycle/v1"]
    effect_coverage_digest: Digest
    effects: list[EffectRegistration] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def distinct_effects(self):
        if len({effect.effect_key for effect in self.effects}) != len(self.effects) or any(
            effect.tool not in self.allowed_tools for effect in self.effects
        ):
            raise ValueError("invalid all-in effect registration")
        return self


class NativeOperatorDeployment(Contract):
    """Protected source contract, explicitly enabled for a human-local run."""

    deployment_id: Name
    tenant_id: Name
    revision: Uuid
    state: Literal["active", "revoked"]
    scope: Literal["operator_local"]
    cap_usd: Literal["20"]
    coverage_contract: Literal["adp-native-test-paid-model-routes/v1"]
    coverage_digest: Digest
    expires_at: int = Field(gt=0)
    source_revision: Sha
    release_sha256: Digest
    gateway_origin: str = Field(pattern=r"^https://[a-z0-9][a-z0-9.-]*[a-z0-9](?::[0-9]{1,5})?$")
    contract_sha256: Digest
    allowed_models: list[Name] = Field(max_length=0)
    allowed_routes: list[Name] = Field(max_length=0)
    allowed_tools: list[Name] = Field(min_length=4, max_length=4)
    effect_coverage_contract: Literal["adp-native-operator-interpreter/v1"]
    effect_coverage_digest: Digest
    effects: list[EffectRegistration] = Field(min_length=7, max_length=7)

    @model_validator(mode="after")
    def exact_effects(self):
        if (
            set(self.allowed_tools) != {"code_interpreter." + name for name in ("start", "execute", "result", "file")}
            or len({effect.effect_key for effect in self.effects}) != len(self.effects)
            or any(effect.tool not in self.allowed_tools or effect.source_sha256 != self.release_sha256 for effect in self.effects)
        ):
            raise ValueError("operator Interpreter effects required")
        return self


def deployment_key(deployment_id):
    return {"pk": {"S": "TOOLS_TEST_DEPLOYMENT#" + deployment_id}, "sk": {"S": "CONFIGURATION"}}


class NativeTestDeploymentVerifier:
    def __init__(self, *, store, deployment_id, clock, coverage, effect_coverage=None):
        self.store, self.deployment_id, self.clock, self.coverage = store, deployment_id, clock, coverage
        self.effect_coverage = effect_coverage

    def current(self):
        raw = self.store.document(deployment_key(self.deployment_id), CONFIGURATION_KIND)
        try:
            model = {"all_in": NativeTestAllInDeployment, "operator_local": NativeOperatorDeployment}.get(
                raw.get("scope") if isinstance(raw, dict) else None, NativeTestDeployment
            )
            configuration = model.model_validate(raw)
            if (
                configuration.model_dump() != raw
                or configuration.deployment_id != self.deployment_id
                or configuration.state != "active"
                or configuration.expires_at <= self.clock().timestamp()
                or configuration.coverage_digest != self.coverage()
            ):
                raise ValueError
            if type(configuration) in {NativeTestAllInDeployment, NativeOperatorDeployment} and (
                self.effect_coverage is None or configuration.effect_coverage_digest != self.effect_coverage()
            ):
                raise ValueError
        except (ValidationError, ValueError, TypeError):
            raise AdmissionRefusedError("native_test_deployment_unavailable") from None
        return configuration

    def guards(self, approval):
        configuration = self.current()
        if (
            approval.deployment_id != configuration.deployment_id
            or approval.principal.tenant_id != configuration.tenant_id
            or not set(approval.allowed_models) <= set(configuration.allowed_models)
            or not set(approval.allowed_routes) <= set(configuration.allowed_routes)
            or not set(approval.tools) <= set(configuration.allowed_tools)
            or approval.dispatch_expires_at > configuration.expires_at
            or approval.admission_scope != configuration.scope
        ):
            raise AdmissionRefusedError("native_test_registered_scope_refused")
        if type(approval) is OperatorLocalApproval:
            if (
                type(configuration) is not NativeOperatorDeployment
                or approval.deployment_revision != configuration.revision
                or approval.contract_sha256 != configuration.contract_sha256
                or approval.source_revision != configuration.source_revision
                or approval.release_sha256 != configuration.release_sha256
                or approval.gateway_origin != configuration.gateway_origin
                or approval.tools != configuration.allowed_tools
                or approval.effects != configuration.effects
            ):
                raise AdmissionRefusedError("native_test_registered_scope_refused")
        elif type(configuration) is NativeOperatorDeployment or approval.job not in configuration.jobs:
            raise AdmissionRefusedError("native_test_registered_scope_refused")
        guards = [self.store.guard(deployment_key(self.deployment_id), CONFIGURATION_KIND, configuration.model_dump())]
        if is_effect_approval(approval):
            from src.agentauth.native_test_effect_contract import ProtectedEffectQuoteAdapter

            if any(effect not in configuration.effects for effect in approval.effects):
                raise AdmissionRefusedError("native_test_registered_scope_refused")
            for effect in approval.effects:
                source = ProtectedEffectQuoteAdapter(
                    store=self.store,
                    deployment_id=self.deployment_id,
                    contract_id=effect.contract_id,
                    contract_sha256=effect.contract_sha256,
                    source_sha256=effect.source_sha256,
                )
                guards.extend(
                    source.registration_guards(effect_key=effect.effect_key, dispatch_expires_at=approval.dispatch_expires_at, now=self.clock())
                )
        return guards
