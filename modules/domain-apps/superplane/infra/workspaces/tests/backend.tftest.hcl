# =============================================================================
# Per-workspace state isolation — Issue #5532 (w6-09) design item 2, AC-01.
# =============================================================================
# "Separate state and ownership from core ADP and other workspaces."
#
# This file pins the property the whole module depends on: the state key carries the
# workspace, so one workspace's plan cannot compute a destroy against another workspace's
# cluster. See main.tf for what a diff against a shared record actually does.
#
# Credential-free: `mock_provider` means no AWS call is made and no credentials are read.
# `command = plan` means nothing is created.
# =============================================================================

mock_provider "aws" {
  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::111122223333:role/workspace-provisioner" }
  }

  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
  }

  # aws_iam_policy_document is a data source whose `json` attribute the IAM roles consume.
  # Mocked to a valid empty policy so the roles have something well-formed to reference.
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

# The caller identity must agree with var.account_id or main.tf's target guard fails the
# plan — which is itself tested, deliberately, in no_inherited_defaults.tftest.hcl.
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

run "state_key_is_scoped_to_the_workspace" {
  command = plan

  assert {
    condition     = output.state_key_convention == "dev/modules/superplane-workspaces/v2/test-org/tenant-alpha/terraform.tfstate"
    error_message = "The state key must carry both the environment and the workspace name. Got: ${output.state_key_convention}"
  }
}

run "state_key_is_not_the_control_plane_or_platform_key" {
  command = plan

  assert {
    # ../control-plane/ uses "<env>/modules/superplane/terraform.tfstate" and the platform
    # uses "<env>/platform/terraform.tfstate". Sharing either would put a tenant's cluster
    # in core ADP's state object, where a platform apply would plan to destroy it.
    condition = (
      output.state_key_convention != "dev/modules/superplane/terraform.tfstate" &&
      output.state_key_convention != "dev/platform/terraform.tfstate"
    )
    error_message = "The workspace state key must differ from the control-plane and platform state keys: separate state from core ADP is the requirement (design item 2)."
  }
}

run "a_different_workspace_gets_a_different_state_key_and_different_resource_names" {
  command = plan

  variables {
    environment     = "dev"
    workspace_name  = "tenant-beta"
    workspace_id    = "tenant-beta"
    account_id      = "111122223333"
    aws_region      = "us-east-1"
    cluster_version = "1.31"

    networking_mode    = "owned"
    vpc_cidr           = "10.65.0.0/16"
    availability_zones = ["us-east-1a", "us-east-1b"]
  }

  # The isolation claim has two halves, and this run asserts both. A distinct state key
  # keeps the RECORDS apart; distinct resource names keep the RESOURCES apart. With shared
  # names, two workspaces would collide on the EKS cluster name and the second apply would
  # fail — or worse, adopt the first workspace's cluster.
  assert {
    condition     = output.state_key_convention == "dev/modules/superplane-workspaces/v2/test-org/tenant-beta/terraform.tfstate"
    error_message = "A second workspace must resolve to its own state key. Got: ${output.state_key_convention}"
  }

  assert {
    condition     = output.cluster_name == "adp-dev-spw-ffeb5ecb37ddea64907a9e5f736b87bb"
    error_message = "The cluster name must carry the workspace name, so two workspaces cannot collide on it. Got: ${output.cluster_name}"
  }
}

run "the_same_workspace_in_a_different_environment_is_a_different_workspace" {
  command = plan

  variables {
    environment     = "staging"
    workspace_name  = "tenant-alpha"
    workspace_id    = "tenant-alpha"
    account_id      = "111122223333"
    aws_region      = "us-east-1"
    cluster_version = "1.31"

    networking_mode    = "owned"
    vpc_cidr           = "10.64.0.0/16"
    availability_zones = ["us-east-1a", "us-east-1b"]
  }

  assert {
    condition     = output.state_key_convention == "staging/modules/superplane-workspaces/v2/test-org/tenant-alpha/terraform.tfstate"
    error_message = "The environment must select the state key, so a staging workspace never shares state with its dev namesake. Got: ${output.state_key_convention}"
  }

  assert {
    condition     = output.cluster_name == "adp-staging-spw-970b320a868d197402321c9d69957998"
    error_message = "Resource names must carry the environment as well as the workspace. Got: ${output.cluster_name}"
  }
}

run "the_module_actually_plans_resources" {
  command = plan

  # THE ANTI-VACUOUS CHECK. Every assertion above reads an output, and outputs would still
  # resolve if this module declared no resources at all — a module that creates nothing
  # passes a state-key test perfectly. This run asserts the plan contains the cluster, the
  # network and the identities, so the tests above are known to be describing a module that
  # does something.
  assert {
    condition     = aws_eks_cluster.workspace.name == "adp-dev-spw-970b320a868d197402321c9d69957998"
    error_message = "The module must actually declare an EKS cluster for the assertions above to be meaningful."
  }

  assert {
    condition     = length(aws_subnet.private) == 2
    error_message = "Owned mode must create one private subnet per availability zone."
  }

  assert {
    condition     = aws_iam_role.cluster.name == "adp-dev-spw-970b320a868d197402321c9d69957998-cluster-role"
    error_message = "The module must declare the cluster IAM role."
  }
}

run "same_display_name_different_org_is_isolated" {
  command = plan
  variables { org_id = "other-org" }
  assert {
    condition     = output.cluster_name != run.state_key_is_scoped_to_the_workspace.cluster_name && output.state_key_convention != run.state_key_is_scoped_to_the_workspace.state_key_convention
    error_message = "Same-name workspaces in different organizations must not share resources or state."
  }
  assert {
    condition     = output.ownership_tags.OrgId == "other-org" && output.ownership_tags.WorkspaceId == var.workspace_id
    error_message = "Owned infrastructure must carry both immutable IDs."
  }
}

run "same_display_name_different_workspace_id_is_isolated" {
  command = plan
  variables { workspace_id = "another-immutable-id" }
  assert {
    condition     = output.cluster_name != run.state_key_is_scoped_to_the_workspace.cluster_name && output.state_key_convention != run.state_key_is_scoped_to_the_workspace.state_key_convention
    error_message = "Different workspace IDs must not share resources or state even with equal display names."
  }
}

run "renaming_does_not_replace_workspace_infrastructure" {
  command = plan
  variables { workspace_name = "display-renamed" }
  assert {
    condition     = output.cluster_name == run.state_key_is_scoped_to_the_workspace.cluster_name && output.state_key_convention == run.state_key_is_scoped_to_the_workspace.state_key_convention
    error_message = "A display-name edit must preserve immutable infrastructure identity."
  }
}

run "invalid_bound_org_id_refused" {
  command = plan
  variables { org_id = "tenant/path" }
  expect_failures = [var.org_id]
}

run "missing_bound_workspace_id_refused" {
  command = plan
  variables { workspace_id = "" }
  expect_failures = [var.workspace_id]
}
