mock_provider "aws" {
  mock_resource "aws_iam_instance_profile" {
    defaults = { arn = "arn:aws:iam::000000000000:instance-profile/example-guest" }
  }
  mock_data "aws_subnet" {
    defaults = { availability_zone = "us-east-1a" }
  }
}

override_resource {
  target = aws_iam_role.workspace_snapshot_dlm
  values = { arn = "arn:aws:iam::000000000000:role/example-snapshot-dlm" }
}

variables {
  account_id                     = "000000000000"
  region                         = "us-east-1"
  environment                    = "example"
  deployment_id                  = "example-deployment"
  vpc_id                         = "vpc-0123456789abcdef0"
  private_subnet_id              = "subnet-0123456789abcdef0"
  gateway_security_group_id      = "sg-11111111111111111"
  ssm_endpoint_security_group_id = "sg-22222222222222222"
  controller_principal_arn       = "arn:aws:iam::000000000000:role/example-controller"
  image_id                       = "ami-0123456789abcdef0"
  source_revision                = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  template_revision              = "example-r1"
  instance_size                  = "t3.small"
  workspace_disk_gb              = 40
  kms_key_arn                    = "arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000000"
}

run "private_base" {
  command = apply

  assert {
    condition     = aws_launch_template.guest.image_id == var.image_id && aws_launch_template.guest.metadata_options[0].http_tokens == "required"
    error_message = "The approved AMI and IMDSv2 must be pinned."
  }
  assert {
    condition     = aws_launch_template.guest.network_interfaces[0].associate_public_ip_address == "false" && aws_launch_template.guest.block_device_mappings[0].ebs[0].encrypted
    error_message = "Guest must be private with an encrypted root volume."
  }
  assert {
    condition     = aws_launch_template.guest.block_device_mappings[0].ebs[0].delete_on_termination && length(aws_launch_template.guest.block_device_mappings) == 1
    error_message = "Only the replaceable root belongs to the launch template."
  }
  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.dashboard) == 0 && aws_vpc_security_group_egress_rule.ssm.referenced_security_group_id == var.ssm_endpoint_security_group_id
    error_message = "Dashboard is default-off; outbound SSM is private."
  }
  assert {
    condition     = length(aws_vpc_security_group_egress_rule.api_gateway) == 0
    error_message = "Without an approved gateway the guest gets no internet HTTPS egress."
  }
  assert {
    condition     = length(aws_dynamodb_table_item.guest_agent) == 0 && output.guest_agent_registry_id == ""
    error_message = "Without an approved gateway the guest role is not registered with the gateway."
  }
  assert {
    condition     = aws_iam_role_policy_attachment.guest_ssm.policy_arn == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" && !strcontains(aws_iam_role.guest.assume_role_policy, "bedrock")
    error_message = "Guest must not inherit the controller policy."
  }
  assert {
    condition     = aws_iam_instance_profile.guest.role == aws_iam_role.guest.name && aws_iam_role_policy.guest_no_ec2.role == aws_iam_role.guest.id && jsondecode(aws_iam_role_policy.guest_no_ec2.policy).Statement == [{ Effect = "Deny", Action = ["ec2:*"], Resource = "*" }]
    error_message = "The actual guest instance profile must explicitly deny all EC2 snapshot discovery, restoration and mutation, even if a later attachment allows EC2 actions."
  }
  assert {
    condition     = jsonencode(output.fallback_sizes) == jsonencode(["t3.small"]) && jsonencode(output.launch_subnet_ids) == jsonencode([var.private_subnet_id]) && jsonencode(output.release_context.private_subnets) == jsonencode([{ subnet_id = var.private_subnet_id, availability_zone = "us-east-1a" }]) && jsonencode(output.release_context.instance_hour_rates_usd) == jsonencode({ "t3.small" = "0.192" })
    error_message = "Without fallback inputs the launch plan is exactly the template's size and subnet, priced at the preferred rate (#8423)."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if try(statement.Sid == "LaunchOwnedInstance" && jsonencode(statement.Condition.StringEquals["ec2:InstanceType"]) == jsonencode(["t3.small"]), false)]) == 1
    error_message = "The controller may launch only the admitted sizes."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if try(statement.Sid == "OperateOwnedInstances" && statement.Condition.StringEquals["aws:ResourceTag/adp:deployment-id"] == var.deployment_id && statement.Condition.Null["aws:ResourceTag/adp:owner-id"] == "false", false)]) == 1
    error_message = "Controller operations must require deployment and owner tags."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if try(statement.Sid == "GrowOwnedDisk" && statement.Action == ["ec2:ModifyVolume"] && statement.Condition.StringEquals["aws:ResourceTag/adp:deployment-id"] == var.deployment_id && statement.Condition.Null["aws:ResourceTag/adp:owner-id"] == "false", false)]) == 1
    error_message = "The controller may grow only owned retained disks (#8835)."
  }
}

