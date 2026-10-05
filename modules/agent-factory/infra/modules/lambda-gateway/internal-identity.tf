# Only provider ingestion receives platform identity-routing privileges. Agent
# workers are separate roles and receive no internal service capabilities.
variable "identity_resolver_url" {
  type    = string
  default = ""
}

variable "identity_registry_table" {
  type    = string
  default = ""
}

locals {
  identity_edge = try(regex("^https://([a-z0-9]+)\\.execute-api\\.([a-z0-9-]+)\\.amazonaws\\.com/([A-Za-z0-9_-]+)$", var.identity_resolver_url), ["", "", ""])
}

resource "aws_iam_role_policy" "ingest_identity" {
  count = var.identity_resolver_url != "" ? 1 : 0
  name  = "internal-identity-resolution"
  role  = aws_iam_role.ingest.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["execute-api:Invoke"]
      Resource = [for route in ["resolve-user", "issue-magic-link"] :
      "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:${local.identity_edge[0]}/${local.identity_edge[2]}/POST/internal/v1/${route}"]
    }]
  })
  lifecycle {
    precondition {
      condition     = local.identity_edge[0] != "" && local.identity_edge[1] == var.aws_region && var.identity_registry_table != ""
      error_message = "Identity resolution requires the regional IAM gateway endpoint and its registry."
    }
  }
}

resource "aws_dynamodb_table_item" "ingest_identity" {
  count      = var.identity_resolver_url != "" ? 1 : 0
  table_name = var.identity_registry_table
  hash_key   = "agent_id"
  item = jsonencode({
    agent_id          = { S = "provider-ingest" }
    role_arn          = { S = aws_iam_role.ingest.arn }
    agent_name        = { S = "provider-ingest" }
    org_id            = { S = "__platform__" }
    team_id           = { S = "__ingress__" }
    owner             = { S = "platform" }
    scope             = { S = "internal" }
    status            = { S = "active" }
    credential_scopes = { SS = ["internal:identity:resolve", "internal:identity:link", "internal:cross-tenant"] }
  })
}
