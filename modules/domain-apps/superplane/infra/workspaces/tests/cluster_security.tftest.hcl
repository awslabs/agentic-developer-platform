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

# ---------------------------------------------------------------------------
# ENDPOINT EXPOSURE
# ---------------------------------------------------------------------------
run "the_api_endpoint_is_private_and_not_public_by_default" {
  command = plan

  assert {
    condition     = aws_eks_cluster.workspace.vpc_config[0].endpoint_private_access == true
    error_message = "Private endpoint access must always be on: it is how the ADP control plane and the workspace's own nodes reach the API without traversing the internet."
  }

  assert {
    # The vendored graph sets this true with no CIDR restriction, publishing every
    # workspace's Kubernetes API to the whole internet.
    condition     = aws_eks_cluster.workspace.vpc_config[0].endpoint_public_access == false
    error_message = "Public endpoint access must default to OFF. A workspace whose API is internet-reachable because nobody set a variable is an exposure nobody chose."
  }

  assert {
    condition     = length(aws_eks_cluster.workspace.vpc_config[0].public_access_cidrs) == 0
    error_message = "With public access off, the CIDR allowlist must be empty so it never reads as an effective allowlist."
  }
}

run "public_access_requires_a_bounded_allowlist_and_is_recorded_in_the_outputs" {
  command = plan

  variables {
    cluster_endpoint_public_access       = true
    cluster_endpoint_public_access_cidrs = ["203.0.113.0/24"]
  }

  assert {
    condition = (
      aws_eks_cluster.workspace.vpc_config[0].endpoint_public_access == true &&
      aws_eks_cluster.workspace.vpc_config[0].public_access_cidrs == toset(["203.0.113.0/24"])
    )
    error_message = "Enabling public access with a bounded allowlist must work — the control is meant to be usable, not merely restrictive."
  }

  assert {
    # Published so an auditor can answer "is this workspace's API exposed?" from state,
    # without reading the tfvars that produced it.
    condition     = output.cluster_endpoint_public_access == true
    error_message = "The exposure must be visible in the outputs."
  }
}

run "an_internet_open_allowlist_is_refused" {
  command = plan

  variables {
    cluster_endpoint_public_access       = true
    cluster_endpoint_public_access_cidrs = ["0.0.0.0/0"]
  }

  # This is the vendored graph's effective posture, stated explicitly. Refused.
  expect_failures = [var.cluster_endpoint_public_access_cidrs]
}

run "the_ipv6_form_of_an_open_allowlist_is_refused_too" {
  command = plan

  variables {
    cluster_endpoint_public_access = true
    # The same defect in IPv6 clothing, which a check for the IPv4 literal alone would pass.
    cluster_endpoint_public_access_cidrs = ["::/0"]
  }

  expect_failures = [var.cluster_endpoint_public_access_cidrs]
}

run "public_access_without_an_allowlist_is_refused" {
  command = plan

  variables {
    cluster_endpoint_public_access       = true
    cluster_endpoint_public_access_cidrs = []
  }

  # Enabling public access and naming nobody means every address — the same outcome as
  # 0.0.0.0/0, reached by omission instead of by statement.
  expect_failures = [var.cluster_endpoint_public_access_cidrs]
}

run "an_allowlist_with_public_access_off_is_refused_rather_than_ignored" {
  command = plan

  variables {
    cluster_endpoint_public_access       = false
    cluster_endpoint_public_access_cidrs = ["203.0.113.0/24"]
  }

  # A populated-but-ineffective allowlist reads as protection that is not in force. The
  # author believed one of these two values; they contradict.
  expect_failures = [var.cluster_endpoint_public_access_cidrs]
}

# ---------------------------------------------------------------------------
# ENCRYPTION
# ---------------------------------------------------------------------------
run "kubernetes_secrets_are_envelope_encrypted_with_a_customer_managed_key" {
  command = plan

  assert {
    # The vendored graph declares no encryptionConfig at all. Note this cannot be added to an
    # existing cluster by an in-place update in every EKS version, which is the practical
    # reason it must be right at creation rather than "added later".
    condition     = aws_eks_cluster.workspace.encryption_config[0].resources == toset(["secrets"])
    error_message = "The cluster must envelope-encrypt Kubernetes Secrets. Without this, Secrets sit in etcd under AWS's default encryption with no customer-managed key."
  }

  assert {
    condition     = length(aws_kms_key.workspace) == 1
    error_message = "With no key supplied, the module must create a workspace-scoped one — a workspace must never be created with no customer-managed key at all."
  }

  assert {
    condition     = aws_kms_key.workspace[0].enable_key_rotation == true
    error_message = "The workspace key must have rotation enabled."
  }

  assert {
    condition     = aws_kms_key.workspace[0].deletion_window_in_days == 30
    error_message = "The key must have the maximum deletion window: a workspace deleted by mistake is recoverable within it, and an encrypted snapshot outliving its key is not recoverable at all."
  }
}

