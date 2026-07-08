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

# Backup (created in Story 5; leave empty until then)
backup_s3_bucket_name = ""
