# Release an existing worker fleet, without whole-module Terraform permissions.
variable "enable_worker_deployment" {
  type    = bool
  default = false
}
resource "aws_iam_role" "worker_deployment" {
  count = var.enable_worker_deployment ? 1 : 0
  name  = "${var.name_prefix}-worker-trusted-deployment"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-worker-deploy-${var.environment}"
    } }
  }] })
}
data "aws_kms_alias" "worker_parameter" {
  count = var.enable_worker_deployment ? 1 : 0
  name  = "alias/${var.name_prefix}-webhook-dynamodb"
}
resource "aws_iam_role_policy" "worker_deployment" {
  count = var.enable_worker_deployment ? 1 : 0
  name  = "rollout-existing-worker"
  role  = aws_iam_role.worker_deployment[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "eks:DescribeCluster", Resource = "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}" },
    { Effect = "Allow", Action = "ssm:DescribeParameters", Resource = "*" },
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"], Resource = data.aws_kms_alias.worker_parameter[0].target_key_arn,
      Condition = { StringEquals = {
        "kms:ViaService"                      = "ssm.${var.aws_region}.amazonaws.com",
        "kms:EncryptionContext:PARAMETER_ARN" = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gateway/agent-authority-worker-images"
      } }
    },
    { Effect = "Allow", Action = "ecr:DescribeImages", Resource = "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/adp-agent-runtime" },
    { Effect = "Allow", Action = ["ssm:GetParameter", "ssm:GetParameters", "ssm:PutParameter"], Resource = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gateway/agent-authority-worker-images",
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/webhook-ingress/deployed-worker-image"
    ] }
  ] })
}
resource "aws_eks_access_entry" "worker_deployment" {
  count             = var.enable_worker_deployment ? 1 : 0
  cluster_name      = var.cluster_name
  principal_arn     = aws_iam_role.worker_deployment[0].arn
  kubernetes_groups = ["adp:worker-release"]
  type              = "STANDARD"
}
# Kubernetes permissions are installed from worker-release-rbac.yaml by the
# operator alongside this opt-in role. There is no cluster-admin association.
output "worker_deployment_role_arn" {
  value = var.enable_worker_deployment ? aws_iam_role.worker_deployment[0].arn : null
}
