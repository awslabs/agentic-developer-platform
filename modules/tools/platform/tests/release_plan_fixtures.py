"""Fictional Terraform plan values; no cloud or native-release authority."""

from copy import deepcopy


ACCOUNT = "123456789012"
GATEWAY_ARN = f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:gateway/example"
BINDING = {
    "version": 1,
    "gateway_arn": GATEWAY_ARN,
    "endpoint": "https://fixture.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/knowledge/mcp",
    "vpc_id": "vpc-00000000000000001",
    "subnet_ids": ["subnet-00000000000000001", "subnet-00000000000000002"],
    "security_group_ids": ["sg-00000000000000001"],
    "routing_domain": "vpce-00000000000000001-regional.execute-api.us-east-1.vpce.amazonaws.com",
}
SELECTORS = {
    "version": 1, "account": ACCOUNT, "region": "us-east-1", "cluster": "adp-dev-eks-cluster",
    "components": {"platform targets": {"gateway_id": "example", "target_ids": ["browser-target", "code-target", "search-target"]}},
}


def existing_target_changes():
    return [{
        "mode": "managed", "type": "aws_bedrockagentcore_gateway_target",
        "address": f'aws_bedrockagentcore_gateway_target.tools["{name}"]',
        "change": {"actions": ["no-op"], "before": {"gateway_identifier": "example", "target_id": identifier},
                   "after": {"gateway_identifier": "example", "target_id": identifier}, "after_unknown": {}},
    } for name, identifier in (("browser", "browser-target"), ("code-interpreter", "code-target"), ("websearch", "search-target"))]


def gateway_change(actions=None):
    identity = {
        "gateway_id": "example", "gateway_arn": GATEWAY_ARN,
        "role_arn": f"arn:aws:iam::{ACCOUNT}:role/adp-tools-dev-gateway", "authorizer_type": "AWS_IAM",
        "protocol_configuration": [{"mcp": [{"search_type": None, "streaming_configuration": [{"enable_response_streaming": True}]}]}],
    }
    return {
        "mode": "managed", "type": "aws_bedrockagentcore_gateway", "address": "aws_bedrockagentcore_gateway.tools",
        "change": {"actions": actions or ["no-op"], "before": deepcopy(identity), "after": deepcopy(identity), "after_unknown": {}},
    }


def knowledge_change():
    return {
        "mode": "managed", "type": "aws_bedrockagentcore_gateway_target", "address": "aws_bedrockagentcore_gateway_target.knowledge[0]",
        "change": {
            "actions": ["create"], "before": None,
            "after": {
                "name": "knowledge", "gateway_identifier": "example",
                "credential_provider_configuration": [{"gateway_iam_role": [{"service": "execute-api"}], "oauth": [], "api_key": []}],
                "target_configuration": [{"mcp": [{"mcp_server": [{"endpoint": BINDING["endpoint"], "listing_mode": "DYNAMIC", "resource_priority": None}], "lambda": [], "open_api_schema": []}]}],
                "private_endpoint": [{"managed_vpc_resource": [{
                    "vpc_identifier": BINDING["vpc_id"], "subnet_ids": BINDING["subnet_ids"][:],
                    "security_group_ids": BINDING["security_group_ids"][:], "endpoint_ip_address_type": "IPV4",
                    "routing_domain": BINDING["routing_domain"],
                }]}],
                "metadata_configuration": [], "certificate_configuration": [],
                "target_id": None, "created_at": None, "updated_at": None,
            },
            "after_unknown": {"target_id": True, "created_at": True, "updated_at": True},
        },
    }
