"""Protected Atlassian native-client admission using the existing lifecycle.

An approval pins deployed schemas, source and complete prices. It does not
create a provider grant or bypass current user policy, site or write consent.
"""

from __future__ import annotations

import hashlib
import os
from decimal import Decimal
from typing import Literal

import boto3
from botocore.config import Config
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from src.agentauth.native_test_effect_contract import EffectCostContract, ProtectedEffectQuoteAdapter
from src.agentauth.native_test_effect_quotes import CostBound, component_total
from src.agentauth.native_test_enrollment import AllInApproval
from src.agentauth.native_test_facade_contract import ATLASSIAN_CALLS, FacadeEffectCostContract
from src.agentauth.native_test_knowledge_runtime import NativeKnowledgeBinding, NativeKnowledgeCatalogueBinding
from src.agentauth.native_test_stateless_runtime import NativeStatelessRuntime, stateless_coverage
from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest
from src.internal.tools_dynamic_catalogue import canonical, read_catalogue, target_binding


class NativeAtlassianCatalogueBinding(NativeKnowledgeCatalogueBinding):
    """Exact private adapter target and complete observed schema digest."""


class NativeAtlassianBinding(NativeKnowledgeBinding):
    schema_version: Literal["5.0"]
    catalogue: NativeAtlassianCatalogueBinding


def atlassian_scope(approval, release_sha256):
    return (
        type(approval) is AllInApproval
        and bool(approval.tools)
        and len(set(approval.tools)) == len(approval.tools)
        and all(tool.replace(".", "___", 1) in ATLASSIAN_CALLS for tool in approval.tools)
        and all(effect.source_sha256 == release_sha256 for effect in approval.effects)
    )


class NativeAtlassianCatalogue:
    def __init__(self, binding, release_sha256):
        if type(binding) is not NativeAtlassianCatalogueBinding:
            raise AdmissionRefusedError("native_test_atlassian_binding_unavailable")
        self.binding, self.release_sha256 = binding, release_sha256

    def read(self, approval):
        binding = self.binding
        if not atlassian_scope(approval, self.release_sha256):
            raise AdmissionRefusedError("native_test_atlassian_scope_refused")
        try:
            if os.environ.get("AGENT_AUTHORITY_TABLE") != binding.catalogue_table:
                raise ValueError
            config = Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}, ignore_configured_endpoint_urls=True)
            control = boto3.client("bedrock-agentcore-control", region_name=binding.region, config=config)
            gateway = control.get_gateway(gatewayIdentifier=binding.gateway_id)
            target = control.get_gateway_target(gatewayIdentifier=binding.gateway_id, targetId=binding.target_id)
            if (
                gateway.get("gatewayArn") != binding.gateway_arn
                or gateway.get("roleArn") != binding.gateway_role_arn
                or target.get("targetId") != binding.target_id
                or target.get("name") != "atlassian"
                or digest(target_binding(binding.gateway_id, target, gateway)) != binding.target_binding_sha256
            ):
                raise ValueError
            tools = read_catalogue(binding.gateway_id, target, gateway)
            if (
                not tools
                or hashlib.sha256(canonical(tools)).hexdigest() != binding.tools_sha256
                or not {tool.split(".", 1)[1] for tool in approval.tools} <= {tool["name"] for tool in tools}
            ):
                raise ValueError
        except Exception:
            raise AdmissionRefusedError("native_test_atlassian_catalogue_unavailable") from None
        return {
            "tools": [{"name": tool} for tool in approval.tools],
            "facade_tools": [{**tool, "name": "atlassian___" + tool["name"]} for tool in tools if "atlassian." + tool["name"] in approval.tools],
        }


def atlassian_coverage(binding):
    return digest(
        {
            "contract": "adp-native-atlassian-lifecycle/v1",
            "parent": stateless_coverage(binding),
            "tools": sorted(ATLASSIAN_CALLS),
            "adapter_routes": ["POST /internal/v1/tool-adapters/authorize", "POST /internal/v1/tool-adapters/dispatch"],
            "dispatch": "original_operation_single_claim_with_current_policy_connection_release",
            "cleanup": "original_finite_execution_allowance",
            "provider_fixture_cleanup": "separate_required_provider_observation",
            "settlement": "retained_until_trusted_complete_evidence",
        }
    )


class NativeAtlassianRuntime(NativeStatelessRuntime):
    binding_type = NativeAtlassianBinding
    catalogue_type = NativeAtlassianCatalogue
    coverage_document = staticmethod(atlassian_coverage)

    def coverage(self):
        from src.internal.tool_adapter_authority import authorize_observation, dispatch_tool

        result = super().coverage()
        for path, endpoint in {
            "/internal/v1/tool-adapters/authorize": authorize_observation,
            "/internal/v1/tool-adapters/dispatch": dispatch_tool,
        }.items():
            matches = [
                route for route in self.runtime.gateway.routes if isinstance(route, APIRoute) and route.path == path and "POST" in route.methods
            ]
            if len(matches) != 1 or matches[0].endpoint is not endpoint:
                raise AdmissionRefusedError("native_test_atlassian_coverage_unavailable")
        return result

    async def require_ready(self, approval):
        self.coverage()
        if (
            not atlassian_scope(approval, self.binding.release_sha256)
            or len(approval.allowed_models) > 2
            or len(approval.allowed_routes) > 2
            or bool(approval.allowed_models) != bool(approval.allowed_routes)
            or {effect.action for effect in approval.effects} != {"setup", "list", "call"}
            or sum(effect.action == "setup" for effect in approval.effects) != 1
            or sum(effect.action == "list" for effect in approval.effects) != 1
            or {effect.tool for effect in approval.effects if effect.action == "call"} != set(approval.tools)
            or any(effect.tool not in approval.tools for effect in approval.effects)
        ):
            raise AdmissionRefusedError("native_test_atlassian_scope_refused")
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
                or type(contract) is not (EffectCostContract if effect.action == "setup" else FacadeEffectCostContract)
                or contract.session_seconds != 300
                or contract.cleanup_seconds != 600
                or contract.cleanup_attempts != 2
            ):
                raise AdmissionRefusedError("native_test_atlassian_contract_refused")
            total += component_total(tuple(CostBound(**term.model_dump()) for term in contract.components))
        if not 0 < total <= 20:
            raise AdmissionRefusedError("native_test_atlassian_contract_exceeds_cap")
