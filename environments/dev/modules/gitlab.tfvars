environment = "dev"
aws_region  = "us-east-1"

# EC2 Instance
instance_type    = "m6i.xlarge"
root_volume_size = 100
root_volume_type = "gp3"

# Networking / DNS
gitlab_domain      = "gitlab.dev.adp.internal"
route53_zone_name  = "dev.adp.internal"
# Self-signed cert (CN=gitlab.dev.adp.internal, ACM-imported) enabling the
# ALB HTTPS listener. GitLab's external_url, OIDC callback, and nginx
# X-Forwarded-Proto all assume https — without a 443 listener every
# post-login redirect and the Cognito SSO hop dead-end. Replace with a
# proper private-CA cert if this outlives the demo.
certificate_arn    = "arn:aws:acm:us-east-1:879318057152:certificate/81b4c6bc-63ed-4d8b-b1d5-e4dea7d1e0b5"

# Cognito OIDC
cognito_user_pool_id = "us-east-1_JEhv9xSGG"
cognito_domain       = "bedrockgw-dev-auth-18057152"

# Backup
backup_enabled        = true
backup_retention_days = 90
backup_schedule       = "cron(0 2 * * ? *)"
backup_s3_bucket_name = ""
