# =============================================================================
# The workspace's EKS cluster — physically separate, encrypted, logged, bounded
# Issue #5532 (w6-09) design items 1 and 2.
# =============================================================================
# THIS CLUSTER IS NOT THE ADP MANAGEMENT CLUSTER, AND CANNOT BECOME IT.
#
# Design item 2: "never schedule tenant GPU/hybrid capacity into the ADP management
# cluster." The cluster below is created in the workspace's own account, in the workspace's
# own network, with the workspace's own OIDC provider. Nothing in this module references the
# management cluster — main.tf deliberately does not read the platform remote state — so
# there is no expression here that could resolve to it even by mistake.
#
# FOUR DEFECTS IN THE VENDORED GRAPH THIS FILE DELIBERATELY DOES NOT REPRODUCE
#
# `../account-factory/vendor/kro-account-factory/02-eks-cluster-stack.yaml` is the reference
# EKS definition vendored for the Account Factory. It works, and it is coherent with this
# file on names, node sizing and log types. It differs on four security properties, and each
# difference here is intentional rather than an oversight in one direction or the other:
#
#   1. It sets `endpointPublicAccess: true` with NO CIDR restriction (lines 97-98), which
#      publishes every workspace's Kubernetes API to the entire internet. Here public access
#      defaults OFF and, when enabled, requires an allowlist that refuses 0.0.0.0/0 (see
#      variables.tf).
#   2. It declares no `encryptionConfig`, so Kubernetes Secrets sit in etcd with only
#      AWS's default at-rest encryption and no customer-managed key. Here envelope
#      encryption with a KMS key is unconditional.
#   3. It pins no Kubernetes version in a reviewable way, matching the reference installer's
#      `releases/latest` resolution. Here `var.cluster_version` is required and refuses
#      moving labels.
#   4. It declares no OIDC provider, so workloads in the workspace cannot use IRSA and fall
#      back to node-role credentials — which means every pod on a node shares that node's
#      permissions. Here the OIDC provider is created so per-workload identity is possible.
#
# These are stated as a list because the two definitions will be compared, and a reader
# finding the differences without this note would reasonably conclude one of them is wrong.
#
# WHAT IS NOT HERE
#
# Only the identity-isolated VPC CNI addon is installed here. Access entries, other addons,
# aws-auth mapping, Karpenter and GPU node groups belong to bootstrap.
# Cluster bootstrap and workload placement are #5533 (w6-10). This module produces an empty,
# reachable, encrypted, logged cluster and its identity outputs — nothing that runs a tenant
# workload, and nothing that spends beyond the bounded node group below.
# =============================================================================

