# Gateway agent registry entry for the guest role.
#
# Every guest -> gateway call is SigV4 through the gateway REST API's AWS_IAM
# /agent/{proxy+} route; the gateway maps the edge-verified caller ARN to a
# registry entry (src/auth/agent_registry.py) and refuses an unregistered role
# (403 agent_not_registered). The module's README has required this entry since
# #7222 ("register the guest role with the existing gateway IAM agent registry")
# but nothing created it: dev 2026-10-19 (#8223) the first Crew host that reached
# the gateway was refused. The entry is owned here, next to the role, and exists
# only while a gateway is approved (same gate as the guest's execute-api grant).
#
# requires_host_identity is explicit as the README demands (the gateway also
# derives it from the adp-dev-box-guest- role name); the gateway binds every such
# caller to a controller-written run for its instance, so org/team are the
# platform's agent plane like the other hosted-agent entries
# (modules/agent-factory/infra/agent-registry-seed.tf).

data "aws_ssm_parameter" "agent_registry_table" {
  count = local.gateway_execute_arn != "" ? 1 : 0
  name  = "/adp/${var.environment}/gateway/agent-registry-table"
}

resource "aws_dynamodb_table_item" "guest_agent" {
  count      = local.gateway_execute_arn != "" ? 1 : 0
  table_name = data.aws_ssm_parameter.agent_registry_table[0].value
  hash_key   = "agent_id"

  item = jsonencode({
    agent_id               = { S = "dev-box-guest-${var.deployment_id}" }
    role_arn               = { S = aws_iam_role.guest.arn }
    agent_name             = { S = "dev-box-guest-${var.deployment_id}" }
    org_id                 = { S = "__platform__" }
    team_id                = { S = "__agents__" }
    owner                  = { S = "dev-box" }
    scope                  = { S = "internal" }
    requires_host_identity = { BOOL = true }
    budget_config_id       = { S = "" }
    allowed_models         = { SS = ["*"] }
    status                 = { S = "active" }
    description            = { S = "Dev Box guest instances (${var.deployment_id}): SigV4 from the instance profile to the approved gateway host routes; bound to a controller-written Crew run per instance." }
    image_uri              = { S = "" }
    code_repo              = { S = "" }
    workflow_name          = { S = "" }
    created_at             = { S = "2026-10-19T00:00:00Z" }
    updated_at             = { S = "2026-10-19T00:00:00Z" }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

output "guest_agent_registry_id" { value = local.gateway_execute_arn != "" ? "dev-box-guest-${var.deployment_id}" : "" }