run "workspace_snapshot_policy" {
  command = apply

  assert {
    condition     = aws_dlm_lifecycle_policy.workspace_snapshot.state == "ENABLED" && jsonencode(aws_dlm_lifecycle_policy.workspace_snapshot.policy_details[0].resource_types) == jsonencode(["VOLUME"])
    error_message = "DLM must target volumes, not root images or guest instances."
  }
  assert {
    condition     = jsonencode(aws_dlm_lifecycle_policy.workspace_snapshot.policy_details[0].target_tags) == jsonencode({ "adp:component" = "dev-box", "adp:deployment-id" = var.deployment_id, "adp:workspace" = "persistent" })
    error_message = "Only persistent workspace volumes from this deployment may be snapshotted."
  }
  assert {
    condition     = aws_dlm_lifecycle_policy.workspace_snapshot.policy_details[0].schedule[0].copy_tags && aws_dlm_lifecycle_policy.workspace_snapshot.policy_details[0].schedule[0].create_rule[0].times[0] == var.snapshot_schedule && aws_dlm_lifecycle_policy.workspace_snapshot.policy_details[0].schedule[0].retain_rule[0].count == var.snapshot_retention_count
    error_message = "Daily snapshots must copy owner/box/generation tags and retain only the configured count."
  }
  assert {
    condition     = jsondecode(aws_iam_role.workspace_snapshot_dlm.assume_role_policy).Statement[0].Principal.Service == "dlm.amazonaws.com" && aws_iam_role_policy_attachment.workspace_snapshot_dlm.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
    error_message = "The snapshot authority belongs to DLM, not the guest."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.workspace_snapshot_kms.policy).Statement : statement if statement.Resource != var.kms_key_arn]) == 0
    error_message = "DLM can use only the selected workspace encryption key."
  }
}

run "snapshot_inventory_permissions" {
  command = apply

  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if try(statement.Sid == "Inventory" && contains(statement.Action, "ec2:DescribeSnapshots") && statement.Resource == "*" && statement.Condition.StringEquals["aws:RequestedRegion"] == var.region, false)]) == 1
    error_message = "Only the controller's region-bound inventory can describe snapshots."
  }
  assert {
    condition     = aws_iam_role_policy_attachment.guest_ssm.role == aws_iam_role.guest.name && length([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if contains(statement.Action, "ec2:DescribeSnapshots") && statement.Sid != "Inventory"]) == 0
    error_message = "Snapshot discovery must not be granted through guest actions or controller mutation statements."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if try(statement.Sid == "RestoreOwnedSnapshot" && statement.Resource == "arn:aws:ec2:us-east-1:000000000000:snapshot/*" && statement.Condition.StringEquals["aws:ResourceTag/adp:deployment-id"] == var.deployment_id && statement.Condition.Null["aws:ResourceTag/adp:owner-id"] == "false", false)]) == 1
    error_message = "Only a deployment-owned tagged snapshot may source a new workspace volume."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if contains(statement.Action, "ec2:CreateSnapshot") || contains(statement.Action, "ec2:DeleteSnapshot")]) == 0
    error_message = "DLM alone creates and deletes snapshots."
  }
}

run "reject_invalid_snapshot_schedule" {
  command = plan
  variables { snapshot_schedule = "25:00" }
  expect_failures = [var.snapshot_schedule]
}

run "reject_invalid_snapshot_retention" {
  command = plan
  variables { snapshot_retention_count = 0 }
  expect_failures = [var.snapshot_retention_count]
}

run "reject_invalid_snapshot_cap" {
  command = plan
  variables { snapshot_max_gb_per_owner = -1 }
  expect_failures = [var.snapshot_max_gb_per_owner]
}

