mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::000000000101:role/adp-eval-staging-test" }
  }
  mock_resource "aws_iam_instance_profile" {
    defaults = { arn = "arn:aws:iam::000000000101:instance-profile/adp-eval-staging-test" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:000000000101:key/00000000-0000-0000-0000-000000000001", key_id = "00000000-0000-0000-0000-000000000001" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::adp-eval-staging-test-000000000101" }
  }
}
override_data {
  target = data.aws_ssm_parameter.gateway_internal_invoke_url
  values = { value = "https://abc123.execute-api.us-east-1.amazonaws.com/dev" }
}
override_data {
  target = data.aws_ssm_parameter.agent_registry_table
  values = { value = "adp-dev-agent-registry" }
}
variables {
  config = {
    account              = "000000000101", region = "us-east-1", repository = "example/adp", target = "staging",
    resource_environment = "dev",
    prefix               = "adp-eval-staging-test", bucket = "adp-eval-staging-test-000000000101",
    cluster              = "adp-dev-eks-cluster", namespace = "adp-gateway", image_repository = "adp-gateway",
    subnet               = "subnet-00000000000000001", security_group = "sg-00000000000000001",
    user_pool            = "us-east-1_Example001", fixture_arn = "arn:aws:secretsmanager:us-east-1:000000000101:secret:adp/dev/gateway/test-admin-credentials-AbCdEf",
    fixture_kms_arn      = "", instance_type = "t3.small", destination_roles = [], create_oidc = true
  }
}
run "scoped_identity_and_lifecycle" {
  command = apply
  assert {
    condition     = data.aws_ssm_parameter.gateway_internal_invoke_url[0].name == "/adp/dev/gateway/apigw-invoke-url" && data.aws_ssm_parameter.agent_registry_table[0].name == "/adp/dev/gateway/agent-registry-table"
    error_message = "Gateway parameters must use the AWS resource namespace independently of the GitHub target."
  }
  assert {
    condition     = jsondecode(aws_iam_role.evaluator[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:example/adp:environment:staging" && length(jsondecode(aws_iam_role.evaluator[0].assume_role_policy).Statement) == 1
    error_message = "Only the selected protected GitHub environment may assume the evaluator."
  }
  assert {
    condition     = jsondecode(aws_iam_role.evaluator[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    error_message = "OIDC audience must remain exact."
  }
  assert {
    condition     = [for s in jsondecode(aws_iam_role_policy.evaluator[0].policy).Statement : s if try(s.Sid, "") == "LaunchOwnedInstances"][0].Condition.StringEquals["aws:RequestTag/adp:evaluator-target"] == var.config.prefix
    error_message = "EC2 launches must carry environment ownership."
  }
  assert {
    condition     = alltrue([for s in jsondecode(aws_iam_role_policy.evaluator[0].policy).Statement : s.Condition.StringEquals["ec2:ResourceTag/adp:evaluator-target"] == var.config.prefix if contains(s.Action, "ec2:TerminateInstances")])
    error_message = "Cleanup must not terminate another environment's instances."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.evaluator[0].policy).Statement : s if contains(s.Action, "secretsmanager:GetSecretValue")]) == 0
    error_message = "The orchestrator must not receive fixture passwords."
  }
  assert {
    condition     = [for s in jsondecode(aws_iam_role_policy.instance[0].policy).Statement : s.Resource if contains(s.Action, "secretsmanager:GetSecretValue")] == [var.config.fixture_arn]
    error_message = "The instance may read only its designated fixture secret."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.product_retire[0].policy).Statement == [{
      Effect   = "Allow", Action = ["execute-api:Invoke"],
      Resource = "arn:aws:execute-api:us-east-1:000000000101:abc123/dev/POST/internal/v1/product-evaluation/retire"
    }] && length([for statement in jsondecode(aws_iam_role_policy.evaluator[0].policy).Statement : statement if contains(statement.Action, "execute-api:Invoke")]) == 0
    error_message = "Only the instance can invoke the exact protected Product retirement method."
  }
  assert {
    condition     = aws_iam_role.instance[0].tags["adp:managed-by"] == "evaluator-setup"
    error_message = "The Gateway's tagged role-identity read must match the evaluator instance."
  }
  assert {
    condition = (
      jsondecode(aws_dynamodb_table_item.product_evaluator[0].item).agent_id.S == "00000000-0000-0000-0000-000000008243" &&
      jsondecode(aws_dynamodb_table_item.product_evaluator[0].item).iam_role_id.S == aws_iam_role.instance[0].unique_id &&
      jsondecode(aws_dynamodb_table_item.product_evaluator[0].item).credential_scopes.SS == ["internal:product-fixture:retire"]
    )
    error_message = "Retirement must be granted only to the current registered instance role."
  }
  assert {
    condition = (
      aws_eks_access_policy_association.gateway_view[0].policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy" &&
      aws_eks_access_policy_association.gateway_view[0].principal_arn == aws_iam_role.evaluator[0].arn &&
      aws_eks_access_policy_association.gateway_view[0].cluster_name == var.config.cluster &&
      one(aws_eks_access_policy_association.gateway_view[0].access_scope).type == "namespace" &&
      one(aws_eks_access_policy_association.gateway_view[0].access_scope).namespaces == toset([var.config.namespace])
    )
    error_message = "Gateway observation must use namespace-scoped read-only EKS authority."
  }
  assert {
    condition     = aws_eks_access_entry.evaluator[0].kubernetes_groups == toset(["adp:evaluator:${var.config.prefix}"])
    error_message = "Gateway access must use the dedicated observer group."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.evaluator[0].policy).Statement : s if contains(s.Action, "kms:GenerateDataKey") && s.Resource == aws_kms_key.state.arn]) == 1 && length([for s in jsondecode(aws_iam_role_policy.instance[0].policy).Statement : s if contains(s.Action, "kms:Decrypt") && s.Resource == aws_kms_key.state.arn]) == 1
    error_message = "Both evaluator and worker need narrowly scoped KMS access for the recovery/bundle contract."
  }
  assert {
    condition     = alltrue([for policy in [aws_iam_role_policy.evaluator[0].policy, aws_iam_role_policy.instance[0].policy] : length([for s in jsondecode(policy).Statement : s if contains(s.Action, "s3:PutObjectAcl")]) == 1]) && one(aws_s3_bucket_ownership_controls.state.rule).object_ownership == "BucketOwnerEnforced"
    error_message = "Recovery writes send the bucket-owner-full-control header; allow the scoped request while enforcing bucket ownership."
  }
  assert {
    condition     = one(aws_s3_bucket_server_side_encryption_configuration.state.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "aws:kms"
    error_message = "The bucket must match the evaluator's mandatory KMS encryption contract."
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.state.block_public_acls && aws_s3_bucket_public_access_block.state.block_public_policy && aws_s3_bucket_public_access_block.state.ignore_public_acls && aws_s3_bucket_public_access_block.state.restrict_public_buckets
    error_message = "Evaluation evidence must remain private."
  }
}

