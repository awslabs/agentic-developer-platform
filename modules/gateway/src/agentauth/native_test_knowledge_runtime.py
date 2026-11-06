"""Explicit protected admission composition for native Knowledge read tests.

The runtime selects no fixture, prices, consent or deployment automatically. It
retains the existing original-operation admission, unknown holds and cleanup.
Memory, writes and semantic model dispatch are outside this read contract.
"""

from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal
from typing import Literal

import boto3
from botocore.config import Config
from pydantic import Field, model_validator
from starlette.concurrency import run_in_threadpool

from src.agentauth.native_test_effect_contract import ProtectedEffectQuoteAdapter
from src.agentauth.native_test_effect_quotes import CostBound, component_total
from src.agentauth.native_test_enrollment import AllInApproval, Contract, Digest, Name
from src.agentauth.native_test_facade_contract import KNOWLEDGE_READS, FacadeEffectCostContract
from src.agentauth.native_test_stateless_runtime import NativeStatelessRuntime, stateless_coverage
from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest
from src.internal.tools_dynamic_catalogue import KNOWLEDGE_TTL_SECONDS, canonical, catalogue_ttl, target_binding, validate_tools


class NativeKnowledgeCatalogueBinding(Contract):
    gateway_arn: str = Field(pattern=r"^arn:aws:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:gateway/[a-zA-Z0-9_-]+$")
    gateway_role_arn: str = Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    target_id: str = Field(pattern=r"^[a-zA-Z0-9]{10}$")
    catalogue_table: str = Field(pattern=r"^[A-Za-z0-9_.-]{3,255}$")
    # Pins the complete control-plane/source/endpoint binding, not a URL alone.
    target_binding_sha256: Digest
    tools_sha256: Digest

    @property
    def account_id(self):
        return self.gateway_arn.split(":")[4]

    @property
    def region(self):
        return self.gateway_arn.split(":")[3]

    @property
    def gateway_id(self):
        return self.gateway_arn.rsplit("/", 1)[1]

    @model_validator(mode="after")
    def same_account(self):
        if self.gateway_role_arn.split(":")[4] != self.account_id:
            raise ValueError("Knowledge catalogue account mismatch")
        return self


class NativeKnowledgeBinding(Contract):
    schema_version: Literal["3.0"]
    deployment_id: Name
    release_sha256: Digest
    authority_table: str = Field(pattern=r"^[A-Za-z0-9_.-]{3,255}$")
    execution_table: str = Field(pattern=r"^[A-Za-z0-9_.-]{3,255}$")
    facade_role_arn: str = Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    reconciler_role_arn: str = Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    catalogue: NativeKnowledgeCatalogueBinding


class NativeKnowledgeCatalogue:
    def __init__(self, binding, release_sha256):
        if type(binding) is not NativeKnowledgeCatalogueBinding:
            raise AdmissionRefusedError("native_test_knowledge_binding_unavailable")
        self.binding, self.release_sha256 = binding, release_sha256

    def read(self, approval):
        binding = self.binding
        if (
            type(approval) is not AllInApproval
            or not approval.tools
            or len(set(approval.tools)) != len(approval.tools)
            or any(tool.replace(".", "___", 1) not in KNOWLEDGE_READS for tool in approval.tools)
            or any(effect.source_sha256 != self.release_sha256 for effect in approval.effects)
        ):
            raise AdmissionRefusedError("native_test_knowledge_scope_refused")
        try:
            config = Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})
            control = boto3.client("bedrock-agentcore-control", region_name=binding.region, config=config)
            gateway = control.get_gateway(gatewayIdentifier=binding.gateway_id)
            target = control.get_gateway_target(gatewayIdentifier=binding.gateway_id, targetId=binding.target_id)
            if (
                gateway.get("gatewayArn") != binding.gateway_arn
                or gateway.get("roleArn") != binding.gateway_role_arn
                or target.get("targetId") != binding.target_id
            ):
                raise ValueError
            actual = target_binding(binding.gateway_id, target, gateway)
            if hashlib.sha256(canonical(actual)).hexdigest() != binding.target_binding_sha256:
                raise ValueError
            item = (
                boto3.client("dynamodb", region_name=binding.region, config=config)
                .get_item(
                    TableName=binding.catalogue_table,
                    Key={"pk": {"S": "TOOLS_DYNAMIC_CATALOGUE#" + binding.gateway_id}, "sk": {"S": "TARGET#" + binding.target_id}},
                    ConsistentRead=True,
                )
                .get("Item", {})
            )
            raw = item["document"]["S"]
            if not isinstance(raw, str) or len(raw.encode()) > 270336:
                raise ValueError
            record = json.loads(raw)
            if (
                record.get("version") != "1"
                or record.get("issuer") != "adp-private-knowledge-mediator"
                or record.get("binding") != actual
                or type(record.get("observed_at")) is not int
                or type(record.get("expires_at")) is not int
                or not record["observed_at"] <= time.time() < record["expires_at"]
                or not 0 < record["expires_at"] - record["observed_at"] <= catalogue_ttl(actual)
            ):
                raise ValueError
            tools = validate_tools(record["tools"])
            schema_digest = hashlib.sha256(canonical(tools)).hexdigest()
            if (
                record.get("tools_sha256") != schema_digest
                or schema_digest != binding.tools_sha256
                or not {tool.split(".", 1)[1] for tool in approval.tools} <= {tool["name"] for tool in tools}
            ):
                raise ValueError
        except Exception:
            raise AdmissionRefusedError("native_test_knowledge_catalogue_unavailable") from None
        return {
            "tools": [{"name": tool} for tool in approval.tools],
            "facade_tools": [{**tool, "name": "knowledge___" + tool["name"]} for tool in tools if "knowledge." + tool["name"] in approval.tools],
        }