run "approved_host_gateway" {
  command = apply
  variables {
    run_table_arn         = "arn:aws:dynamodb:us-east-1:000000000000:table/example-runs"
    run_table_kms_key_arn = "arn:aws:kms:us-east-1:000000000000:key/11111111-2222-3333-4444-555555555555"
    gateway_execute_arn   = "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev"
    crew_profile_revision = "crew-r1"
    crew_model_id         = "example-model"
  }

  assert {
    condition     = length(aws_vpc_security_group_egress_rule.api_gateway) == 1 && aws_vpc_security_group_egress_rule.api_gateway[0].cidr_ipv4 == "0.0.0.0/0" && aws_vpc_security_group_egress_rule.api_gateway[0].from_port == 443 && aws_vpc_security_group_egress_rule.api_gateway[0].to_port == 443 && aws_vpc_security_group_egress_rule.api_gateway[0].ip_protocol == "tcp" && aws_vpc_security_group_egress_rule.api_gateway[0].prefix_list_id == null && aws_vpc_security_group_egress_rule.api_gateway[0].referenced_security_group_id == null
    error_message = "An approved gateway adds exactly one egress: HTTPS to the internet for the Regional REST API, no other port or protocol."
  }
  assert {
    condition     = length(aws_dynamodb_table_item.guest_agent) == 1 && jsondecode(aws_dynamodb_table_item.guest_agent[0].item).role_arn.S == aws_iam_role.guest.arn && jsondecode(aws_dynamodb_table_item.guest_agent[0].item).requires_host_identity.BOOL == true && jsondecode(aws_dynamodb_table_item.guest_agent[0].item).status.S == "active" && jsondecode(aws_dynamodb_table_item.guest_agent[0].item).scope.S == "internal" && output.guest_agent_registry_id == "dev-box-guest-example-deployment"
    error_message = "An approved gateway registers exactly the guest role, host-bound, as an internal platform agent."
  }

  assert {
    condition = length(aws_iam_role_policy.guest_gateway) == 1 && jsondecode(aws_iam_role_policy.guest_gateway[0].policy).Statement == [
      {
        Effect   = "Allow"
        Action   = ["execute-api:Invoke"]
        Resource = ["arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/POST/agent/model/*/invoke", "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/POST/agent/model/*/invoke-with-response-stream", "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/POST/agent/crew/model-token/refresh", "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/POST/agent/crew/host-restore/context", "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/POST/agent/crew/host-restore/grant", "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/POST/agent/crew/host-restore/release", "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/POST/agent/crew/tools", "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/POST/agent/crew/host-checkpoint/begin", "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/POST/agent/crew/host-checkpoint/complete", "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/POST/agent/crew/github-token", "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/POST/agent/crew/aws-credentials", "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/POST/agent/openai/v1/responses", "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/GET/agent/crew/orchestration/*", "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/POST/agent/crew/orchestration/flows/drafts", "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev/POST/agent/crew/orchestration/flows/drafts/preview"]
      }
    ]
    error_message = "The guest can only invoke the approved gateway model, token refresh, host restore, Crew tools, host checkpoint and host-run orchestration read/draft routes, not Bedrock or other API routes."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.guest_gateway[0].policy).Statement : statement if anytrue([for action in statement.Action : startswith(action, "ec2:")])]) == 0
    error_message = "The enabled guest gateway policy never grants EC2 snapshot discovery or mutation."
  }
  assert {
    condition     = aws_iam_role_policy.guest_no_ec2.role == aws_iam_role.guest.id && jsondecode(aws_iam_role_policy.guest_no_ec2.policy).Statement == [{ Effect = "Deny", Action = ["ec2:*"], Resource = "*" }]
    error_message = "Enabling the gateway must not weaken the guest EC2 deny."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.controller_run_registry[0].policy).Statement == [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:DeleteItem"]
        Resource = var.run_table_arn
      },
      {
        Effect    = "Allow"
        Action    = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource  = var.run_table_kms_key_arn
        Condition = { StringEquals = { "kms:ViaService" = "dynamodb.us-east-1.amazonaws.com" } }
      }
    ]
    error_message = "Mint, renewal and revocation require item-level actions on only the selected run table, plus its customer KMS key via DynamoDB only."
  }
}

run "run_registry_without_customer_key" {
  command = apply
  variables {
    run_table_arn         = "arn:aws:dynamodb:us-east-1:000000000000:table/example-runs"
    gateway_execute_arn   = "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev"
    crew_profile_revision = "crew-r1"
    crew_model_id         = "example-model"
  }

  assert {
    condition     = length(jsondecode(aws_iam_role_policy.controller_run_registry[0].policy).Statement) == 1
    error_message = "An AWS-owned-key run table grants no KMS statement."
  }
}

