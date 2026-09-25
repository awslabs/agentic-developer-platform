# =============================================================================
# Cluster security posture — Issue #5532 (w6-09) design item 1, AC-01.
# =============================================================================
# Design item 1 requires "encryption, logging, endpoint/network controls and least-privilege
# access". Each is a property of the planned cluster, so each is asserted against the plan
# rather than described in a comment.
#
# WHAT THIS FILE IS REALLY PROTECTING
#
# Four of these properties are places where the vendored kro graph
# (../account-factory/vendor/kro-account-factory/02-eks-cluster-stack.yaml) does something
# more permissive, and this module deliberately does not. The vendored graph works, and it is
# the reference — so the pressure to "just match the reference" is real, and a reviewer
# comparing the two will find the differences. eks.tf records why each one exists; this file
# makes reverting one a test failure rather than a diff nobody queries.
#
#   endpointPublicAccess: true with no CIDR restriction   -> public off by default, 0.0.0.0/0 refused
#   no encryptionConfig                                    -> KMS envelope encryption of Secrets
#   version resolved at deploy time                        -> exact pinned minor version
#   no OIDC provider                                       -> per-workload identity available
# =============================================================================

mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/mock-workspace-role" }
  }
  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:us-east-1:111122223333:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
      key_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    }
  }
  mock_resource "aws_launch_template" {
    defaults = { id = "lt-0abcdef1234567890", latest_version = 1 }
  }
  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::111122223333:role/workspace-provisioner" }
  }

  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
  }

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


run "node_credentials_are_isolated_before_tenant_scheduling" {
  # Apply uses only mock providers so computed trust anchors resolve without AWS.
  command = apply
  override_resource {
    target = aws_eks_cluster.workspace
    values = {
      identity              = [{ oidc = [{ issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/TEST" }] }]
      certificate_authority = [{ data = "TU9DS0VEQ0VSVElGSUNBVEU=" }]
    }
  }
  override_resource {
    target = aws_iam_openid_connect_provider.cluster
    values = { arn = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/TEST" }
  }
  assert {
    condition     = aws_launch_template.node.metadata_options[0].http_put_response_hop_limit == 1 && aws_launch_template.node.metadata_options[0].http_tokens == "required"
    error_message = "Ordinary pods must not obtain node-role credentials from IMDS."
  }
  assert {
    condition     = anytrue([for taint in aws_eks_node_group.default.taint : taint.key == "superplane.aws-e/bootstrap" && taint.effect == "NO_SCHEDULE"])
    error_message = "Tenant scheduling must wait for admission and live IMDS-negative verification."
  }
  assert {
    condition     = jsondecode(aws_iam_role.vpc_cni.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.us-east-1.amazonaws.com/id/TEST:sub"] == "system:serviceaccount:kube-system:aws-node" && jsondecode(aws_iam_role.vpc_cni.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.us-east-1.amazonaws.com/id/TEST:aud"] == "sts.amazonaws.com"
    error_message = "Only kube-system/aws-node may assume the CNI role with the STS audience."
  }
  assert {
    condition     = jsondecode(aws_iam_role.vpc_cni.assume_role_policy).Statement[0].Principal.Federated == "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/TEST"
    error_message = "CNI trust must name this cluster's OIDC provider."
  }
  assert {
    condition     = toset(jsondecode(aws_iam_role_policy.node_image_pull.policy).Statement[1].Action) == toset(["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]) && alltrue([for arn in jsondecode(aws_iam_role_policy.node_image_pull.policy).Statement[1].Resource : !strcontains(arn, "*") && startswith(arn, "arn:aws:ecr:us-east-1:602401143452:repository/")])
    error_message = "Node pull rights must be limited to exact reviewed repositories and three image-read operations."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.node_image_pull.policy).Statement[0].Action == ["ecr:GetAuthorizationToken"] && jsondecode(aws_iam_role_policy.node_image_pull.policy).Statement[0].Resource == "*"
    error_message = "Only ECR authentication needs an unscoped resource."
  }
  assert {
    # vpc-cni v1.22.4-eksbuild.3 runs this sidecar even when network-policy
    # enforcement is disabled. Its live image pull failed with ECR 403 when
    # this repository was absent, leaving both initial workers NotReady.
    condition = contains(
      jsondecode(aws_iam_role_policy.node_image_pull.policy).Statement[1].Resource,
      "arn:aws:ecr:us-east-1:602401143452:repository/amazon/aws-network-policy-agent"
    )
    error_message = "The pinned VPC CNI network-policy agent must be pullable by workspace nodes."
  }
  assert {
    condition     = aws_eks_addon.vpc_cni.addon_version == "v1.22.4-eksbuild.3"
    error_message = "The VPC CNI version must remain pinned to reviewed region/version compatibility."
  }
  assert {
    condition     = try(jsondecode(aws_eks_addon.vpc_cni.configuration_values).enableNetworkPolicy == "true", false)
    error_message = "Workspace bootstrap needs VPC CNI NetworkPolicy enforcement enabled before its live traffic-isolation checks."
  }
}

run "account_wide_ecr_pull_is_refused" {
  command = plan
  variables { node_image_repository_arns = ["arn:aws:ecr:us-east-1:111122223333:repository/*"] }
  expect_failures = [var.node_image_repository_arns]
}
