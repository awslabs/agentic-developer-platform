mock_provider "aws" {
  mock_data "aws_lambda_function" {
    defaults = {
      arn  = "arn:aws:lambda:us-east-1:123456789012:function:adp-dev-shared-tools-shared"
      role = "arn:aws:iam::123456789012:role/adp-dev-shared-tools-shared-lambda"
      environment = [{ variables = {
        ADP_TOOLS_ASSERTION_KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001"
        ADP_TOOLS_FACADE_ROLE_ARN   = "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade"
      } }]
    }
  }
  mock_resource "aws_bedrockagentcore_gateway" {
    defaults = {
      gateway_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example"
      gateway_id  = "adp-tools-example"
      gateway_url = "https://tools.example.invalid/mcp"
      workload_identity_details = [{
        workload_identity_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:workload-identity-directory/default/workload-identity/adp-tools-example"
      }]
    }
  }
}

provider "aws" {
  alias                       = "oauth_secrets"
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
}

variables {
  aws_account_id            = "123456789012"
  tools_facade_role_arn     = "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade"
  gateway_role_arn          = "arn:aws:iam::123456789012:role/adp-tools-dev-gateway"
  gateway_kms_key_arn       = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001"
  oauth_provider_secret_ids = {}
}

run "gateway_uses_bootstrapped_authority" {
  command = apply
  assert {
    condition = (
      aws_bedrockagentcore_gateway.tools.role_arn == var.gateway_role_arn &&
      aws_bedrockagentcore_gateway.tools.kms_key_arn == var.gateway_kms_key_arn &&
      aws_bedrockagentcore_gateway.tools.authorizer_type == "AWS_IAM" &&
      aws_bedrockagentcore_gateway.tools.protocol_configuration[0].mcp[0].search_type == "SEMANTIC" &&
      length(aws_bedrockagentcore_gateway.tools.protocol_configuration[0].mcp[0].session_configuration) == 0 &&
      aws_bedrockagentcore_gateway.tools.protocol_configuration[0].mcp[0].streaming_configuration[0].enable_response_streaming == true &&
      length(aws_bedrockagentcore_gateway.tools.interceptor_configuration) == 0 &&
      length(aws_bedrockagentcore_gateway_target.tools) == 0 &&
      output.caller_role_arn == var.tools_facade_role_arn &&
      output.gateway_workload_identity_arn == "arn:aws:bedrock-agentcore:us-east-1:123456789012:workload-identity-directory/default/workload-identity/adp-tools-example"
    )
    error_message = "Release may configure AgentCore using only reviewed bootstrap role and key references."
  }
  assert {
    condition = (
      jsondecode(aws_bedrockagentcore_resource_policy.gateway.policy).Statement[0].Principal.AWS == var.tools_facade_role_arn &&
      jsondecode(aws_bedrockagentcore_resource_policy.gateway.policy).Statement[1].Principal.AWS == ["arn:aws:iam::123456789012:role/ADP-Agent-dev-platform"] &&
      jsondecode(aws_bedrockagentcore_resource_policy.gateway.policy).Statement[2].Effect == "Deny" &&
      jsondecode(aws_bedrockagentcore_resource_policy.gateway.policy).Statement[2].Condition.ArnNotEquals["aws:PrincipalArn"] == [var.tools_facade_role_arn, "arn:aws:iam::123456789012:role/ADP-Agent-dev-platform"]
    )
    error_message = "Gateway policy must admit only the façade and the environment's runtime-verification role, and reject every other caller."
  }
  assert {
    condition = (
      length(aws_bedrockagentcore_oauth2_credential_provider.user_connection) == 0 &&
      length(aws_bedrockagentcore_gateway_target.github_user) == 0 &&
      length(aws_bedrockagentcore_workload_identity.oauth_user) == 0 &&
      output.oauth_workload_name == "" &&
      output.gateway_oauth == null
    )
    error_message = "Gateway releases without OAuth apps must not provision Identity providers or a workload."
  }
}

run "accept_public_oauth_return_url" {
  command = plan
  variables { oauth_return_url = "https://dashboard.example.invalid/api/auth/connections/oauth/return" }
}

