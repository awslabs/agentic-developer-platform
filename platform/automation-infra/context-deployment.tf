# Release existing agent-context services; cluster setup is platform-owned.
variable "enable_context_deployment" {
  type    = bool
  default = false
}
data "aws_secretsmanager_secret" "context_callback" {
  count = var.enable_context_deployment ? 1 : 0
  name  = "adp/${var.environment}/gateway/internal-api-key"
}
resource "aws_iam_role" "context_deployment" {
  count = var.enable_context_deployment ? 1 : 0
  name  = "${var.name_prefix}-context-trusted-deployment"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-context-deploy-${var.environment}"
    } }
  }] })
}
resource "aws_iam_role_policy" "context_deployment" {
  count = var.enable_context_deployment ? 1 : 0
  name  = "release-existing-context-services"
  role  = aws_iam_role.context_deployment[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["eks:DescribeCluster"], Resource = "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}" },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [for path in [
      "agent-context/ingestion-queue-url", "agent-context/neptune-endpoint", "agent-context/neptune-port", "agent-context/neptune-bulk-load-role-arn", "rds/endpoint"
    ] : "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/${path}"] },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = data.aws_secretsmanager_secret.context_callback[0].arn },
    { Effect = "Allow", Action = ["ecr:DescribeImages"], Resource = [for image in [
      "ingestion", "context-mcp", "codegraph-context", "litellm-proxy", "deepwiki"
    ] : "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/adp-${var.environment}-agent-context-${image}"] }
  ] })
}
resource "aws_eks_access_entry" "context_deployment" {
  count         = var.enable_context_deployment ? 1 : 0
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.context_deployment[0].arn
  type          = "STANDARD"
}
resource "aws_eks_access_policy_association" "context_deployment" {
  count         = var.enable_context_deployment ? 1 : 0
  cluster_name  = var.cluster_name
  principal_arn = aws_eks_access_entry.context_deployment[0].principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type       = "namespace"
    namespaces = ["agent-context"]
  }
}
