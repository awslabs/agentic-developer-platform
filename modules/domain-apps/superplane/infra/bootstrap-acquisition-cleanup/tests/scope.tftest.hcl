mock_provider "aws" {
  mock_data "aws_iam_role" {
    defaults = {
      arn       = "arn:aws:iam::123456789012:role/adp-dev-box-operator"
      unique_id = "AROABCDEFGHIJKLMNOPQR"
    }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:sts::123456789012:assumed-role/adp-dev-box-operator/operator-session"
      user_id    = "AROABCDEFGHIJKLMNOPQR:operator-session"
    }
  }
}

variables {
  account_id          = "123456789012"
  region              = "us-west-2"
  cluster_name        = "workspace"
  generation          = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  installer_role_name = "adp-dev-box-operator"
  installer_role_id   = "AROABCDEFGHIJKLMNOPQR"
  expires_at          = timeadd(timestamp(), "30m")
  entry_arns = [
    "arn:aws:eks:us-west-2:123456789012:access-entry/workspace/role/123456789012/registrar/immutable-1",
    "arn:aws:eks:us-west-2:123456789012:access-entry/workspace/role/123456789012/controller/immutable-2",
    "arn:aws:eks:us-west-2:123456789012:access-entry/workspace/role/123456789012/installer/immutable-3",
  ]
}

run "enrolled_local_operator_has_exact_temporary_authority" {
  command = plan
  assert {
    condition     = jsondecode(aws_iam_role.cleanup.assume_role_policy).Statement[0].Principal.AWS == "arn:aws:iam::123456789012:role/adp-dev-box-operator"
    error_message = "Only the verified enrolled operator may assume the cleanup role."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role.cleanup.assume_role_policy).Statement[0].Condition.DateLessThan["aws:CurrentTime"] == var.expires_at &&
      jsondecode(aws_iam_role.cleanup.assume_role_policy).Statement[0].Condition.StringEquals["sts:RoleSessionName"] == local.name &&
      aws_iam_role.cleanup.max_session_duration == 3600
    )
    error_message = "Cleanup trust must retain its exact session name and expiry."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.installer_cleanup.policy).Statement[0].Resource == local.cleanup_arn &&
      toset(jsondecode(aws_iam_role_policy.installer_cleanup.policy).Statement[0].Action) == toset(["sts:AssumeRole"]) &&
      toset(jsondecode(aws_iam_role_policy.installer_cleanup.policy).Statement[1].Resource) == var.entry_arns &&
      toset(jsondecode(aws_iam_role_policy.installer_cleanup.policy).Statement[1].Action) == toset(["eks:DeleteAccessEntry", "eks:DisassociateAccessPolicy"]) &&
      jsondecode(aws_iam_role_policy.installer_cleanup.policy).Statement[1].Condition.DateLessThan["aws:CurrentTime"] == var.expires_at
    )
    error_message = "Operator cleanup permissions must remain limited to the cleanup role and three original immutable access entries."
  }
  assert {
    condition = (
      length(aws_iam_role.cleanup.inline_policy) == 1 &&
      toset(jsondecode(one(aws_iam_role.cleanup.inline_policy).policy).Statement[0].Resource) == var.entry_arns &&
      toset(jsondecode(one(aws_iam_role.cleanup.inline_policy).policy).Statement[0].Action) == toset(["eks:DeleteAccessEntry", "eks:DisassociateAccessPolicy"]) &&
      jsondecode(one(aws_iam_role.cleanup.inline_policy).policy).Statement[0].Condition.DateLessThan["aws:CurrentTime"] == var.expires_at
    )
    error_message = "The disposable role must support native immutable-entry session restriction without requiring the owner to self-assume."
  }
  assert {
    condition = (
      aws_eks_access_entry.cleanup.principal_arn == local.cleanup_arn &&
      aws_eks_access_entry.cleanup.cluster_name == var.cluster_name &&
      aws_eks_access_policy_association.cleanup.cluster_name == var.cluster_name &&
      aws_eks_access_policy_association.cleanup.principal_arn == local.cleanup_arn
    )
    error_message = "Disposable Kubernetes authority must belong to this cleanup role and workspace cluster."
  }
}

