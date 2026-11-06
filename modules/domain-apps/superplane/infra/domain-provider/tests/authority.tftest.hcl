mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "111122223333" } }
  mock_data "aws_iam_role" { defaults = { arn = "arn:aws:iam::111122223333:role/gateway", unique_id = "AROAABCDEFGHIJKLMNOP" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::111122223333:policy/fixture-child-boundary" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::111122223333:role/fixture-provider", unique_id = "AROAABCDEFGHIJKLMNOP" } }
}
variables {
  account_id                 = "111122223333"
  aws_region                 = "us-east-1"
  environment                = "dev"
  installation_id            = "aaaaaaaaaaaaaaaaaaaaaaaa"
  org_id                     = "00000000-0000-4000-8000-000000000001"
  workspace_id               = "00000000-0000-5000-8000-000000000002"
  gateway_role_arn           = "arn:aws:iam::111122223333:role/gateway"
  gateway_role_id            = "AROAABCDEFGHIJKLMNOP"
  beneficiary_user_id        = "fixture-beneficiary"
  external_id                = "fixture-only-not-a-real-external-id-0000"
  node_image_repository_arns = ["arn:aws:ecr:us-east-1:111122223333:repository/reviewed-controller"]
}
run "preparation_has_no_execution_grants" {
  command = plan
  assert {
    condition     = length(aws_iam_policy.execution) == 0 && output.execution_policy_configured == false
    error_message = "Provider identity preparation must grant no executable policy or claim readiness."
  }
}
run "active_policy" {
  command = apply
  variables {
    execution = {
      kms_key_arn                   = "arn:aws:kms:us-east-1:111122223333:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
      actor_role_arns               = ["arn:aws:iam::111122223333:role/registrar", "arn:aws:iam::111122223333:role/installer", "arn:aws:iam::111122223333:role/supervisor"]
      state_bucket                  = "fixture-state"
      lock_table                    = "fixture-locks"
      node_image_ids                = ["ami-0123456789abcdef1"]
      node_instance_types           = ["t3.small"]
      validation_image_id           = "ami-0123456789abcdef0"
      validation_instance_type      = "t3.small"
      validation_subnet_id          = "subnet-0123456789abcdef0"
      validation_security_group_ids = ["sg-0123456789abcdef0"]
    }
  }
  assert {
    condition     = length(aws_iam_policy.execution) == 4 && alltrue([for policy in aws_iam_policy.execution : length(policy.policy) <= 6144]) && length(aws_iam_policy.child_boundary.policy) <= 6144
    error_message = "Exact four provider shards and the child boundary must fit AWS managed-policy size limits."
  }
  assert {
    condition     = alltrue([for statement in jsondecode(aws_iam_policy.child_boundary.policy).Statement : statement.Effect != "Allow" || alltrue([for action in statement.Action : !startswith(action, "iam:") && !startswith(action, "sts:") && !startswith(action, "secretsmanager:")])])
    error_message = "A child role must never obtain IAM, chained credentials or secrets."
  }
  assert {
    condition = (
      alltrue([for action in ["ec2:DescribeAddressesAttribute", "ec2:DescribePrefixLists"] :
        contains(jsondecode(aws_iam_policy.execution["state-validation"].policy).Statement[0].Action, action) &&
        !contains(local.child_reads, action)
      ]) &&
      jsondecode(aws_iam_policy.execution["state-validation"].policy).Statement[0].Condition.StringEquals["aws:RequestedRegion"] == var.aws_region
    )
    error_message = "The provisioner needs the regional EIP and endpoint prefix-list reads used by Terraform; node and CNI roles do not."
  }
  assert {
    condition = (
      length(local.node_launch_statements) == 4 &&
      jsonencode(local.node_launch_statements[0].Resource) == jsonencode(["arn:aws:ec2:us-east-1::image/ami-0123456789abcdef1", "${local.ec2_prefix}:security-group/*"]) &&
      jsonencode(local.node_launch_statements[1].Condition.StringEquals["ec2:InstanceType"]) == jsonencode(["t3.small"]) &&
      local.node_launch_statements[1].Condition.StringEquals["ec2:MetadataHttpTokens"] == "required" &&
      alltrue([for statement in slice(local.node_launch_statements, 0, 3) :
        statement.Condition.ArnLike["ec2:LaunchTemplate"] == "${local.ec2_prefix}:launch-template/*" &&
        statement.Condition.StringEquals["aws:PrincipalTag/adp:agent_id"] == "superplane-operation"
      ]) &&
      local.node_launch_statements[3].Condition.StringEquals["aws:ResourceTag/WorkspaceId"] == var.workspace_id &&
      local.node_launch_statements[3].Condition.StringEquals["aws:ResourceTag/OrgId"] == var.org_id &&
      contains(local.execution_policy.Statement[3].Condition.StringEquals["ec2:CreateAction"], "RunInstances")
    )
    error_message = "Tagged EKS nodes need owned template/subnet, pinned image/type, IMDSv2 and atomic ownership tags; validator permissions alone are insufficient."
  }

  assert {
    condition = (
      jsonencode(local.execution_policy.Statement[53]) == jsonencode({
        Effect    = "Allow", Action = ["eks:DescribeAccessEntry"]
        Resource  = "${local.eks_prefix}:access-entry/${local.workspace_name}/*"
        Condition = { StringEquals = local.operation }
      }) &&
      jsonencode(local.execution_policy.Statement[11].Action) == jsonencode(["eks:ListAssociatedAccessPolicies", "eks:DeleteAccessEntry", "eks:DisassociateAccessPolicy", "eks:TagResource"]) &&
      toset(local.execution_policy.Statement[11].Resource) == toset([for arn in var.execution.actor_role_arns : "${local.eks_prefix}:access-entry/${local.workspace_name}/role/${var.account_id}/${basename(arn)}/*"]) &&
      local.execution_policy.Statement[10].Condition.StringEquals["aws:RequestTag/OrgId"] == var.org_id &&
      local.execution_policy.Statement[10].Condition.StringEquals["aws:RequestTag/WorkspaceId"] == var.workspace_id &&
      toset(local.execution_policy.Statement[10].Condition.StringEquals["eks:principalArn"]) == var.execution.actor_role_arns
    )
    error_message = "Node and other principal entries need exact-cluster description; mutations must remain restricted to the bootstrap actors."
  }
  assert {
    condition = jsonencode(jsondecode(aws_iam_policy.execution["identity"].policy).Statement[10]) == jsonencode({
      Effect   = "Allow", Action = ["iam:GetRole"]
      Resource = "arn:aws:iam::111122223333:role/aws-service-role/eks-nodegroup.amazonaws.com/AWSServiceRoleForAmazonEKSNodegroup"
    })
    error_message = "EKS must read only its exact existing nodegroup service role, without account-wide IAM mutation."
  }
}
run "closed_partition_covers_every_statement_once" {
  command = plan
  assert {
    condition     = sort([for index in flatten(values(local.policy_shard_indexes)) : tostring(index)]) == sort([for index in range(54) : tostring(index)])
    error_message = "The exact four shards must partition every intended statement with no omission or duplicate."
  }
}
run "gateway_recreation_is_refused" {
  command = plan
  variables { gateway_role_id = "AROAOTHERROLEIDENTITY" }
  expect_failures = [terraform_data.verified_gateway]
}
run "session_tag_trust_uses_supported_conditions" {
  command = plan
  assert {
    condition = (
      length(jsondecode(aws_iam_role.provider.assume_role_policy).Statement) == 2 &&
      jsondecode(aws_iam_role.provider.assume_role_policy).Statement[0].Action == "sts:AssumeRole" &&
      jsondecode(aws_iam_role.provider.assume_role_policy).Statement[1].Action == "sts:TagSession" &&
      jsondecode(aws_iam_role.provider.assume_role_policy).Statement[0].Condition.StringEquals["sts:ExternalId"] == var.external_id &&
      !contains(keys(jsondecode(aws_iam_role.provider.assume_role_policy).Statement[1].Condition.StringEquals), "sts:ExternalId") &&
      alltrue([for statement in jsondecode(aws_iam_role.provider.assume_role_policy).Statement :
        statement.Principal.AWS == var.gateway_role_arn &&
        statement.Condition.StringEquals["aws:RequestTag/adp:user_id"] == var.beneficiary_user_id &&
        statement.Condition.StringEquals["aws:RequestTag/adp:agent_id"] == ["superplane-operation", "superplane-provider-validation"] &&
        statement.Condition.StringEquals["aws:RequestTag/adp:persona"] == ["superplane-operation", "superplane-provider-validation"] &&
        statement.Condition.StringLike["aws:RequestTag/adp:task_id"] == "?*" &&
        statement.Condition["ForAllValues:StringEquals"]["aws:TagKeys"] == ["adp:user_id", "adp:agent_id", "adp:task_id", "adp:persona"]
      ])
    )
    error_message = "ExternalId must protect AssumeRole while both actions retain exact principal and session-tag restrictions."
  }
}