# ---------------------------------------------------------------------------
# Encryption key for Kubernetes Secrets and node volumes.
#
# Created only when the operator did not supply one (var.kms_key_arn). The supplied case is
# the one to prefer in production: a key created here is destroyed with the workspace, and
# an encrypted EBS snapshot outliving its key is unrecoverable. The comment on
# var.kms_key_arn records that reasoning; this resource is the fallback so a workspace is
# never created with NO customer-managed key at all.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "workspace" {
  count = var.kms_key_arn == "" ? 1 : 0

  description = "Envelope encryption for Superplane workspace ${var.workspace_name} (${var.environment}) Kubernetes Secrets and node volumes."

  enable_key_rotation = true

  # A deletion window, not immediate deletion. Thirty days is the maximum AWS allows and the
  # right choice for a key that encrypts backups: a workspace deleted by mistake is
  # recoverable within the window, and after it the data is gone whether or not the key is.
  deletion_window_in_days = 30

  # -------------------------------------------------------------------------
  # WHY THIS POLICY EXISTS AT ALL (review finding W9-01 / independent F1)
  #
  # Without it, a DEFAULT INSTALL OF THIS MODULE CANNOT BE CREATED. That is the whole
  # finding, and it is worth stating precisely because the failure is not subtle once it
  # happens and completely invisible before.
  #
  # A KMS key with no policy gets AWS's default key policy, which grants the account's IAM
  # principals administrative use of the key. It does NOT grant any AWS SERVICE access.
  # CloudWatch Logs encrypts a log group using its own regional service principal
  # (logs.<region>.amazonaws.com), so `aws_cloudwatch_log_group.cluster` below —- which sets
  # `kms_key_id` to this key -— is rejected by the Logs API with an
  # InvalidParameterException. The apply fails partway: key created, log group not, no
  # cluster. Encrypted audit logging, which is what makes "who did what in this workspace"
  # answerable, never starts.
  #   https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/encrypt-log-data-kms.html
  #
  # `jsonencode` RATHER THAN aws_iam_policy_document, DELIBERATELY
  #
  # Every other policy in this module is built with `data.aws_iam_policy_document`. This one
  # is not, and the reason is that it must be TESTABLE. The provider-free test suites mock
  # that data source, and a mock necessarily replaces its `json` attribute with a fixed
  # string -- every one of this module's suites substitutes a document with an EMPTY
  # statement list. So an assertion that "the key policy grants CloudWatch Logs access"
  # written against a data-source-built policy passes identically when the policy grants
  # nothing at all. The review made exactly this point: "Validate the real rendered policy;
  # the current mock replaces every policy document with an empty statement list."
  #
  # `jsonencode` of a literal structure is resolved by Terraform itself, not by the provider,
  # so the rendered document appears in the plan as a known value and
  # tests/kms_key_policy.tftest.hcl asserts on its actual statements.
  # -------------------------------------------------------------------------
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # 1. Key administration and recovery.
      #
      # This statement is what AWS's default policy would otherwise have provided, and
      # omitting it is the classic way to create an UNMANAGEABLE key: attaching a policy
      # replaces the default entirely, so a policy containing only the service grants below
      # produces a key that no human and no automation can schedule for deletion, re-enable,
      # or rotate -- including during the incident where that is the needed action. The
      # review calls for preserving "a usable key administration/recovery policy".
      #
      # The principal is the account root, which in a KEY policy means "delegate to this
      # account's IAM" -- i.e. principals in this account may act on the key to the extent
      # their own IAM policies allow. That is the documented idiom for KMS key policies and
      # is NOT the same as the bare-account principal refused in iam.tf's trust policies: a
      # trust policy's bare account principal grants assumption to every principal outright,
      # whereas a key policy without this statement grants nobody anything.
      {
        Sid    = "WorkspaceKeyAdministrationAndRecovery"
        Effect = "Allow"
        Principal = {
          AWS = "arn:${local.partition}:iam::${var.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },

      # CreateCluster calls KMS on behalf of the provisioning caller. It does not set
      # GrantIsForAWSResource for this operation. The caller also needs the identity
      # permissions published in provisioning_caller_kms_requirements.
      {
        Sid       = "AllowProvisioningCallerToConfigureEKSEncryption"
        Effect    = "Allow"
        Principal = { AWS = data.aws_iam_session_context.provisioner.issuer_arn }
        Action    = ["kms:DescribeKey", "kms:CreateGrant"]
        Resource  = "*"
      },

      # 2. CloudWatch Logs, scoped to THIS workspace's log group and nothing else.
      #
      # The actions are the set the Logs service documents as required to encrypt a log
      # group; Decrypt is needed to READ the logs back, so omitting it produces a group that
      # accepts writes and returns errors on every query.
      #
      # `kms:EncryptionContext:aws:logs:arn` is the narrow scoping the review requires ("the
      # exact log-group encryption context"). CloudWatch Logs sets this context to the ARN of
      # the log group being encrypted, so this condition is what prevents the regional Logs
      # service principal -- shared by every log group in the account, including a tenant's
      # own -- from using this workspace's key for anything other than this one group. Without
      # the condition the grant is "CloudWatch Logs may use this key", which is a materially
      # different and much broader permission than the one intended.
      #
      # `Resource = "*"` is required and means "this key": a key policy's resource field can
      # only refer to the key the policy is attached to, and KMS rejects any other value.
      # tests/test_least_privilege.py's star-resource rule covers IAM policy documents, which
      # is a different shape from a key policy; this is not an exemption from it.
      {
        Sid    = "AllowCloudWatchLogsToEncryptThisWorkspacesClusterLogGroup"
        Effect = "Allow"
        Principal = {
          Service = "logs.${var.aws_region}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ]
        Resource = "*"
        Condition = {
          ArnEquals = {
            "kms:EncryptionContext:aws:logs:arn" = local.cluster_log_group_arn
          }
        }
      },

      # 3. The EBS-through-Auto-Scaling path for the node group's encrypted root volumes.
      #
      # Needed because of aws_launch_template.node below, which encrypts node root volumes
      # with this key (review finding W9-02). The EC2 Auto Scaling service -- not the node,
      # and not the operator -- is the principal that creates those volumes when it launches
      # an instance, so it is the principal that must be able to use the key. Without this,
      # the launch template is accepted and every instance launch fails with a KMS access
      # error, which surfaces as nodes that never join the cluster: a failure that looks like
      # a networking problem.
      #
      # `kms:ViaService` restricts these grants to calls KMS receives from EC2 in this region,
      # so the service-linked role cannot use this key through any other service. The
      # CreateGrant statement is separated because it carries a different condition
      # (`GrantIsForAWSResource`), which is what confines grant creation to AWS's own
      # resource-attachment flow rather than allowing arbitrary grants.
      {
        Sid    = "AllowAutoScalingToUseThisKeyForNodeRootVolumes"
        Effect = "Allow"
        Principal = {
          AWS = "arn:${local.partition}:iam::${var.account_id}:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ec2.${var.aws_region}.amazonaws.com"
          }
        }
      },
      {
        Sid    = "AllowAutoScalingToCreateGrantsForAttachedVolumes"
        Effect = "Allow"
        Principal = {
          AWS = "arn:${local.partition}:iam::${var.account_id}:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
        }
        Action   = "kms:CreateGrant"
        Resource = "*"
        Condition = {
          Bool = {
            "kms:GrantIsForAWSResource" = "true"
          }
        }
      },
    ]
  })

  tags = {
    Name = "${local.name_prefix}-secrets"
  }
}

