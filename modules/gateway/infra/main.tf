# =============================================================================
# Gateway Infrastructure — layers on top of the shared platform
# =============================================================================
# Gateway-specific resources (RDS, Redis, Cognito, CloudFront, S3, Lambdas,
# API Gateway, CloudWatch Dashboard). Networking, EKS, ECR, IAM base roles,
# and CloudTrail are owned by the shared platform in platform/infra/.
#
# Pattern matches modules/agent-factory/infra/main.tf.
# =============================================================================

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "BedrockGateway"
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = "platform-team"
      CostCenter  = var.cost_center
    }
  }
}

# =============================================================================
# Shared Platform Remote State
# =============================================================================
# Read outputs from the shared platform infrastructure (VPC, EKS, ECR, IAM)
# deployed via platform/infra/. This avoids duplicating networking and compute.
# =============================================================================

data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${var.account_id}"
    key    = "${var.environment}/platform/terraform.tfstate"
    region = var.aws_region
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Local values for resource naming and platform lookups
locals {
  name_prefix = "bedrockgw-${var.environment}"

  # Common tags to merge with provider default tags
  common_tags = {
    Project     = "BedrockGateway"
    Environment = var.environment
    ManagedBy   = "terraform"
    Owner       = "platform-team"
    CostCenter  = var.cost_center
  }

  # Shared platform resources
  vpc_id                    = data.terraform_remote_state.platform.outputs.vpc_id
  private_subnets           = data.terraform_remote_state.platform.outputs.private_subnet_ids
  cluster_name              = data.terraform_remote_state.platform.outputs.eks_cluster_name
  cluster_endpoint          = data.terraform_remote_state.platform.outputs.eks_cluster_endpoint
  cluster_ca                = data.terraform_remote_state.platform.outputs.eks_cluster_ca_certificate
  oidc_issuer               = data.terraform_remote_state.platform.outputs.eks_oidc_issuer
  oidc_provider_arn         = data.terraform_remote_state.platform.outputs.eks_oidc_provider_arn
  ecr_gateway_url           = data.terraform_remote_state.platform.outputs.ecr_repository_urls["adp-gateway"]
  cluster_security_group_id = data.terraform_remote_state.platform.outputs.eks_cluster_security_group_id
  rds_security_group_id     = data.terraform_remote_state.platform.outputs.rds_security_group_id
  redis_security_group_id   = data.terraform_remote_state.platform.outputs.redis_security_group_id

  # Gateway service IRSA role (created by platform EKS module)
  gateway_service_irsa_role_arn  = data.terraform_remote_state.platform.outputs.gateway_service_irsa_role_arn
  gateway_service_irsa_role_name = data.terraform_remote_state.platform.outputs.gateway_service_irsa_role_name
}

# =============================================================================
# Kubernetes & Helm Providers (using shared EKS cluster)
# =============================================================================

provider "kubernetes" {
  host                   = local.cluster_endpoint
  cluster_ca_certificate = base64decode(local.cluster_ca)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", local.cluster_name]
  }
}

provider "helm" {
  kubernetes {
    host                   = local.cluster_endpoint
    cluster_ca_certificate = base64decode(local.cluster_ca)

    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", local.cluster_name]
    }
  }
}

# =============================================================================
# Gateway-specific IAM Policy Extensions (Option B)
# =============================================================================
# The platform's EKS module creates the base IRSA role with Bedrock, STS,
# CloudWatch Logs, and DynamoDB permissions. Here we attach incremental
# policies that depend on gateway-specific resources (Cognito pool ID,
# RDS IAM auth, ElastiCache IAM auth, chat logs bucket, etc.).
# =============================================================================

# RDS IAM Authentication — attach to the platform IRSA role
resource "aws_iam_role_policy" "gateway_rds_iam_auth" {
  count = var.enable_rds_iam_auth ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-rds-iam-auth"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["rds-db:connect"]
        Resource = "arn:aws:rds-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:dbuser:*/${var.rds_username}"
      }
    ]
  })
}

# ElastiCache IAM Authentication
resource "aws_iam_role_policy" "gateway_elasticache_iam_auth" {
  count = var.enable_redis && var.enable_elasticache_iam_auth ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-elasticache-iam-auth"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["elasticache:Connect"]
        Resource = [
          "arn:aws:elasticache:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:replicationgroup:${module.redis[0].replication_group_id}",
          "arn:aws:elasticache:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:user:${module.redis[0].redis_iam_user_id}"
        ]
      }
    ]
  })

  depends_on = [module.redis]
}

