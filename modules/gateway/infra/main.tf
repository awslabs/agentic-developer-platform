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

# Local values for resource naming
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
}

# Networking Module
module "networking" {
  source = "./modules/networking"

  environment = var.environment
  aws_region  = var.aws_region
  vpc_cidr    = var.vpc_cidr
  az_count    = var.az_count
  name_prefix = local.name_prefix
  common_tags = local.common_tags
}

# IAM Module (created early as other modules depend on it)
module "iam" {
  source = "./modules/iam"

  environment       = var.environment
  name_prefix       = local.name_prefix
  common_tags       = local.common_tags
  cluster_name      = "${local.name_prefix}-eks-cluster"
  pool_account_arns = var.pool_account_arns

  # RDS IAM Authentication
  enable_rds_iam_auth = true
  rds_db_username     = var.rds_username

  # ElastiCache IAM Authentication
  enable_elasticache_iam_auth = var.enable_redis
  redis_replication_group_id  = var.enable_redis ? module.redis[0].replication_group_id : ""
  redis_iam_user_id           = var.enable_redis ? module.redis[0].redis_iam_user_id : ""

  # Chat Logging (Issue #143)
  enable_chat_logging   = var.enable_chat_logging
  chat_logs_bucket_arn  = var.enable_chat_logging ? module.s3_chat_logs[0].bucket_arn : ""
  enable_comprehend_pii = var.enable_chat_logging && var.chat_logging_scrub_level == "standard"

  # Issue #144: X-Ray Tracing
  enable_xray_tracing = var.enable_xray_tracing

  depends_on = [module.networking, module.rds, module.redis, module.s3_chat_logs]
}

# S3 Chat Logs Module (Issue #143)
module "s3_chat_logs" {
  count  = var.enable_chat_logging ? 1 : 0
  source = "./modules/s3-chat-logs"

  environment     = var.environment
  name_prefix     = local.name_prefix
  common_tags     = local.common_tags
  kms_key_arn     = var.chat_logging_kms_key_arn
  log_bucket_name = "" # Optional: set to access log bucket if needed
}

# EKS Module
module "eks" {
  source = "./modules/eks"

  environment               = var.environment
  name_prefix               = local.name_prefix
  common_tags               = local.common_tags
  vpc_id                    = module.networking.vpc_id
  private_subnet_ids        = module.networking.private_subnet_ids
  eks_security_group_id     = module.networking.eks_security_group_id
  cluster_version           = var.eks_cluster_version
  node_group_instance_types = var.node_group_instance_types
  node_group_desired_size   = var.node_group_desired_size
  node_group_max_size       = var.node_group_max_size
  node_group_min_size       = var.node_group_min_size
  gateway_service_role_arn  = module.iam.eks_cluster_role_arn
  node_group_role_arn       = module.iam.eks_node_group_role_arn
  eks_public_access_cidrs   = var.eks_public_access_cidrs

  # IRSA gateway service configuration
  enable_rds_iam_auth         = true
  rds_db_username             = var.rds_username
  enable_elasticache_iam_auth = var.enable_redis
  redis_replication_group_id  = var.enable_redis ? module.redis[0].replication_group_id : ""
  redis_iam_user_id           = var.enable_redis ? module.redis[0].redis_iam_user_id : ""
  pool_account_arns           = var.pool_account_arns

  # Issue #226: Cognito User Pool ID for entity list endpoints
  cognito_user_pool_id = module.cognito.cognito_user_pool_id

  # Issue #143: Chat Logging — S3 and Comprehend permissions for IRSA role
  chat_logs_bucket_arn  = var.enable_chat_logging ? module.s3_chat_logs[0].bucket_arn : ""
  enable_comprehend_pii = var.enable_chat_logging && var.chat_logging_scrub_level == "standard"

  # Container Insights — CloudWatch Observability addon
  enable_container_insights = var.enable_container_insights

  depends_on = [module.networking, module.iam]
}

# RDS Module
module "rds" {
  source = "./modules/rds"

  environment             = var.environment
  name_prefix             = local.name_prefix
  common_tags             = local.common_tags
  vpc_id                  = module.networking.vpc_id
  private_subnet_ids      = module.networking.private_subnet_ids
  rds_security_group_id   = module.networking.rds_security_group_id
  instance_class          = var.rds_instance_class
  allocated_storage       = var.rds_allocated_storage
  max_allocated_storage   = var.rds_max_allocated_storage
  multi_az                = var.rds_multi_az
  backup_retention_period = var.rds_backup_retention_period
  backup_window           = var.rds_backup_window
  maintenance_window      = var.rds_maintenance_window
  db_name                 = var.rds_db_name
  username                = var.rds_username

  depends_on = [module.networking]
}

# ElastiCache Redis Module (optional)
module "redis" {
  count  = var.enable_redis ? 1 : 0
  source = "./modules/redis"

  environment             = var.environment
  name_prefix             = local.name_prefix
  common_tags             = local.common_tags
  vpc_id                  = module.networking.vpc_id
  private_subnet_ids      = module.networking.private_subnet_ids
  redis_security_group_id = module.networking.redis_security_group_id
  node_type               = var.redis_node_type
  num_cache_nodes         = var.redis_num_cache_nodes
  parameter_group_name    = var.redis_parameter_group_name
  port                    = var.redis_port

