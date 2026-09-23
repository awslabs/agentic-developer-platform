# Exact operator-approved database identities for retained diagnostic workflows.
# A username is configuration; the workflow never fetches a master password.
variable "deployment_db_user_arns" {
  type    = list(string)
  default = []
  validation {
    condition     = alltrue([for arn in var.deployment_db_user_arns : can(regex("^arn:aws:rds-db:[a-z0-9-]+:[0-9]{12}:dbuser:[A-Za-z0-9-]+/[A-Za-z0-9_]+$", arn))])
    error_message = "Use exact database-resource/user ARNs without wildcards."
  }
}
resource "aws_iam_role_policy" "database_diagnostics" {
  count = length(var.deployment_db_user_arns) == 0 ? 0 : 1
  name  = "reviewed-database-diagnostics"
  role  = aws_iam_role.deployment.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["rds-db:connect"], Resource = var.deployment_db_user_arns
  }] })
}
resource "aws_iam_role_policy" "context_artifact_diagnostics" {
  name = "existing-context-artifact-diagnostics"
  role = aws_iam_role.deployment.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = ["s3:ListBucket"],
    Resource  = "arn:aws:s3:::agent-context-platform-data-${data.aws_caller_identity.current.account_id}",
    Condition = { StringLike = { "s3:prefix" = ["content/*"] } }
  }] })
}

# Domain clusters and CAPE are optional, independently inventoried rollout targets.
variable "additional_cluster_names" {
  type    = set(string)
  default = []
  validation {
    condition     = alltrue([for name in var.additional_cluster_names : can(regex("^adp-[A-Za-z0-9_-]+$", name))])
    error_message = "Supply exact ADP domain cluster names without wildcards."
  }
}
resource "aws_eks_access_entry" "domain_deployment" {
  for_each          = setsubtract(var.additional_cluster_names, toset([var.cluster_name]))
  cluster_name      = each.value
  principal_arn     = aws_iam_role.deployment.arn
  kubernetes_groups = ["adp:trusted-deployment"]
  type              = "STANDARD"
}
resource "aws_eks_access_policy_association" "domain_deployment" {
  for_each      = aws_eks_access_entry.domain_deployment
  cluster_name  = each.key
  principal_arn = aws_iam_role.deployment.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
}

variable "cape_instance_ids" {
  type    = set(string)
  default = []
  validation {
    condition     = alltrue([for id in var.cape_instance_ids : can(regex("^i-[0-9a-f]{8,17}$", id))])
    error_message = "Supply the exact existing CAPE instance IDs."
  }
}
resource "aws_iam_role_policy" "cape_registration" {
  count = length(var.cape_instance_ids) == 0 ? 0 : 1
  name  = "reviewed-cape-image-registration"
  role  = aws_iam_role.deployment.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Effect = "Allow", Action = ["ssm:SendCommand"],
      Resource = concat(
        ["arn:aws:ssm:${var.aws_region}::document/AWS-RunShellScript"],
        [for id in var.cape_instance_ids : "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/${id}"]
      )
    },
    { Effect = "Allow", Action = ["ssm:GetCommandInvocation"], Resource = "*" },
  ] })
}
