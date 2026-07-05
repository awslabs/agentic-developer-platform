# =============================================================================
# Webhook Ingress Infrastructure
# =============================================================================
# Foundational infrastructure for the hosted multi-tenant webhook ingress layer.
# Receives GitHub webhooks via REST API Gateway v1, validates with Lambda,
# and queues work onto SQS FIFO for downstream agent processing.
#
# State key: dev/modules/webhook-ingress/terraform.tfstate
# =============================================================================

terraform {
  backend "s3" {
    # Configured via -backend-config during terraform init
    # key = "<env>/modules/webhook-ingress/terraform.tfstate"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "adp"
      Environment = var.environment
      Module      = "webhook-ingress"
      ManagedBy   = "terraform"
      Owner       = "agent-team"
      CostCenter  = "engineering"
    }
  }
}

# =============================================================================
# Kubernetes Provider — connects to EKS for namespace, SA, and KEDA manifests
# =============================================================================

data "aws_eks_cluster" "main" {
  name = var.eks_cluster_name
}

data "aws_eks_cluster_auth" "main" {
  name = var.eks_cluster_name
}

provider "kubernetes" {
  host                   = data.aws_eks_cluster.main.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.main.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.main.token
}

provider "helm" {
  kubernetes {
    host                   = data.aws_eks_cluster.main.endpoint
    cluster_ca_certificate = base64decode(data.aws_eks_cluster.main.certificate_authority[0].data)
    token                  = data.aws_eks_cluster_auth.main.token
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# =============================================================================
# EKS OIDC + KEDA discovery
# =============================================================================
# The ScaledJob IRSA trust needs the cluster's OIDC provider ARN + issuer URL,
# and the KEDA operator role ARN for chain-assume. Discovered from AWS directly
# instead of passed in as variables — keeps the module self-contained and
# avoids terraform_remote_state reads to other modules (see tech-debt #337).

locals {
  name_prefix            = "adp-${var.environment}"
  account_id             = data.aws_caller_identity.current.account_id
  lambda_artifact_bucket = var.lambda_artifact_bucket != "" ? var.lambda_artifact_bucket : "adp-terraform-state-${data.aws_caller_identity.current.account_id}"
  agent_image            = var.agent_image != "" ? var.agent_image : "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/adp-agent-runtime:latest"

  # OIDC issuer URL from the EKS cluster — e.g.
  #   https://oidc.eks.us-east-1.amazonaws.com/id/ABC123...
  oidc_issuer = data.aws_eks_cluster.main.identity[0].oidc[0].issuer

  # OIDC provider ARN — derived from issuer URL + account ID. The provider
  # itself is registered once at platform setup; we only need its ARN here,
  # not ownership.
  oidc_provider_arn = "arn:aws:iam::${local.account_id}:oidc-provider/${replace(local.oidc_issuer, "https://", "")}"

  # Issue #2949: Gateway internal API URL — resolved from SSM (published by
  # wire-gateway-alb.sh), with var override for pipeline-threading path.
  # Form: "http://<internal-alb-dns>" (plain HTTP, in-VPC only).
  gateway_api_url = var.gateway_api_url != "" ? var.gateway_api_url : "http://${data.aws_ssm_parameter.gateway_internal_alb_dns.value}"

  # Issue #2949: Internal API key secret ARN — resolved from Secrets Manager
  # data source, with var override for pipeline-threading path.
  internal_api_key_arn = var.internal_api_key_arn != "" ? var.internal_api_key_arn : data.aws_secretsmanager_secret.gateway_internal_api_key.arn
}

# KEDA operator role is now owned by this module (keda.tf).
# Phase 8 references it via data source by name.

# Issue #575: the gateway's API Gateway invoke URL, published to SSM by
# modules/gateway/infra/. Nullable because the param may not exist in a
# freshly-bootstrapped environment — consumers handle "" gracefully.
data "aws_ssm_parameter" "gateway_apigw_invoke_url" {
  name = "/adp/${var.environment}/gateway/apigw-invoke-url"

  # Workflow-dispatch apply ordering: gateway infra must apply first so this
  # param exists. If the param is missing at plan time, Terraform fails loudly
  # rather than silently producing an empty value — which is what we want.
}

# Issue #2949: the gateway's internal ALB DNS, published to SSM by
# modules/gateway/infra/ (via wire-gateway-alb.sh). Required for the webhook
# Lambda to call /internal/v1/resolve-installation and /internal/v1/resolve-user
# on the in-VPC ALB. Without this, RESOLVE_CANONICAL_VIA_GATEWAY=true has no
# target and every webhook 403s with unknown_installation.
data "aws_ssm_parameter" "gateway_internal_alb_dns" {
  name = "/adp/${var.environment}/gateway/internal-alb-dns"
}

# Issue #2949: the gateway's internal API key secret (shared secret for
# X-Internal-Api-Key header). The Lambda reads this at runtime to authenticate
# against gateway internal endpoints. Published by modules/gateway/infra/.
data "aws_secretsmanager_secret" "gateway_internal_api_key" {
  name = "adp/${var.environment}/gateway/internal-api-key"
}

# Gateway's customer-managed KMS key (created by gateway-infra at
# modules/gateway/infra/kms.tf). It encrypts adp-<env>-identity-index and
# adp-<env>-user-identity-index, which the webhook Lambda reads at every
# webhook to resolve installation→tenant and sender→user. Without
# kms:Decrypt on this key, those GetItem calls fail with AccessDeniedException
# and the handler returns 403 unknown_installation for every webhook.
# Referenced by alias so key rotation doesn't break the policy.
data "aws_kms_alias" "gateway_dynamodb" {
  name = "alias/adp-${var.environment}-gateway-dynamodb"
}
