resource "aws_dynamodb_table_item" "l5_security_worker" {
  count      = var.enable_l5_security ? 1 : 0
  table_name = module.lambda_authorizer[0].agent_registry_table_name
  hash_key   = "agent_id"

  item = jsonencode({
    agent_id          = { S = "l5-worker" }
    role_arn          = { S = module.l5_security[0].worker_role_arn }
    iam_role_id       = { S = module.l5_security[0].worker_role_id }
    agent_name        = { S = "l5-worker" }
    org_id            = { S = "__platform__" }
    team_id           = { S = "__agents__" }
    owner             = { S = "platform" }
    scope             = { S = "internal" }
    credential_scopes = { SS = ["l5:credential-session"] }
    status            = { S = "active" }
  })
}