run "a_supplied_key_is_used_and_no_workspace_key_is_created" {
  command = plan

  variables {
    kms_key_arn = "arn:aws:kms:us-east-1:111122223333:key/12345678-1234-1234-1234-123456789012"
  }

  assert {
    condition     = length(aws_kms_key.workspace) == 0
    error_message = "When the operator supplies a key, this module must not create a second one — a key created here is destroyed with the workspace, which is why the supplied case is preferable in production."
  }

  assert {
    condition     = aws_eks_cluster.workspace.encryption_config[0].provider[0].key_arn == "arn:aws:kms:us-east-1:111122223333:key/12345678-1234-1234-1234-123456789012"
    error_message = "The supplied key must be the one the cluster encrypts Secrets with."
  }

  assert {
    condition     = output.kms_key_arn == "arn:aws:kms:us-east-1:111122223333:key/12345678-1234-1234-1234-123456789012"
    error_message = "The effective key must be published, so a consumer need not know which branch produced it."
  }
}

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
run "all_control_plane_logs_are_enabled_with_bounded_retention" {
  command = plan

  assert {
    condition = aws_eks_cluster.workspace.enabled_cluster_log_types == toset([
      "api", "audit", "authenticator", "controllerManager", "scheduler"
    ])
    error_message = "All five control-plane log types must be enabled by default: an audit trail whose contents depend on a per-workspace choice is not an audit trail."
  }

  assert {
    # Declared explicitly because the group EKS creates IMPLICITLY has never-expire
    # retention — an unbounded cost and, for audit logs, a compliance decision nobody made.
    condition     = aws_cloudwatch_log_group.cluster.retention_in_days == 90
    error_message = "The log group must have a bounded, reviewed retention. Got: ${aws_cloudwatch_log_group.cluster.retention_in_days}"
  }

  assert {
    # If this name is wrong, EKS silently creates its own never-expiring group and this one
    # stays empty — a failure that looks exactly like working logging.
    condition     = aws_cloudwatch_log_group.cluster.name == "/aws/eks/adp-dev-spw-970b320a868d197402321c9d69957998/cluster"
    error_message = "The log group name must be the path EKS writes to. Got: ${aws_cloudwatch_log_group.cluster.name}"
  }
}

run "omitting_the_audit_log_type_is_refused" {
  command = plan

  variables {
    cluster_log_types = ["api", "authenticator", "controllerManager", "scheduler"]
  }

  # "Who did what in this workspace" must stay answerable after the fact.
  expect_failures = [var.cluster_log_types]
}

run "never_expiring_log_retention_is_refused" {
  command = plan

  variables {
    # 0 means "never expire" in CloudWatch. Logs that never expire and logs that vanish are
    # both operational defects; this input offers neither.
    log_retention_days = 0
  }

  expect_failures = [var.log_retention_days]
}

# ---------------------------------------------------------------------------
# ACCESS AND IDENTITY
# ---------------------------------------------------------------------------
run "the_applying_principal_does_not_become_a_cluster_administrator" {
  command = plan

  assert {
    # The AWS provider DEFAULTS this to true, which means whichever CI role or human ran the
    # apply gets permanent cluster-admin on a tenant's cluster: an access grant nobody
    # reviewed, tied to an identity that may be a shared CI role.
    condition     = aws_eks_cluster.workspace.access_config[0].bootstrap_cluster_creator_admin_permissions == false
    error_message = "The creating principal must NOT silently become a cluster administrator. Operator access is granted explicitly through the workspace admin role."
  }

  assert {
    condition     = aws_eks_cluster.workspace.access_config[0].authentication_mode == "API"
    error_message = "Authentication must use EKS access entries, not the legacy aws-auth ConfigMap: access entries are auditable AWS resources, and a ConfigMap's history is whatever is in the cluster now."
  }
}

run "the_cluster_has_its_own_oidc_trust_anchor" {
  command = plan

  assert {
    # The vendored graph declares no OIDC provider, so workspace workloads fall back to
    # node-role credentials — meaning every pod on a node shares that node's permissions.
    condition     = aws_iam_openid_connect_provider.cluster.client_id_list == toset(["sts.amazonaws.com"])
    error_message = "The cluster must have an IAM OIDC provider so workloads can use per-workload identity instead of sharing the node role."
  }
}

