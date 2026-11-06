# Optional runtime wiring for the existing separate adapter. Preparing this
# boundary does not create consent, switch MCP routes or activate a release.
variable "observation_runtime" {
  type = object({
    vpc_id                  = string
    subnet_ids              = set(string)
    core_security_group_ids = set(string)
    vpc_link_id             = string
    adapter_alb_arn         = string
    core_api_id             = string
    core_api_stage          = string
    core_role_arn           = string
    assertion_key_arn       = optional(string)
    registry_table          = string
    tenant_id               = string
    release = optional(object({
      contract_version   = string
      source_revision    = string
      image_digest       = string
      group              = string
      group_generation   = number
      backend_generation = number
    }))
  })
  default     = null
  description = "Confirmed runtime bindings; release stays null until a matching observation configuration and image have been independently verified."
  validation {
    condition = var.observation_runtime == null ? true : try(
      can(regex("^vpc-[a-f0-9]{8,17}$", var.observation_runtime.vpc_id)) &&
      length(var.observation_runtime.subnet_ids) >= 2 &&
      alltrue([for id in var.observation_runtime.subnet_ids : can(regex("^subnet-[a-f0-9]{8,17}$", id))]) &&
      length(var.observation_runtime.core_security_group_ids) > 0 &&
      alltrue([for id in var.observation_runtime.core_security_group_ids : can(regex("^sg-[a-f0-9]{8,17}$", id))]) &&
      can(regex("^[a-z0-9]+$", var.observation_runtime.vpc_link_id)) &&
      can(regex("^arn:aws:elasticloadbalancing:${var.aws_region}:${var.aws_account_id}:loadbalancer/app/[^/]+/[a-z0-9]+$", var.observation_runtime.adapter_alb_arn)) &&
      can(regex("^[a-z0-9]+$", var.observation_runtime.core_api_id)) &&
      can(regex("^[A-Za-z0-9_-]+$", var.observation_runtime.core_api_stage)) &&
      can(regex("^arn:aws:iam::${var.aws_account_id}:role/[A-Za-z0-9_+=,.@-]+$", var.observation_runtime.core_role_arn)) &&
      var.observation_runtime.core_role_arn != "arn:aws:iam::${var.aws_account_id}:role/${var.runtime_role_name}" &&
      (var.observation_runtime.assertion_key_arn == null || can(regex("^arn:aws:kms:${var.aws_region}:${var.aws_account_id}:key/[a-f0-9-]{36}$", var.observation_runtime.assertion_key_arn))) &&
      can(regex("^[A-Za-z0-9_.-]{3,255}$", var.observation_runtime.registry_table)) &&
      can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", var.observation_runtime.tenant_id)) &&
      var.private_edge_enabled, false
    )
    error_message = "Observation requires explicit same-account roles, key, internal ALB, private network and tenant bindings, with the adapter ingress enabled."
  }
  validation {
    condition = try(var.observation_runtime.release, null) == null ? true : try(
      var.observation_runtime.release.contract_version == "1.0" &&
      can(regex("^[0-9a-f]{40}$", var.observation_runtime.release.source_revision)) &&
      can(regex("^sha256:[0-9a-f]{64}$", var.observation_runtime.release.image_digest)) &&
      can(regex("^[a-z][a-z0-9-]{0,63}$", var.observation_runtime.release.group)) &&
      alltrue([for value in [var.observation_runtime.release.group_generation, var.observation_runtime.release.backend_generation] : value >= 1 && floor(value) == value]), false
    )
    error_message = "Activation requires an immutable, versioned adapter release."
  }
}

locals {
  observation       = var.observation_runtime == null ? {} : { runtime = var.observation_runtime }
  observation_route = "/internal/v1/tool-adapters/observe"
}

data "aws_lb" "observation" {
  for_each = local.observation
  arn      = each.value.adapter_alb_arn
  lifecycle {
    postcondition {
      condition     = self.internal && self.vpc_id == var.observation_runtime.vpc_id && self.dns_name == try(kubernetes_ingress_v1.adapter_private[0].status[0].load_balancer[0].ingress[0].hostname, "")
      error_message = "The observation edge must target this adapter's observed internal ALB in the selected VPC."
    }
  }
}

data "aws_subnet" "observation" {
  for_each = var.observation_runtime == null ? toset([]) : var.observation_runtime.subnet_ids
  id       = each.value
  lifecycle {
    postcondition {
      condition     = self.vpc_id == var.observation_runtime.vpc_id && !self.map_public_ip_on_launch
      error_message = "Observation endpoint subnets must be private and belong to the selected VPC."
    }
  }
}

