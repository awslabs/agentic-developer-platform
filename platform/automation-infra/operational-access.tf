# Exact operator-approved database identities for retained diagnostic workflows.
# A username is configuration; the workflow never fetches a master password.
variable "deployment_db_user_arns" {
  type    = list(string)
  default = []
  validation {
    condition     = alltrue([for arn in var.deployment_db_user_arns : can(regex("^arn:aws:rds-db:[a-z0-9-]+:[0-9]{12}:dbuser:[A-Za-z0-9-]+/[A-Za-z0-9_]+$", arn))])
    error_message = "Use exact database-resource/user ARNs without wildcards."
  }
}
resource "aws_iam_role_policy" "database_diagnostics" {
  count = length(var.deployment_db_user_arns) == 0 ? 0 : 1
  name  = "reviewed-database-diagnostics"
  role  = aws_iam_role.deployment.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["rds-db:connect"], Resource = var.deployment_db_user_arns
  }] })
}
resource "aws_iam_role_policy" "context_artifact_diagnostics" {
  name = "existing-context-artifact-diagnostics"
  role = aws_iam_role.deployment.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = ["s3:ListBucket"],
    Resource  = "arn:aws:s3:::agent-context-platform-data-${data.aws_caller_identity.current.account_id}",
    Condition = { StringLike = { "s3:prefix" = ["content/*"] } }
  }] })
}

# Domain clusters and CAPE are optional, independently inventoried rollout targets.
variable "additional_cluster_names" {
  type    = set(string)
  default = []
  validation {
    condition     = alltrue([for name in var.additional_cluster_names : can(regex("^adp-[A-Za-z0-9_-]+$", name))])
    error_message = "Supply exact ADP domain cluster names without wildcards."
  }
}
resource "aws_eks_access_entry" "domain_deployment" {
  for_each          = length(var.deployment_role_boundaries) == 0 ? toset([]) : setsubtract(var.additional_cluster_names, toset([var.cluster_name]))
  cluster_name      = each.value
  principal_arn     = aws_iam_role.deployment.arn
  kubernetes_groups = ["adp:trusted-deployment"]
  type              = "STANDARD"
}
resource "aws_eks_access_policy_association" "domain_deployment" {
  for_each      = aws_eks_access_entry.domain_deployment
  cluster_name  = each.key
  principal_arn = aws_iam_role.deployment.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
}

variable "cape_instance_ids" {
  type    = set(string)
  default = []
  validation {
    condition     = alltrue([for id in var.cape_instance_ids : can(regex("^i-[0-9a-f]{8,17}$", id))])
    error_message = "Supply the exact existing CAPE instance IDs."
  }
}
resource "aws_iam_role_policy" "cape_registration" {
  count = length(var.cape_instance_ids) == 0 ? 0 : 1
  name  = "reviewed-cape-image-registration"
  role  = aws_iam_role.deployment.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Effect = "Allow", Action = ["ssm:SendCommand"],
      Resource = concat(
        ["arn:aws:ssm:${var.aws_region}::document/AWS-RunShellScript"],
        [for id in var.cape_instance_ids : "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/${id}"]
      )
    },
    { Effect = "Allow", Action = ["ssm:GetCommandInvocation"], Resource = "*" },
  ] })
}

# Smoke/live checks have credentials independent of the deploying job. Cognito
# InitiateAuth does not use IAM authorization; possession of the scoped test
# refresh token is the authority. Do not add a fictitious Cognito IAM grant.
variable "smoke_refresh_token_arn" {
  type    = string
  default = ""
  validation {
    condition     = var.smoke_refresh_token_arn == "" || can(regex("^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:adp/[a-z0-9-]+/gateway/smoke-user-refresh-token-[A-Za-z0-9]{6}$", var.smoke_refresh_token_arn))
    error_message = "Supply the exact smoke-user refresh-token ARN, including its generated suffix."
  }
}
resource "aws_iam_role" "checks" {
  name = "${var.name_prefix}-trusted-checks"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-checks-${var.environment}"
    } }
  }] })
}
resource "aws_iam_role_policy" "checks" {
  name = "gateway-post-deploy-checks"
  role = aws_iam_role.checks.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [for parameter in ["cognito-user-pool-id", "cognito-client-id", "cloudfront-domain"] : "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gateway/${parameter}"] },
    { Effect = "Allow", Action = ["sts:GetCallerIdentity"], Resource = "*" },
    ], var.smoke_refresh_token_arn == "" ? [] : [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.smoke_refresh_token_arn }
  ]) })
}
output "checks_role_arn" { value = aws_iam_role.checks.arn }

# The GitLab deployed-contract tests also sign webhook requests. Those secrets
# must never be loaded into a pull-request job; PRs exercise the local handler.
variable "gitlab_checks_secret_arns" {
  type    = list(string)
  default = []
  validation {
    condition     = alltrue([for arn in var.gitlab_checks_secret_arns : can(regex("^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}$", arn)) && !can(regex("adp/(users|teams|orgs|domain-apps)/|/tenants/", arn))])
    error_message = "Use the exact GitLab/test webhook secret ARNs; tenant vaults and wildcard prefixes are forbidden."
  }
}
variable "gitlab_checks_queue_arn" {
  type    = string
  default = ""
  validation {
    condition     = var.gitlab_checks_queue_arn == "" || can(regex("^arn:aws:sqs:[a-z0-9-]+:[0-9]{12}:adp-[A-Za-z0-9_.-]+$", var.gitlab_checks_queue_arn))
    error_message = "Supply the exact webhook queue ARN used by fleet diagnostics."
  }
}
resource "aws_iam_role_policy" "gitlab_checks" {
  name = "gitlab-deployed-contract-checks"
  role = aws_iam_role.checks.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [for path in [
      "webhook-ingress/gitlab-endpoint", "webhook-ingress/gitlab-webhook-secret-arn",
      "gitlab/url", "gitlab/api-token-arn", "gitlab/test-project-id", "gitlab/test-project-path",
      "webhook-ingress/endpoint", "webhook-ingress/webhook-secret-arn", "webhook-ingress/sqs-queue-url",
    ] : "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/${path}"] },
    ], [for _ in range(length(var.gitlab_checks_secret_arns) == 0 ? 0 : 1) : {
      Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.gitlab_checks_secret_arns
      }], [for _ in range(var.gitlab_checks_queue_arn == "" ? 0 : 1) : {
      Effect = "Allow", Action = ["sqs:GetQueueAttributes"], Resource = var.gitlab_checks_queue_arn
  }]) })
}