resource "aws_kms_alias" "workspace" {
  count = var.kms_key_arn == "" ? 1 : 0

  name          = "alias/${local.name_prefix}-secrets"
  target_key_id = aws_kms_key.workspace[0].key_id
}

# ---------------------------------------------------------------------------
# Control-plane log group.
#
# Declared EXPLICITLY rather than letting EKS create it implicitly, for one reason:
# the implicitly-created group has NEVER-EXPIRE retention. That is both an unbounded cost
# and, for audit logs, a compliance decision nobody made. Declaring it here means retention
# is a reviewed value (var.log_retention_days, which refuses 0).
#
# The name is not free-form: EKS writes to /aws/eks/<cluster-name>/cluster and will not be
# redirected. If this name is wrong, EKS silently creates its own never-expiring group and
# this one stays empty — a failure that looks like working logging.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "cluster" {
  #checkov:skip=CKV_AWS_338: EKS control-plane logs use an explicitly bounded workspace-selected operational retention.
  # `local.cluster_log_group_name`, not an inline string: the KMS key policy above must name
  # this exact group in its encryption-context condition, and a second copy of the path here
  # could drift from the one the key authorises — which would produce a key that cannot
  # encrypt this group while both values look individually correct. main.tf records why the
  # name is constructed there rather than read back from this resource.
  name              = local.cluster_log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = local.kms_key_arn

  tags = {
    Name = "${local.name_prefix}-cluster-logs"
  }
}

