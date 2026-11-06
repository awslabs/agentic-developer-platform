terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws      = { source = "hashicorp/aws", version = "~> 6.0" }
    random   = { source = "hashicorp/random", version = "~> 3.0" }
    external = { source = "hashicorp/external", version = "~> 2.3" }
  }
}

variable "enabled" {
  type    = bool
  default = false
}
variable "oauth_mediator_vendors" {
  type    = set(string)
  default = []
  validation {
    condition     = alltrue([for vendor in var.oauth_mediator_vendors : contains(["github", "atlassian", "slack"], vendor)])
    error_message = "Only supported user OAuth vendors may use the private mediator edge."
  }
}
variable "name_prefix" { type = string }
variable "stage" { type = string }
variable "region" { type = string }
variable "gateway_role_arn" { type = string }
variable "execute_api_vpc_endpoint_id" {
  type    = string
  default = ""
}
variable "gateway_vpc_link_id" { type = string }
variable "gateway_internal_alb_dns" { type = string }
variable "gateway_internal_alb_arn" { type = string }
variable "gateway_edge_provenance" {
  type      = string
  sensitive = true
}

# This independent secret proves that the IAM request traversed this PRIVATE
# edge. The ordinary gateway provenance secret also authenticates the final hop.
resource "random_password" "private_provenance" {
  count   = var.enabled ? 1 : 0
  length  = 64
  special = false
}

locals {
  route  = "/internal/v1/tools/knowledge/mcp"
  routes = concat([local.route], [for vendor in sort(tolist(var.oauth_mediator_vendors)) : "/internal/v1/tools/${vendor == "atlassian" ? "jira" : vendor}/mcp"])
  specification = var.enabled ? jsonencode({
    swagger = "2.0"
    info    = { title = "Private ADP knowledge mediator", version = "1.0" }
    schemes = ["https"]
    securityDefinitions = {
      sigv4 = { type = "apiKey", name = "Authorization", in = "header", "x-amazon-apigateway-authtype" = "awsSigv4" }
    }
    paths = {
      for route in local.routes : route => {
        post = {
          security                   = [{ sigv4 = [] }]
          "x-amazon-apigateway-auth" = { type = "AWS_IAM" }
          responses                  = { "200" = { description = "MCP response" } }
          "x-amazon-apigateway-integration" = {
            type                = "http_proxy"
            httpMethod          = "POST"
            connectionType      = "VPC_LINK"
            connectionId        = var.gateway_vpc_link_id
            integrationTarget   = var.gateway_internal_alb_arn
            uri                 = "http://${var.gateway_internal_alb_dns}${route}"
            timeoutInMillis     = 29000
            passthroughBehavior = "when_no_match"
            requestParameters = {
              "integration.request.header.X-Caller-Identity"               = "context.identity.userArn"
              "integration.request.header.X-Adp-Edge-Provenance"           = "'${var.gateway_edge_provenance}'"
              "integration.request.header.X-Adp-Knowledge-Edge-Provenance" = "'${random_password.private_provenance[0].result}'"
            }
          }
        }
      }
    }
  }) : null
}

data "external" "retained_handoff" {
  count   = var.enabled ? 1 : 0
  program = ["python3", "${path.module}/retained_handoff.py"]
  query = {
    region           = var.region
    gateway_role_arn = var.gateway_role_arn
    name             = "${var.name_prefix}-knowledge-private"
    stage            = var.stage
    endpoint_id      = local.execute_api_vpc_endpoint_id
    specification    = local.specification
  }
}

locals {
  retained_handoff       = var.enabled ? data.external.retained_handoff[0].result.prepared == "true" : false
  selected_specification = local.retained_handoff ? sensitive(data.external.retained_handoff[0].result.document) : local.specification
}

