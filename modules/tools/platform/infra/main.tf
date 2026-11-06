resource "aws_bedrockagentcore_gateway_target" "user_connections" {
  for_each           = { for vendor, secret in var.oauth_provider_secret_ids : vendor => secret if vendor != "github" && vendor != "atlassian" && (vendor != "slack" || var.slack_mediator_endpoint == "") }
  name               = local.oauth_target_names[each.key]
  gateway_identifier = aws_bedrockagentcore_gateway.tools.gateway_id
  credential_provider_configuration {
    oauth {
      provider_arn       = aws_bedrockagentcore_oauth2_credential_provider.user_connection[each.key].credential_provider_arn
      grant_type         = "AUTHORIZATION_CODE"
      default_return_url = var.oauth_return_url
      scopes             = local.oauth_target_scopes[each.key]
    }
  }
  target_configuration {
    mcp {
      open_api_schema {
        inline_payload {
          payload = file("${path.module}/../schemas/${local.oauth_target_names[each.key]}.yaml")
        }
      }
    }
  }
  lifecycle {
    precondition {
      condition     = var.interceptor_lambda_arn != "" && var.assertion_signing_key_arn != ""
      error_message = "User connection targets require the verified signed-assertion interceptor."
    }
  }
  depends_on = [aws_bedrockagentcore_resource_policy.gateway]
}

resource "aws_bedrockagentcore_gateway_target" "github_user" {
  count              = var.github_mediator_endpoint == "" ? 0 : 1
  name               = "github"
  gateway_identifier = aws_bedrockagentcore_gateway.tools.gateway_id
  credential_provider_configuration {
    gateway_iam_role { service = "execute-api" }
  }
  target_configuration {
    mcp {
      mcp_server {
        endpoint     = var.github_mediator_endpoint
        listing_mode = "DYNAMIC"
        # resource_priority is unsupported by AgentCore for DYNAMIC targets.
      }
    }
  }
  private_endpoint {
    managed_vpc_resource {
      vpc_identifier           = var.knowledge_door_vpc_id
      subnet_ids               = var.knowledge_door_subnet_ids
      security_group_ids       = var.knowledge_door_security_group_ids
      endpoint_ip_address_type = "IPV4"
      routing_domain           = var.knowledge_door_routing_domain != "" ? var.knowledge_door_routing_domain : null
    }
  }
  lifecycle {
    precondition {
      condition = (
        (contains(keys(var.oauth_provider_secret_ids), "github") || contains(keys(var.external_oauth_provider_arns), "github")) &&
        var.interceptor_lambda_arn != "" && var.assertion_signing_key_arn != "" &&
        var.knowledge_door_endpoint != "" &&
        can(regex("^vpc-[a-f0-9]{8,17}$", var.knowledge_door_vpc_id)) &&
        length(var.knowledge_door_subnet_ids) >= 2 && length(var.knowledge_door_security_group_ids) > 0
      )
      error_message = "GitHub mediation requires the protected user OAuth provider, interceptor and existing private edge."
    }
  }
  depends_on = [aws_bedrockagentcore_resource_policy.gateway]
}

