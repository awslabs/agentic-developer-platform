# Test Environment Configuration

# Environment
environment = "test"
cost_center = "engineering-test"

# AWS Configuration
aws_region = "us-east-1"

# Networking
vpc_cidr = "10.1.0.0/16"
az_count = 2

# EKS Configuration
eks_cluster_version       = "1.31"
node_group_instance_types = ["t3.medium"]
node_group_desired_size   = 2
node_group_max_size       = 4
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

# ALB Configuration
domain_name        = "test-gateway.example.com"
certificate_domain = "*.example.com"

# ECR Configuration
ecr_image_tag_mutability   = "MUTABLE"
ecr_scan_on_push           = true
ecr_lifecycle_policy_rules = 10

# IAM Configuration - Pool accounts for cross-account Bedrock access
pool_account_arns = [
  # Add actual account IDs for Bedrock pool accounts when available
  # "123456789012",
  # "234567890123"
]

# EKS Public Access CIDRs - REPLACE with your IP/32 before applying
# eks_public_access_cidrs = ["YOUR.IP.HERE/32"]