# ---------------------------------------------------------------------------
# Cluster security group.
#
# EKS creates its own managed security group too; this one is the workspace's additional
# group, and it exists so that the default egress rule is a CHOICE rather than an inherited
# default. A security group declared with no rules allows nothing in and nothing out, which
# would break image pulls; declared here, egress is explicit and ingress is empty.
#
# No ingress rules at all: nothing reaches the cluster's network interfaces from outside the
# VPC except through the API endpoint, whose exposure is controlled by
# var.cluster_endpoint_public_access. An `aws_vpc_security_group_ingress_rule` allowing
# 0.0.0.0/0 here would quietly undo that.
# ---------------------------------------------------------------------------
resource "aws_security_group" "cluster" {
  name        = "${local.name_prefix}-cluster"
  description = "Superplane workspace ${var.workspace_name} cluster security group: egress only, no ingress from outside the VPC."
  vpc_id      = local.vpc_id

  tags = {
    Name = "${local.name_prefix}-cluster"
  }

  lifecycle {
    # Replacing this group means detaching it from a live cluster's network interfaces, which
    # interrupts the control plane. Create the replacement first.
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "cluster_all" {
  security_group_id = aws_security_group.cluster.id
  description       = "Outbound: image pulls, AWS API calls, and the workspace's own outbound traffic via the NAT gateway."

  # Unrestricted egress. Stated plainly rather than presented as a restriction: this is what
  # EKS needs to function, and narrowing it requires knowing every registry, AWS endpoint and
  # external API a tenant workload will reach — which is the tenant's decision to make on
  # their own cluster, not one this module can make for them. Ingress, which is the direction
  # that admits an attacker, is empty.
  ip_protocol = "-1"
  cidr_ipv4   = "0.0.0.0/0"
}

# ---------------------------------------------------------------------------
# The cluster.
# ---------------------------------------------------------------------------
resource "aws_eks_cluster" "workspace" {
  name = local.cluster_name

  # The exact version supplied, never a resolved-at-apply-time label. See
  # var.cluster_version.
  version = var.cluster_version

  role_arn = aws_iam_role.cluster.arn

  # All five control-plane log types by default, including audit and authenticator, which
  # var.cluster_log_types refuses to omit. This is what makes "who did what in this
  # workspace" answerable after the fact.
  enabled_cluster_log_types = var.cluster_log_types

  # Envelope encryption of Kubernetes Secrets in etcd with a customer-managed key. The
  # vendored graph omits this entirely. Note that this cannot be added to an existing
  # cluster by an in-place update in every EKS version — which is the practical reason it
  # must be right at creation rather than "added later".
  encryption_config {
    provider {
      key_arn = local.kms_key_arn
    }
    resources = ["secrets"]
  }

  vpc_config {
    subnet_ids         = local.private_subnet_ids
    security_group_ids = [aws_security_group.cluster.id]

    # Private access ALWAYS on: this is how the ADP control plane and the workspace's own
    # nodes reach the API without traversing the internet.
    endpoint_private_access = true

    # Public access OFF unless explicitly enabled with a bounded allowlist. The variable
    # defaults to false and its paired CIDR validations refuse 0.0.0.0/0 and ::/0.
    endpoint_public_access = var.cluster_endpoint_public_access

    # When public access is off this must be empty, and variables.tf enforces that pairing
    # so a populated list never reads as an effective allowlist.
    public_access_cidrs = var.cluster_endpoint_public_access_cidrs
  }

  access_config {
    # API-only, not the legacy aws-auth ConfigMap. Access entries are auditable AWS
    # resources; a ConfigMap edit is not, and its history is whatever is in the cluster now.
    authentication_mode = "API"

    # The creating principal does NOT silently become a cluster administrator. This defaults
    # to true in the AWS provider, which means whichever CI role or human ran the apply gets
    # permanent cluster-admin on a tenant's cluster — an access grant nobody reviewed, tied
    # to an identity that may be a shared CI role. Operator access is granted explicitly
    # through the workspace admin role in iam.tf and the access entries #5533 owns.
    bootstrap_cluster_creator_admin_permissions = false
  }

  tags = {
    Name = local.cluster_name
  }

  # The log group must exist before the cluster starts writing, or EKS creates its own with
  # never-expire retention. The IAM policy attachment must land before the cluster is
  # created, or control-plane setup fails partway with a permissions error.
  depends_on = [
    aws_cloudwatch_log_group.cluster,
    aws_iam_role_policy_attachment.cluster_eks,
  ]
}

# ---------------------------------------------------------------------------
# OIDC provider — per-workload identity inside this workspace.
#
# Without this, a pod needing AWS permissions uses the NODE's role, which means every pod on
# that node shares those permissions and a compromise of any one of them is a compromise of
# all. With it, a workload gets a role scoped to its own service account.
#
# This provider is THIS cluster's. The control plane's IRSA roles trust the MANAGEMENT
# cluster's provider (../control-plane/irsa.tf); the two are different trust anchors and
# conflating them would let a workspace workload assume a control-plane role.
# ---------------------------------------------------------------------------
data "tls_certificate" "cluster_oidc" {
  url = aws_eks_cluster.workspace.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "cluster" {
  url             = aws_eks_cluster.workspace.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.cluster_oidc.certificates[0].sha1_fingerprint]

  tags = {
    Name = "${local.name_prefix}-oidc"
  }
}

# ---------------------------------------------------------------------------
# Default node group — PRIVATE SUBNETS ONLY, and bounded.
#
# Private subnets only: workspace capacity is not internet-reachable. `local.private_subnet_ids`
# resolves to this module's private subnets in owned mode and to the supplied private subnets
# in supplied mode, so there is no expression here that can place a node in a public subnet.
#
# Bounded: max_size is capped by variables.tf, which is what makes design item 4's cost
# estimate a finite number. An unbounded node group has no upper bill.
#
# This is a general-purpose group, NOT a GPU group. GPU and hybrid capacity is #5533's
# (w6-10) concern and lands on THIS cluster when it lands — which is design item 2's point:
# it has somewhere to go that is not the ADP management cluster.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Node launch template — ENCRYPTED, BOUNDED ROOT VOLUMES (review finding W9-02 / F2)
#
# WHAT WAS WRONG WITHOUT THIS
#
# variables.tf, outputs.tf and the PR description all stated that node EBS volumes are
# encrypted with this workspace's reviewed key. Nothing implemented it. The key was used for
# Kubernetes Secrets (`encryption_config` on the cluster) and for the log group, and node
# disks were left to EKS's default node template.
#
# That default does NOT establish the promised property. EKS's managed default template
# creates an unencrypted 20 GiB gp3 root volume unless the account has
# EBS-encryption-by-default enabled, in which case the volume is encrypted with the ACCOUNT'S
# DEFAULT KEY — not this workspace's key. So the guarantee was either absent or, at best,
# dependent on an account-level setting this module neither sets nor reads, using a key shared
# with everything else in the account. The review's instruction is explicit that this must not
# be resolved by editing the description or by pointing at Secrets encryption, which is a
# different claim about a different store.
#
# Node root volumes matter because they are not empty: they hold pulled container images, the
# kubelet's cached Secret and ConfigMap material, and anything a tenant workload writes to
# local or ephemeral storage.
#
# BOUNDED, which also closes a gap in the cost estimate
#
# `volume_size` is a reviewed input (variables.tf caps it) rather than an inherited default.
# That makes node storage a priced component in scripts/check_workspace_plan.py instead of the
# "cannot be bounded" item it used to be listed as — the previous UNBOUNDED_COMPONENTS entry
# said node EBS was "sized by the node group's launch template defaults, which this plan does
# not set and therefore cannot bound". This template is that setting.
#
# The KMS side of this path is granted in aws_kms_key.workspace's policy above: EC2 Auto
# Scaling, not the node, creates these volumes, so it is the principal that needs key access.
# Without that grant every instance launch fails and nodes never join — which presents as a
# networking fault.
# ---------------------------------------------------------------------------
resource "aws_launch_template" "node" {
  name        = "${local.name_prefix}-node"
  description = "Superplane workspace ${var.workspace_name} node template: encrypted, size-bounded root volume using the workspace's reviewed KMS key."

  block_device_mappings {
    # The root device for EKS's Amazon Linux node AMIs. A mapping under any other device name
    # is accepted by EC2 and silently attaches an ADDITIONAL volume, leaving the actual root
    # volume on the unencrypted default — a failure that looks like success in the plan.
    device_name = "/dev/xvda"

    ebs {
      volume_size = var.node_volume_size
      volume_type = "gp3"

      # The two attributes that are the whole point of this resource.
      encrypted  = true
      kms_key_id = local.kms_key_arn

      # Delete the volume with the instance. A node is disposable and its root volume holds
      # cached tenant data; orphaned encrypted volumes are both a cost that survives the
      # workspace and data that outlives the cluster it belonged to.
      delete_on_termination = true
    }
  }

  # IMDSv2 and one response hop block normal pods from obtaining node credentials.
  # Bootstrap admission must also deny hostNetwork and privileged pod escapes.
  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 1
  }

  # Tags applied to the instances and volumes this template launches. `default_tags` on the
  # provider does not reach them: they are created by EC2 from this template, not declared as
  # Terraform resources, so without these blocks the workspace's nodes and disks carry no
  # Workspace tag and drop out of the per-workspace cost attribution design item 4 depends on.
  tag_specifications {
    resource_type = "instance"
    tags          = merge(local.common_tags, { Name = "${local.name_prefix}-node" })
  }

  tag_specifications {
    resource_type = "volume"
    tags          = merge(local.common_tags, { Name = "${local.name_prefix}-node-root" })
  }

  tags = {
    Name = "${local.name_prefix}-node"
  }

  lifecycle {
    # A new template version must exist before the node group moves to it.
    create_before_destroy = true
  }
}

resource "aws_eks_node_group" "default" {
  cluster_name    = aws_eks_cluster.workspace.name
  node_group_name = "${local.name_prefix}-default"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = local.private_subnet_ids

  instance_types  = [var.node_instance_type]
  ami_type        = local.node_image_policy.ami_type
  version         = var.cluster_version
  release_version = local.node_image_release

  # Tenant scheduling remains blocked until #5533 verifies restricted admission,
  # IMDS denial from a normal pod and rejection of hostNetwork/privileged escape.
  taint {
    key    = "superplane.aws-e/bootstrap"
    value  = "pending"
    effect = "NO_SCHEDULE"
  }

  # The encrypted, bounded root volume above. Without this block the template exists and is
  # simply unused -- the node group would fall back to EKS's default template and the
  # encryption claim would be false while a `aws_launch_template` resource sat in the plan
  # looking like evidence for it. tests/cluster_security.tftest.hcl asserts this reference,
  # not merely the template's existence.
  launch_template {
    id = aws_launch_template.node.id
    # Track the template's latest version so a future change to the volume size or key is
    # actually picked up; pinning a literal version would leave the group on the original.
    version = aws_launch_template.node.latest_version
  }

  scaling_config {
    desired_size = var.node_group_desired_size
    min_size     = var.node_group_min_size
    max_size     = var.node_group_max_size
  }

  update_config {
    # One node at a time. A workspace with a 1-node group has no spare capacity, and a
    # percentage-based update would take the whole group down at once.
    max_unavailable = 1
  }

  tags = {
    Name = "${local.name_prefix}-default"
  }

  lifecycle {
    precondition {
      condition = !contains(local.supported_regions, var.aws_region) || !contains(local.createable_cluster_versions, var.cluster_version) || try(
        can(regex("^${var.cluster_version}\\.[0-9]+-[0-9]{8}$", local.node_image_release)), false
      )
      error_message = "No exact reviewed node image release exists for this region and Kubernetes version. Update node-image-pins.json through review before planning."
    }

    # desired_size drifts legitimately: the cluster autoscaler or a manual scale changes it,
    # and Terraform would otherwise scale the group back to var.node_group_desired_size on
    # the next unrelated apply — evicting running pods as a side effect of a tag change.
    # min_size and max_size are NOT ignored: those are the reviewed bounds.
    ignore_changes = [scaling_config[0].desired_size]
  }

  depends_on = [
    aws_iam_role_policy_attachment.node_worker,
    aws_iam_role_policy.node_image_pull,
    aws_eks_addon.vpc_cni,
    aws_vpc_endpoint.private_sts,
    aws_vpc_security_group_ingress_rule.private_sts_nodes,
    data.aws_security_group.supplied_sts,
  ]
}
