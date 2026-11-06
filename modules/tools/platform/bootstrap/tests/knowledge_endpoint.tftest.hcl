mock_provider "aws" {
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_kms_key" { defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001" } }
}

variables {
  aws_account_id              = "123456789012"
  tools_facade_role_arn       = "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade"
  gateway_arn                 = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example"
  knowledge_mediator_role_arn = "arn:aws:iam::123456789012:role/fixture-gateway-service"
  eks_oidc_provider_arn       = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABCDEF"
}

run "accept_same_private_endpoint_as_release" {
  command = plan
  variables {
    knowledge_door_endpoint           = "https://fixtureapi.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/knowledge/mcp"
    knowledge_door_vpc_id             = "vpc-00000000000000000"
    knowledge_door_subnet_ids         = ["subnet-00000000000000001", "subnet-00000000000000002"]
    knowledge_door_security_group_ids = ["sg-00000000000000000"]
    knowledge_door_routing_domain     = "vpce-00000000000000000.execute-api.us-east-1.vpce.amazonaws.com"
  }
  assert {
    condition     = aws_iam_role.gateway.name == "adp-tools-dev-gateway"
    error_message = "Shared private knowledge configuration must not prevent the bootstrap foundation plan."
  }
}

run "empty_endpoint_stays_disabled" {
  command = plan
  assert {
    condition     = var.knowledge_door_endpoint == ""
    error_message = "Knowledge remains explicitly disabled by default in shared inputs."
  }
}

run "reject_legacy_direct_door" {
  command = plan
  variables { knowledge_door_endpoint = "https://door.example.invalid/mcp" }
  expect_failures = [var.knowledge_door_endpoint]
}

run "reject_legacy_direct_door_trailing_slash" {
  command = plan
  variables { knowledge_door_endpoint = "https://door.example.invalid/mcp/" }
  expect_failures = [var.knowledge_door_endpoint]
}

run "reject_non_execute_api_host" {
  command = plan
  variables { knowledge_door_endpoint = "https://door.example.invalid/dev/internal/v1/tools/knowledge/mcp" }
  expect_failures = [var.knowledge_door_endpoint]
}

run "reject_foreign_tool_route" {
  command = plan
  variables { knowledge_door_endpoint = "https://fixtureapi.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/github/mcp" }
  expect_failures = [var.knowledge_door_endpoint]
}

run "reject_plain_http" {
  command = plan
  variables { knowledge_door_endpoint = "http://fixtureapi.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/knowledge/mcp" }
  expect_failures = [var.knowledge_door_endpoint]
}

run "reject_url_credentials" {
  command = plan
  variables { knowledge_door_endpoint = "https://user@fixtureapi.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/knowledge/mcp" }
  expect_failures = [var.knowledge_door_endpoint]
}

run "reject_query" {
  command = plan
  variables { knowledge_door_endpoint = "https://fixtureapi.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/knowledge/mcp?user=other" }
  expect_failures = [var.knowledge_door_endpoint]
}

run "reject_fragment" {
  command = plan
  variables { knowledge_door_endpoint = "https://fixtureapi.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/knowledge/mcp#other" }
  expect_failures = [var.knowledge_door_endpoint]
}

run "reject_extra_path" {
  command = plan
  variables { knowledge_door_endpoint = "https://fixtureapi.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/knowledge/mcp/" }
  expect_failures = [var.knowledge_door_endpoint]
}
