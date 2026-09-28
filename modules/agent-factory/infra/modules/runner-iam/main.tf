# =============================================================================
# Runner IAM — IRSA role for GitHub Actions runner pods
# =============================================================================
# Extracted from github-actions-runner/infrastructure/iam.tf
# Uses the shared EKS OIDC provider instead of creating a new one.
#
# Historical policy names are retained for in-place migration.
# Deployment grants now live in platform/automation-infra.
# Previous policy split rationale (Issue #1204):
# The scoped runner policy from synthesis #1200 is ~11 KB, exceeding the AWS
# managed-policy limit of 10,240 bytes. Split into two managed policies:
#   - runner-base: infrastructure-heavy statements (EC2, EKS, IAM, KMS,
#     CloudTrail, EventBridge, CodeBuild, ECR, ELB)
#   - runner-services: application-deploy statements (S3, SecretsManager, SSM,
#     CloudFront, Lambda, SQS, DynamoDB, Logs, WAFv2, APIGateway, STS, Bedrock,
#     ExecuteAPI, CloudWatch)
# =============================================================================

data "aws_caller_identity" "current" {}

locals {
  # The runner's own namespace is always trusted; extras are opt-in and reviewed.
  # distinct() so listing runner_namespace in the extras is harmless rather than
  # producing a duplicate condition value.
  runner_trusted_namespaces = distinct(concat(
    [var.runner_namespace],
    var.runner_trusted_namespaces,
  ))

  privilege_escalation_actions = [
    # IAM mutations are denied by the shared API ceiling. The sole IAM API it
    # permits is PassRole for the service-only gateway PR validation identity.
    "sts:AssumeRole", "sts:AssumeRoleWithSAML", "sts:AssumeRoleWithWebIdentity",
  ]

  tenant_vault_secret_arns = [
    "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/users/*",
    "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/teams/*",
    "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/orgs/*",
    "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/domain-apps/*",
    "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/*/tenants/*",
  ]
}

# Permissions boundary — scoped version (Issue #1204, #596 fix)
resource "aws_iam_policy" "runner_boundary" {
  name        = "${var.name_prefix}-runner-boundary"
  description = "Permissions boundary for GitHub runner pods"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(module.runtime_policy.boundary, [
      {
        Sid      = "DenyPrivilegeEscalation"
        Effect   = "Deny"
        Action   = local.privilege_escalation_actions
        Resource = "*"
      },
      {
        # Cross-tenant vault lockout. The runner may manage deployment secrets
        # under explicitly scoped deployment paths; tenant secrets also
        # include environment-scoped adp/<env>/tenants/ paths. Deny the entire Secrets Manager API on those
        # paths so writes, deletion, replication, rotation and resource-policy
        # changes cannot be reintroduced by another attached policy.
        Sid      = "DenyTenantVaultSecrets"
        Effect   = "Deny"
        Action   = "secretsmanager:*"
        Resource = local.tenant_vault_secret_arns
      }
    ])
  })
}

# IRSA role for runner pods
resource "aws_iam_role" "runner" {
  name                 = var.runner_role_name != "" ? var.runner_role_name : "${var.name_prefix}-runner-role"
  permissions_boundary = aws_iam_policy.runner_boundary.arn

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = var.oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      # A18 (#5674): exact match, replacing StringLike on
      # "system:serviceaccount:${var.runner_namespace}*:github-runner-sa".
      #
      # Note the trailing "*" sat OUTSIDE the interpolation, so with the default
      # runner_namespace the pattern was "arc-runners*" — matching not just
      # "arc-runners" but every "arc-runners-<anything>" namespace. Creating a
      # conventionally named namespace was therefore the same act as being
      # trusted by this role, which holds the wide deploy grants in
      # runner_base/runner_services. That is an authorization decision made by a
      # naming convention rather than by review.
      #
      # runner_trusted_namespaces defaults to exactly [var.runner_namespace], so
      # the intended runner keeps working and nothing else is admitted. A second
      # runner namespace is added there explicitly and applied — the review step
      # the wildcard skipped.
      #
      # `aud` is asserted because a federated trust policy conditioned only on
      # `sub` accepts a token minted for a different audience.
      Condition = {
        StringEquals = {
          "${replace(var.oidc_issuer, "https://", "")}:sub" = [
            for namespace in local.runner_trusted_namespaces :
            "system:serviceaccount:${namespace}:github-runner-sa"
          ]
          "${replace(var.oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

# Managed names remain stable; the in-place update removes old deployment grants.
resource "aws_iam_policy" "runner_base" {
  # IAM descriptions are immutable; retain historical metadata for in-place rollout.
  description = "Runner scoped policy — infrastructure (EC2, EKS, IAM, KMS, CloudTrail, EventBridge, CodeBuild, ECR, ELB)"
  name        = "${var.name_prefix}-runner-base"
  policy      = jsonencode({ Version = "2012-10-17", Statement = module.runtime_policy.grants })
}
resource "aws_iam_policy" "runner_services" {
  # IAM descriptions are immutable; retain historical metadata for in-place rollout.
  description = "Runner scoped policy — application deploy (S3, Secrets, SSM, CloudFront, Lambda, SQS, DynamoDB, Logs, WAF, STS, Bedrock, CloudWatch)"
  name        = "${var.name_prefix}-runner-services"
  policy      = jsonencode({ Version = "2012-10-17", Statement = module.runtime_policy.grants })
}
resource "aws_iam_role_policy_attachment" "runner_base" {
  role       = aws_iam_role.runner.name
  policy_arn = aws_iam_policy.runner_base.arn
}
resource "aws_iam_role_policy_attachment" "runner_services" {
  role       = aws_iam_role.runner.name
  policy_arn = aws_iam_policy.runner_services.arn
}
module "runtime_policy" {
  environment               = var.environment
  gateway_execution_arns    = var.gateway_execution_arns
  source                    = "../runner-runtime-policy"
  account_id                = data.aws_caller_identity.current.account_id
  aws_region                = var.aws_region
  name_prefix               = var.name_prefix
  transport_secret_arns     = var.transport_secret_arns
  transport_secret_kms_arns = var.transport_secret_kms_arns
}