run "no_workspace_admin_role_exists_until_an_operator_is_named" {
  command = plan

  assert {
    condition     = length(aws_iam_role.workspace_admin) == 0
    error_message = "With no principal named, no admin role may exist: its existence would imply an operator does."
  }

  assert {
    # Null rather than an empty string, so a consumer cannot treat "not configured" as a
    # valid ARN.
    condition     = output.workspace_admin_role_arn == null
    error_message = "workspace_admin_role_arn must be null when no operator is named."
  }
}

run "a_named_operator_gets_a_role_scoped_to_this_cluster_only" {
  command = plan

  variables {
    workspace_admin_principal_arns = ["arn:aws:iam::111122223333:user/on-call-operator"]
  }

  assert {
    condition     = length(aws_iam_role.workspace_admin) == 1
    error_message = "Naming an operator must create the admin role."
  }

  assert {
    # An operation, not a working day.
    condition     = aws_iam_role.workspace_admin[0].max_session_duration == 3600
    error_message = "The workspace admin session must be bounded to an hour, not the 12-hour maximum."
  }

  assert {
    # The role NAME, not the ARN. An ARN is computed by AWS, so at plan time it is unknown
    # and a condition referencing it cannot be evaluated — Terraform reports "Unknown
    # condition value" and the run errors rather than failing an assertion.
    #
    # The null case in the run above works for a different reason: there the count is zero,
    # so the output is literally null rather than an unknown string. The asymmetry is worth
    # recording, because "assert the ARN is null" passing does NOT imply "assert the ARN is
    # non-null" can pass. Name and count are known at plan time; the ARN is not.
    condition     = aws_iam_role.workspace_admin[0].name == "adp-dev-spw-970b320a868d197402321c9d69957998-admin"
    error_message = "The admin role must carry the workspace-scoped name prefix. Got: ${aws_iam_role.workspace_admin[0].name}"
  }
}

run "an_account_root_principal_is_refused_as_a_workspace_operator" {
  command = plan

  variables {
    # A trust policy naming the account root is assumable by EVERY principal in that
    # account — which, in a workspace account, includes the tenant's own roles and every
    # role created there in future.
    workspace_admin_principal_arns = ["arn:aws:iam::111122223333:root"]
  }

  expect_failures = [var.workspace_admin_principal_arns]
}

# ---------------------------------------------------------------------------
# NETWORK PLACEMENT AND BOUNDED CAPACITY
# ---------------------------------------------------------------------------
run "nodes_run_in_private_subnets_only" {
  command = plan

  # WHY THIS ASSERTS COUNTS AND CIDRS RATHER THAN COMPARING SUBNET IDS
  #
  # The direct assertion — that the node group's subnet ids do not intersect the public
  # subnets' ids — cannot be made here. In owned mode both sets are AWS-assigned and unknown
  # at plan time, so Terraform reports "Unknown condition value" and the run ERRORS rather
  # than failing; every later run in the file is then skipped, which is a worse outcome than
  # the missing check.
  #
  # The invariant is covered in three complementary places instead:
  #   * supplied_networking.tftest.hcl asserts the exact ids, because there they are literal
  #     inputs rather than computed values — the same assertion, where it is expressible.
  #   * tests/test_least_privilege.py asserts at the source level that the node group's
  #     subnet_ids is local.private_subnet_ids, which is the expression that makes public
  #     placement unreachable in BOTH modes.
  #   * this run asserts the counts and the disjoint CIDR ranges, which plan does know.
  # Note the assertion is on `aws_subnet.private`, not on the node group's `subnet_ids`.
  # Even `length(aws_eks_node_group.default.subnet_ids) == 2` is unevaluable at plan time:
  # the set's SIZE is known but its members are AWS-assigned, and Terraform marks the whole
  # collection unknown rather than partially known. Verified by the error it produced.
  assert {
    condition     = length(aws_subnet.private) == 2
    error_message = "Owned mode must create exactly one private subnet per availability zone for the node group to be placed in."
  }

  assert {
    # Private and public carves must not overlap. cidrsubnet offsets guarantee it, and a
    # future change to the carve that broke it would land here.
    condition = length(setintersection(
      toset(aws_subnet.private[*].cidr_block),
      toset(aws_subnet.public[*].cidr_block)
    )) == 0
    error_message = "Private and public subnet CIDR ranges must be disjoint."
  }

  assert {
    condition     = aws_subnet.public[0].map_public_ip_on_launch == false
    error_message = "Public subnets must not auto-assign public IPs — that default is how an instance becomes internet-reachable without its author choosing it."
  }
}

