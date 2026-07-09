environment = "dev"
aws_region  = "us-east-1"

# EC2 Instance
instance_type    = "m6i.xlarge"
root_volume_size = 100
root_volume_type = "gp3"

# Networking / DNS
gitlab_domain      = "gitlab.dev.adp.internal"
route53_zone_name  = "dev.adp.internal"
certificate_arn    = ""

# Cognito OIDC
cognito_user_pool_id = "us-east-1_JEhv9xSGG"
cognito_domain       = "bedrockgw-dev-auth-18057152"

# Backup
backup_enabled        = true
backup_retention_days = 90
backup_schedule       = "cron(0 2 * * ? *)"
backup_s3_bucket_name = ""