run "accept_direct_gateway_oauth_return_url" {
  command = plan
  variables { oauth_return_url = "https://gateway.example.invalid/auth/connections/oauth/return" }
}

run "reject_query_oauth_return_url" {
  command = plan
  variables { oauth_return_url = "https://dashboard.example.invalid/api/auth/connections/oauth/return?state=untrusted" }
  expect_failures = [var.oauth_return_url]
}

run "reject_other_oauth_return_url" {
  command = plan
  variables { oauth_return_url = "https://dashboard.example.invalid/api/api/auth/connections/oauth/return" }
  expect_failures = [var.oauth_return_url]
}

run "reject_public_or_malformed_github_mediator" {
  command = plan
  variables { github_mediator_endpoint = "https://api.github.com/user" }
  expect_failures = [var.github_mediator_endpoint]
}

run "reject_foreign_gateway_role" {
  command = plan
  variables { gateway_role_arn = "arn:aws:iam::999999999999:role/adp-tools-dev-gateway" }
  expect_failures = [var.gateway_role_arn]
}

run "reject_foreign_gateway_key" {
  command = plan
  variables { gateway_kms_key_arn = "arn:aws:kms:us-east-1:999999999999:key/00000000-0000-0000-0000-000000000001" }
  expect_failures = [var.gateway_kms_key_arn]
}

run "reject_unprotected_broker_target" {
  command = plan
  variables {
    credential_target_url     = "https://fixtureapi.execute-api.us-east-1.amazonaws.com/dev"
    credential_target_api_arn = "arn:aws:execute-api:us-east-1:123456789012:fixtureapi/dev"
  }
  expect_failures = [aws_bedrockagentcore_gateway_target.credentials]
}

run "reject_broker_stage_drift" {
  command = plan
  variables {
    credential_target_url     = "https://fixtureapi.execute-api.us-east-1.amazonaws.com/other"
    credential_target_api_arn = "arn:aws:execute-api:us-east-1:123456789012:fixtureapi/dev"
  }
  expect_failures = [var.credential_target_url]
}

run "protected_broker_target" {
  command = plan
  variables {
    interceptor_lambda_arn         = "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-dev-interceptor:live"
    interceptor_execution_role_arn = "arn:aws:iam::123456789012:role/adp-tools-dev-interceptor"
    assertion_signing_key_arn      = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001"
    credential_target_url          = "https://fixtureapi.execute-api.us-east-1.amazonaws.com/dev"
    credential_target_api_arn      = "arn:aws:execute-api:us-east-1:123456789012:fixtureapi/dev"
  }
  assert {
    condition = (
      aws_bedrockagentcore_gateway_target.credentials[0].credential_provider_configuration[0].gateway_iam_role[0].service == "execute-api" &&
      toset(aws_bedrockagentcore_gateway_target.credentials[0].metadata_configuration[0].allowed_request_headers) == toset(["x-adp-assertion"]) &&
      strcontains(aws_bedrockagentcore_gateway_target.credentials[0].target_configuration[0].mcp[0].open_api_schema[0].inline_payload[0].payload, var.credential_target_url)
    )
    error_message = "Broker target must use the authorized API stage, IAM SigV4 and a forwarded signed assertion."
  }
}

run "protected_shared_targets" {
  command = plan
  variables {
    interceptor_lambda_arn         = "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-dev-interceptor:live"
    interceptor_execution_role_arn = "arn:aws:iam::123456789012:role/adp-tools-dev-interceptor"
    assertion_signing_key_arn      = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001"
  }
  assert {
    condition = (
      length(aws_bedrockagentcore_gateway.tools.interceptor_configuration) == 1 &&
      aws_bedrockagentcore_gateway.tools.interceptor_configuration[0].interceptor[0].lambda[0].arn == var.interceptor_lambda_arn &&
      toset(keys(aws_bedrockagentcore_gateway_target.tools)) == toset(["websearch", "code-interpreter", "browser"]) &&
      length(aws_bedrockagentcore_gateway_target.tools["browser"].target_configuration[0].mcp[0].lambda[0].tool_schema[0].inline_payload) == 7
    )
    error_message = "Verified interceptor must precede the three protected shared targets."
  }
}

