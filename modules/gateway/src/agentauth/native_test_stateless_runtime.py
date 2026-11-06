"""Protected production composition for the tool-only native search slice.

The binding is installed by the protected deployment, and its complete digest is
part of deployment coverage. It does not select a fixture, grant permissions,
install prices or attest an image from an arbitrary HTTP request.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Literal

from fastapi.routing import APIRoute
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from src.agentauth.native_test_catalogue import CATALOGUE_CONTRACT, NativeSearchCatalogue, NativeSearchCatalogueBinding
from src.agentauth.native_test_effect_contract import ProtectedEffectQuoteAdapter
from src.agentauth.native_test_effect_quotes import CostBound, component_total
from src.agentauth.native_test_enrollment import AllInApproval, Contract, Digest, Name
from src.agentauth.native_test_facade_admission import NativeFacadeAdmission
from src.agentauth.native_test_facade_contract import FacadeEffectCostContract
from src.agentauth.native_test_lifecycle import CLEANUP_SEND_SECONDS, MAX_STATUS_READS, NativeTestLifecycle
from src.agentauth.native_test_setup_admission import reserve_native_setup
from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest

EFFECT_COVERAGE_CONTRACT = "adp-native-test-tool-lifecycle/v1"
STATELESS_ACTIONS = {"setup", "list", "call"}


class NativeStatelessBinding(Contract):
    schema_version: Literal["2.0"]
    deployment_id: Name
    release_sha256: Digest
    authority_table: str = Field(pattern=r"^[A-Za-z0-9_.-]{3,255}$")
    execution_table: str = Field(pattern=r"^[A-Za-z0-9_.-]{3,255}$")
    facade_role_arn: str = Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    reconciler_role_arn: str = Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    catalogue: NativeSearchCatalogueBinding


def stateless_coverage(binding):
    return digest(
        {
            "contract": EFFECT_COVERAGE_CONTRACT,
            "binding": binding.model_dump(),
            "routes": [
                "POST /internal/v1/tools/authorize",
                "POST /internal/v1/tools/native-dispatch",
                "POST /internal/v1/tools/native-cleanup",
                "GET /v1/native-tests/runs/{run_id}/status",
            ],
            "scope": "tool_only_stateless_search",
            "actions": sorted(STATELESS_ACTIONS),
            "public_discovery_pages": 1,
            "catalogue_contract": CATALOGUE_CONTRACT,
            "catalogue_control_reads": ["GetGateway", "GetGatewayTarget"],
            "catalogue_sdk_attempts": 1,
            "max_status_reads": MAX_STATUS_READS,
            "new_work_seconds": 300,
            "cleanup_seconds": 600,
            "cleanup_send_margin_seconds": CLEANUP_SEND_SECONDS,
            "retained_until_trusted_complete_settlement": True,
            "governance_complete": False,
        }
    )


class NativeStatelessRuntime:
    binding_type = NativeStatelessBinding
    catalogue_type = NativeSearchCatalogue
    coverage_document = staticmethod(stateless_coverage)

    def __init__(self, *, runtime, budget, binding, execution_client):
        self.runtime, self.enrollment, self.budget, self.binding = runtime, runtime.enrollment, budget, binding
        if (
            type(binding) is not self.binding_type
            or binding.deployment_id != self.enrollment.deployment_id
            or binding.authority_table != runtime.store.table
            or binding.facade_role_arn == binding.reconciler_role_arn
            or any(arn.split(":")[4] != binding.catalogue.account_id for arn in (binding.facade_role_arn, binding.reconciler_role_arn))
        ):
            raise AdmissionRefusedError("native_test_stateless_binding_unavailable")
        self.catalogue = self.catalogue_type(binding.catalogue, binding.release_sha256)
        runtime.tool_admission = NativeFacadeAdmission(enrollment=self.enrollment, budget=budget)
        runtime.tool_lifecycle = NativeTestLifecycle(
            enrollment=self.enrollment, budget=budget, execution_table=binding.execution_table, execution_client=execution_client
        )

    def coverage(self):
        from src.agentauth.native_test_routes import run_status
        from src.internal.tools_authorize import native_dispatch, native_reconcile_cleanup, tools_authorize

        expected = {
            ("POST", "/internal/v1/tools/authorize"): tools_authorize,
            ("POST", "/internal/v1/tools/native-dispatch"): native_dispatch,
            ("POST", "/internal/v1/tools/native-cleanup"): native_reconcile_cleanup,
            ("GET", "/v1/native-tests/runs/{run_id}/status"): run_status,
        }
        for (method, path), endpoint in expected.items():
            matches = [
                route for route in self.runtime.gateway.routes if isinstance(route, APIRoute) and route.path == path and method in route.methods
            ]
            if len(matches) != 1 or matches[0].endpoint is not endpoint:
                raise AdmissionRefusedError("native_test_stateless_coverage_unavailable")
        if (
            os.environ.get("ADP_TOOLS_FACADE_ROLE_ARN") != self.binding.facade_role_arn
            or os.environ.get("ADP_TOOLS_RECONCILER_ROLE_ARN") != self.binding.reconciler_role_arn
            or os.environ.get("ADP_TOOLS_GATEWAY_ROLE_ARN") != self.binding.catalogue.gateway_role_arn
            or type(self.catalogue) is not self.catalogue_type
            or self.catalogue.binding != self.binding.catalogue
            or self.catalogue.release_sha256 != self.binding.release_sha256
            or type(self.runtime.tool_admission) is not NativeFacadeAdmission
            or type(self.runtime.tool_lifecycle) is not NativeTestLifecycle
            or self.runtime.tool_admission.enrollment is not self.enrollment
            or self.runtime.tool_lifecycle.enrollment is not self.enrollment
            or self.runtime.tool_lifecycle.execution_table != self.binding.execution_table
            or self.runtime.tool_admission.budget is not self.budget
            or self.runtime.tool_lifecycle.budget is not self.budget
        ):
            raise AdmissionRefusedError("native_test_stateless_coverage_unavailable")
        return self.coverage_document(self.binding)

    async def require_ready(self, approval):
        self.coverage()
        if (
            type(approval) is not AllInApproval
            or approval.allowed_models
            or approval.allowed_routes
            or approval.tools != ["websearch.search"]
            or len(approval.effects) != 3
            or {effect.action for effect in approval.effects} != STATELESS_ACTIONS
            or any(effect.tool != "websearch.search" or effect.source_sha256 != self.binding.release_sha256 for effect in approval.effects)
        ):
            raise AdmissionRefusedError("native_test_stateless_scope_refused")
        total = Decimal(0)
        for effect in approval.effects:
            source = ProtectedEffectQuoteAdapter(
                store=self.runtime.store,
                deployment_id=self.binding.deployment_id,
                contract_id=effect.contract_id,
                contract_sha256=effect.contract_sha256,
                source_sha256=self.binding.release_sha256,
            )
            _, contract = await run_in_threadpool(source._read, self.enrollment.clock())
            if (
                contract.effect_key != effect.effect_key
                or contract.expires_at < approval.dispatch_expires_at
                or (effect.action == "setup") == isinstance(contract, FacadeEffectCostContract)
                or contract.session_seconds != 300
                or contract.cleanup_seconds != 600
                or contract.cleanup_attempts != 2
            ):
                raise AdmissionRefusedError("native_test_stateless_contract_refused")
            total += component_total(tuple(CostBound(**term.model_dump()) for term in contract.components)) * (2 if effect.action == "list" else 1)
        if not 0 < total <= 20:
            raise AdmissionRefusedError("native_test_stateless_contract_exceeds_cap")
        # Functional behavior may run while fully funded accounting is pending.
        # This does not claim trusted complete settlement or governance success.
        return None

    async def reserve_setup(self, context, approval, capability):
        return await reserve_native_setup(context=context, enrollment=self.enrollment, approval=approval, capability=capability, budget=self.budget)
