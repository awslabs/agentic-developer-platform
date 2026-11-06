mock_provider "aws" {}

override_data {
  target = data.aws_caller_identity.operator
  values = { account_id = "123456789012", user_id = "AROAABCDEFGHIJKLMNOPQ:operator", arn = "arn:aws:sts::123456789012:assumed-role/SelectedOperator/operator" }
}
override_data {
  target = data.aws_iam_role.operator
  values = { arn = "arn:aws:iam::123456789012:role/SelectedOperator", unique_id = "AROAABCDEFGHIJKLMNOPQ" }
}
override_data {
  target = data.aws_eks_cluster.management
  values = { arn = "arn:aws:eks:us-east-1:123456789012:cluster/selected-management" }
}

variables {
  account_id            = "123456789012"
  region                = "us-east-1"
  environment           = "test"
  cluster_name          = "selected-management"
  installation_id       = "1234567890abcdef12345678"
  operator_role_arn     = "arn:aws:iam::123456789012:role/SelectedOperator"
  operator_role_id      = "AROAABCDEFGHIJKLMNOPQ"
  namespaces            = ["example-domain", "example-sky"]
  installer_policy_json = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"sts:GetCallerIdentity\",\"Resource\":\"*\"}]}"
}

run "bounded_default" {
  command = plan
  assert {
    condition     = !output.native_runtime_state_access_enabled && aws_iam_role_policy.installer.policy == var.installer_policy_json
    error_message = "Omitting native runtime setup must preserve the supplied policy without extra grants."
  }
  assert {
    condition     = aws_eks_access_policy_association.installer.access_scope[0].type == "namespace" && toset(aws_eks_access_policy_association.installer.access_scope[0].namespaces) == toset(["example-domain", "example-sky", "sp-preflight-*"])
    error_message = "Default installation access must remain namespace-scoped."
  }
  assert {
    condition     = jsondecode(aws_iam_role.installer.assume_role_policy).Statement[0].Principal.AWS == var.operator_role_arn && length(jsondecode(aws_iam_role.installer.assume_role_policy).Statement) == 1
    error_message = "Only the selected bootstrap operator may assume the dedicated installer role."
  }
  assert {
    condition     = aws_eks_access_entry.installer.kubernetes_groups == toset(["adp:superplane:installer:1234567890abcdef12345678"])
    error_message = "Installer uses only its app-owned group."
  }
}
run "native_runtime_state_is_exact" {
  command = plan
  variables { native_runtime_state_access = true }
  assert {
    condition = output.native_runtime_state_access_enabled && length(jsondecode(aws_iam_role_policy.installer.policy).Statement) == 4 && jsonencode(slice(jsondecode(aws_iam_role_policy.installer.policy).Statement, 1, 4)) == jsonencode([
      {
        Sid      = "NativeRuntimeState", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"],
        Resource = ["arn:aws:s3:::adp-terraform-state-123456789012/test/modules/superplane-domain-runtime/terraform.tfstate"]
      },
      {
        Sid       = "NativeRuntimeStateList", Effect = "Allow", Action = ["s3:ListBucket"],
        Resource  = ["arn:aws:s3:::adp-terraform-state-123456789012"],
        Condition = { StringLike = { "s3:prefix" = ["test/modules/superplane-domain-runtime/*"] } }
      },
      {
        Sid      = "NativeRuntimeStateLocks", Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"],
        Resource = ["arn:aws:dynamodb:us-east-1:123456789012:table/adp-terraform-locks"],
        Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [
          "adp-terraform-state-123456789012/test/modules/superplane-domain-runtime/terraform.tfstate",
          "adp-terraform-state-123456789012/test/modules/superplane-domain-runtime/terraform.tfstate-md5"
        ] } }
      }
    ])
    error_message = "Native setup must grant only the exact selected state object, prefix listing and two lock keys."
  }
  assert {
    condition     = aws_eks_access_policy_association.installer.access_scope[0].type == "namespace" && !output.namespace_bootstrap_active
    error_message = "Native state access must not enable cluster bootstrap privileges."
  }
}
run "reject_hand_authored_native_statement" {
  command = plan
  variables {
    native_runtime_state_access = true
    installer_policy_json       = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"NativeRuntimeState\",\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"*\"}]}"
  }
  expect_failures = [aws_iam_role_policy.installer]
}
run "explicit_bootstrap" {
  command = plan
  variables { namespace_bootstrap = true }
  assert {
    condition     = aws_eks_access_policy_association.installer.access_scope[0].type == "cluster" && output.namespace_bootstrap_active
    error_message = "Bootstrap privilege must be explicit in the plan and output."
  }
}
run "different_role_id_refused" {
  command = plan
  variables { operator_role_id = "AROAQRSTUVWXYZABCDEFG" }
  expect_failures = [aws_iam_role.installer]
}