resource "aws_bedrockagentcore_gateway_target" "slack_user" {
  count              = var.slack_mediator_endpoint == "" ? 0 : 1
  name               = "slack"
  gateway_identifier = aws_bedrockagentcore_gateway.tools.gateway_id
  credential_provider_configuration {
    gateway_iam_role { service = "execute-api" }
  }
  target_configuration {
    mcp {
      mcp_server {
        endpoint     = var.slack_mediator_endpoint
        listing_mode = "DYNAMIC"
        # resource_priority is unsupported by AgentCore for DYNAMIC targets.
      }
    }
  }
  private_endpoint {
    managed_vpc_resource {
      vpc_identifier           = var.knowledge_door_vpc_id
      subnet_ids               = var.knowledge_door_subnet_ids
      security_group_ids       = var.knowledge_door_security_group_ids
      endpoint_ip_address_type = "IPV4"
      routing_domain           = var.knowledge_door_routing_domain != "" ? var.knowledge_door_routing_domain : null
    }
  }
  lifecycle {
    precondition {
      condition = (
        (contains(keys(var.oauth_provider_secret_ids), "slack") || contains(keys(var.external_oauth_provider_arns), "slack")) &&
        var.interceptor_lambda_arn != "" && var.assertion_signing_key_arn != "" &&
        var.knowledge_door_endpoint != "" &&
        can(regex("^vpc-[a-f0-9]{8,17}$", var.knowledge_door_vpc_id)) &&
        length(var.knowledge_door_subnet_ids) >= 2 && length(var.knowledge_door_security_group_ids) > 0
      )
      error_message = "Slack mediation requires the protected user OAuth provider, interceptor and existing private edge."
    }
  }
  depends_on = [aws_bedrockagentcore_resource_policy.gateway]
}

resource "aws_bedrockagentcore_gateway_target" "jira_user" {
  count              = var.jira_mediator_endpoint == "" ? 0 : 1
  name               = "jira"
  gateway_identifier = aws_bedrockagentcore_gateway.tools.gateway_id
  credential_provider_configuration {
    gateway_iam_role { service = "execute-api" }
  }
  target_configuration {
    mcp {
      mcp_server {
        endpoint     = var.jira_mediator_endpoint
        listing_mode = "DYNAMIC"
        # resource_priority is unsupported by AgentCore for DYNAMIC targets.
      }
    }
  }
  private_endpoint {
    managed_vpc_resource {
      vpc_identifier           = var.knowledge_door_vpc_id
      subnet_ids               = var.knowledge_door_subnet_ids
      security_group_ids       = var.knowledge_door_security_group_ids
      endpoint_ip_address_type = "IPV4"
      routing_domain           = var.knowledge_door_routing_domain != "" ? var.knowledge_door_routing_domain : null
    }
  }
  lifecycle {
    precondition {
      condition = (
        (contains(keys(var.oauth_provider_secret_ids), "atlassian") || contains(keys(var.external_oauth_provider_arns), "atlassian")) &&
        var.interceptor_lambda_arn != "" && var.assertion_signing_key_arn != "" &&
        var.knowledge_door_endpoint != "" &&
        can(regex("^vpc-[a-f0-9]{8,17}$", var.knowledge_door_vpc_id)) &&
        length(var.knowledge_door_subnet_ids) >= 2 && length(var.knowledge_door_security_group_ids) > 0
      )
      error_message = "Jira mediation requires the protected user OAuth provider, interceptor and existing private edge."
    }
  }
  depends_on = [aws_bedrockagentcore_resource_policy.gateway]
}

resource "aws_bedrockagentcore_gateway_target" "credentials" {
  count              = var.credential_target_url == "" ? 0 : 1
  name               = "credentials"
  gateway_identifier = aws_bedrockagentcore_gateway.tools.gateway_id
  credential_provider_configuration {
    gateway_iam_role { service = "execute-api" }
  }
  metadata_configuration {
    allowed_request_headers = ["x-adp-assertion"]
  }
  target_configuration {
    mcp {
      open_api_schema {
        inline_payload {
          payload = replace(file("${path.module}/../schemas/credentials.yaml"), "https://broker.example.invalid", var.credential_target_url)
        }
      }
    }
  }
  lifecycle {
    precondition {
      condition     = var.interceptor_lambda_arn != "" && var.assertion_signing_key_arn != "" && var.tools_facade_role_arn != ""
      error_message = "Credential tools require the verified assertion interceptor, signer, and façade role."
    }
  }
  depends_on = [aws_bedrockagentcore_resource_policy.gateway]
}

