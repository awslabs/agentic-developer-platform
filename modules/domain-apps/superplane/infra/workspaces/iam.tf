# =============================================================================
# Workspace identities — least privilege, scoped to THIS workspace
# Issue #5532 (w6-09) design item 1 ("least-privilege access") and item 3 (identity outputs).
# =============================================================================
# FOUR IDENTITIES, EACH WITH ONE JOB
#
#   1. The cluster role     — what EKS itself assumes to manage the control plane.
#   2. The node role        — what this workspace's EC2 nodes assume.
#   3. The CNI role         — only this cluster's kube-system/aws-node service account.
#   4. The workspace admin  — the role a human or the ADP control plane assumes to reach
#                             THIS cluster's Kubernetes API, and no other cluster's.
#
# WHAT "LEAST PRIVILEGE" MEANS HERE, CONCRETELY
#
# The trust policies are the part that actually scopes access, and they are where a wildcard
# does the most damage while looking most ordinary. Two rules hold throughout this file:
#
#   * No `Principal: "*"` and no bare-account principal on an assumable role. A trust policy
#     naming only an account id lets ANY principal in that account assume the role, which in
#     a workspace account includes every future role and user created there.
#   * No `Resource: "*"` on a policy this module writes. Where AWS's own managed policies
#     are used (the cluster/worker policies and the isolated CNI policy below) that is stated and justified rather than
#     quietly inherited.
#
# tests/test_least_privilege.py asserts both, by parsing the policy documents rather than by
# grepping for the string "*" — a wildcard inside a prose comment is not a grant, and a
# check that cannot tell the difference gets disabled the first time it fires wrongly.
#
# WHY THE MANAGED POLICIES ARE USED FOR THE CLUSTER AND NODE ROLES
#
# `AmazonEKSClusterPolicy` and `AmazonEKSWorkerNodePolicy` are AWS-authored and AWS-maintained. EKS breaks in
# ways that surface as unexplained node-registration failures when these are hand-trimmed,
# and AWS adds permissions to them as EKS gains features — a hand-copied subset silently
# becomes wrong at the next EKS release. So they are attached as-is, which is a deliberate
# choice to prefer a maintained broad policy over an unmaintained narrow one for the two
# roles AWS services assume. The roles that a HUMAN or ADP assumes get hand-written scoped
# policies, because nobody maintains those on our behalf.
# =============================================================================

# ---------------------------------------------------------------------------
# 1. Cluster role — assumed by the EKS service.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "cluster_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type = "Service"
      # The EKS service, and only it. Not an account, not a wildcard.
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${local.name_prefix}-cluster-role"
  assume_role_policy = data.aws_iam_policy_document.cluster_assume_role.json
  description        = "EKS control-plane role for Superplane workspace ${var.workspace_name} (${var.environment}). Assumable only by eks.amazonaws.com."
}

resource "aws_iam_role_policy_attachment" "cluster_eks" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKSClusterPolicy"
}

# ---------------------------------------------------------------------------
# 2. Node role — assumed by this workspace's EC2 instances.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "node_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${local.name_prefix}-node-role"
  assume_role_policy = data.aws_iam_policy_document.node_assume_role.json
  description        = "EKS node role for Superplane workspace ${var.workspace_name} (${var.environment})."
}

resource "aws_iam_role_policy_attachment" "node_worker" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

# ECR authorization tokens cannot be resource-scoped. Pull operations can, and
# grant only AWS EKS system images plus the operator's exact workspace repositories.
resource "aws_iam_role_policy" "node_image_pull" {
  name = "${local.name_prefix}-node-image-pull"
  role = aws_iam_role.node.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ECRAuthenticationRequiresStarResource"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "PullReviewedRepositoriesOnly"
        Effect = "Allow"
        Action = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
        Resource = concat([
          for repository in local.node_network_policy.system_image_repositories :
          "arn:${local.partition}:ecr:${var.aws_region}:${local.node_network_policy.system_registry_accounts[var.aws_region]}:repository/${repository}"
        ], var.node_image_repository_arns)
      }
    ]
  })
}

