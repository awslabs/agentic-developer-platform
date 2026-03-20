# Production Environment Configuration

# Environment
environment = "prod"
cost_center = "platform-production"

# AWS Configuration
aws_region = "us-east-1"

# Networking
vpc_cidr = "10.2.0.0/16"
az_count = 3

# EKS Configuration
eks_cluster_version       = "1.31"
node_group_instance_types = ["t3.large", "t3.xlarge"]
node_group_desired_size   = 3
node_group_max_size       = 10
node_group_min_size       = 2

# RDS Configuration
rds_instance_class          = "db.r6g.large"
rds_allocated_storage       = 500
rds_max_allocated_storage   = 2000
rds_multi_az                = true
rds_backup_retention_period = 30
rds_backup_window           = "03:00-04:00"
rds_maintenance_window      = "sun:04:00-sun:05:00"
rds_db_name                 = "bedrockgateway"
rds_username                = "bgadmin"

# Redis Configuration
enable_redis               = true
redis_node_type            = "cache.r6g.large"
redis_num_cache_nodes      = 3
redis_parameter_group_name = "default.redis7"
redis_port                 = 6379

# ALB Configuration
domain_name        = "gateway.example.com"
certificate_domain = "*.example.com"

# ECR Configuration
ecr_image_tag_mutability   = "IMMUTABLE"
ecr_scan_on_push           = true
ecr_lifecycle_policy_rules = 20

# IAM Configuration - Pool accounts for cross-account Bedrock access
pool_account_arns = [
  # Add actual account IDs for Bedrock pool accounts when available
  # "123456789012",
  # "234567890123",
  # "345678901234"
]

# EKS Public Access CIDRs - REPLACE with your IP/32 before applying
# eks_public_access_cidrs = ["YOUR.IP.HERE/32"]