resource "aws_api_gateway_rest_api" "knowledge" {
  count = var.enabled ? 1 : 0
  name  = "${var.name_prefix}-knowledge-private"
  body  = local.selected_specification
  endpoint_configuration {
    types            = ["PRIVATE"]
    vpc_endpoint_ids = [local.execute_api_vpc_endpoint_id]
  }
  lifecycle {
    precondition {
      condition = (
        can(regex("^vpce-[a-f0-9]{8,17}$", local.execute_api_vpc_endpoint_id)) &&
        can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$", var.gateway_role_arn)) &&
        can(regex("^[A-Za-z0-9_-]+$", var.stage)) &&
        can(regex("^internal-[A-Za-z0-9.-]+\\.elb\\.amazonaws\\.com$", var.gateway_internal_alb_dns)) &&
        var.gateway_vpc_link_id != "" && var.gateway_internal_alb_arn != "" &&
        length(var.gateway_edge_provenance) >= 32
      )
      error_message = "Knowledge requires a private execute-api endpoint, internal Gateway ALB/VPC link, exact AgentCore role and protected edge proof."
    }
  }
}

resource "aws_api_gateway_rest_api_policy" "knowledge" {
  count       = var.enabled ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.knowledge[0].id
  policy = local.retained_handoff ? data.external.retained_handoff[0].result.api_policy : jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Deny", Principal = "*", Action = "execute-api:Invoke", Resource = "${aws_api_gateway_rest_api.knowledge[0].execution_arn}/*",
        Condition = { StringNotEquals = { "aws:SourceVpce" = local.execute_api_vpc_endpoint_id } }
      },
      {
        Effect    = "Deny", Principal = "*", Action = "execute-api:Invoke", Resource = "${aws_api_gateway_rest_api.knowledge[0].execution_arn}/*",
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = var.gateway_role_arn } }
      },
      {
        Effect   = "Allow", Principal = { AWS = var.gateway_role_arn }, Action = "execute-api:Invoke",
        Resource = [for route in local.routes : "${aws_api_gateway_rest_api.knowledge[0].execution_arn}/${var.stage}/POST${route}"]
      }
    ]
  })
}

resource "aws_iam_role_policy" "invoke" {
  count = var.enabled ? 1 : 0
  name  = "${var.name_prefix}-knowledge-private-invoke"
  role  = element(reverse(split("/", var.gateway_role_arn)), 0)
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Action = "execute-api:Invoke",
    Resource = [for route in local.routes : "${aws_api_gateway_rest_api.knowledge[0].execution_arn}/${var.stage}/POST${route}"]
  }] })
}

resource "aws_api_gateway_deployment" "knowledge" {
  count       = var.enabled ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.knowledge[0].id
  triggers    = { source = sha256(local.selected_specification), policy = sha256(aws_api_gateway_rest_api_policy.knowledge[0].policy) }
  depends_on  = [aws_api_gateway_rest_api_policy.knowledge]
  lifecycle { create_before_destroy = true }
}

resource "aws_api_gateway_stage" "knowledge" {
  count         = var.enabled ? 1 : 0
  rest_api_id   = aws_api_gateway_rest_api.knowledge[0].id
  deployment_id = local.retained_handoff ? data.external.retained_handoff[0].result.deployment_id : aws_api_gateway_deployment.knowledge[0].id
  stage_name    = var.stage
}

output "mediator_url" {
  value      = var.enabled ? "https://${aws_api_gateway_rest_api.knowledge[0].id}.execute-api.${var.region}.amazonaws.com/${var.stage}${local.route}" : ""
  depends_on = [aws_api_gateway_stage.knowledge, aws_iam_role_policy.invoke, aws_vpc_endpoint_policy.knowledge]
}
output "oauth_mediator_urls" {
  value = var.enabled ? {
    for vendor in var.oauth_mediator_vendors : vendor => "https://${aws_api_gateway_rest_api.knowledge[0].id}.execute-api.${var.region}.amazonaws.com/${var.stage}/internal/v1/tools/${vendor == "atlassian" ? "jira" : vendor}/mcp"
  } : {}
  depends_on = [aws_api_gateway_stage.knowledge, aws_iam_role_policy.invoke, aws_vpc_endpoint_policy.knowledge]
}
output "private_edge_provenance" {
  value     = var.enabled ? random_password.private_provenance[0].result : ""
  sensitive = true
}