run "reject_unqualified_interceptor_placeholder" {
  command = plan
  variables {
    interceptor_lambda_arn         = "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-dev-interceptor"
    interceptor_execution_role_arn = "arn:aws:iam::123456789012:role/adp-tools-dev-interceptor"
  }
  expect_failures = [var.interceptor_lambda_arn]
}

run "reject_foreign_interceptor_alias" {
  command = plan
  variables {
    interceptor_lambda_arn         = "arn:aws:lambda:us-east-1:123456789012:function:other-interceptor:live"
    interceptor_execution_role_arn = "arn:aws:iam::123456789012:role/adp-tools-dev-interceptor"
  }
  expect_failures = [var.interceptor_lambda_arn]
}

run "knowledge_requires_private_execute_api_mediator" {
  command = plan
  variables {
    interceptor_lambda_arn            = "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-dev-interceptor:live"
    interceptor_execution_role_arn    = "arn:aws:iam::123456789012:role/adp-tools-dev-interceptor"
    assertion_signing_key_arn         = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001"
    knowledge_door_endpoint           = "https://fixtureapi.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/knowledge/mcp"
    knowledge_door_vpc_id             = "vpc-00000000000000000"
    knowledge_door_subnet_ids         = ["subnet-00000000000000001", "subnet-00000000000000002"]
    knowledge_door_security_group_ids = ["sg-00000000000000000"]
    knowledge_door_routing_domain     = "vpce-00000000000000000.execute-api.us-east-1.vpce.amazonaws.com"
  }
  assert {
    condition = (
      aws_bedrockagentcore_gateway_target.knowledge[0].credential_provider_configuration[0].gateway_iam_role[0].service == "execute-api" &&
      aws_bedrockagentcore_gateway_target.knowledge[0].target_configuration[0].mcp[0].mcp_server[0].listing_mode == "DYNAMIC" &&
      aws_bedrockagentcore_gateway.tools.protocol_configuration[0].mcp[0].search_type == null
    )
    error_message = "Knowledge must use IAM-authenticated dynamic private mediation, not direct Door transport."
  }
}

run "jira_uses_private_target_without_taking_over_connections_app" {
  command = plan
  variables {
    jira_mediator_endpoint            = "https://fixtureapi.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/jira/mcp"
    external_oauth_provider_arns      = { atlassian = "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/adp-tools-dev-atlassian-user" }
    interceptor_lambda_arn            = "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-dev-interceptor:live"
    interceptor_execution_role_arn    = "arn:aws:iam::123456789012:role/adp-tools-dev-interceptor"
    assertion_signing_key_arn         = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001"
    knowledge_door_endpoint           = "https://fixtureapi.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/knowledge/mcp"
    knowledge_door_vpc_id             = "vpc-00000000000000000"
    knowledge_door_subnet_ids         = ["subnet-00000000000000001", "subnet-00000000000000002"]
    knowledge_door_security_group_ids = ["sg-00000000000000000"]
    knowledge_door_routing_domain     = "vpce-00000000000000000.execute-api.us-east-1.vpce.amazonaws.com"
  }
  assert {
    condition = (
      length(aws_bedrockagentcore_oauth2_credential_provider.user_connection) == 0 &&
      length(aws_bedrockagentcore_workload_identity.oauth_user) == 0 &&
      aws_bedrockagentcore_gateway_target.jira_user[0].credential_provider_configuration[0].gateway_iam_role[0].service == "execute-api" &&
      aws_bedrockagentcore_gateway_target.jira_user[0].target_configuration[0].mcp[0].mcp_server[0].listing_mode == "DYNAMIC"
    )
    error_message = "Jira must use the private IAM mediator and leave Connections app ownership unchanged."
  }
}

run "reject_foreign_external_oauth_provider" {
  command = plan
  variables { external_oauth_provider_arns = { atlassian = "arn:aws:bedrock-agentcore:us-east-1:999999999999:token-vault/default/oauth2credentialprovider/adp-tools-dev-atlassian-user" } }
  expect_failures = [var.external_oauth_provider_arns]
}