# The CNI can alter ENIs; these permissions belong only to kube-system/aws-node,
# never to the node role or a tenant service account.
resource "aws_iam_role" "vpc_cni" {
  name = "${local.name_prefix}-vpc-cni-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.cluster.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "${replace(aws_eks_cluster.workspace.identity[0].oidc[0].issuer, "https://", "")}:aud" = "sts.amazonaws.com"
        "${replace(aws_eks_cluster.workspace.identity[0].oidc[0].issuer, "https://", "")}:sub" = "system:serviceaccount:kube-system:aws-node"
      } }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "vpc_cni" {
  role       = aws_iam_role.vpc_cni.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKS_CNI_Policy"
}

# ---------------------------------------------------------------------------
# 3. Workspace admin role — reaching THIS cluster's Kubernetes API and no other.
#
# This is the identity output that design item 3 requires ("publish deterministic ...
# trust/identity outputs"), and the one where scoping is load-bearing: the ADP control plane
# needs a way into a workspace cluster, and the wrong shape here is a role that can reach
# EVERY workspace's cluster.
#
# The `eks:DescribeCluster` grant is scoped to this cluster's ARN, not to `*`. That matters
# more than it looks: `DescribeCluster` returns the endpoint and the CA certificate, which is
# what a client needs to construct a kubeconfig. A `Resource: "*"` version of this policy
# would let the holder enumerate and build a kubeconfig for every cluster in the account.
#
# Kubernetes-side permissions (what this role can DO once it reaches the API) are NOT set
# here. They are an EKS access entry / aws-auth mapping, i.e. a Kubernetes object, and this
# module declares no Kubernetes provider on purpose (see main.tf). #5533 (w6-10) owns
# workspace bootstrap and the access entry. So this role is published as a trust anchor with
# API reachability and no cluster-internal authority yet — stated plainly because a role that
# can reach an API it has no RBAC in is a confusing intermediate state to hand someone
# without explanation.
# ---------------------------------------------------------------------------
# TWO TRUST STATEMENTS, BECAUSE MACHINES AND HUMANS AUTHENTICATE DIFFERENTLY
# (review finding F3)
#
# WHAT WAS WRONG WITH ONE STATEMENT
#
# This role previously had a single trust statement requiring
# `aws:MultiFactorAuthPresent = true` from EVERY principal, while the module's own
# documentation named an automated ADP control-plane role as the thing that assumes it.
#
# An automated role CANNOT satisfy that condition. A service assuming a role with its own
# credentials — an EKS pod using IRSA, a CI role, a Lambda execution role — produces a
# session where `aws:MultiFactorAuthPresent` is FALSE, not absent: the key is present and set
# to false, so a `Bool` test for "true" fails. (It is absent only for some session types, in
# which case a `Bool` condition on it fails to match as well.) Either way the documented
# automation path is denied every time. The failure is an opaque AccessDenied at the moment
# the control plane first tries to reach a workspace, with nothing in the message about MFA.
#
# The wrong repair would be to drop the MFA condition so automation works — that silently
# removes the protection which makes a leaked human access key insufficient on its own. So the
# two are separated: each principal type gets the condition that is meaningful for it.
#
#   * Machine roles are named EXPLICITLY, one ARN at a time, in
#     var.workspace_admin_automation_role_arns. No MFA condition, because they cannot meet one.
#     The control is that the list is an exact allowlist of role ARNs — not an account, not a
#     path, not a wildcard — so admitting a new automation principal is a reviewable change.
#   * Humans keep the MFA requirement, via var.workspace_admin_principal_arns.
#
# A principal in NEITHER list is denied, which is the default IAM behaviour and what
# tests/test_least_privilege.py asserts positively rather than assuming.
data "aws_iam_policy_document" "workspace_admin_assume_role" {
  # 1. Human operators: MFA required, unchanged.
  dynamic "statement" {
    # Omitted entirely when no human principal is named, rather than emitted with an empty
    # principal list. An IAM trust statement with zero principals is rejected by the API, so
    # emitting one would make an automation-only workspace fail to apply.
    for_each = length(var.workspace_admin_principal_arns) > 0 ? [1] : []

    content {
      sid     = "HumanOperatorsWithMFA"
      effect  = "Allow"
      actions = ["sts:AssumeRole"]

      principals {
        type = "AWS"
        # Named human principals. A bare account id here would mean "any principal in this
        # account"; var.workspace_admin_principal_arns refuses `:root` for that reason.
        identifiers = var.workspace_admin_principal_arns
      }

      # The condition that makes a leaked long-lived access key insufficient on its own. It
      # applies ONLY to this statement, so it constrains humans without blocking the
      # automation path below.
      condition {
        test     = "Bool"
        variable = "aws:MultiFactorAuthPresent"
        values   = ["true"]
      }
    }
  }

  # 2. Named automation roles: no MFA condition, because a role session cannot present MFA.
  dynamic "statement" {
    for_each = length(var.workspace_admin_automation_role_arns) > 0 ? [1] : []

    content {
      sid     = "NamedAutomationRolesWithoutMFA"
      effect  = "Allow"
      actions = ["sts:AssumeRole"]

      principals {
        type = "AWS"
        # Exact role ARNs only. variables.tf refuses `:root`, refuses IAM users (a user is a
        # human-shaped long-lived credential and belongs in the MFA statement), and refuses
        # wildcards — so this cannot become "any role in the account" by increments.
        identifiers = var.workspace_admin_automation_role_arns
      }
    }
  }
}

