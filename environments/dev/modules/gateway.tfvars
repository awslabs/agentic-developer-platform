environment = "dev"
aws_region  = "us-east-1"

account_id = "879318057152"

# Cost center
cost_center = "engineering"

# Database
rds_instance_class    = "db.t3.medium"
rds_allocated_storage = 20

# Redis
redis_node_type = "cache.t3.micro"

# Cognito
cognito_custom_domain = ""

# API Gateway (REST API with 15-min timeout)
enable_api_gateway = true
