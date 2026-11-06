"""Two exact control reads for the protected native Search fixture.

Target metadata pins the provider ARN and schema. Image/source attestation stays
with the existing reviewed release evidence; these reads do not attest code.
"""

from __future__ import annotations

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import Field, model_validator

from src.agentauth.native_test_enrollment import AllInApproval, Contract, Digest
from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest

CATALOGUE_CONTRACT = "adp-native-search-exact-catalogue/v1"


class NativeSearchCatalogueBinding(Contract):
    gateway_arn: str = Field(pattern=r"^arn:aws:bedrock-agentcore:[a-z0-9-]{1,20}:[0-9]{12}:gateway/[a-z0-9-]{1,100}-[a-z0-9]{10}$")
    gateway_role_arn: str = Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    target_id: str = Field(pattern=r"^[a-zA-Z0-9]{10}$")
    lambda_arn: str = Field(pattern=r"^arn:aws:lambda:[a-z0-9-]{1,20}:[0-9]{12}:function:[A-Za-z0-9_-]+(:[A-Za-z0-9_-]+)?$")
    # Full GetGatewayTarget.targetConfiguration, including provider and schema.
    # Supplied by protected registration, never inferred from an HTTP request.
    target_configuration_sha256: Digest

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
    def same_deployment(self):
        if self.gateway_role_arn.split(":")[4] != self.account_id or self.lambda_arn.split(":")[3:5] != [self.region, self.account_id]:
            raise ValueError("Native catalogue resources must share account and region")
        return self


class NativeSearchCatalogue:
    def __init__(self, binding, release_sha256):
        if type(binding) is not NativeSearchCatalogueBinding:
            raise AdmissionRefusedError("native_test_catalogue_binding_unavailable")
        self.binding, self.release_sha256 = binding, release_sha256

    def read(self, approval):
        binding = self.binding
        if (
            type(approval) is not AllInApproval
            or approval.tools != ["websearch.search"]
            or approval.allowed_models
            or approval.allowed_routes
            or len(approval.effects) != 3
            or {effect.action for effect in approval.effects} != {"setup", "list", "call"}
            or any(effect.source_sha256 != self.release_sha256 or effect.tool != "websearch.search" for effect in approval.effects)
        ):
            raise AdmissionRefusedError("native_test_catalogue_scope_refused")
        try:
            client = boto3.client(
                "bedrock-agentcore-control",
                region_name=binding.region,
                config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}),
            )
            gateway = client.get_gateway(gatewayIdentifier=binding.gateway_id)
            if (
                gateway.get("gatewayArn") != binding.gateway_arn
                or gateway.get("gatewayId") != binding.gateway_id
                or gateway.get("name") != "adp-tools"
                or gateway.get("status") != "READY"
                or gateway.get("roleArn") != binding.gateway_role_arn
                or gateway.get("protocolType") != "MCP"
                or gateway.get("authorizerType") != "AWS_IAM"
            ):
                raise ValueError
            target = client.get_gateway_target(gatewayIdentifier=binding.gateway_id, targetId=binding.target_id)
            configuration = target["targetConfiguration"]
            provider = configuration["mcp"]["lambda"]
            entries = provider["toolSchema"]["inlinePayload"]
            if (
                target.get("gatewayArn") != binding.gateway_arn
                or target.get("targetId") != binding.target_id
                or target.get("name") != "websearch"
                or target.get("status") != "READY"
                or target.get("credentialProviderConfigurations") != [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
                or set(configuration) != {"mcp"}
                or set(configuration["mcp"]) != {"lambda"}
                or provider.get("lambdaArn") != binding.lambda_arn
                or digest(configuration) != binding.target_configuration_sha256
                or not isinstance(entries, list)
                or len([entry for entry in entries if entry.get("name") == "search" and entry.get("inputSchema", {}).get("type") == "object"]) != 1
            ):
                raise ValueError
        except (BotoCoreError, ClientError, KeyError, TypeError, ValueError, AttributeError):
            raise AdmissionRefusedError("native_test_catalogue_unavailable") from None
        return {"tools": [{"name": "websearch.search"}]}