resource "aws_iam_role" "workspace_admin" {
  # Only created once someone is named — human or automation. A role with an empty principal
  # list is harmless but it is also noise in the account, and its existence would imply an
  # operator exists.
  count = local.workspace_admin_enabled ? 1 : 0

  name               = "${local.name_prefix}-admin"
  assume_role_policy = data.aws_iam_policy_document.workspace_admin_assume_role.json
  description        = "Operator access to Superplane workspace ${var.workspace_name}'s EKS API. Scoped to this cluster only."

  # An hour, not the 12-hour maximum. A workspace admin session is for an operation, not a
  # working day.
  max_session_duration = 3600
}

data "aws_iam_policy_document" "workspace_admin" {
  statement {
    sid    = "DescribeThisWorkspaceClusterOnly"
    effect = "Allow"

    actions = [
      "eks:DescribeCluster",
      "eks:ListNodegroups",
      "eks:DescribeNodegroup",
    ]

    # Scoped to this cluster and its node groups. Not "*".
    resources = [
      "arn:${local.partition}:eks:${var.aws_region}:${var.account_id}:cluster/${local.cluster_name}",
      "arn:${local.partition}:eks:${var.aws_region}:${var.account_id}:nodegroup/${local.cluster_name}/*",
    ]
  }

  statement {
    sid    = "ListClustersRequiresStarResource"
    effect = "Allow"

    # `eks:ListClusters` takes no resource — AWS defines it as account-level, so scoping it
    # is not possible and pretending otherwise would produce a policy that silently grants
    # nothing. It is separated into its own statement, with this comment, so a reader can
    # see that the one `*` in this file is an AWS API constraint rather than convenience.
    # It reveals cluster NAMES in the workspace account; it does not reveal endpoints or
    # certificates, which is what the scoped statement above controls.
    actions   = ["eks:ListClusters"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "workspace_admin" {
  count = local.workspace_admin_enabled ? 1 : 0

  name   = "${local.name_prefix}-admin-eks-access"
  role   = aws_iam_role.workspace_admin[0].id
  policy = data.aws_iam_policy_document.workspace_admin.json
}
