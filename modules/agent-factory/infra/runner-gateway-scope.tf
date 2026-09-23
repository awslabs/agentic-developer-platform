# Fresh installs already deploy the gateway before the factory. Derive its
# concrete API/stage from operator-owned configuration, never from a job body.
# Explicit route inputs remain available for reviewed upgrade/custom routes.
data "aws_ssm_parameter" "runner_gateway_endpoint" {
  count           = var.runner_gateway_execution_arns == null && var.gateway_deployed ? 1 : 0
  name            = "/adp/${var.environment}/gateway/apigw-invoke-url"
  with_decryption = false
  lifecycle {
    postcondition {
      condition     = can(regex("^https://([a-z0-9]+)[.]execute-api[.]${var.aws_region}[.]amazonaws[.]com/([A-Za-z0-9_$-]+)/?$", self.value))
      error_message = "The configured gateway must name one regional API and exact stage."
    }
  }
}
locals {
  runner_gateway_parts          = length(data.aws_ssm_parameter.runner_gateway_endpoint) == 0 ? [] : regex("^https://([a-z0-9]+)[.]execute-api[.]${var.aws_region}[.]amazonaws[.]com/([A-Za-z0-9_$-]+)/?$", nonsensitive(data.aws_ssm_parameter.runner_gateway_endpoint[0].value))
  runner_gateway_execution_arns = var.runner_gateway_execution_arns != null ? var.runner_gateway_execution_arns : length(local.runner_gateway_parts) == 0 ? [] : [for route in ["POST/agent/*", "POST/internal/*", "GET/internal/*"] : "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:${local.runner_gateway_parts[0]}/${local.runner_gateway_parts[1]}/${route}"]
}
