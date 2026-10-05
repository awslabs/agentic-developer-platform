# A separate identity for repository-authored developer work. Reuse the reviewed
# runtime resource scopes, but omit build, PassRole, S3, ECR and deployment APIs.
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}
variable "name_prefix" { type = string }
variable "environment" { type = string }
variable "aws_region" { type = string }
variable "oidc_provider_arn" { type = string }
variable "oidc_issuer" { type = string }
variable "runner_namespace" {
  type    = string
  default = "arc-runners"
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]*[a-z0-9]$", var.runner_namespace))
    error_message = "Runner namespace must be an exact Kubernetes namespace."
  }
}
variable "github_org" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9-]*$", var.github_org))
    error_message = "GitHub organization must be an exact name."
  }
}
variable "gateway_execution_arns" { type = list(string) }

data "aws_caller_identity" "current" {}
data "aws_secretsmanager_secret" "github_dev" {
  for_each = toset(["id", "key"])
  name     = "adp/${var.github_org}/gh-app-dev-${each.key}"
}
# Secrets encrypted with the AWS-managed default key still require KMS access:
# the boundary's explicit deny otherwise overrides the key's service policy.
data "aws_kms_key" "github_dev" {
  for_each = data.aws_secretsmanager_secret.github_dev
  key_id   = coalesce(each.value.kms_key_id, "alias/aws/secretsmanager")
}
module "runtime" {
  source                 = "../runner-runtime-policy"
  account_id             = data.aws_caller_identity.current.account_id
  aws_region             = var.aws_region
  name_prefix            = var.name_prefix
  environment            = var.environment
  gateway_execution_arns = var.gateway_execution_arns
  transport_secret_arns  = [for secret in data.aws_secretsmanager_secret.github_dev : secret.arn]
}
locals {
  service_account = "agent-workflow-sa"
  runner_label    = "arc-runner-agent"
  runtime_grants = [for grant in module.runtime.grants : grant if contains([
    "Identity", "ModelInference", "GatewayEndpoint", "GatewayTransport", "LegacyEngineTransport"
  ], grant.Sid)]
  kms_resources    = distinct([for key in data.aws_kms_key.github_dev : key.arn])
  secret_resources = [for secret in data.aws_secretsmanager_secret.github_dev : secret.arn]
  kms_service      = "secretsmanager.${var.aws_region}.amazonaws.com"
  grants = concat(local.runtime_grants, [{
    Sid = "DeveloperSecretDecryption", Effect = "Allow", Action = ["kms:Decrypt"], Resource = local.kms_resources
    Condition = { StringEquals = {
      "kms:ViaService"                  = local.kms_service
      "kms:EncryptionContext:SecretARN" = local.secret_resources
    } }
  }])
  actions = distinct(flatten([for grant in local.grants : grant.Action]))
  boundary = concat([
    { Sid = "AgentApiCeiling", Effect = "Allow", Action = local.actions, Resource = "*" },
    { Sid = "DenyOutsideAgentApis", Effect = "Deny", NotAction = local.actions, Resource = "*" },
    ], [for grant in local.grants : {
      Sid    = "DenyOther${grant.Sid}Resources", Effect = "Deny",
      Action = grant.Action, NotResource = grant.Resource
    } if grant.Resource != ["*"]], [
    { Sid = "DenyDirectKeyDecryption", Effect = "Deny", Action = ["kms:Decrypt"], Resource = "*",
    Condition = { StringNotEquals = { "kms:ViaService" = local.kms_service } } },
    { Sid = "DenyOtherSecretDecryption", Effect = "Deny", Action = ["kms:Decrypt"], Resource = "*",
    Condition = { StringNotEquals = { "kms:EncryptionContext:SecretARN" = local.secret_resources } } }
  ])
}
resource "aws_iam_policy" "boundary" {
  name   = "${var.name_prefix}-agent-workflow-boundary"
  policy = jsonencode({ Version = "2012-10-17", Statement = local.boundary })
}
resource "aws_iam_role" "agent" {
  name                 = "${var.name_prefix}-agent-workflow"
  permissions_boundary = aws_iam_policy.boundary.arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = var.oidc_provider_arn }
      Condition = { StringEquals = {
        "${replace(var.oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
        "${replace(var.oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:${var.runner_namespace}:${local.service_account}"
      } }
    }]
  })
}
resource "aws_iam_role_policy" "runtime" {
  name   = "bounded-agent-runtime"
  role   = aws_iam_role.agent.name
  policy = jsonencode({ Version = "2012-10-17", Statement = local.grants })
}
output "role_arn" { value = aws_iam_role.agent.arn }
output "service_account" { value = local.service_account }
output "runner_label" { value = local.runner_label }
output "namespace" { value = var.runner_namespace }