run "reject_foreign_run_registry_key" {
  command = plan
  variables {
    run_table_arn         = "arn:aws:dynamodb:us-east-1:000000000000:table/example-runs"
    run_table_kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/11111111-2222-3333-4444-555555555555"
    gateway_execute_arn   = "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev"
    crew_profile_revision = "crew-r1"
    crew_model_id         = "example-model"
  }
  expect_failures = [aws_iam_role_policy.controller_run_registry]
}

run "reject_foreign_gateway" {
  command = plan
  variables {
    run_table_arn         = "arn:aws:dynamodb:us-east-1:000000000000:table/example-runs"
    gateway_execute_arn   = "arn:aws:execute-api:us-east-1:111111111111:exampleapi/dev"
    crew_profile_revision = "crew-r1"
    crew_model_id         = "example-model"
  }
  expect_failures = [aws_iam_role_policy.guest_gateway]
}

run "gateway_only_dashboard" {
  command = apply
  variables {
    dashboard_enabled     = true
    crew_profile_revision = "crew-r1"
  }

  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.dashboard) == 1 && aws_vpc_security_group_ingress_rule.dashboard[0].referenced_security_group_id == var.gateway_security_group_id
    error_message = "Dashboard access may only originate at the selected gateway group."
  }
}

run "reject_oversized_disk" {
  command = plan
  variables { workspace_disk_gb = 20000 }
  expect_failures = [aws_launch_template.guest]
}

run "reject_broad_dashboard_source" {
  command = plan
  variables { gateway_security_group_id = "0.0.0.0/0" }
  expect_failures = [var.gateway_security_group_id]
}

run "self_managed_controller_runtime" {
  command = apply
  variables {
    controller_principal_arn = ""
    eks_oidc_provider_arn    = "arn:aws:iam::000000000000:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE0123456789ABCDEF"
    rds_resource_id          = "db-EXAMPLERESOURCE"
    policy_org_id            = "example-org"
  }

  assert {
    condition     = length(aws_iam_role.controller_runtime) == 1 && aws_iam_role.controller_runtime[0].name == "adp-example-dev-box-controller-runtime"
    error_message = "Without a supplied principal the module owns the controller runtime identity."
  }
  assert {
    condition     = strcontains(aws_iam_role.controller.assume_role_policy, "adp-example-dev-box-controller-runtime") || strcontains(aws_iam_role.controller.assume_role_policy, aws_iam_role.controller_runtime[0].arn)
    error_message = "The fenced controller role trusts the runtime role."
  }
  assert {
    condition     = jsondecode(aws_iam_role.controller_runtime[0].assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE0123456789ABCDEF:sub"] == "system:serviceaccount:adp-gateway:dev-box-controller"
    error_message = "Only the controller service account may assume the runtime role."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.controller_runtime[0].policy).Statement : statement if statement.Sid == "GatewayDatabaseIamAuth" && statement.Resource == "arn:aws:rds-db:us-east-1:000000000000:dbuser:db-EXAMPLERESOURCE/bgadmin"]) == 1
    error_message = "The runtime connects to the gateway database as the configured user only."
  }
  assert {
    condition     = aws_ssm_parameter.stage["deployment-id"].name == "/adp/example/dev-box/deployment-id" && aws_ssm_parameter.stage["max-deployment-live-boxes"].value == "2" && aws_ssm_parameter.stage["policy-org-id"].value == "example-org"
    error_message = "The renderers read the deployment identity and bounds from /adp/<env>/dev-box/*."
  }
  assert {
    condition     = length(output.policy_spec.allowed_sizes) == 1 && output.policy_spec.allowed_sizes[0] == "t3.small" && output.policy_spec.max_disk_gb == 40 && output.policy_spec.max_storage_gb == 160 && output.policy_spec.template.image_ref == var.image_id
    error_message = "Policy defaults derive from the instance size and workspace disk."
  }
}

run "gateway_route_requires_pinned_crew_profile" {
  command = apply
  variables {
    run_table_arn       = "arn:aws:dynamodb:us-east-1:000000000000:table/example-runs"
    gateway_execute_arn = "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev"
    dashboard_enabled   = true
  }

  assert {
    condition     = length(aws_iam_role_policy.guest_gateway) == 0 && length(aws_vpc_security_group_ingress_rule.dashboard) == 0 && output.gateway_execute_arn == "" && output.dashboard_enabled == false
    error_message = "Without a Crew profile and model pin the guest gets no gateway route and no dashboard ingress; the manifest says the same."
  }
  assert {
    condition     = aws_ssm_parameter.stage["gateway-execute-arn"].value == " " && aws_ssm_parameter.stage["run-table-arn"].value == var.run_table_arn
    error_message = "SSM reflects the effective route and the registry."
  }
}

