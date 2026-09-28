# Managed infrastructure for one immutable org/workspace binding (#5532, EPIC A #4910).
# VPC/EKS/IAM live in a separate workspace cluster, never in ADP management state.
# Required org_id/workspace_id originate in the trusted provisioning OperationBinding.
# Display names cannot identify tenants. State uses both full IDs; deterministic AWS names
# use the 128-bit identifier below. Supplied networks/keys are read and never adopted.
# No Kubernetes objects, AWS accounts or shared account prerequisites are owned here.
# Bootstrap/registration and provider execution belong to #5533/#5534; merging this
# module is code readiness, not live apply authorization. See README.md for migration.

data "aws_caller_identity" "current" {}

data "aws_iam_session_context" "provisioner" {
  arn = data.aws_caller_identity.current.arn
}

data "aws_partition" "current" {}

locals {
  # Compact canonical JSON over safe ASCII IDs is identical to workspace_identity.py.
  # Full IDs remain in state keys/tags; display-name changes cannot replace resources.
  infrastructure_id = substr(sha256(jsonencode([var.org_id, var.workspace_id])), 0, 32)
  name_prefix       = "adp-${var.environment}-spw-${local.infrastructure_id}"

  # The EKS cluster name is the resource identity other components resolve this workspace
  # by, so it is derived — not a separate input that could disagree with the prefix.
  cluster_name = local.name_prefix

  partition = data.aws_partition.current.partition

  # Which networking mode is active. Every network resource in network.tf is gated on
  # `owned`, and tests/test_networking_modes.py asserts that gating holds for each one, so
  # `supplied` mode provably declares no network resource.
  owns_network = var.networking_mode == "owned"

  # The subnets the cluster and node group are placed in, resolved from whichever mode is
  # active. One expression, so the cluster declaration does not branch on mode.
  private_subnet_ids = local.owns_network ? aws_subnet.private[*].id : var.supplied_private_subnet_ids

  vpc_id = local.owns_network ? aws_vpc.workspace[0].id : data.aws_vpc.supplied[0].id

  # The KMS key used for Secret envelope encryption and node EBS volumes: the operator's
  # key when supplied, otherwise the workspace-scoped key created in eks.tf.
  kms_key_arn = var.kms_key_arn != "" ? var.kms_key_arn : aws_kms_key.workspace[0].arn

  # The control-plane log group's name and ARN, CONSTRUCTED rather than read back from
  # `aws_cloudwatch_log_group.cluster`.
  #
  # This is not a style choice — it breaks a dependency cycle that would otherwise make the
  # module un-plannable. The log group is encrypted with the workspace key, so the group
  # depends on the key. The key's policy must name the exact log group it grants CloudWatch
  # Logs access to (an unscoped grant is what the review rejected), so the key would depend
  # on the group. Terraform cannot order that.
  #
  # Constructing the ARN is sound here because this module CHOOSES the name: `cluster_name`
  # is derived from the prefix, and EKS's log group path is fixed at
  # /aws/eks/<cluster>/cluster. The account and region are required inputs, and
  # `terraform_data.target_account_guard` fails the plan when the named account is not the
  # caller's — so these values cannot silently describe someone else's log group.
  cluster_log_group_name = "/aws/eks/${local.cluster_name}/cluster"
  cluster_log_group_arn  = "arn:${local.partition}:logs:${var.aws_region}:${var.account_id}:log-group:${local.cluster_log_group_name}"

  # Whether the workspace admin role is created at all. True once EITHER a human operator or
  # an automation role is named — the two are separate inputs because they need different
  # trust conditions (iam.tf records why an automation role cannot satisfy an MFA condition),
  # but either one alone is a legitimate reason for the role to exist.
  workspace_admin_enabled = length(var.workspace_admin_principal_arns) > 0 || length(var.workspace_admin_automation_role_arns) > 0

  common_tags = {
    Project     = "adp"
    Environment = var.environment
    Module      = "domain-apps/superplane"
    ManagedBy   = "terraform"
    Component   = "superplane-workspace"
    DomainApp   = "superplane"

    # The tag that makes one workspace's resources and spend separable from every other
    # workspace's and from ADP's own. Design item 4 requires bounded cost estimates per
    # workspace; that is only answerable if the resources carry the workspace.
    Workspace   = var.workspace_name
    WorkspaceId = var.workspace_id
    OrgId       = var.org_id

    # Which mode produced this workspace's network, recorded on the resources themselves.
    # `cleanup.py` in ../account-factory/ branches on the equivalent `ClusterOwnership` for
    # the same reason: an operator looking at a VPC needs to know from the VPC whether ADP
    # created it, without reconstructing the request that made it.
    NetworkOwnership = local.owns_network ? "adp-created" : "supplied"

    CostCenter = var.cost_center
  }
}

# ---------------------------------------------------------------------------
# The account this module plans against must be the account it was TOLD to act on.
#
# `var.account_id` is a required input with no default, but on its own it is only a claim:
# nothing stops a plan naming account A while the credentials in the environment belong to
# account B. The apply would then create this workspace's infrastructure in B — the
# "acts on an account nobody chose" defect that #5530 removed from the reference
# implementation, arriving by a different route.
#
# This precondition compares the named target against the caller's real identity and fails
# the plan when they disagree. It creates nothing in the account (`terraform_data` is a
# plan-time carrier, the same mechanism ../control-plane/ecr.tf uses).
#
# This is the "target mismatch" case AC-01 requires a test for: the run block
# "a_named_account_that_disagrees_with_the_credentials_is_refused" in
# tests/no_inherited_defaults.tftest.hcl names a different valid account than the mocked
# caller identity and expects this precondition to fail the plan.
# ---------------------------------------------------------------------------
resource "terraform_data" "target_account_guard" {
  input = var.account_id

  lifecycle {
    precondition {
      condition     = var.account_id == data.aws_caller_identity.current.account_id
      error_message = "account_id names ${var.account_id} but these credentials belong to ${data.aws_caller_identity.current.account_id}. Refusing to plan a workspace into an account the request did not name. Supply the credentials for the named account, or correct account_id."
    }
  }
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = local.common_tags
  }
}
