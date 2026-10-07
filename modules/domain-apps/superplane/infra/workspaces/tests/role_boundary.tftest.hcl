# Explicit boundary on every role; this verifies wiring, not policy sufficiency.
mock_provider "aws" {
  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::111122223333:role/workspace-provisioner" }
  }

  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
  }

  # Present for the reason every suite in this module has it: without it
  # `aws_iam_role.*.assume_role_policy` is an unresolved value and the plan errors. It makes
  # the trust STATEMENTS unassertable here, which is why test_admin_trust_separation.py
  # exists; nothing in this file asserts on the mocked document.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

mock_provider "tls" {
  mock_data "tls_certificate" {
    defaults = {
      certificates = [
        {
          sha1_fingerprint = "9e99a48a9960b14926bb7f3b02e22da2b0ab7280"
        }
      ]
    }
  }
}

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "111122223333"
    arn        = "arn:aws:sts::111122223333:assumed-role/workspace-provisioner/test"
  }
}

override_data {
  target = data.aws_partition.current
  values = {
    partition = "aws"
  }
}

variables {
  org_id          = "test-org"
  environment     = "dev"
  workspace_name  = "tenant-alpha"
  workspace_id    = "tenant-alpha"
  account_id      = "111122223333"
  aws_region      = "us-east-1"
  cluster_version = "1.31"

  networking_mode    = "owned"
  vpc_cidr           = "10.64.0.0/16"
  availability_zones = ["us-east-1a", "us-east-1b"]
}

run "boundary_reaches_every_created_role" {
  command = plan
  variables {
    workspace_role_permissions_boundary_arn = "arn:aws:iam::111122223333:policy/owner/workspace-services"
    workspace_admin_automation_role_arns    = ["arn:aws:iam::111122223333:role/approved-provider"]
  }
  assert {
    condition = alltrue([
      aws_iam_role.cluster.permissions_boundary == var.workspace_role_permissions_boundary_arn,
      aws_iam_role.node.permissions_boundary == var.workspace_role_permissions_boundary_arn,
      aws_iam_role.vpc_cni.permissions_boundary == var.workspace_role_permissions_boundary_arn,
      aws_iam_role.workspace_admin[0].permissions_boundary == var.workspace_role_permissions_boundary_arn,
    ])
    error_message = "Every created workspace role must carry the exact reviewed boundary."
  }
  assert {
    condition     = length(aws_default_security_group.workspace) == 0
    error_message = "Governed mode must not claim an initially untagged AWS default security group."
  }
  assert {
    condition     = jsondecode(jsondecode(aws_eks_addon.vpc_cni.configuration_values).env.ADDITIONAL_ENI_TAGS).WorkspaceId == var.workspace_id && jsondecode(jsondecode(aws_eks_addon.vpc_cni.configuration_values).env.ADDITIONAL_ENI_TAGS).OrgId == var.org_id
    error_message = "CNI-created interfaces need both immutable ownership tags at creation."
  }
  assert {
    condition     = length([for specification in aws_launch_template.node.tag_specifications : specification if specification.resource_type == "network-interface" && specification.tags.OrgId == var.org_id && specification.tags.WorkspaceId == var.workspace_id]) == 1
    error_message = "Primary node interfaces need ownership before CNI may tag or change them."
  }
}

run "existing_external_account_config_is_unchanged" {
  command = plan
  assert {
    condition     = aws_iam_role.cluster.permissions_boundary == null && aws_iam_role.node.permissions_boundary == null && aws_iam_role.vpc_cni.permissions_boundary == null
    error_message = "Omitting the boundary must preserve existing external-account configurations."
  }
}

run "foreign_boundary_is_refused" {
  command = plan
  variables { workspace_role_permissions_boundary_arn = "arn:aws:iam::444455556666:policy/foreign" }
  expect_failures = [var.workspace_role_permissions_boundary_arn]
}

run "wildcard_boundary_is_refused" {
  command = plan
  variables { workspace_role_permissions_boundary_arn = "arn:aws:iam::111122223333:policy/owner/*" }
  expect_failures = [var.workspace_role_permissions_boundary_arn]
}

run "aws_managed_boundary_is_refused" {
  command = plan
  variables { workspace_role_permissions_boundary_arn = "arn:aws:iam::aws:policy/AdministratorAccess" }
  expect_failures = [var.workspace_role_permissions_boundary_arn]
}
