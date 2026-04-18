environment = "dev"
aws_region  = "us-east-1"

# Account ID — set dynamically by deploy scripts or override here
# account_id = "123456789012"

# Cost center
cost_center = "engineering"

# Database
rds_instance_class    = "db.t3.medium"
rds_allocated_storage = 20

# Redis
redis_node_type = "cache.t3.micro"

# Cognito
cognito_custom_domain = ""
