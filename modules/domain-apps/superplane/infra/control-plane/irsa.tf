# =============================================================================
# Runtime identities for the Superplane control plane — Issue #5042 (U3).
# =============================================================================
# "Superplane services must have scoped runtime identities and resource limits"
# (platform isolation requirement, 2026-09-16).
#
# Two roles, not one: the API/controller needs to read its own secrets, and the SkyPilot
# API server needs to launch and tear down compute. Merging them would give the API
# server's pod the ability to create instances, which is a materially larger blast radius
# for the component that terminates untrusted-ish HTTP.
#
# Every trust policy below is scoped to a NAMED service account in a NAMED namespace via
# the OIDC `sub` condition. A trust policy scoped only to the OIDC provider would let any
# pod in the cluster — including a core ADP pod — assume a domain-app role. That is the
# assertion tests/platform_isolation.tftest.hcl checks, because it is the difference
# between a scoped runtime identity and a cluster-wide one.
# =============================================================================

locals {
  # The OIDC subject claim EKS presents for a pod: system:serviceaccount:<ns>:<name>.
  # `oidc_issuer` is the issuer URL with the scheme stripped, which is the form the
  # condition key must take.
  api_service_account        = "superplane-api"
  controller_service_account = "superplane-controller"
  skypilot_service_account   = "skypilot-api"
}

# ---------------------------------------------------------------------------
# Control-plane role: the API and controller pods.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "control_plane_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer}:sub"
      values = [
        "system:serviceaccount:${var.namespace}:${local.api_service_account}",
        "system:serviceaccount:${var.namespace}:${local.controller_service_account}",
      ]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "control_plane" {
  name               = "${local.name_prefix}-control-plane"
  description        = "Runtime identity for Superplane API and controller pods (domain app; issue #5042)."
  assume_role_policy = data.aws_iam_policy_document.control_plane_assume.json
}

data "aws_iam_policy_document" "control_plane" {
  # Read its own secrets, and only its own. Resource-scoped to the two secrets this
  # module was told about, so a compromised pod cannot enumerate the account's secrets —
  # notably not the gateway's database or GitHub App credentials.
  statement {
    sid    = "ReadOwnSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]
    resources = [
      "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:${var.database_secret_name}-*",
      "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:${var.jwt_secret_name}-*",
    ]
  }

  # Read its own configuration.
  statement {
    sid       = "ReadOwnParameters"
    effect    = "Allow"
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:${var.aws_region}:${var.account_id}:parameter/adp/${var.environment}/superplane/*"]
  }

  # Pull its own images, and only its own repositories.
  statement {
    sid    = "PullOwnImages"
    effect = "Allow"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchCheckLayerAvailability",
    ]
    resources = [for r in aws_ecr_repository.superplane : r.arn]
  }

  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # This action does not support resource-level permissions.
  }
}

resource "aws_iam_role_policy" "control_plane" {
  name   = "${local.name_prefix}-control-plane"
  role   = aws_iam_role.control_plane.id
  policy = data.aws_iam_policy_document.control_plane.json
}

# ---------------------------------------------------------------------------
# SkyPilot API server role.
#
# Compute-launching permissions are deliberately NOT written here. SkyPilot's required
# policy set is broad (EC2 run/terminate, IAM instance-profile passing, service quotas),
# and inventing it in this story would either under-provision — producing launch failures
# that look like SkyPilot bugs — or over-provision a role in an account nobody has named.
# U19 owns the resource and state handover for the SkyPilot service
# (docs/design-notes/4910-skypilot-eks-migration-amendment.md), and the live acceptance
# that would validate such a policy is deferred.
#
# So this role exists with the identity boundary established and its own secret access
# scoped, and the compute grant is attached by whoever resolves the account and the
# spend authorization. `var.skypilot_compute_policy_arns` is the seam for that: default
# empty, so nothing is granted by accident, and no placeholder policy that looks
# authoritative.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "skypilot_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer}:sub"
      values   = ["system:serviceaccount:${var.skypilot_namespace}:${local.skypilot_service_account}"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "skypilot" {
  name               = "${local.name_prefix}-skypilot-api"
  description        = "Runtime identity for the SkyPilot API server (domain app; issue #5042). Compute grant attached separately - see irsa.tf."
  assume_role_policy = data.aws_iam_policy_document.skypilot_assume.json
}

data "aws_iam_policy_document" "skypilot" {
  # Its own state-backend credentials. U2 pins state_backend = postgres because SQLite
  # loses cluster/job state on pod replacement.
  statement {
    sid    = "ReadOwnSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]
    resources = [
      "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:${var.database_secret_name}-*",
    ]
  }
}

resource "aws_iam_role_policy" "skypilot" {
  name   = "${local.name_prefix}-skypilot-api"
  role   = aws_iam_role.skypilot.id
  policy = data.aws_iam_policy_document.skypilot.json
}

# The seam described above. Empty by default: an unresolved grant stays visibly
# unresolved rather than being approximated.
resource "aws_iam_role_policy_attachment" "skypilot_compute" {
  for_each = toset(var.skypilot_compute_policy_arns)

  role       = aws_iam_role.skypilot.name
  policy_arn = each.value
}
