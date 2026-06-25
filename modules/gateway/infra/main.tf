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

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"
    key    = "${var.environment}/platform/terraform.tfstate"
    region = var.aws_region
  }
}

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

  state_bucket         = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"
  layer_builder_script = "${path.module}/../../../platform/scripts/build-lambda-layers.sh"
}

# =============================================================================
# Lambda layer builds (Issue #1038 / #408) — auto-build so NO deploy path fails
# =============================================================================
# The budget-lambda (psycopg2) and lambda-authorizer (pyjwt) modules read their
# layer zips from s3://<state-bucket>/lambda-layers/*.zip via `aws_s3_object`
# data sources that resolve at PLAN time. If the zip is absent the apply dies
# with "couldn't find resource". Previously only deploy-all.sh built these, so
# stage-by-stage `terraform apply` and CI failed.
#
# These null_resources trigger the CodeBuild layer build (via the shared
# build-lambda-layers.sh) BEFORE the consuming modules read S3. They are gated
# by the SAME feature flags as their consumers (enable_chat_logging for
# psycopg2, enable_api_gateway for pyjwt), so we never build a layer no Lambda
# will use. CodeBuild does the Docker work — no local Docker needed. Matches the
# existing null_resource + local-exec pattern used in the cognito module.
resource "null_resource" "build_psycopg2_layer" {
  count = var.enable_chat_logging ? 1 : 0

  # Rebuild when the layer's build recipe changes; the build script itself is
  # idempotent so re-running on every change is safe.
  triggers = {
    build_script = filesha256(local.layer_builder_script)
    layer_recipe = filesha256("${path.module}/../lambda/layers/psycopg2/build.sh")
    state_bucket = local.state_bucket
  }

  provisioner "local-exec" {
    command     = "bash '${local.layer_builder_script}' psycopg2"
    interpreter = ["/bin/bash", "-c"]
    environment = {
      AWS_REGION       = var.aws_region
      ENVIRONMENT      = var.environment
      ADP_STATE_BUCKET = local.state_bucket
    }
  }
}

resource "null_resource" "build_pyjwt_layer" {
  count = var.enable_api_gateway ? 1 : 0

  triggers = {
    build_script = filesha256(local.layer_builder_script)
    layer_recipe = filesha256("${path.module}/../lambda/layers/pyjwt/build.sh")
    state_bucket = local.state_bucket
  }

  provisioner "local-exec" {
    command     = "bash '${local.layer_builder_script}' pyjwt"
    interpreter = ["/bin/bash", "-c"]
    environment = {
      AWS_REGION       = var.aws_region
      ENVIRONMENT      = var.environment
      ADP_STATE_BUCKET = local.state_bucket
    }
  }
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

# Cognito permissions (scoped to the gateway's Cognito pool).
# Read: onboarding identity lookup, admin group/user listing.
# Write (AdminUpdateUserAttributes): onboarding approval syncs the approved
# user's role/org onto their Cognito custom: attributes so the pre-token Lambda
# emits them in the access token (see admin/onboarding/approval.py). Without
# this the approved user logs in with an empty role/org → broken SPA nav +
# dashboard. Scoped to this pool only.
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
          "cognito-idp:AdminGetUser",
          "cognito-idp:AdminUpdateUserAttributes"
        ]
        Resource = "arn:aws:cognito-idp:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:userpool/${module.cognito.cognito_user_pool_id}"
      }
    ]
  })

  depends_on = [module.cognito]
}