data "aws_security_group" "observation_core" {
  for_each = var.observation_runtime == null ? toset([]) : var.observation_runtime.core_security_group_ids
  id       = each.value
  lifecycle {
    postcondition {
      condition     = self.vpc_id == var.observation_runtime.vpc_id
      error_message = "Core source security groups must belong to the selected VPC."
    }
  }
}

resource "random_password" "observation_edge" {
  for_each = local.observation
  length   = 64
  special  = false
}

resource "aws_security_group" "observation_endpoint" {
  for_each    = local.observation
  name_prefix = "adp-${var.environment}-adapter-observation-"
  description = "Core HTTPS access to the private adapter observation API"
  vpc_id      = each.value.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "observation_core" {
  for_each                     = data.aws_security_group.observation_core
  security_group_id            = aws_security_group.observation_endpoint["runtime"].id
  referenced_security_group_id = each.value.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_endpoint" "observation" {
  for_each            = local.observation
  vpc_id              = each.value.vpc_id
  subnet_ids          = [for subnet in data.aws_subnet.observation : subnet.id]
  service_name        = "com.amazonaws.${var.aws_region}.execute-api"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = false
  security_group_ids  = [aws_security_group.observation_endpoint[each.key].id]
}

locals {
  observation_specifications = { for name, settings in local.observation : name => jsonencode({
    swagger             = "2.0"
    info                = { title = "ADP adapter observation", version = "1.0" }
    schemes             = ["https"]
    securityDefinitions = { sigv4 = { type = "apiKey", name = "Authorization", in = "header", "x-amazon-apigateway-authtype" = "awsSigv4" } }
    paths = { (local.observation_route) = { post = {
      security                   = [{ sigv4 = [] }]
      "x-amazon-apigateway-auth" = { type = "AWS_IAM" }
      responses                  = { "200" = { description = "Owner-bound observation" } }
      "x-amazon-apigateway-integration" = {
        type            = "http_proxy", httpMethod = "POST", connectionType = "VPC_LINK"
        connectionId    = settings.vpc_link_id, integrationTarget = settings.adapter_alb_arn
        uri             = "http://${data.aws_lb.observation[name].dns_name}${local.observation_route}"
        timeoutInMillis = 29000, passthroughBehavior = "when_no_match"
        requestParameters = {
          # This canonical role is safe only because the API resource policy
          # denies EVERY other principal, including callers with broad IAM grants.
          "integration.request.header.X-Caller-Identity"     = "'${settings.core_role_arn}'"
          "integration.request.header.X-Adp-Edge-Provenance" = "'${random_password.observation_edge[name].result}'"
        }
      }
    } } }
  }) }
}

resource "aws_api_gateway_rest_api" "observation" {
  for_each = local.observation
  name     = "adp-${var.environment}-adapter-observation"
  body     = local.observation_specifications[each.key]
  endpoint_configuration {
    types            = ["PRIVATE"]
    vpc_endpoint_ids = [aws_vpc_endpoint.observation[each.key].id]
  }
}

resource "aws_api_gateway_rest_api_policy" "observation" {
  for_each    = local.observation
  rest_api_id = aws_api_gateway_rest_api.observation[each.key].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Deny", Principal = "*", Action = "execute-api:Invoke", Resource = "${aws_api_gateway_rest_api.observation[each.key].execution_arn}/*",
    Condition = { StringNotEquals = { "aws:SourceVpce" = aws_vpc_endpoint.observation[each.key].id } } },
    { Effect = "Deny", Principal = "*", Action = "execute-api:Invoke", Resource = "${aws_api_gateway_rest_api.observation[each.key].execution_arn}/*",
    Condition = { ArnNotEquals = { "aws:PrincipalArn" = each.value.core_role_arn } } },
    { Effect = "Allow", Principal = { AWS = each.value.core_role_arn }, Action = "execute-api:Invoke",
    Resource = "${aws_api_gateway_rest_api.observation[each.key].execution_arn}/${var.environment}/POST${local.observation_route}" }
  ] })
}

resource "aws_vpc_endpoint_policy" "observation" {
  for_each        = local.observation
  vpc_endpoint_id = aws_vpc_endpoint.observation[each.key].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Principal = { AWS = each.value.core_role_arn }, Action = "execute-api:Invoke",
    Resource = "${aws_api_gateway_rest_api.observation[each.key].execution_arn}/${var.environment}/POST${local.observation_route}"
  }] })
}

resource "aws_api_gateway_deployment" "observation" {
  for_each    = local.observation
  rest_api_id = aws_api_gateway_rest_api.observation[each.key].id
  triggers    = { source = sha256(local.observation_specifications[each.key]), policy = sha256(aws_api_gateway_rest_api_policy.observation[each.key].policy) }
  lifecycle { create_before_destroy = true }
  depends_on = [aws_api_gateway_rest_api_policy.observation]
}

resource "aws_api_gateway_stage" "observation" {
  for_each      = local.observation
  rest_api_id   = aws_api_gateway_rest_api.observation[each.key].id
  deployment_id = aws_api_gateway_deployment.observation[each.key].id
  stage_name    = var.environment
}

resource "aws_iam_role_policy" "observation_core" {
  for_each = local.observation
  name     = "adp-tool-adapter-observation"
  role     = element(reverse(split("/", each.value.core_role_arn)), 0)
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["kms:Sign", "kms:Verify"], Resource = local.observation_assertion_keys[each.key] },
    { Effect = "Allow", Action = "execute-api:Invoke", Resource = "${aws_api_gateway_rest_api.observation[each.key].execution_arn}/${var.environment}/POST${local.observation_route}" }
  ] })
}

resource "aws_iam_role_policy" "observation_adapter" {
  for_each = local.observation
  name     = "adp-tool-adapter-authority"
  role     = aws_iam_role.runtime.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "kms:Verify", Resource = local.observation_assertion_keys[each.key] },
    { Effect = "Allow", Action = "execute-api:Invoke", Resource = [for operation in ["authorize", "dispatch", "catalogue", "catalogue/legacy"] :
    "arn:aws:execute-api:${var.aws_region}:${var.aws_account_id}:${each.value.core_api_id}/${each.value.core_api_stage}/POST/internal/v1/tool-adapters/${operation}"] }
  ] })
}

resource "aws_dynamodb_table_item" "observation_adapter" {
  for_each   = local.observation
  table_name = each.value.registry_table
  hash_key   = "agent_id"
  item = jsonencode({
    agent_id          = { S = "tool-adapters-${var.environment}" }, agent_name = { S = "tool-adapters" },
    role_arn          = { S = aws_iam_role.runtime.arn }, org_id = { S = each.value.tenant_id },
    team_id           = { S = "__tools__" }, owner = { S = "platform" }, scope = { S = "internal" }, status = { S = "active" },
    credential_scopes = { SS = ["tools:adapter:observe", "tools:adapter:authorize", "tools:adapter:dispatch"] }
  })
}

locals {
  observation_bindings = { for name, settings in local.observation : name => {
    # API/endpoint association DNS works with private DNS disabled, preserving
    # connectivity to the existing regional core API.
    observe_url       = "https://${aws_api_gateway_rest_api.observation[name].id}-${aws_vpc_endpoint.observation[name].id}.execute-api.${var.aws_region}.amazonaws.com/${var.environment}${local.observation_route}"
    assertion_key_arn = local.observation_assertion_keys[name]
    core_role_arn     = settings.core_role_arn
    adapter_role_arn  = aws_iam_role.runtime.arn
    release           = settings.release
  } }
}

resource "aws_ssm_parameter" "observation_gateway" {
  for_each   = { for name, settings in local.observation : name => settings if settings.release != null }
  name       = "/adp/${var.environment}/gateway/tool-adapter-config"
  type       = "String"
  value      = jsonencode(local.observation_bindings[each.key])
  depends_on = [aws_api_gateway_stage.observation, aws_vpc_endpoint_policy.observation, aws_iam_role_policy.observation_core, aws_iam_role_policy.observation_adapter, aws_dynamodb_table_item.observation_adapter]
}

output "observation_runtime" {
  description = "Prepared deployment bindings; these do not establish OAuth consent or activate connector routes."
  value = var.observation_runtime == null ? null : {
    gateway                = local.observation_bindings["runtime"]
    core_authorize_url     = "https://${var.observation_runtime.core_api_id}.execute-api.${var.aws_region}.amazonaws.com/${var.observation_runtime.core_api_stage}/internal/v1/tool-adapters/authorize"
    edge_provenance_sha256 = nonsensitive(sha256(random_password.observation_edge["runtime"].result))
  }
}