resource "aws_bedrockagentcore_gateway_target" "knowledge" {
  count              = var.knowledge_door_endpoint == "" ? 0 : 1
  name               = "knowledge"
  gateway_identifier = aws_bedrockagentcore_gateway.tools.gateway_id
  credential_provider_configuration {
    gateway_iam_role { service = "execute-api" }
  }
  target_configuration {
    mcp {
      mcp_server {
        endpoint     = var.knowledge_door_endpoint
        listing_mode = "DYNAMIC"
        # resource_priority is unsupported by AgentCore for DYNAMIC targets.
      }
    }
  }
  private_endpoint {
    managed_vpc_resource {
      vpc_identifier           = var.knowledge_door_vpc_id
      subnet_ids               = var.knowledge_door_subnet_ids
      security_group_ids       = var.knowledge_door_security_group_ids
      endpoint_ip_address_type = "IPV4"
      routing_domain           = var.knowledge_door_routing_domain != "" ? var.knowledge_door_routing_domain : null
    }
  }
  lifecycle {
    precondition {
      condition = (
        var.interceptor_lambda_arn != "" &&
        can(regex("^https://[A-Za-z0-9-]+\\.execute-api\\.[a-z0-9-]+\\.amazonaws\\.com/[A-Za-z0-9_-]+/internal/v1/tools/knowledge/mcp$", var.knowledge_door_endpoint)) &&
        can(regex("^[A-Za-z0-9.-]+$", var.knowledge_door_routing_domain)) &&
        var.knowledge_door_routing_domain != "" &&
        can(regex("^vpc-[a-f0-9]{8,17}$", var.knowledge_door_vpc_id)) &&
        length(var.knowledge_door_subnet_ids) >= 2 &&
        alltrue([for id in var.knowledge_door_subnet_ids : can(regex("^subnet-[a-f0-9]{8,17}$", id))]) &&
        length(var.knowledge_door_security_group_ids) > 0 &&
        alltrue([for id in var.knowledge_door_security_group_ids : can(regex("^sg-[a-f0-9]{8,17}$", id))])
      )
      error_message = "Knowledge requires an interceptor and a configured private VPC route to Door."
    }
  }
  depends_on = [aws_bedrockagentcore_resource_policy.gateway]
}

resource "aws_bedrockagentcore_gateway" "tools" {
  name            = "adp-tools"
  description     = "ADP tools gateway (${var.environment})"
  role_arn        = var.gateway_role_arn
  authorizer_type = "AWS_IAM"
  protocol_type   = "MCP"
  kms_key_arn     = var.gateway_kms_key_arn
  protocol_configuration {
    mcp {
      # AgentCore rejects DYNAMIC MCP targets when semantic tool search is on.
      # ADP discovers via tools/list and invokes explicit names; websearch is
      # an independent target and does not depend on semantic tool search.
      search_type = anytrue([
        var.knowledge_door_endpoint != "", var.github_mediator_endpoint != "",
        var.jira_mediator_endpoint != "", var.slack_mediator_endpoint != "",
      ]) ? null : "SEMANTIC"
      # No MCP session configuration: the façade signs every request with a
      # per-call assertion and the interceptor binds it to exactly that
      # tools/list or tools/call. A stateful gateway would demand an
      # `initialize` handshake and an Mcp-Session-Id header on every call,
      # which neither the façade nor the interceptor contract models.
      streaming_configuration {
        enable_response_streaming = true
      }
    }
  }
  dynamic "interceptor_configuration" {
    for_each = var.interceptor_lambda_arn != "" ? [var.interceptor_lambda_arn] : []
    content {
      interception_points = ["REQUEST", "RESPONSE"]
      interceptor {
        lambda {
          arn = interceptor_configuration.value
        }
      }
      input_configuration {
        pass_request_headers = true
      }
    }
  }
  lifecycle {
    precondition {
      condition     = (var.cognito_user_pool_id == "") == (var.discovery_url == "")
      error_message = "Supply both existing login inputs together, or leave both empty."
    }
  }
}

