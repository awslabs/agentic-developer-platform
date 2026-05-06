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

# Issue #60: Provision test users for dev environment
create_test_users = true

# Issue #313: enable "Sign in with GitHub" on the admin UI.
# Client_id + client_secret are read from Secrets Manager at
# adp/dev/cognito/github-oauth-credentials (pre-provisioned out-of-band)
# rather than passed through tfvars to keep them out of TF state.
enable_github_oauth = true
