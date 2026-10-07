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
}
run "closed_partition_covers_every_statement_once" {
  command = plan
  assert {
    condition     = sort([for index in flatten(values(local.policy_shard_indexes)) : tostring(index)]) == sort([for index in range(48) : tostring(index)])
    error_message = "The exact four shards must partition every intended statement with no omission or duplicate."
  }
}
run "gateway_recreation_is_refused" {
  command = plan
  variables { gateway_role_id = "AROAOTHERROLEIDENTITY" }
  expect_failures = [terraform_data.verified_gateway]
}