run "remove_credentials_retain_evidence_and_shared_provider" {
  command = apply
  variables { credentials_enabled = false }
  assert {
    condition     = length(aws_iam_role.evaluator) == 0 && length(aws_iam_role.instance) == 0 && length(aws_iam_instance_profile.instance) == 0 && length(aws_eks_access_entry.evaluator) == 0 && length(aws_eks_access_policy_association.gateway_view) == 0 && length(aws_iam_role_policy.evaluator) == 0 && length(aws_iam_role_policy.instance) == 0 && length(aws_iam_role_policy.product_retire) == 0 && length(aws_dynamodb_table_item.product_evaluator) == 0 && length(aws_iam_role_policy_attachment.ssm) == 0
    error_message = "Deletion must remove all evaluator credentials and EKS access."
  }
  assert {
    condition     = aws_s3_bucket.state.id != "" && aws_kms_key.state.id != "" && aws_kms_alias.state.name != "" && length(aws_iam_openid_connect_provider.github) == 1 && aws_s3_bucket_public_access_block.state.block_public_policy
    error_message = "Evidence, encryption, bucket protection and shared OIDC must survive deletion."
  }
}
run "restore_credentials" {
  command = apply
  assert {
    condition     = length(aws_iam_role.evaluator) == 1 && length(aws_eks_access_entry.evaluator) == 1
    error_message = "Setup must support reinstalling credentials over retained evidence."
  }
}

run "legacy_resource_namespace_follows_target" {
  command = plan
  variables {
    config = merge(var.config, { resource_environment = null })
  }
  assert {
    condition     = data.aws_ssm_parameter.gateway_internal_invoke_url[0].name == "/adp/staging/gateway/apigw-invoke-url" && data.aws_ssm_parameter.agent_registry_table[0].name == "/adp/staging/gateway/agent-registry-table"
    error_message = "Existing evaluator inputs must retain their original namespace when the option is absent."
  }
}