locals {
  # Callers admitted to InvokeGateway: the façade (tool execution) and the
  # engine's verification roles (tools/list only — the interceptor refuses any
  # call that lacks a façade assertion, so these roles can discover, not invoke).
  # The operator connection role for this environment is always admitted so an
  # operations deployment can be runtime-verified without a per-environment tfvar.
  gateway_verifier_role_arns = distinct(concat(
    ["arn:aws:iam::${var.aws_account_id}:role/ADP-Agent-${var.environment}-platform"],
    var.gateway_verifier_role_arns,
  ))
  gateway_invokers = concat([var.tools_facade_role_arn], local.gateway_verifier_role_arns)
  invocation_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowToolsFacade"
        Effect    = "Allow"
        Principal = { AWS = var.tools_facade_role_arn }
        Action    = "bedrock-agentcore:InvokeGateway"
        Resource  = aws_bedrockagentcore_gateway.tools.gateway_arn
      },
      {
        Sid       = "AllowRuntimeVerifiers"
        Effect    = "Allow"
        Principal = { AWS = local.gateway_verifier_role_arns }
        Action    = "bedrock-agentcore:InvokeGateway"
        Resource  = aws_bedrockagentcore_gateway.tools.gateway_arn
      },
      {
        Sid       = "DenyOtherCallers"
        Effect    = "Deny"
        Principal = "*"
        Action    = "bedrock-agentcore:InvokeGateway"
        Resource  = aws_bedrockagentcore_gateway.tools.gateway_arn
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = local.gateway_invokers } }
      }
    ]
  })
}

resource "aws_bedrockagentcore_resource_policy" "gateway" {
  # The admitted caller must exist before a policy names it; when this stack owns
  # the façade role the apply must order them.
  resource_arn = aws_bedrockagentcore_gateway.tools.gateway_arn
  policy       = local.invocation_policy
}

resource "aws_bedrockagentcore_gateway_target" "tools" {
  for_each           = local.tool_targets
  name               = each.value.name
  gateway_identifier = aws_bedrockagentcore_gateway.tools.gateway_id
  credential_provider_configuration {
    gateway_iam_role {}
  }
  target_configuration {
    mcp {
      lambda {
        lambda_arn = each.value.lambda_arn
        tool_schema {
          dynamic "inline_payload" {
            for_each = lookup(local.lambda_tool_schemas, each.key, [{
              name        = each.value.tool_name
              description = each.value.tool_description
              properties  = each.value.input_properties
            }])
            content {
              name        = inline_payload.value.name
              description = inline_payload.value.description
              input_schema {
                type = "object"
                dynamic "property" {
                  for_each = inline_payload.value.properties
                  content {
                    name        = property.value.name
                    type        = property.value.type
                    description = property.value.description
                    required    = property.value.required
                  }
                }
              }
            }
          }
        }
      }
    }
  }
  lifecycle {
    precondition {
      condition     = !contains(keys(local.shared_tools_targets), each.key) || !contains([for target in var.targets : target.name], each.key)
      error_message = "Shared tools targets cannot be replaced by explicit target inputs."
    }
    precondition {
      condition = !contains(keys(local.shared_tools_targets), each.key) || (
        try(data.aws_lambda_function.shared_tools[0].environment[0].variables["ADP_TOOLS_ASSERTION_KEY_ARN"], "") == var.assertion_signing_key_arn &&
        try(data.aws_lambda_function.shared_tools[0].environment[0].variables["ADP_TOOLS_FACADE_ROLE_ARN"], "") == var.tools_facade_role_arn &&
        data.aws_lambda_function.shared_tools[0].role == local.shared_tools_role_arn
      )
      error_message = "Shared Lambda must verify this gateway signing key and facade role before exposing its tools."
    }
  }
  depends_on = [aws_bedrockagentcore_resource_policy.gateway]
}