  depends_on = [module.networking]
}

# Note: ALB is managed by the EKS Ingress controller (AWS Load Balancer Controller).
# The Ingress resource in k8s/ingress.yaml creates and manages the ALB automatically.
# CloudFront's ALB origin is updated by the backend-deploy workflow after the
# Ingress ALB is created, since its DNS name is dynamic.

# ECR Module
module "ecr" {
  source = "./modules/ecr"

  environment = var.environment
  name_prefix = local.name_prefix
  common_tags = local.common_tags

  image_tag_mutability   = var.ecr_image_tag_mutability
  scan_on_push           = var.ecr_scan_on_push
  lifecycle_policy_rules = var.ecr_lifecycle_policy_rules
}

# CloudTrail Module for audit logging
module "cloudtrail" {
  source = "./modules/cloudtrail"

  environment = var.environment
  name_prefix = local.name_prefix
  common_tags = local.common_tags
}

# Cognito Module for authentication
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

  depends_on = [module.cloudfront]
}

# S3 bucket for CloudFront access logs (optional)
module "s3_cloudfront_logs" {
  count  = var.enable_cloudfront_logging ? 1 : 0
  source = "./modules/s3-cloudfront-logs"

  environment        = var.environment
  name_prefix        = local.name_prefix
  common_tags        = local.common_tags
  log_retention_days = var.cloudfront_log_retention_days
}

# CloudFront Module for Frontend CDN
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

# Frontend S3 Module for SPA Hosting
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
# Unified end-to-end latency dashboard: CloudFront → ALB → Pod → Bedrock
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
  eks_cluster_name           = module.eks.cluster_name
  eks_namespace              = "bedrockgw"
  pod_deployment_name        = "bedrockgateway"
}

# Configure Kubernetes provider after EKS cluster is created
provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_ca_certificate)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
  }
}

provider "helm" {
  kubernetes = {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_ca_certificate)

    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
    }
  }
}

# =============================================================================
# Security Group Rules for EKS Auto Mode Cluster SG
# =============================================================================
# EKS Auto Mode creates its own cluster security group that is used by managed
# node pools and pods. The networking module's security groups only have rules
# for the Terraform-managed EKS SG, not the auto-created cluster SG.
#
# These rules must be in main.tf (not in networking module) to avoid circular
# dependency: networking creates SGs before EKS exists, but the cluster SG
# is only created by EKS.
# =============================================================================

# Allow EKS cluster SG to access RDS on port 5432
resource "aws_security_group_rule" "eks_cluster_to_rds" {
  description              = "Allow EKS Auto Mode pods to access RDS PostgreSQL"
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = module.networking.rds_security_group_id
  source_security_group_id = module.eks.cluster_security_group_id

  depends_on = [module.networking, module.eks]
}

# Allow EKS cluster SG to access Redis on port 6379
resource "aws_security_group_rule" "eks_cluster_to_redis" {
  count = var.enable_redis ? 1 : 0

  description              = "Allow EKS Auto Mode pods to access ElastiCache Redis"
  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  security_group_id        = module.networking.redis_security_group_id
  source_security_group_id = module.eks.cluster_security_group_id

  depends_on = [module.networking, module.eks]
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

  environment           = var.environment
  name_prefix           = local.name_prefix
  common_tags           = local.common_tags
  aws_region            = var.aws_region

  # S3 Chat Logs Bucket
  chat_logs_bucket_name = module.s3_chat_logs[0].bucket_name
  chat_logs_bucket_arn  = module.s3_chat_logs[0].bucket_arn

  # VPC Configuration
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids

  # RDS Configuration
  rds_security_group_id = module.networking.rds_security_group_id
  db_host               = module.rds.db_instance_address
  db_port               = module.rds.db_instance_port
  db_name               = var.rds_db_name
  db_username           = var.rds_username
  rds_resource_id       = module.rds.db_instance_resource_id

  depends_on = [module.s3_chat_logs, module.rds, module.networking]
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
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids

  # ALB Configuration (set dynamically by backend-deploy workflow)
  # The ALB is created by the EKS Ingress controller, so the ARN/DNS
  # are not known at Terraform plan time. The workflow updates these.
  internal_alb_arn = var.internal_alb_arn
  internal_alb_dns = "" # Set dynamically by workflow

  # Authentication (backend handles JWT validation)
  cognito_user_pool_arn = module.cognito.cognito_user_pool_arn

  # Throttling
  throttle_burst_limit = var.api_gateway_throttle_burst_limit
  throttle_rate_limit  = var.api_gateway_throttle_rate_limit

  # Logging
  log_retention_days = var.api_gateway_log_retention_days

  depends_on = [module.networking, module.cognito]
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

  # Cognito Configuration
  cognito_user_pool_id = module.cognito.cognito_user_pool_id

  # API Gateway Configuration
  api_gateway_id            = module.api_gateway[0].api_gateway_id
  api_gateway_execution_arn = module.api_gateway[0].api_gateway_execution_arn

  depends_on = [module.api_gateway, module.cognito]
}
