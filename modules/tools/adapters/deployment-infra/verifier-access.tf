# The controller's registered environment connection is independent of the
# protected publisher. It reads the exact immutable image after an interrupted
# publication, and describes the reviewed cluster for runtime observation.
variable "runtime_verifier_role_arn" {
  type        = string
  description = "Existing registered environment connection role, already mapped to runtime_verifier_group in the reviewed EKS cluster."
  validation {
    condition = (
      can(regex("^arn:aws:iam::${var.aws_account_id}:role/[A-Za-z0-9_+=,.@/-]+$", var.runtime_verifier_role_arn)) &&
      var.runtime_verifier_role_arn != "arn:aws:iam::${var.aws_account_id}:role/${var.runtime_role_name}"
    )
    error_message = "Select an exact existing same-account verifier role, distinct from the adapter pod role."
  }
}

data "aws_iam_role" "runtime_verifier" {
  name = element(reverse(split("/", var.runtime_verifier_role_arn)), 0)
  lifecycle {
    postcondition {
      condition     = self.arn == var.runtime_verifier_role_arn
      error_message = "The existing verifier role ARN must match the reviewed connection, including its IAM path."
    }
  }
  depends_on = [data.aws_eks_cluster.target]
}

# Read the existing mapping rather than trying to adopt or replace an access
# entry owned by environment registration or another Terraform state.
data "aws_eks_access_entry" "runtime_verifier" {
  cluster_name  = var.cluster_name
  principal_arn = data.aws_iam_role.runtime_verifier.arn
  lifecycle {
    postcondition {
      condition     = self.type == "STANDARD" && contains(self.kubernetes_groups, var.runtime_verifier_group)
      error_message = "The registered verifier must already map to the reviewed runtime-verifier group in this cluster."
    }
  }
}

resource "aws_iam_role_policy" "runtime_verifier" {
  name = "adp-tools-${var.environment}-adapters-verifier"
  role = data.aws_iam_role.runtime_verifier.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Sid      = "ReadPinnedAdapterImage"
      Effect   = "Allow"
      Action   = ["ecr:DescribeRepositories", "ecr:BatchGetImage"]
      Resource = module.publication.repository_arn
    },
    {
      Sid      = "DescribeReviewedAdapterCluster"
      Effect   = "Allow"
      Action   = "eks:DescribeCluster"
      Resource = data.aws_eks_cluster.target.arn
    },
    {
      Sid      = "ReadAdapterBuildRecovery"
      Effect   = "Allow"
      Action   = ["codebuild:ListBuildsForProject", "codebuild:BatchGetBuilds"]
      Resource = "arn:aws:codebuild:${var.aws_region}:${var.aws_account_id}:project/adp-tools-${var.environment}-adapters-build"
    },
  ] })
  depends_on = [data.aws_eks_access_entry.runtime_verifier]
}

output "runtime_verifier_role_arn" { value = data.aws_iam_role.runtime_verifier.arn }