run "capacity_fallback_plan" {
  command = apply
  variables {
    allowed_sizes           = ["t3.small", "t3a.small", "t3.medium"]
    fallback_sizes          = ["t3.small", "t3a.small"]
    private_subnet_ids      = ["subnet-0fedcba9876543210", "subnet-0123456789abcdef0"]
    instance_hour_rates_usd = { "t3a.small" = "0.0188" }
    policy_org_id           = "example-org"
  }

  assert {
    condition     = jsonencode(output.fallback_sizes) == jsonencode(["t3.small", "t3a.small"]) && jsonencode(output.launch_subnet_ids) == jsonencode(["subnet-0123456789abcdef0", "subnet-0fedcba9876543210"])
    error_message = "The plan keeps the declared size order and de-duplicates subnets with the template's subnet first (#8423)."
  }
  assert {
    condition     = jsonencode(output.release_context.instance_hour_rates_usd) == jsonencode({ "t3.small" = "0.192", "t3a.small" = "0.0188" }) && length(output.release_context.private_subnets) == 2 && output.release_context.private_subnets[1].subnet_id == "subnet-0fedcba9876543210"
    error_message = "Every fallback size carries a rate (inherited when not given) and every subnet carries its zone."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if try(statement.Sid == "LaunchOwnedInstance" && jsonencode(statement.Condition.StringEquals["ec2:InstanceType"]) == jsonencode(["t3.small", "t3a.small"]), false)]) == 1
    error_message = "The controller IAM admits exactly the fallback sizes, not every allowed size."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if try(statement.Sid == "LaunchApprovedInputs" && contains(statement.Resource, "arn:aws:ec2:us-east-1:000000000000:subnet/subnet-0fedcba9876543210") && contains(statement.Resource, "arn:aws:ec2:us-east-1:000000000000:subnet/subnet-0123456789abcdef0") && length(statement.Resource) == 5, false)]) == 1
    error_message = "RunInstances is authorized on every approved subnet and nothing else."
  }
  assert {
    condition     = jsonencode(output.policy_spec.allowed_sizes) == jsonencode(["t3.small", "t3a.small", "t3.medium"]) && aws_launch_template.guest.instance_type == "t3.small"
    error_message = "The policy still admits every allowed size; the template keeps the preferred one."
  }
}

run "reject_fallback_outside_allowed_sizes" {
  command = plan
  variables {
    allowed_sizes  = ["t3.small"]
    fallback_sizes = ["t3.small", "t3.large"]
  }
  expect_failures = [aws_launch_template.guest]
}

run "reject_fallback_not_starting_with_preferred_size" {
  command = plan
  variables {
    allowed_sizes  = ["t3.small", "t3a.small"]
    fallback_sizes = ["t3a.small", "t3.small"]
  }
  expect_failures = [aws_launch_template.guest]
}

run "private_sts_accepts_only_guest_https" {
  command = apply
  variables {
    sts_endpoint_security_group_ids = ["sg-33333333333333333"]
    gateway_execute_arn             = "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev"
    crew_profile_revision           = "example-crew"
    crew_model_id                   = "example-model"
  }
  assert {
    condition = length(aws_vpc_security_group_ingress_rule.sts_from_guest) == 1 && alltrue([
      for rule in aws_vpc_security_group_ingress_rule.sts_from_guest :
      rule.security_group_id == "sg-33333333333333333" && rule.referenced_security_group_id == aws_security_group.guest.id &&
      rule.ip_protocol == "tcp" && rule.from_port == 443 && rule.to_port == 443 && rule.cidr_ipv4 == null && rule.cidr_ipv6 == null
    ])
    error_message = "Private STS must accept only HTTPS from the exact guest security group, never a CIDR."
  }
}

run "no_private_sts_adds_no_ingress" {
  command = apply
  variables {
    gateway_execute_arn   = "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev"
    crew_profile_revision = "example-crew"
    crew_model_id         = "example-model"
  }
  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.sts_from_guest) == 0
    error_message = "VPCs without private STS must need no endpoint rule or manual input."
  }
}