def knowledge_coverage(binding):
    # The parent digest retains exact authorizer/dispatch/cleanup routes and
    # original finite horizons; this separate digest cannot qualify as Search.
    return digest(
        {
            "contract": "adp-native-knowledge-read-lifecycle/v1",
            "parent": stateless_coverage(binding),
            "scope": "knowledge_reads_with_native_model_admission",
            "read_tools": sorted(KNOWLEDGE_READS),
            "mediator_route": "POST /internal/v1/tools/knowledge/mcp",
            "knowledge_dispatch": "original_journal_single_use",
            "catalogue_read_attempts": 1,
            "catalogue_ttl_seconds": KNOWLEDGE_TTL_SECONDS,
            "memory_write_semantic": "refused",
            "settlement": "retained_until_trusted_complete_evidence",
        }
    )


class NativeKnowledgeRuntime(NativeStatelessRuntime):
    binding_type = NativeKnowledgeBinding
    catalogue_type = NativeKnowledgeCatalogue
    coverage_document = staticmethod(knowledge_coverage)

    def coverage(self):
        from fastapi.routing import APIRoute

        from src.internal.tools_knowledge import knowledge_mcp

        value = super().coverage()
        matches = [
            route
            for route in self.runtime.gateway.routes
            if isinstance(route, APIRoute) and route.path == "/internal/v1/tools/knowledge/mcp" and "POST" in route.methods
        ]
        if len(matches) != 1 or matches[0].endpoint is not knowledge_mcp:
            raise AdmissionRefusedError("native_test_knowledge_coverage_unavailable")
        return value

    async def require_ready(self, approval):
        self.coverage()
        if (
            type(approval) is not AllInApproval
            or not approval.tools
            or len(set(approval.tools)) != len(approval.tools)
            or any(tool.replace(".", "___", 1) not in KNOWLEDGE_READS for tool in approval.tools)
            or len(approval.allowed_models) > 2
            or len(approval.allowed_routes) > 2
            or bool(approval.allowed_models) != bool(approval.allowed_routes)
            or {effect.action for effect in approval.effects} != {"setup", "list", "call"}
            or sum(effect.action == "setup" for effect in approval.effects) != 1
            or sum(effect.action == "list" for effect in approval.effects) != 1
            or {effect.tool for effect in approval.effects if effect.action == "call"} != set(approval.tools)
            or any(effect.tool not in approval.tools or effect.source_sha256 != self.binding.release_sha256 for effect in approval.effects)
        ):
            raise AdmissionRefusedError("native_test_knowledge_scope_refused")
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
                raise AdmissionRefusedError("native_test_knowledge_contract_refused")
            total += component_total(tuple(CostBound(**term.model_dump()) for term in contract.components))
        if not 0 < total <= 20:
            raise AdmissionRefusedError("native_test_knowledge_contract_exceeds_cap")
        return None
