# A shared worker role may also carry AdministratorAccess (#1619). Removing one
# inline Allow cannot restrict it. When authority is enabled, intersect EVERY
# identity policy with this boundary; reject privilege escalation and sensitive
# resource access explicitly, including resource-policy grants to role sessions.
# Flag-off deployments retain their existing permissions and bootstrap path.
locals {
  agent_worker_role_arn = var.agent_authority_enabled ? aws_iam_role.agent_authority_worker[0].arn : aws_iam_role.agent_scaledjob.arn
  agent_worker_sa_name  = var.agent_authority_enabled ? kubernetes_service_account.agent_authority_worker[0].metadata[0].name : kubernetes_service_account.agent_scaledjob_sa.metadata[0].name
  agent_authority_api_resources = [
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/*/agent/*",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/*/internal/v1/agent/*",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/github-installation-token",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/credential-assume-role",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/worker-task-credentials",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/credential-raw-read",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/proxy-request",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/credential-materialize",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/GET/internal/v1/user-credentials",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/provenance*",
  ]
  # Protected task delivery and archives cross the run-authenticated gateway.
  # No shared queue receipt or S3 bucket authority belongs in a worker role.
  agent_authority_boundary_allow = concat([
    for statement in local.agent_worker_scoped_policy.Statement : statement
    if contains([
      "CloudWatchLogGroups", "BootstrapLogging", "ProvenanceMetrics"
    ], statement.Sid)
    ], [
    {
      Sid      = "CorrelationUpdates"
      Effect   = "Allow"
      Action   = ["dynamodb:UpdateItem"]
      Resource = "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/${local.name_prefix}-correlation-pointers"
    },
    {
      Sid      = "CorrelationEncryption"
      Effect   = "Allow"
      Action   = ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
      Resource = aws_kms_key.dynamodb.arn
      Condition = {
        StringEquals = { "kms:ViaService" = "dynamodb.${var.aws_region}.amazonaws.com" }
      }
    },
    {
      Sid      = "AuthenticatedGateway"
      Effect   = "Allow"
      Action   = ["execute-api:Invoke"]
      Resource = local.agent_authority_api_resources
    },
    {
      Sid      = "Identity"
      Effect   = "Allow"
      Action   = ["sts:GetCallerIdentity"]
      Resource = "*"
    }
  ])
  agent_authority_boundary = {
    Version = "2012-10-17"
    Statement = concat(local.agent_authority_boundary_allow, [
      {
        # Protected model requests must cross the gateway's current policy and
        # shared spend checks. Explicit denial also covers resource-policy grants
        # to a role session and future additions to the inherited Allow list.
        Sid      = "DenyDirectModelInvocation"
        Effect   = "Deny"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:StartAsyncInvoke"]
        Resource = "*"
      },
      {
        # Blocks IAM/boundary changes, STS role chaining/federation, EKS access
        # entries, compute/job mutation, webhook invocation, SSM and state reads.
        Sid       = "DenyUnlistedActions"
        Effect    = "Deny"
        NotAction = distinct(flatten([for statement in local.agent_authority_boundary_allow : statement.Action]))
        Resource  = "*"
      },
      {
        Sid         = "DenyAuthorityData"
        Effect      = "Deny"
        Action      = ["dynamodb:*"]
        NotResource = "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/${local.name_prefix}-correlation-pointers"
      },
      {
        # Do not let a shared-key, App-private-key, platform-admin credential or
        # tenant-vault read restore privileges through another transport.
        Sid      = "DenyAllSecrets"
        Effect   = "Deny"
        Action   = ["secretsmanager:*"]
        Resource = "*"
      },
      {
        Sid      = "DenyDirectArtifacts"
        Effect   = "Deny"
        Action   = ["s3:*"]
        Resource = "*"
      },
      {
        Sid      = "DenyDirectQueues"
        Effect   = "Deny"
        Action   = ["sqs:*"]
        Resource = "*"
      },
      {
        Sid         = "DenyOtherEncryptionKeys"
        Effect      = "Deny"
        Action      = ["kms:*"]
        NotResource = aws_kms_key.dynamodb.arn
      },
      {
        Sid      = "DenyDirectKMS"
        Effect   = "Deny"
        Action   = ["kms:*"]
        Resource = "*"
        Condition = {
          StringNotEquals = { "kms:ViaService" = "dynamodb.${var.aws_region}.amazonaws.com" }
        }
      },
      {
        Sid         = "DenyOtherGatewayRoutes"
        Effect      = "Deny"
        Action      = ["execute-api:Invoke"]
        NotResource = local.agent_authority_api_resources
      }
    ])
  }
}

resource "aws_iam_policy" "agent_authority_boundary" {
  count       = local.agent_authority_provisioned ? 1 : 0
  name        = "${local.name_prefix}-agent-authority-boundary"
  description = "Maximum permissions for workers using verified delegated authority"
  policy      = jsonencode(local.agent_authority_boundary)
}

# The legacy role can have an EKS ClusterAdmin access entry. IAM boundaries do
# not constrain Kubernetes authorization. Use a new role AND a distinct subject:
# reusing the old SA would let its projected token assume the legacy role with
# the unsigned AssumeRoleWithWebIdentity API, bypassing the new boundary.
resource "aws_iam_role" "agent_authority_worker" {
  count                = local.agent_authority_provisioned ? 1 : 0
  name                 = "${local.name_prefix}-agent-authority-worker-role"
  permissions_boundary = aws_iam_policy.agent_authority_boundary[0].arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Federated = local.oidc_provider_arn }
        Action    = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${replace(local.oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:adp-agents:agent-authority-worker-sa"
            "${replace(local.oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
          }
        }
      },
      { Effect = "Allow", Principal = { AWS = aws_iam_role.keda_operator.arn }, Action = "sts:AssumeRole" }
    ]
  })
  tags = { Component = "hosted-agent-worker-authority" }
}

resource "aws_iam_role_policy" "agent_authority_worker" {
  count  = local.agent_authority_provisioned ? 1 : 0
  name   = "agent-worker-scoped-permissions"
  role   = aws_iam_role.agent_authority_worker[0].id
  policy = jsonencode(local.agent_authority_boundary)
}

resource "kubernetes_service_account" "agent_authority_worker" {
  count = local.agent_authority_provisioned ? 1 : 0
  metadata {
    name        = "agent-authority-worker-sa"
    namespace   = kubernetes_namespace.adp_agents.metadata[0].name
    annotations = { "eks.amazonaws.com/role-arn" = aws_iam_role.agent_authority_worker[0].arn }
  }
}

data "aws_ssm_parameter" "agent_authority_registry" {
  count = local.agent_authority_provisioned ? 1 : 0
  name  = "/adp/${var.environment}/gateway/agent-registry-table"
}

resource "aws_dynamodb_table_item" "agent_authority_worker" {
  count      = local.agent_authority_provisioned ? 1 : 0
  table_name = data.aws_ssm_parameter.agent_authority_registry[0].value
  hash_key   = "agent_id"
  item = jsonencode({
    agent_id              = { S = "authority-worker" }
    role_arn              = { S = aws_iam_role.agent_authority_worker[0].arn }
    agent_name            = { S = "authority-worker" }
    org_id                = { S = "__platform__" }
    team_id               = { S = "__agents__" }
    owner                 = { S = "platform" }
    scope                 = { S = "internal" }
    requires_run_identity = { BOOL = true }
    status                = { S = "active" }
    allowed_models        = { SS = ["*"] }
    credential_scopes     = { SS = ["credential:raw-read", "credential:materialize"] }
    budget_config_id      = { S = "" }
  })
}
