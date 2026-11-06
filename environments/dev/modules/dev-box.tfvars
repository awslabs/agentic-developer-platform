# Dev Box foundation (#7998) — platform dev account 879318057152, us-east-1.
# Applied by modules/user-services/dev-box/scripts/deploy-dev-box.sh (deploy-all.sh
# Step 12 with --dev-box on, or --dev-box-only). The stage adds at run time:
# image_id / image_revision / source_revision / crew_profile_revision (from SSM
# /adp/dev/dev-box/image-*, published by scripts/build-dev-box-image.sh),
# kms_key_arn and ssm_endpoint_security_group_id (from infra/platform via SSM),
# eks_oidc_provider_arn and rds_resource_id (resolved from the live cluster/RDS).
# Dev Box is an optional module: an environment without this file never runs it.

# Read by deploy-all.sh only: run the Dev Box stage by default on dev.
stage_enabled = true

account_id    = "879318057152"
region        = "us-east-1"
environment   = "dev"
deployment_id = "adp-dev-dev-box"

# Platform VPC and the private subnet the guests launch in (platform/infra outputs
# vpc_id / private_subnet_ids[0]). Single subnet: the launch template is bound to one.
vpc_id            = "vpc-0d6115bead9301d25"
private_subnet_id = "subnet-0860c744097c41a03"

# Gateway pods run on EKS nodes in the cluster security group (platform output
# eks_cluster_security_group_id; the RDS group already admits it). It is the only
# source for the dashboard ingress (8443) and the only HTTPS egress target besides
# the SSM endpoints.
gateway_security_group_id = "sg-0d08851d0139bb2eb"

# Run registry the gateway uses for Crew host runs (src/agentauth/chat_crew_run.py
# current_crew_store -> settings.webhook_events_table; webhook-ingress output
# webhook_events_table) and the gateway REST API + stage (gateway infra
# api_gateway_id / api_gateway_stage_name). The guest's model-route grant and the
# dashboard are applied only once a Crew profile and model are pinned.
run_table_arn       = "arn:aws:dynamodb:us-east-1:879318057152:table/adp-dev-webhook-events"
gateway_execute_arn = "arn:aws:execute-api:us-east-1:879318057152:59o2rakc50/dev"
dashboard_enabled   = true
# crew_model_id    = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"   # set with the Crew image (profile pin)

# Owner defaults: 100 GB workspace. Size moved m6i.xlarge -> m6a.xlarge on 2026-10-20:
# EC2 refused every m6i.xlarge launch in us-east-1a with InsufficientInstanceCapacity
# (CloudTrail 13:45 UTC, setup op 623235a1 provider_refused) while c6a/m6a capacity
# was available in the same subnet. Same vCPU/memory class; AMD.
instance_size     = "m6a.xlarge"
# 2026-10-26: 100 GB was too small for a developer (repos, node_modules, images, models); retained
# disks grow in place on the next Start/Replace (controller/storage.py grow_volume), gp3.
workspace_disk_gb = 500
template_revision = "r1"

# Admission policy for the aws-e tenant (organizations.id), seeded through the
# gateway (python -m src.cloud_desktop.seed) with these exact values.
policy_org_id     = "aws-e"
allowed_sizes     = ["m6a.xlarge", "m6i.xlarge"]
# #8423 capacity fallback: order follows allowed_sizes (m6a, then m6i); price the fallback
# size; a second approved private subnet (us-east-1b, NAT + SSM endpoint present) lets a
# fresh box leave a zone that has no capacity. A retained workspace disk pins its zone.
instance_hour_rates_usd = { "m6i.xlarge" = "0.192" }
private_subnet_ids      = ["subnet-0860c744097c41a03", "subnet-03ae2ea2ebdf611bb"]
max_disk_gb       = 1000
max_live_boxes    = 3
max_running_boxes = 2
max_storage_gb    = 6000
max_running_hours = 400
budget_usd        = "250"
max_lease_minutes = 600
idle_stop_minutes = 60
# us-east-1 on-demand m6a.xlarge (pricing API 2026-10-20); gp3 0.08 USD/GB-month and snapshot 0.05 USD/GB-month per hour.
instance_hour_rate_usd    = "0.1728"
disk_gb_hour_rate_usd     = "0.00011"
snapshot_gb_hour_rate_usd = "0.00007"

# Gateway database user for the controller's IAM connection (gateway tfvars rds_username).
rds_username = "bgadmin"
