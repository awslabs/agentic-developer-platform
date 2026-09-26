# Run the sibling app-owned native root in the existing credential-free CI job.
# Terraform test alternate module paths resolve from this root, not this file.
mock_provider "aws" {
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::111122223333:policy/native-fixture" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/native-fixture" }
  }
}

variables {
  lane = {
    name                     = "fixture-native"
    account_id               = "111122223333"
    region                   = "us-east-1"
    vpc_id                   = "vpc-0123456789abcdef0"
    build_subnet_ids         = ["subnet-0123456789abcdef0"]
    build_security_group_id  = "sg-0123456789abcdef0"
    helper_subnet_id         = "subnet-1123456789abcdef0"
    helper_security_group_id = "sg-1123456789abcdef0"
    helper_ami_id            = "ami-0123456789abcdef0"
    helper_instance_type     = "m6i.large"
    source_snapshot_ids      = ["snap-0123456789abcdef0"]
    kms_key_arn              = "arn:aws:kms:us-east-1:111122223333:key/01234567-89ab-cdef-0123-456789abcdef"
    input_bucket_name        = "fixture-native-input"
    output_bucket_name       = "fixture-native-output"
    environment_image        = "registry.example/build@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    compute_type             = "BUILD_GENERAL1_MEDIUM"
    retention_days           = 90
    timeout_minutes          = 150
    dispatcher_role_arn      = "arn:aws:iam::111122223333:role/existing-dispatcher"
  }
}

run "native_lane_has_bounded_cloud_authority" {
  # Provider is mocked: apply resolves computed mock ARNs, never contacts AWS.
  command = apply
  module { source = "../native-image" }
  override_resource {
    target = aws_iam_role.helper
    values = { arn = "arn:aws:iam::111122223333:role/fixture-native-helper" }
  }
  override_resource {
    target = aws_iam_instance_profile.helper
    values = { arn = "arn:aws:iam::111122223333:instance-profile/fixture-native-helper" }
  }
  override_resource {
    target = aws_cloudwatch_log_group.native
    values = { arn = "arn:aws:logs:us-east-1:111122223333:log-group:/aws/codebuild/fixture-native" }
  }
  override_resource {
    target = aws_s3_bucket.native["input"]
    values = { arn = "arn:aws:s3:::fixture-native-input" }
  }
  override_resource {
    target = aws_s3_bucket.native["output"]
    values = { arn = "arn:aws:s3:::fixture-native-output" }
  }
  assert {
    condition     = alltrue([for statement in local.build_statements : !contains(statement.Action, "ec2:DeregisterImage") && !(contains(statement.Action, "ec2:CreateTags") && can(regex(":image/", jsonencode(statement.Resource))))])
    error_message = "The native build role must not tag or delete untagged images."
  }
  assert {
    condition     = one([for statement in local.build_statements : statement.Condition.StringEquals["aws:ResourceTag/superplane-native-caller"] if statement.Sid == "RegisterFromCallerSnapshot"]) == "$${aws:userid}"
    error_message = "RegisterImage snapshot authorization must be bound to the current role session."
  }
  assert {
    condition     = alltrue([for statement in local.build_statements : statement.Resource == "arn:aws:iam::111122223333:role/fixture-native-helper" if contains(statement.Action, "iam:PassRole")])
    error_message = "Only the exact dedicated readonly helper role may be passed."
  }
  assert {
    condition     = length(jsonencode(local.boundary_policy)) <= 6144 && alltrue([for policy in aws_iam_policy.build : length(policy.policy) <= 6144])
    error_message = "IAM policies must fit the actual AWS managed policy document limit."
  }
  assert {
    condition     = aws_codebuild_project.native.concurrent_build_limit == 1 && aws_codebuild_project.native.source[0].buildspec == "modules/domain-apps/superplane/releases/buildspecs/native-node-lane.yml"
    error_message = "The dedicated project must use the maintained lane and bounded concurrency."
  }
  assert {
    condition     = alltrue([for bucket in aws_s3_bucket_public_access_block.native : bucket.block_public_acls && bucket.block_public_policy && bucket.ignore_public_acls && bucket.restrict_public_buckets]) && alltrue([for bucket in aws_s3_bucket.native : !bucket.force_destroy])
    error_message = "Source and evidence remain private and cannot be force-destroyed."
  }
  assert {
    condition     = alltrue(flatten([for lifecycle in aws_s3_bucket_lifecycle_configuration.native : [for rule in lifecycle.rule : contains(["native-input/", "codebuild/", "builds/"], rule.filter[0].prefix)]]))
    error_message = "Dispatch claims and durable receipts must not be covered by automatic expiry."
  }
  assert {
    condition     = alltrue([for statement in local.build_statements : strcontains(jsonencode(statement.Resource), "/receipts/*") && !strcontains(jsonencode(statement.Resource), "/dispatch/") if statement.Sid == "EvidenceWrite"])
    error_message = "Build may retain reconciliation receipts but must not overwrite dispatcher claims."
  }

}