# Cognito read permissions (scoped to the gateway's Cognito pool)
resource "aws_iam_role_policy" "gateway_cognito_read" {
  name = "${local.name_prefix}-policy-gateway-cognito-read"
  role = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CognitoReadUsers"
        Effect = "Allow"
        Action = [
          "cognito-idp:ListUsers",
          "cognito-idp:ListGroups",
          "cognito-idp:ListUsersInGroup",
          "cognito-idp:AdminGetUser"
        ]
        Resource = "arn:aws:cognito-idp:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:userpool/${module.cognito.cognito_user_pool_id}"
      }
    ]
  })

  depends_on = [module.cognito]
}

# S3 chat logs write permissions
resource "aws_iam_role_policy" "gateway_chat_logs_s3" {
  count = var.enable_chat_logging ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-chat-logs-s3"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ChatLogsS3Write"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${module.s3_chat_logs[0].bucket_arn}/*"
      },
      {
        Sid      = "ChatLogsS3BucketAccess"
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation"]
        Resource = module.s3_chat_logs[0].bucket_arn
      }
    ]
  })

  depends_on = [module.s3_chat_logs]
}

# Comprehend PII detection permissions
resource "aws_iam_role_policy" "gateway_comprehend_pii" {
  count = var.enable_chat_logging && var.chat_logging_scrub_level == "standard" ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-comprehend-pii"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ComprehendPiiDetection"
        Effect   = "Allow"
        Action   = ["comprehend:DetectPiiEntities"]
        Resource = "*"
      }
    ]
  })
}

# X-Ray Tracing permissions
resource "aws_iam_role_policy" "gateway_xray_tracing" {
  count = var.enable_xray_tracing ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-xray-tracing"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets"
        ]
        Resource = "*"
      }
    ]
  })
}

# Secrets Manager read access for agent Cognito credentials (Issue #33)
# The gateway (and E2E tests running in-cluster) need to read the agent
# client_credentials secret to obtain M2M tokens.
resource "aws_iam_role_policy" "gateway_secretsmanager_read" {
  name = "${local.name_prefix}-policy-gateway-secretsmanager-read"
  role = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SecretsManagerReadAgentCreds"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ]
        Resource = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:bedrockgw-*"
      }
    ]
  })

  depends_on = [module.cognito]
}

# Vault credentials — full CRUD across all 4 ownership scopes.
# The gateway is the sole writer/reader of user-vault secrets; agent pods
# have no direct Secrets Manager access and must route through
# /internal/v1/proxy-request (per docs/user-identity-and-credentials-design.md).
#
# Namespaces (issue #440 scope relaxation):
#   adp/users/<cognito_sub>/*           — user-owned
#   adp/teams/<team_id>/*               — team-owned
#   adp/orgs/<org_id>/*                 — tenant-owned
#   adp/domain-apps/<app>/<org_id>/*    — domain-app, per-tenant install
resource "aws_iam_role_policy" "gateway_vault_secrets" {
  name = "${local.name_prefix}-policy-gateway-vault-secrets"
  role = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "VaultSecretsCRUD"
        Effect = "Allow"
        Action = [
          "secretsmanager:CreateSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:PutSecretValue",
          "secretsmanager:UpdateSecret",
          "secretsmanager:DescribeSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:TagResource",
          "secretsmanager:UntagResource"
        ]
        Resource = [
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:adp/users/*",
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:adp/teams/*",
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:adp/orgs/*",
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:adp/domain-apps/*"
        ]
      },
      {
        # ListSecrets is account-wide by necessity (no resource-level scoping).
        # The gateway uses it to enumerate its own vault inventory (e.g. for
        # the orphan sweeper, admin listings, and per-user quota checks).
        Sid      = "VaultSecretsList"
        Effect   = "Allow"
        Action   = ["secretsmanager:ListSecrets"]
        Resource = "*"
      }
    ]
  })
}

# Cross-account Bedrock pool assume role (if pool accounts configured)
resource "aws_iam_role_policy" "gateway_cross_account" {
  count = length(var.pool_account_arns) > 0 ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-cross-account"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = [for acct in var.pool_account_arns : "arn:aws:iam::${acct}:role/*BedrockGateway-Pool*"]
      }
    ]
  })
}

# =============================================================================
# S3 Chat Logs Module (Issue #143)
# =============================================================================

module "s3_chat_logs" {
  count  = var.enable_chat_logging ? 1 : 0
  source = "./modules/s3-chat-logs"

  environment     = var.environment
  name_prefix     = local.name_prefix
  common_tags     = local.common_tags
  kms_key_arn     = var.chat_logging_kms_key_arn
  log_bucket_name = "" # Optional: set to access log bucket if needed
}

# =============================================================================
# RDS Module
# =============================================================================

module "rds" {
  source = "./modules/rds"

  environment             = var.environment
  name_prefix             = local.name_prefix
  common_tags             = local.common_tags
  vpc_id                  = local.vpc_id
  private_subnet_ids      = local.private_subnets
  rds_security_group_id   = local.rds_security_group_id
  instance_class          = var.rds_instance_class
  allocated_storage       = var.rds_allocated_storage
  max_allocated_storage   = var.rds_max_allocated_storage
  multi_az                = var.rds_multi_az
  backup_retention_period = var.rds_backup_retention_period
  backup_window           = var.rds_backup_window
  maintenance_window      = var.rds_maintenance_window
  db_name                 = var.rds_db_name
  username                = var.rds_username
}

# =============================================================================
# ElastiCache Redis Module (optional)
# =============================================================================

module "redis" {
  count  = var.enable_redis ? 1 : 0
  source = "./modules/redis"

  environment             = var.environment
  name_prefix             = local.name_prefix
  common_tags             = local.common_tags
  vpc_id                  = local.vpc_id
  private_subnet_ids      = local.private_subnets
  redis_security_group_id = local.redis_security_group_id
  node_type               = var.redis_node_type
  num_cache_nodes         = var.redis_num_cache_nodes
  parameter_group_name    = var.redis_parameter_group_name
  port                    = var.redis_port
}

# Note: ALB is managed by the EKS Ingress controller (AWS Load Balancer Controller).
# The Ingress resource in k8s/ingress.yaml creates and manages the ALB automatically.
# CloudFront's ALB origin is updated by the backend-deploy workflow after the
# Ingress ALB is created, since its DNS name is dynamic.

# =============================================================================
# Cognito Module for authentication
# =============================================================================

module "cognito" {
  source = "./modules/cognito"

  environment       = var.environment
  name_prefix       = local.name_prefix
  common_tags       = local.common_tags
  mfa_configuration = var.cognito_mfa_configuration
  callback_urls = concat(
    var.cognito_callback_urls,
    ["https://${module.cloudfront.distribution_domain_name}/auth/callback"]
  )
  logout_urls = concat(
    var.cognito_logout_urls,
    ["https://${module.cloudfront.distribution_domain_name}"]
  )
  custom_domain          = var.cognito_custom_domain
  access_token_validity  = var.cognito_access_token_validity
  refresh_token_validity = var.cognito_refresh_token_validity
  id_token_validity      = var.cognito_id_token_validity

  # Issue #60: Provision test users (admins group, test user, test admin)
  create_test_users = var.create_test_users

  # Issue #313: GitHub OAuth identity provider
  enable_github_oauth        = var.enable_github_oauth
  github_oauth_client_id     = var.github_oauth_client_id
  github_oauth_client_secret = var.github_oauth_client_secret

  depends_on = [module.cloudfront]
}

# =============================================================================
# S3 bucket for CloudFront access logs (optional)
# =============================================================================

module "s3_cloudfront_logs" {
  count  = var.enable_cloudfront_logging ? 1 : 0
  source = "./modules/s3-cloudfront-logs"

  environment        = var.environment
  name_prefix        = local.name_prefix
  common_tags        = local.common_tags
  log_retention_days = var.cloudfront_log_retention_days
}

# =============================================================================
# CloudFront Module for Frontend CDN
# =============================================================================

module "cloudfront" {
  source = "./modules/cloudfront"

  environment                    = var.environment
  name_prefix                    = local.name_prefix
  common_tags                    = local.common_tags
  s3_bucket_regional_domain_name = module.frontend_s3.bucket_regional_domain_name
  s3_bucket_id                   = module.frontend_s3.bucket_id
  custom_domain_name             = var.frontend_domain_name
  acm_certificate_arn            = var.frontend_acm_certificate_arn
  waf_web_acl_arn                = ""
  log_bucket_domain_name         = var.enable_cloudfront_logging ? module.s3_cloudfront_logs[0].bucket_domain_name : ""
  # ALB domain is set dynamically by backend-deploy workflow after Ingress ALB is created
  # Pass empty string here — CloudFront will only have the S3 origin initially
  alb_domain_name = ""

  # VPC Origin configuration for internal ALB (security enhancement)
  # When enable_vpc_origin=true, CloudFront uses VPC Origin instead of custom origin
  # This allows the ALB to be internal, blocking direct public access
  # NOTE: internal_alb_arn is typically set dynamically in backend-deploy workflow
  # after the Ingress ALB is created by EKS
  enable_vpc_origin            = var.enable_vpc_origin
  internal_alb_arn             = var.internal_alb_arn
  vpc_origin_read_timeout      = var.vpc_origin_read_timeout
  vpc_origin_keepalive_timeout = var.vpc_origin_keepalive_timeout
}

# =============================================================================
# Frontend S3 Module for SPA Hosting
# =============================================================================

module "frontend_s3" {
  source = "./modules/s3-frontend"

  environment                 = var.environment
  name_prefix                 = local.name_prefix
  common_tags                 = local.common_tags
  cloudfront_distribution_arn = module.cloudfront.distribution_arn
  cors_allowed_origins        = var.frontend_domain_name != "" ? ["https://${var.frontend_domain_name}"] : ["*"]
}

# =============================================================================
# CloudWatch Latency Dashboard (Issue #144)
# =============================================================================
# Unified end-to-end latency dashboard: CloudFront -> ALB -> Pod -> Bedrock
# ALB ARN suffix is set via variable because the ALB is created dynamically
# by the EKS Ingress controller, not by Terraform.

module "cloudwatch_dashboard" {
  source = "./modules/cloudwatch-dashboard"

  environment                = var.environment
  name_prefix                = local.name_prefix
  common_tags                = local.common_tags
  aws_region                 = var.aws_region
  cloudfront_distribution_id = module.cloudfront.distribution_id
  alb_arn_suffix             = var.alb_arn_suffix
  eks_cluster_name           = local.cluster_name
  eks_namespace              = "adp-gateway"
  pod_deployment_name        = "bedrockgateway"
}

# =============================================================================
# Security Group Rules for EKS Auto Mode Cluster SG
# =============================================================================
# EKS Auto Mode creates its own cluster security group that is used by managed
# node pools and pods. These rules allow pods to reach RDS and Redis.
# =============================================================================

# Allow EKS cluster SG to access RDS on port 5432
resource "aws_security_group_rule" "eks_cluster_to_rds" {
  description              = "Allow EKS Auto Mode pods to access RDS PostgreSQL"
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = local.rds_security_group_id
  source_security_group_id = local.cluster_security_group_id

  depends_on = [module.rds]
}

# Allow EKS cluster SG to access Redis on port 6379
resource "aws_security_group_rule" "eks_cluster_to_redis" {
  count = var.enable_redis ? 1 : 0

  description              = "Allow EKS Auto Mode pods to access ElastiCache Redis"
  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  security_group_id        = local.redis_security_group_id
  source_security_group_id = local.cluster_security_group_id

  depends_on = [module.redis]
}

# =============================================================================
# RDS Bootstrap Module (Issue #60)
# =============================================================================
# One-shot Job that runs `GRANT rds_iam TO bgadmin` on fresh databases.
# Without this, IAM-authenticated connections fail even though RDS has
# iam_database_authentication_enabled = true. The Postgres role must
# explicitly have the rds_iam grant.
#
# Must run AFTER RDS is available and SG rules allow EKS → RDS.
# =============================================================================

module "rds_bootstrap" {
  source = "./modules/rds-bootstrap"

  name_prefix            = local.name_prefix
  namespace              = "adp-gateway"
  aws_region             = var.aws_region
  db_host                = module.rds.db_instance_address
  db_name                = var.rds_db_name
  db_username            = var.rds_username
  master_user_secret_arn = module.rds.master_user_secret_arn
  oidc_provider_arn      = local.oidc_provider_arn
  oidc_issuer            = local.oidc_issuer
  common_tags            = local.common_tags
  rds_instance_id        = module.rds.db_instance_id

  depends_on = [
    module.rds,
    aws_security_group_rule.eks_cluster_to_rds,
  ]
}

# =============================================================================
# Budget Lambda Module (Issue #234)
# =============================================================================
# Creates Lambda functions for accurate budget tracking:
# 1. Usage Tracker Lambda - S3 event-driven cost recording from chat logs
# 2. Pricing Refresh Lambda - Daily pricing updates from AWS Pricing API
# =============================================================================

module "budget_lambda" {
  count  = var.enable_chat_logging ? 1 : 0
  source = "./modules/budget-lambda"

  environment = var.environment
  name_prefix = local.name_prefix
  common_tags = local.common_tags
  aws_region  = var.aws_region

  # S3 Chat Logs Bucket
  chat_logs_bucket_name = module.s3_chat_logs[0].bucket_name
  chat_logs_bucket_arn  = module.s3_chat_logs[0].bucket_arn

  # VPC Configuration
  vpc_id             = local.vpc_id
  private_subnet_ids = local.private_subnets

  # RDS Configuration
  rds_security_group_id = local.rds_security_group_id
  db_host               = module.rds.db_instance_address
  db_port               = module.rds.db_instance_port
  db_name               = var.rds_db_name
  db_username           = var.rds_username
  rds_resource_id       = module.rds.db_instance_resource_id

  depends_on = [module.s3_chat_logs, module.rds]
}

# =============================================================================
# API Gateway REST API Module (Issue #236)
# =============================================================================
# Creates an API Gateway REST API as an alternate route to the internal ALB.
# This provides 15-minute timeout support for long-running LLM requests
# (vs CloudFront's 60s hard limit due to OriginReadTimeout constraints).
#
# Architecture:
# Client -> API Gateway REST API (regional) -> VPC Link -> Internal ALB -> EKS
#
# Both CloudFront and API Gateway routes coexist. Clients choose which
# endpoint to use based on their needs.
# =============================================================================

module "api_gateway" {
  count  = var.enable_api_gateway ? 1 : 0
  source = "./modules/api-gateway"

  environment = var.environment
  name_prefix = local.name_prefix
  common_tags = local.common_tags
  aws_region  = var.aws_region

  # VPC Configuration
  vpc_id             = local.vpc_id
  private_subnet_ids = local.private_subnets

  # ALB Configuration (set dynamically by backend-deploy workflow)
  # The ALB is created by the EKS Ingress controller, so the ARN/DNS
  # are not known at Terraform plan time. The workflow updates these.
  internal_alb_arn = var.internal_alb_arn
  internal_alb_dns = var.internal_alb_dns

  # ALB Security Groups (Issue #42) — VPC Link v2 SG needs egress to these
  # Set dynamically by the deploy workflow alongside ALB ARN/DNS
  alb_security_group_ids = var.alb_security_group_ids

  # Authentication (backend handles JWT validation)
  cognito_user_pool_arn = module.cognito.cognito_user_pool_arn

  # Throttling
  throttle_burst_limit = var.api_gateway_throttle_burst_limit
  throttle_rate_limit  = var.api_gateway_throttle_rate_limit

  # Logging
  log_retention_days = var.api_gateway_log_retention_days

  depends_on = [module.cognito]
}

# =============================================================================
# Lambda Authorizer Module (Issue #239)
# =============================================================================
# Creates a Lambda authorizer for API Gateway that supports:
# 1. JWT validation against Cognito JWKS
# 2. IAM-based agent authentication via DynamoDB registry
#
# The authorizer sets context headers (X-Auth-Source, X-Agent-*) that are
# passed to the backend for identity resolution.
# =============================================================================

module "lambda_authorizer" {
  count  = var.enable_api_gateway ? 1 : 0
  source = "./modules/lambda-authorizer"

  environment = var.environment
  name_prefix = local.name_prefix
  common_tags = local.common_tags
  aws_region  = var.aws_region

  # S3 bucket containing pre-built Lambda layer artifacts (Issue #408)
  lambda_artifact_bucket = "adp-terraform-state-${var.account_id}"

  # Cognito Configuration
  cognito_user_pool_id = module.cognito.cognito_user_pool_id

  # API Gateway Configuration
  api_gateway_id            = module.api_gateway[0].api_gateway_id
  api_gateway_execution_arn = module.api_gateway[0].api_gateway_execution_arn

  depends_on = [module.api_gateway, module.cognito]
}

# =============================================================================
# SSM Parameters for deploy-all.sh and CI/CD workflows
# =============================================================================
# These parameters allow deploy scripts and GitHub Actions workflows to
# discover resource names without running terraform output.

resource "aws_ssm_parameter" "frontend_bucket" {
  name        = "/adp/${var.environment}/gateway/frontend-bucket"
  description = "S3 bucket name for gateway frontend assets"
  type        = "String"
  value       = module.frontend_s3.bucket_name

  tags = local.common_tags
}

resource "aws_ssm_parameter" "cloudfront_id" {
  name        = "/adp/${var.environment}/gateway/cloudfront-id"
  description = "CloudFront distribution ID for cache invalidation"
  type        = "String"
  value       = module.cloudfront.distribution_id

  tags = local.common_tags
}

resource "aws_ssm_parameter" "cloudfront_domain" {
  name        = "/adp/${var.environment}/gateway/cloudfront-domain"
  description = "CloudFront distribution domain name"
  type        = "String"
  value       = module.cloudfront.distribution_domain_name

  tags = local.common_tags
}

resource "aws_ssm_parameter" "internal_alb_arn" {
  name        = "/adp/${var.environment}/gateway/internal-alb-arn"
  description = "ARN of the internal ALB created by EKS Ingress controller"
  type        = "String"
  value       = "pending"

  tags = local.common_tags

  # The value is updated by deploy-all.sh after ALB discovery.
  # Terraform should not revert it to "pending" on subsequent applies.
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "internal_alb_dns" {
  name        = "/adp/${var.environment}/gateway/internal-alb-dns"
  description = "DNS name of the internal ALB created by EKS Ingress controller"
  type        = "String"
  value       = "pending"

  tags = local.common_tags

  lifecycle {
    ignore_changes = [value]
  }
}

# =============================================================================
# Identity Index DynamoDB Table (Issue #375)
# =============================================================================
# Unified identity lookup table for tenant-identity Phase A.
# Maps identity_type + identity_value → org_id for O(1) lookups from
# webhook-ingress and pre-token-generation Lambdas.
# Gateway API is the authoritative writer; other services read via SSM name.
# =============================================================================

resource "aws_dynamodb_table" "identity_index" {
  name         = "adp-${var.environment}-identity-index"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "identity_type"
  range_key    = "identity_value"

  attribute {
    name = "identity_type"
    type = "S"
  }

  attribute {
    name = "identity_value"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name    = "adp-${var.environment}-identity-index"
    Service = "dynamodb"
    Purpose = "tenant-identity-index"
  })
}

resource "aws_ssm_parameter" "identity_index_table" {
  name        = "/adp/${var.environment}/gateway/identity-index-table"
  description = "DynamoDB table name for identity-index (Issue #375)"
  type        = "String"
  value       = aws_dynamodb_table.identity_index.name

  tags = local.common_tags
}

# =============================================================================
# GitHub Auth Broker Lambda (Issue #520)
# =============================================================================
# Lambda-based broker that converts GitHub OAuth flow into Cognito sessions.
# Replaces the failed Cognito-native OIDC approach (#518/#519).
# =============================================================================

module "github_auth_broker" {
  count  = var.enable_github_oauth ? 1 : 0
  source = "./modules/github-auth-broker"

  environment          = var.environment
  name_prefix          = local.name_prefix
  common_tags          = local.common_tags
  aws_region           = var.aws_region
  cognito_user_pool_id = module.cognito.cognito_user_pool_id
  cognito_user_pool_arn = module.cognito.cognito_user_pool_arn
  cognito_client_id    = module.cognito.cognito_user_pool_client_id
  github_oauth_secret_arn = module.cognito.github_oauth_credentials_secret_arn
  frontend_url         = "https://${module.cloudfront.distribution_domain_name}"
  allowlist_mode       = var.github_auth_allowlist_mode
  allowed_orgs         = var.github_auth_allowed_orgs
  github_token_secret_arn = var.github_auth_token_secret_arn
  lambda_artifact_bucket  = "adp-terraform-state-${var.account_id}"

  depends_on = [module.cognito, module.cloudfront]
}

# IAM policy for Gateway IRSA role to access identity-index table
resource "aws_iam_role_policy" "gateway_identity_index" {
  name = "${local.name_prefix}-policy-gateway-identity-index"
  role = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "IdentityIndexReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:DeleteItem",
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:BatchWriteItem"
        ]
        Resource = [
          aws_dynamodb_table.identity_index.arn
        ]
      }
    ]
  })
}
