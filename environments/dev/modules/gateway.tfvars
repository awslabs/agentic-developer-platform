environment = "dev"
aws_region  = "us-east-1"

# Database
rds_instance_class    = "db.t3.medium"
rds_allocated_storage = 20

# Redis
redis_node_type = "cache.t3.micro"

# Cognito
cognito_domain_prefix = "adp-gateway-dev"
