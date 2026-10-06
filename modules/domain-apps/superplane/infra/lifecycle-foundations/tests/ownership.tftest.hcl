mock_provider "aws" {}

override_data {
  target = data.aws_caller_identity.current
  values = { account_id = "111122223333" }
}
override_data {
  target = data.aws_iam_role.provider
  values = {
    arn       = "arn:aws:iam::111122223333:role/reviewed-provider"
    unique_id = "AROAEXACTPROVIDER12345"
  }
}
override_data {
  target = data.aws_iam_role.autoscaling
  values = { arn = "arn:aws:iam::111122223333:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling" }
}
variables {
  account_id                = "111122223333"
  aws_region                = "us-east-1"
  environment               = "dev"
  org_id                    = "11111111-1111-4111-8111-111111111111"
  workspace_id              = "22222222-2222-4222-8222-222222222222"
  provider_role_arn         = "arn:aws:iam::111122223333:role/reviewed-provider"
  expected_provider_role_id = "AROAEXACTPROVIDER12345"
  actor_role_names = {
    registrar  = "fixture-registrar"
    installer  = "fixture-installer"
    supervisor = "fixture-supervisor"
  }
}

run "exact_actor_trust_and_retained_key_policy" {
  command = plan
  assert {
    condition = alltrue([
      for role in aws_iam_role.actor :
      jsondecode(role.assume_role_policy).Statement == [{
        Sid       = "OnlyReviewedWorkspaceProvider", Effect = "Allow",
        Principal = { AWS = var.provider_role_arn }, Action = "sts:AssumeRole"
      }]
    ])
    error_message = "Every actor must trust only the exact selected provider role."
  }
  assert {
    condition = (
      jsondecode(aws_kms_key.retained.policy).Statement[1].Principal.AWS == var.provider_role_arn &&
      !can(jsondecode(aws_kms_key.retained.policy).Statement[1].Condition) &&
      toset(jsondecode(aws_kms_key.retained.policy).Statement[1].Action) == toset(["kms:DescribeKey", "kms:CreateGrant"])
    )
    error_message = "EKS provisioning must use the reviewed caller and the real CreateGrant contract."
  }
  assert {
    condition = (
      jsondecode(aws_kms_key.retained.policy).Statement[2].Condition.ArnEquals["kms:EncryptionContext:aws:logs:arn"] ==
      "arn:aws:logs:us-east-1:111122223333:log-group:/aws/eks/adp-dev-spw-${substr(sha256(jsonencode([var.org_id, var.workspace_id])), 0, 32)}/cluster"
    )
    error_message = "Logs authority must be the immutable workspace's exact log group."
  }
  assert {
    condition = (
      jsondecode(aws_kms_key.retained.policy).Statement[3].Condition.StringEquals["kms:ViaService"] == "ec2.us-east-1.amazonaws.com" &&
      jsondecode(aws_kms_key.retained.policy).Statement[4].Condition.Bool["kms:GrantIsForAWSResource"] == "true" &&
      !contains(keys(aws_kms_key.retained.tags), "WorkspaceId") &&
      aws_kms_key.retained.tags.Lifecycle == "retained-independent-owner"
    )
    error_message = "AutoScaling must retain its distinct conditions and key ownership must stay independent."
  }
}

run "recreated_provider_role_is_refused" {
  command = plan
  variables {
    expected_provider_role_id = "AROAANOTHERPROVIDER123"
  }
  expect_failures = [terraform_data.verified_owner]
}
run "foreign_account_role_is_refused" {
  command = plan
  variables {
    provider_role_arn = "arn:aws:iam::444455556666:role/foreign"
  }
  expect_failures = [var.provider_role_arn]
}
run "aliased_actor_roles_are_refused" {
  command = plan
  variables {
    actor_role_names = { registrar = "shared", installer = "shared", supervisor = "third" }
  }
  expect_failures = [var.actor_role_names]
}
run "display_name_is_not_workspace_identity" {
  command = plan
  variables {
    workspace_id = "demo-one"
  }
  expect_failures = [var.workspace_id]
}