run "workspace_and_management_regions_are_independent" {
  command = apply
  variables {
    aws_region                 = "us-west-2"
    state_region               = "us-east-1"
    management_region          = "us-east-1"
    node_image_repository_arns = ["arn:aws:ecr:us-east-1:111122223333:repository/reviewed-controller"]
    execution = {
      kms_key_arn                   = "arn:aws:kms:us-west-2:111122223333:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
      actor_role_arns               = ["arn:aws:iam::111122223333:role/registrar", "arn:aws:iam::111122223333:role/installer", "arn:aws:iam::111122223333:role/supervisor"]
      state_bucket                  = "fixture-state"
      lock_table                    = "fixture-locks"
      node_image_ids                = ["ami-0123456789abcdef1"]
      node_instance_types           = ["t3.small"]
      validation_image_id           = "ami-0123456789abcdef0"
      validation_instance_type      = "t3.small"
      validation_subnet_id          = "subnet-0123456789abcdef0"
      validation_security_group_ids = ["sg-0123456789abcdef0"]
    }
  }
  assert {
    condition     = contains(jsondecode(aws_iam_policy.execution["state-validation"].policy).Statement[0].Action, "tag:GetResources") && jsondecode(aws_iam_policy.execution["state-validation"].policy).Statement[0].Condition.StringEquals["aws:RequestedRegion"] == "us-west-2" && !contains(jsondecode(aws_iam_policy.execution["state-validation"].policy).Statement[0].Action, "tag:TagResources")
    error_message = "Applied inventory may discover tagged resources only in the workspace region, without tag mutation."
  }
  assert {
    condition     = alltrue([for statement in jsondecode(aws_iam_policy.execution["state-validation"].policy).Statement : !contains(statement.Action, "dynamodb:DescribeTable") && !contains(statement.Action, "dynamodb:PutItem") || statement.Resource == "arn:aws:dynamodb:us-east-1:111122223333:table/fixture-locks"])
    error_message = "State locks must stay in their configured region."
  }
  assert {
    condition     = jsondecode(aws_iam_policy.execution["state-validation"].policy).Statement[0].Condition.StringEquals["aws:RequestedRegion"] == "us-west-2" && length(local.execution_policy.Statement) == 55 && jsonencode(local.execution_policy.Statement[54].Action) == jsonencode(["ec2:DescribeNatGateways"]) && local.execution_policy.Statement[54].Condition.StringEquals["aws:RequestedRegion"] == "us-east-1"
    error_message = "Only NAT observation may read the management region."
  }
  assert {
    condition     = alltrue([for policy in aws_iam_policy.execution : length(policy.policy) <= 6144])
    error_message = "Regional provider shards must fit AWS policy limits."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_policy.child_boundary.policy).Statement : statement if contains(statement.Action, "ecr:GetAuthorizationToken") && try(contains(statement.Condition.StringEquals["aws:RequestedRegion"], "us-east-1"), false)]) == 1
    error_message = "Regional nodes need token access for the exact reviewed image regions."
  }
}

run "additional_gateway_provider_grant_is_exact" {
  command = apply
  variables { gateway_access_enabled = true }
  assert {
    condition     = length(aws_iam_policy.gateway_access) == 1 && aws_iam_role_policy_attachment.gateway_access[0].role == "gateway" && jsondecode(aws_iam_policy.gateway_access[0].policy).Statement[0].Resource == aws_iam_role.provider.arn && jsondecode(aws_iam_policy.gateway_access[0].policy).Statement[3].Resource == aws_secretsmanager_secret.external_id.arn
    error_message = "Additional Gateway access must name only this provider role and exact secret."
  }
}