run "legacy_canonical_installer_still_supported" {
  command = plan
  variables {
    installer_role_name = "adp-dev-superplane-installer-0123456789abcdef"
    installer_role_id   = null
  }
  assert {
    condition     = length(data.aws_iam_role.installer) == 0 && length(data.aws_caller_identity.installer) == 0
    error_message = "Existing canonical installer callers must not require a new enrollment lookup."
  }
}

run "local_operator_requires_enrollment" {
  command = plan
  variables {
    installer_role_id = null
  }
  expect_failures = [aws_iam_role.cleanup]
}

run "recreated_role_id_refused" {
  command = plan
  override_data {
    target = data.aws_iam_role.installer[0]
    values = {
      arn       = "arn:aws:iam::123456789012:role/adp-dev-box-operator"
      unique_id = "AROABCDEFGHIJKLMNOPQS"
    }
  }
  expect_failures = [aws_iam_role.cleanup]
}

run "foreign_role_arn_refused" {
  command = plan
  override_data {
    target = data.aws_iam_role.installer[0]
    values = {
      arn       = "arn:aws:iam::123456789012:role/other-operator"
      unique_id = "AROABCDEFGHIJKLMNOPQR"
    }
  }
  expect_failures = [aws_iam_role.cleanup]
}

run "foreign_caller_role_id_refused" {
  command = plan
  override_data {
    target = data.aws_caller_identity.installer[0]
    values = {
      account_id = "123456789012"
      arn        = "arn:aws:sts::123456789012:assumed-role/adp-dev-box-operator/operator-session"
      user_id    = "AROABCDEFGHIJKLMNOPQS:operator-session"
    }
  }
  expect_failures = [aws_iam_role.cleanup]
}

run "foreign_caller_account_refused" {
  command = plan
  override_data {
    target = data.aws_caller_identity.installer[0]
    values = {
      account_id = "999999999999"
      arn        = "arn:aws:sts::999999999999:assumed-role/adp-dev-box-operator/operator-session"
      user_id    = "AROABCDEFGHIJKLMNOPQR:operator-session"
    }
  }
  expect_failures = [aws_iam_role.cleanup]
}

run "foreign_caller_role_name_refused" {
  command = plan
  override_data {
    target = data.aws_caller_identity.installer[0]
    values = {
      account_id = "123456789012"
      arn        = "arn:aws:sts::123456789012:assumed-role/adp-dev-box-operator-extra/operator-session"
      user_id    = "AROABCDEFGHIJKLMNOPQR:operator-session"
    }
  }
  expect_failures = [aws_iam_role.cleanup]
}

run "foreign_cluster_entry_refused" {
  command = plan
  variables {
    cluster_name = "other-workspace"
  }
  expect_failures = [aws_iam_role_policy.installer_cleanup]
}

run "additional_original_actor_refused" {
  command = plan
  variables {
    entry_arns = [
      "arn:aws:eks:us-west-2:123456789012:access-entry/workspace/role/123456789012/registrar/immutable-1",
      "arn:aws:eks:us-west-2:123456789012:access-entry/workspace/role/123456789012/controller/immutable-2",
      "arn:aws:eks:us-west-2:123456789012:access-entry/workspace/role/123456789012/installer/immutable-3",
      "arn:aws:eks:us-west-2:123456789012:access-entry/workspace/role/123456789012/unrelated/immutable-4",
    ]
  }
  expect_failures = [var.entry_arns]
}

run "expired_authority_refused" {
  command = plan
  variables {
    expires_at = timeadd(timestamp(), "-1m")
  }
  expect_failures = [aws_iam_role.cleanup]
}

run "standing_authority_refused" {
  command = plan
  variables {
    expires_at = timeadd(timestamp(), "3h")
  }
  expect_failures = [aws_iam_role.cleanup]
}