run "node_capacity_is_bounded_so_a_cost_ceiling_exists" {
  command = plan

  assert {
    # Design item 4 requires bounded cost estimates. An unbounded node group has no upper
    # bill, so the estimate would have no ceiling to compute against.
    condition     = aws_eks_node_group.default.scaling_config[0].max_size == 2
    error_message = "The node group's maximum must be the bounded default. Got: ${aws_eks_node_group.default.scaling_config[0].max_size}"
  }
}

run "a_maximum_below_the_desired_size_is_refused" {
  command = plan

  variables {
    node_group_desired_size = 5
    node_group_max_size     = 2
  }

  # Would be accepted by AWS as a contradiction resolved at runtime; refused here so the
  # reviewed ceiling and the requested size cannot disagree.
  expect_failures = [var.node_group_max_size]
}

run "the_cluster_security_group_admits_nothing_from_outside_the_vpc" {
  command = plan

  # There is no ingress rule resource in this module at all — the assertion is the absence,
  # and Terraform cannot reference a resource that is not declared. So this checks the
  # egress rule is the only rule, and tests/test_least_privilege.py asserts at the source
  # level that no ingress rule has been added.
  assert {
    condition     = aws_vpc_security_group_egress_rule.cluster_all.ip_protocol == "-1"
    error_message = "Egress must be explicit (a security group with no rules allows nothing out, which breaks image pulls). Ingress — the direction that admits an attacker — is empty."
  }
  assert {
    condition = (
      length(aws_vpc_security_group_egress_rule.cluster_all.description) < 256 &&
      length(regexall("[^a-zA-Z0-9. _:/()#,@\\[\\]+=&;{}!$*-]", aws_vpc_security_group_egress_rule.cluster_all.description)) == 0
    )
    error_message = "EC2 security-group rule descriptions must use the API's allowed characters; apostrophes cause a live apply failure."
  }
}

run "supplied_kms_wrong_region_is_refused" {
  command = plan
  variables { kms_key_arn = "arn:aws:kms:us-west-2:111122223333:key/12345678-1234-1234-1234-123456789012" }
  expect_failures = [var.kms_key_arn]
}

run "supplied_kms_wrong_account_is_refused" {
  command = plan
  variables { kms_key_arn = "arn:aws:kms:us-east-1:999988887777:key/12345678-1234-1234-1234-123456789012" }
  expect_failures = [var.kms_key_arn]
}

run "supplied_kms_wrong_partition_is_refused" {
  command = plan
  variables { kms_key_arn = "arn:aws-us-gov:kms:us-east-1:111122223333:key/12345678-1234-1234-1234-123456789012" }
  expect_failures = [var.kms_key_arn]
}

run "noncanonical_internet_open_allowlist_is_refused" {
  command = plan
  variables {
    cluster_endpoint_public_access       = true
    cluster_endpoint_public_access_cidrs = ["1.2.3.4/0"]
  }
  expect_failures = [var.cluster_endpoint_public_access_cidrs]
}

run "node_image_is_an_exact_reviewed_release" {
  command = plan
  assert {
    condition     = aws_eks_node_group.default.ami_type == "AL2023_x86_64_STANDARD" && aws_eks_node_group.default.release_version == "1.31.14-20260917" && aws_eks_node_group.default.version == "1.31"
    error_message = "The managed node group must use the reviewed AL2023 release, not a moving recommendation."
  }
  assert {
    condition     = output.node_image_pin.release_version == aws_eks_node_group.default.release_version && output.node_image_pin.region == "us-east-1"
    error_message = "The selected image pin must appear in reviewable outputs."
  }
}

run "desired_size_below_minimum_is_refused" {
  command = plan
  variables {
    node_group_min_size     = 5
    node_group_desired_size = 1
    node_group_max_size     = 10
  }
  expect_failures = [var.node_group_desired_size]
}

run "equal_minimum_desired_and_maximum_is_accepted" {
  command = plan
  variables {
    node_group_min_size     = 2
    node_group_desired_size = 2
    node_group_max_size     = 2
  }
  assert {
    condition     = aws_eks_node_group.default.scaling_config[0].desired_size == 2
    error_message = "A valid equal-bound scaling tuple must remain accepted."
  }
}
