"""Explicit operator-local Interpreter composition over the original journal.

No factory enrolls a human, writes a protected registration, or grants consent.
The complete installed binding and every exact payload need protected cost facts.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

import boto3
from botocore.config import Config
from fastapi.routing import APIRoute
from pydantic import Field, model_validator
from starlette.concurrency import run_in_threadpool

from src.agentauth.native_test_catalogue import NativeSearchCatalogueBinding
from src.agentauth.native_test_effect_contract import ProtectedEffectQuoteAdapter
from src.agentauth.native_test_effect_quotes import CostBound, component_total
from src.agentauth.native_test_enrollment import Contract, Digest, Name, OperatorLocalApproval, Sha
from src.agentauth.native_test_facade_contract import InterpreterStepCostContract
from src.agentauth.native_test_stateless_runtime import NativeStatelessRuntime, stateless_coverage
from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest

INTERPRETER_TOOLS = {"code_interpreter." + name for name in ("start", "execute", "result", "file")}
INTERPRETER_STEPS = {"list", "start", "execute", "execute_status", "result", "file"}


class NativeInterpreterBinding(Contract):
    schema_version: Literal["4.0"]
    deployment_id: Name
    release_sha256: Digest
    source_revision: Sha
    gateway_origin: str = Field(pattern=r"^https://[a-z0-9][a-z0-9.-]*[a-z0-9](?::[0-9]{1,5})?$")
    authority_table: str = Field(pattern=r"^[A-Za-z0-9_.-]{3,255}$")
    execution_table: str = Field(pattern=r"^[A-Za-z0-9_.-]{3,255}$")
    facade_role_arn: str = Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    reconciler_role_arn: str = Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    catalogue: NativeSearchCatalogueBinding
    execute_code: str = Field(min_length=1, max_length=8192)
    file_path: str = Field(pattern=r"^/tmp/[A-Za-z0-9_.-]{1,100}$")

    @model_validator(mode="after")
    def bounded_payload(self):
        if len(self.execute_code.encode()) > 8192 or ".." in self.file_path:
            raise ValueError("bounded operator payload required")
        return self


def interpreter_request_digests(binding):
    """Normalized payloads; actual original-owned handles stay in request hash."""

    def owned(field):
        return {"original_owned_reference": field}

    def call(tool, arguments, execution, ttl=None):
        return digest(
            {
                "request_binding": "facade_call_v1",
                "tool": "code_interpreter___" + tool,
                "arguments": arguments,
                "execution_binding": execution,
                "ttl_seconds": ttl,
            }
        )

    execute = {"session_id": owned("session_id"), "code": binding.execute_code, "language": "python"}
    return {
        "list": digest({"request_binding": "facade_list_v1", "max_pages": 1}),
        "start": call("start", {}, "new", 300),
        "execute": call("execute", execute, "owned"),
        "execute_status": digest(
            {
                "request_binding": "facade_recover_v1",
                "tool": "code_interpreter___execute",
                "arguments": execute,
                "execution_binding": "owned",
                "ttl_seconds": None,
            }
        ),
        "result": call("result", {"session_id": owned("session_id"), "provider_id": owned("provider_id")}, "owned"),
        "file": call("file", {"session_id": owned("session_id"), "path": binding.file_path}, "owned"),
    }


def interpreter_coverage(binding):
    return digest(
        {
            "contract": "adp-native-operator-interpreter/v1",
            "parent": stateless_coverage(binding),
            "producer": "operator_local",
            "approval_audience": "adp-native-operator-local-v1",
            "routes": [
                "POST /v1/operator-tools/approvals",
                "POST /v1/operator-tools/capabilities/exchange",
                "POST /v1/operator-tools/capabilities/revoke",
                "GET /v1/operator-tools/runs/{run_id}/status",
            ],
            "steps": sorted(INTERPRETER_STEPS),
            "requests": interpreter_request_digests(binding),
            "step_requests": 1,
            "original_execution_and_request_tombstones": True,
            "models": [],
            "ci_designation": False,
        }
    )


class NativeInterpreterCatalogue:
    def __init__(self, binding, release_sha256):
        if type(binding) is not NativeSearchCatalogueBinding:
            raise AdmissionRefusedError("native_operator_catalogue_binding_unavailable")
        self.binding, self.release_sha256 = binding, release_sha256

    def read(self, approval):
        if (
            type(approval) is not OperatorLocalApproval
            or set(approval.tools) != INTERPRETER_TOOLS
            or approval.allowed_models
            or approval.allowed_routes
            or approval.release_sha256 != self.release_sha256
        ):
            raise AdmissionRefusedError("native_operator_catalogue_scope_refused")
        binding = self.binding
        try:
            client = boto3.client(
                "bedrock-agentcore-control",
                region_name=binding.region,
                config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}),
            )
            gateway = client.get_gateway(gatewayIdentifier=binding.gateway_id)
            target = client.get_gateway_target(gatewayIdentifier=binding.gateway_id, targetId=binding.target_id)
            configuration = target["targetConfiguration"]
            provider = configuration["mcp"]["lambda"]
            entries = provider["toolSchema"]["inlinePayload"]
            if (
                gateway.get("gatewayArn") != binding.gateway_arn
                or gateway.get("gatewayId") != binding.gateway_id
                or gateway.get("name") != "adp-tools"
                or gateway.get("status") != "READY"
                or gateway.get("roleArn") != binding.gateway_role_arn
                or gateway.get("protocolType") != "MCP"
                or gateway.get("authorizerType") != "AWS_IAM"
                or target.get("gatewayArn") != binding.gateway_arn
                or target.get("targetId") != binding.target_id
                or target.get("name") != "code-interpreter"
                or target.get("status") != "READY"
                or target.get("credentialProviderConfigurations") != [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
                or set(configuration) != {"mcp"}
                or set(configuration["mcp"]) != {"lambda"}
                or provider.get("lambdaArn") != binding.lambda_arn
                or digest(configuration) != binding.target_configuration_sha256
                or not isinstance(entries, list)
            ):
                raise ValueError
            facade = []
            for name in ("start", "execute", "result", "file"):
                found = [entry for entry in entries if entry.get("name") == name and entry.get("inputSchema", {}).get("type") == "object"]
                if len(found) != 1:
                    raise ValueError
                facade.append({**found[0], "name": "code_interpreter___" + name})
        except Exception:
            raise AdmissionRefusedError("native_operator_catalogue_unavailable") from None
        return {"tools": [{"name": tool} for tool in sorted(INTERPRETER_TOOLS)], "facade_tools": facade}


class NativeInterpreterRuntime(NativeStatelessRuntime):
    binding_type = NativeInterpreterBinding
    catalogue_type = NativeInterpreterCatalogue
    coverage_document = staticmethod(interpreter_coverage)

    def coverage(self):
        from src.agentauth.native_operator_routes import approve, exchange, revoke, run_status

        value = super().coverage()
        for method, path, endpoint in [
            ("POST", "/v1/operator-tools/approvals", approve),
            ("POST", "/v1/operator-tools/capabilities/exchange", exchange),
            ("POST", "/v1/operator-tools/capabilities/revoke", revoke),
            ("GET", "/v1/operator-tools/runs/{run_id}/status", run_status),
        ]:
            found = [route for route in self.runtime.gateway.routes if isinstance(route, APIRoute) and route.path == path and method in route.methods]
            if len(found) != 1 or found[0].endpoint is not endpoint:
                raise AdmissionRefusedError("native_operator_coverage_unavailable")
        return value

    async def require_ready(self, approval):
        self.coverage()
        if (
            type(approval) is not OperatorLocalApproval
            or approval.contract_sha256 != digest(self.binding.model_dump())
            or approval.gateway_origin != self.binding.gateway_origin
            or approval.source_revision != self.binding.source_revision
            or approval.release_sha256 != self.binding.release_sha256
            or set(approval.tools) != INTERPRETER_TOOLS
            or len(approval.tools) != 4
            or approval.allowed_models
            or approval.allowed_routes
            or len(approval.effects) != 7
        ):
            raise AdmissionRefusedError("native_operator_scope_refused")
        total, seen = Decimal(0), set()
        expected = interpreter_request_digests(self.binding)
        for effect in approval.effects:
            source = ProtectedEffectQuoteAdapter(
                store=self.runtime.store,
                deployment_id=self.binding.deployment_id,
                contract_id=effect.contract_id,
                contract_sha256=effect.contract_sha256,
                source_sha256=self.binding.release_sha256,
            )
            _, contract = await run_in_threadpool(source._read, self.enrollment.clock())
            if effect.action == "setup":
                step, action, tool = "setup", "setup", "code_interpreter.start"
                request_digest = digest({"effect_key": effect.effect_key, "phase": "test_setup"})
                if type(contract) is InterpreterStepCostContract:
                    raise AdmissionRefusedError("native_operator_contract_refused")
            elif type(contract) is InterpreterStepCostContract:
                step = contract.interpreter_step
                action = "list" if step == "list" else "recover" if step == "execute_status" else "call"
                tool = "code_interpreter." + ("start" if step == "list" else "execute" if step == "execute_status" else step)
                request_digest = expected.get(step)
            else:
                raise AdmissionRefusedError("native_operator_contract_refused")
            if (
                step in seen
                or effect.action != action
                or effect.tool != tool
                or effect.source_sha256 != self.binding.release_sha256
                or contract.effect_key != effect.effect_key
                or contract.request_sha256 != request_digest
                or contract.expires_at < approval.dispatch_expires_at
                or contract.session_seconds != 300
                or contract.cleanup_seconds != 600
                or contract.cleanup_attempts != 2
            ):
                raise AdmissionRefusedError("native_operator_contract_refused")
            seen.add(step)
            total += component_total(tuple(CostBound(**term.model_dump()) for term in contract.components))
        if seen != INTERPRETER_STEPS | {"setup"} or not 0 < total <= 20:
            raise AdmissionRefusedError("native_operator_contract_exceeds_cap")
