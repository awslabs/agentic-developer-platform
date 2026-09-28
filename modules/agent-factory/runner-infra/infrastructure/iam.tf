# =============================================================================
# EKS Cluster IAM Role
# =============================================================================

resource "aws_iam_role" "cluster" {
  permissions_boundary = var.automation_permissions_boundary_arn
  name                 = "${local.cluster_name}-cluster-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = ["sts:AssumeRole", "sts:TagSession"]
      Effect = "Allow"
      Principal = {
        Service = "eks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster_AmazonEKSClusterPolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role_policy_attachment" "cluster_AmazonEKSComputePolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSComputePolicy"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role_policy_attachment" "cluster_AmazonEKSBlockStoragePolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSBlockStoragePolicy"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role_policy_attachment" "cluster_AmazonEKSLoadBalancingPolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSLoadBalancingPolicy"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role_policy_attachment" "cluster_AmazonEKSNetworkingPolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSNetworkingPolicy"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role_policy_attachment" "cluster_AmazonEKSVPCResourceController" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController"
  role       = aws_iam_role.cluster.name
}

# =============================================================================
# EKS Node IAM Role (for Auto Mode nodes)
# =============================================================================

resource "aws_iam_role" "node" {
  permissions_boundary = var.automation_permissions_boundary_arn
  name                 = "${local.cluster_name}-node-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = ["sts:AssumeRole"]
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "node_AmazonEKSWorkerNodeMinimalPolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodeMinimalPolicy"
  role       = aws_iam_role.node.name
}

resource "aws_iam_role_policy_attachment" "node_AmazonEC2ContainerRegistryPullOnly" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly"
  role       = aws_iam_role.node.name
}

# =============================================================================
# Permissions Boundary for Runner Pods (Issue #1204 — scoped, #596 fix)
# =============================================================================

resource "aws_iam_policy" "runner_boundary" {
  name        = "${var.project_name}-runner-boundary"
  description = "Permissions boundary for GitHub runner pods"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(module.runtime_policy.boundary, [
      {
        # A18 (#5674). A Deny in a permissions boundary is absolute for the roles
        # it caps: no policy attached to a bounded role can grant these, so a
        # future edit to the onboarding script's inline policy — or to the
        # managed policies below — cannot reopen the path. That is the point of
        # denying here rather than only omitting from the grant.
        #
        # Every action listed lets the holder obtain an identity other than the
        # one it was issued. iam:PassRole is included because handing an existing
        # privileged role to a service the runner can invoke (Lambda, CodeBuild,
        # EC2) reaches administrator without creating anything. The three
        # sts:AssumeRole* variants are included because the shared runner role
        # reachable through them could read the whole adp/ secret prefix — every
        # tenant's stored credentials and the signing keys for agent control
        # messages and GitHub App auth.
        Sid    = "DenyPrivilegeEscalation"
        Effect = "Deny"
        # The shared API ceiling denies IAM mutation and permits only passing
        # the exact service-only gateway PR role to CodeBuild.
        Action   = ["sts:AssumeRole", "sts:AssumeRoleWithSAML", "sts:AssumeRoleWithWebIdentity"]
        Resource = "*"
      },
      {
        # A18 (#5674). The onboarding script now grants each repository only its
        # own secret paths, and the shared role's grant is env-segmented — but
        # this boundary's ceiling is still secretsmanager:* on "*", so a future
        # policy edit could re-grant cross-tenant reads. A Deny here cannot be
        # out-voted by any attached policy, making that edit inert.
        #
        # Mirror the active runner boundary: block all secret operations in
        # both environment-less vault namespaces and environment-scoped tenant
        # paths, including stored GitHub App and customer AWS credentials.
        Sid    = "DenyTenantVaultSecrets"
        Effect = "Deny"
        Action = "secretsmanager:*"
        Resource = [
          "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/users/*",
          "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/teams/*",
          "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/orgs/*",
          "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/domain-apps/*",
          "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/*/tenants/*"
        ]
      }
    ])
  })
}

# =============================================================================
# IRSA Role for Runner Pods
# =============================================================================

resource "aws_iam_role" "runner" {
  name                 = "${var.project_name}-runner-role"
  permissions_boundary = aws_iam_policy.runner_boundary.arn

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.eks.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      # A18 (#5674): exact match on the allowed service accounts, replacing
      # StringLike on "system:serviceaccount:arc-runners-*:github-runner-sa".
      #
      # The pattern was equivalent to "any namespace whose name starts
      # arc-runners-, with the conventional service-account name". Namespaces are
      # created per repository by scripts/onboard-repo.sh, and the service
      # account name is fixed by that script — so onboarding a repository
      # silently made its runner trusted by this role, which reaches this
      # module's shared grants. Nobody decided that; the wildcard did.
      #
      # With StringEquals on a list, a new runner namespace is NOT trusted until
      # its service account is added here and applied — which is the review step
      # the wildcard skipped. `aud` is asserted because a federated trust policy
      # that conditions only on `sub` accepts a token minted for a different
      # audience.
      Condition = {
        StringEquals = {
          "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub" = [
            for namespace in var.runner_trusted_namespaces :
            "system:serviceaccount:${namespace}:github-runner-sa"
          ]
          "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

# =============================================================================
# Runner Managed Policies (Issue #1204 — synthesis #1200 Section 1)
# =============================================================================
# Split into two policies to stay under the 10,240-byte AWS managed-policy
# limit. Policy A = infrastructure-heavy, Policy B = application-deploy.
# Both are attached to the same runner role.
# =============================================================================

# The in-place update removes old deployment grants.
resource "aws_iam_policy" "runner_base" {
  # IAM descriptions are immutable; retain historical metadata for in-place rollout.
  description = "Runner scoped policy — infrastructure (EC2, EKS, IAM, KMS, CloudTrail, EventBridge, CodeBuild, ECR, ELB)"
  name        = "${var.project_name}-runner-base"
  policy      = jsonencode({ Version = "2012-10-17", Statement = module.runtime_policy.grants })
}
resource "aws_iam_policy" "runner_services" {
  # IAM descriptions are immutable; retain historical metadata for in-place rollout.
  description = "Runner scoped policy — application deploy (S3, Secrets, SSM, CloudFront, Lambda, SQS, DynamoDB, Logs, WAF, STS, Bedrock, CloudWatch)"
  name        = "${var.project_name}-runner-services"
  policy      = jsonencode({ Version = "2012-10-17", Statement = module.runtime_policy.grants })
}
resource "aws_iam_role_policy_attachment" "runner_base" {
  role       = aws_iam_role.runner.name
  policy_arn = aws_iam_policy.runner_base.arn
}
resource "aws_iam_role_policy_attachment" "runner_services" {
  role       = aws_iam_role.runner.name
  policy_arn = aws_iam_policy.runner_services.arn
}
module "runtime_policy" {
  environment               = var.environment
  gateway_execution_arns    = var.gateway_execution_arns
  source                    = "../../infra/modules/runner-runtime-policy"
  account_id                = data.aws_caller_identity.current.account_id
  aws_region                = var.aws_region
  name_prefix               = "adp-${var.environment}"
  transport_secret_arns     = var.transport_secret_arns
  transport_secret_kms_arns = var.transport_secret_kms_arns
}