# CFN template read permissions (issue #562)
# The gateway pre-signs an S3 GET URL for the CloudFormation template YAML;
# AWS Console requires templateURL to be an S3 host, so we host the template
# in the existing frontend bucket and let the gateway sign a short-lived URL.
resource "aws_iam_role_policy" "gateway_cfn_template_read" {
  name = "${local.name_prefix}-policy-gateway-cfn-template-read"
  role = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CfnTemplateGet"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${module.frontend_s3.bucket_arn}/cfn-templates/*"
      },
      {
        # The "Add AWS account" flow pre-signs a GET for the CFN template; the
        # SDK/console fetch path also performs s3:ListBucket on the bucket (a
        # bucket-level action, so it's scoped by prefix here, not the object
        # ARN). Without it the flow fails: "not authorized to perform
        # s3:ListBucket on resource arn:aws:s3:::<frontend-bucket>".
        Sid      = "CfnTemplateList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = module.frontend_s3.bucket_arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["cfn-templates/*"]
          }
        }
      }
    ]
  })

  depends_on = [module.frontend_s3]
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
  account_id      = data.aws_caller_identity.current.account_id
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

  # Issue #642: KMS encryption for DynamoDB tables
  kms_key_arn = aws_kms_key.dynamodb.arn

  # Issue #769: removed `depends_on = [module.cloudfront]`. The implicit
  # dependency through callback_urls/logout_urls (which reference
  # module.cloudfront.distribution_domain_name) already enforces ordering.
  # The explicit depends_on caused all data sources inside this module to
  # be deferred to apply-time per Terraform's documented module-depends_on
  # behavior, which made data.aws_caller_identity.current.account_id
  # `(known after apply)` at plan-time, which forced the domain attribute
  # to be `(known after apply)`, which forced replacement of an already-
  # existing AWS resource — yielding "Domain already exists" on every apply.
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
  internal_alb_dns             = var.internal_alb_dns
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
  namespace              = "bedrockgw"
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

  # S3 bucket containing pre-built Lambda layer artifacts (Issue #1038)
  lambda_artifact_bucket = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"

  # Ensure the psycopg2 layer zip is built+uploaded before this module's
  # aws_s3_object data source reads it.
  depends_on = [module.s3_chat_logs, module.rds, null_resource.build_psycopg2_layer]
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

  # Issue #1011: GitHub Auth Broker route in OpenAPI body
  broker_lambda_invoke_arn    = var.enable_github_auth_broker ? module.github_auth_broker[0].invoke_arn : ""
  broker_lambda_function_name = var.enable_github_auth_broker ? module.github_auth_broker[0].function_name : ""
  # Plan-time-known bool so the broker Lambda permission's count is evaluable
  # at plan time (the invoke_arn above is unknown until apply).
  enable_broker_route = var.enable_github_auth_broker

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
  lambda_artifact_bucket = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"

  # Cognito Configuration
  cognito_user_pool_id = module.cognito.cognito_user_pool_id

  # API Gateway Configuration
  api_gateway_id            = module.api_gateway[0].api_gateway_id
  api_gateway_execution_arn = module.api_gateway[0].api_gateway_execution_arn

  # Issue #642: KMS encryption for DynamoDB tables
  kms_key_arn = aws_kms_key.dynamodb.arn

  # Ensure the pyjwt layer zip is built+uploaded before this module's
  # aws_s3_object data source reads it.
  depends_on = [module.api_gateway, module.cognito, null_resource.build_pyjwt_layer]
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

# Issue #575: Worker pods read this to SigV4-sign calls to /agent/internal/*.
# Published here so any consumer (agent-factory, webhook-ingress, etc.) can
# resolve it at apply time rather than plumbing the invoke URL through as
# a hardcoded tfvar per environment.
resource "aws_ssm_parameter" "apigw_invoke_url" {
  count = var.enable_api_gateway ? 1 : 0

  name        = "/adp/${var.environment}/gateway/apigw-invoke-url"
  description = "Gateway API Gateway stage invoke URL (for /agent/* IAM-authed routes)"
  type        = "String"
  value       = module.api_gateway[0].api_gateway_invoke_url

  tags = local.common_tags
}

# Issue #575: Worker pods need this table name to seed their IRSA role entry.
# Publishing here rather than reading via cross-module state so agent-factory
# stays loosely coupled to gateway-infra.
resource "aws_ssm_parameter" "agent_registry_table" {
  count = var.enable_api_gateway ? 1 : 0

  name        = "/adp/${var.environment}/gateway/agent-registry-table"
  description = "DynamoDB table name for the agent registry (IAM ARN → agent mapping)"
  type        = "String"
  value       = module.lambda_authorizer[0].agent_registry_table_name

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

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
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

# Issue #537: The existing identity-index table is retained as the
# channel-tenant cache (github_installation_id, cognito_client_id rows).
# Once github_user rows are removed post-migration, a future cleanup PR
# may rename this resource to aws_dynamodb_table.channel_tenant_cache
# using a `moved` block. The AWS table name stays unchanged.

# =============================================================================
# Cognito SSM Parameters (Issue #1007)
# =============================================================================
# Publish Cognito identifiers to SSM so the frontend build workflow can resolve
# them at deploy time instead of hardcoding platform-account values.
# =============================================================================

resource "aws_ssm_parameter" "cognito_user_pool_id" {
  name        = "/adp/${var.environment}/gateway/cognito-user-pool-id"
  description = "Cognito User Pool ID for frontend auth"
  type        = "String"
  value       = module.cognito.cognito_user_pool_id

  tags = local.common_tags
}

resource "aws_ssm_parameter" "cognito_client_id" {
  name        = "/adp/${var.environment}/gateway/cognito-client-id"
  description = "Cognito User Pool Client ID for frontend auth"
  type        = "String"
  value       = module.cognito.cognito_user_pool_client_id

  tags = local.common_tags
}

resource "aws_ssm_parameter" "cognito_domain" {
  name        = "/adp/${var.environment}/gateway/cognito-domain"
  description = "Cognito hosted-UI domain prefix"
  type        = "String"
  value       = module.cognito.cognito_domain

  tags = local.common_tags
}

resource "aws_ssm_parameter" "github_auth_broker_url" {
  count = var.enable_github_auth_broker ? 1 : 0

  name        = "/adp/${var.environment}/gateway/github-auth-broker-url"
  description = "GitHub auth broker API Gateway invoke URL"
  type        = "String"
  # Issue #1011: Append /auth/github so the frontend can construct /start and /callback
  value = "${module.api_gateway[0].api_gateway_invoke_url}/auth/github"

  tags = local.common_tags
}

# =============================================================================
# Chat-Logging SSM Parameter (Issue #1014 / EPIC #1013)
# =============================================================================
# Publish the chat-logs bucket name so the gateway-deploy workflow can inject
# BG_CHAT_LOGGING_BUCKET into the pod's ConfigMap without a terraform-output
# dependency. Only created when chat logging is enabled.
# =============================================================================

resource "aws_ssm_parameter" "chat_logs_bucket" {
  count = var.enable_chat_logging ? 1 : 0

  name        = "/adp/${var.environment}/gateway/chat-logs-bucket"
  description = "S3 bucket name for chat-log archive (read by gateway pod's chat_logging service + cost-tracking Lambdas)"
  type        = "String"
  value       = module.s3_chat_logs[0].bucket_name

  tags = local.common_tags
}

# =============================================================================
# RDS + Redis SSM Parameters (Issue #1008)
# =============================================================================
# Publish RDS and Redis connection details so the gateway-deploy workflow can
# render the ConfigMap without running terraform output.
# =============================================================================

resource "aws_ssm_parameter" "rds_host" {
  name        = "/adp/${var.environment}/gateway/rds-host"
  description = "RDS instance hostname (without port) for gateway ConfigMap"
  type        = "String"
  value       = module.rds.db_instance_address

  tags = local.common_tags
}

resource "aws_ssm_parameter" "rds_database_name" {
  name        = "/adp/${var.environment}/gateway/rds-database-name"
  description = "RDS database name for gateway ConfigMap"
  type        = "String"
  value       = module.rds.db_instance_name

  tags = local.common_tags
}

resource "aws_ssm_parameter" "redis_host" {
  count = var.enable_redis ? 1 : 0

  name        = "/adp/${var.environment}/gateway/redis-host"
  description = "ElastiCache Redis endpoint for gateway ConfigMap"
  type        = "String"
  value       = module.redis[0].primary_endpoint_address

  tags = local.common_tags
}

resource "aws_ssm_parameter" "redis_port" {
  count = var.enable_redis ? 1 : 0

  name        = "/adp/${var.environment}/gateway/redis-port"
  description = "ElastiCache Redis port for gateway ConfigMap"
  type        = "String"
  value       = tostring(module.redis[0].port)

  tags = local.common_tags
}

# =============================================================================
# GitHub Auth Broker Lambda (Issue #520)
# =============================================================================
# Lambda-based broker that converts GitHub OAuth flow into Cognito sessions.
# Replaces the failed Cognito-native OIDC approach (#518/#519).
# =============================================================================

# The GitHub OAuth App credentials are provisioned out-of-band in Secrets
# Manager (see #520). Read the ARN via a data source instead of relying on
# the cognito submodule, whose own IdP output is gated behind the separate
# var.enable_github_oauth flag (that path was reverted in #519).
data "aws_secretsmanager_secret" "github_oauth_for_broker" {
  count = var.enable_github_auth_broker ? 1 : 0
  name  = "adp/${var.environment}/cognito/github-oauth-credentials"
}

module "github_auth_broker" {
  count  = var.enable_github_auth_broker ? 1 : 0
  source = "./modules/github-auth-broker"

  environment             = var.environment
  name_prefix             = local.name_prefix
  common_tags             = local.common_tags
  aws_region              = var.aws_region
  cognito_user_pool_id    = module.cognito.cognito_user_pool_id
  cognito_user_pool_arn   = module.cognito.cognito_user_pool_arn
  cognito_client_id       = module.cognito.cognito_user_pool_client_id
  github_oauth_secret_arn = data.aws_secretsmanager_secret.github_oauth_for_broker[0].arn
  frontend_url            = "https://${module.cloudfront.distribution_domain_name}"
  allowlist_mode          = var.github_auth_allowlist_mode
  allowed_orgs            = var.github_auth_allowed_orgs
  github_token_secret_arn = var.github_auth_token_secret_arn
  lambda_artifact_bucket  = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"

  # Issue #1011: API Gateway route is now defined in the api-gateway module's
  # OpenAPI body. The broker module only creates the Lambda + IAM.

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
      },
      {
        Sid    = "DynamoDBKMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = [aws_kms_key.dynamodb.arn]
      }
    ]
  })
}

# Issue #1797: SQS SendMessage permission for Phase 1 inline ingestion dispatch.
# The gateway publishes to the agent-context ingestion queue when a user registers
# or reindexes a knowledge asset. Phase 2 (sweeper) will make this additive-only
# (removable without breaking the registration flow).
resource "aws_iam_role_policy" "gateway_ingestion_sqs_publish" {
  count = var.enable_agent_context_sqs ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-ingestion-sqs-publish"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "IngestionSQSPublish"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:GetQueueUrl"
        ]
        Resource = var.agent_context_ingestion_queue_arn
      }
    ]
  })
}
