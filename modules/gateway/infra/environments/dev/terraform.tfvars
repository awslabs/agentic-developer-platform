# Development Environment Configuration

# Environment
environment = "dev"
cost_center = "engineering-dev"

# AWS Configuration
aws_region = "us-east-1"

# Networking
vpc_cidr = "10.0.0.0/16"
az_count = 2

# EKS Configuration
eks_cluster_version       = "1.35"
node_group_instance_types = ["t3.medium"]
node_group_desired_size   = 2
node_group_max_size       = 3
node_group_min_size       = 1

# RDS Configuration
rds_instance_class          = "db.t4g.medium"
rds_allocated_storage       = 100
rds_max_allocated_storage   = 500
rds_multi_az                = false
rds_backup_retention_period = 7
rds_backup_window           = "03:00-04:00"
rds_maintenance_window      = "sun:04:00-sun:05:00"
rds_db_name                 = "bedrockgateway"
rds_username                = "bgadmin"

# Redis Configuration
enable_redis               = true
redis_node_type            = "cache.t3.micro"
redis_num_cache_nodes      = 1
redis_parameter_group_name = "default.redis7"
redis_port                 = 6379

# ALB is managed by EKS Ingress controller — no Terraform ALB config needed

# ECR Configuration
ecr_image_tag_mutability   = "MUTABLE"
ecr_scan_on_push           = true
ecr_lifecycle_policy_rules = 5

# IAM Configuration - Pool accounts for cross-account Bedrock access
pool_account_arns = [
  # Add actual account IDs for Bedrock pool accounts when available
  # "123456789012",
  # "234567890123"
]

# EKS Public Access CIDRs - REPLACE with your IP/32 before applying
eks_public_access_cidrs = ["0.0.0.0/0"]

# Chat Logging (Issue #143) - Async chat logging to S3 with PII scrubbing
enable_chat_logging = true

# Container Insights — ships pod logs + metrics to CloudWatch
enable_container_insights = true

# CloudFront access logging — end-to-end request latency from CDN edge
enable_cloudfront_logging     = true
cloudfront_log_retention_days = 90

# CloudWatch Latency Dashboard (Issue #144)
# ALB ARN suffix — set after EKS Ingress controller creates the ALB
# Format: app/<alb-name>/<alb-id>
alb_arn_suffix = "app/k8s-bedrockg-bedrockg-96a0136fc5/a04d4e1ab78a9b6c"

# API Gateway REST API (Issue #236) — alternate route with 15min timeout + streaming
enable_api_gateway = true
