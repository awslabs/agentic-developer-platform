# Existing chat workload release only; not an infrastructure administrator.
variable "enable_chat_deployment" {
  type    = bool
  default = false
}
resource "aws_iam_role" "chat_deployment" {
  count = var.enable_chat_deployment ? 1 : 0
  name  = "${var.name_prefix}-chat-trusted-deployment"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-chat-deploy-${var.environment}"
    } }
  }] })
}
resource "aws_iam_role_policy" "chat_deployment" {
  lifecycle {
    precondition {
      condition     = length(var.runtime_check_model_ids) > 0
      error_message = "Chat deployment requires the exact runtime model inventory."
    }
  }
  count = var.enable_chat_deployment ? 1 : 0
  name  = "publish-existing-chat-workload"
  role  = aws_iam_role.chat_deployment[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["codebuild:StartBuild", "codebuild:StopBuild", "codebuild:BatchGetBuilds", "codebuild:BatchGetProjects"], Resource = [
      "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/adp-${var.environment}-chat-agent",
      "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:build/adp-${var.environment}-chat-agent:*"
    ] },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}/codebuild/src/adp-${var.environment}-chat-agent/*" },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}/${var.environment}/modules/agent-factory/terraform.tfstate" },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}" },
    { Effect = "Allow", Action = ["eks:DescribeCluster"], Resource = "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}" },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/adp/${var.environment}/gateway/apigw-invoke-url" },
    { Effect = "Allow", Action = ["ecr:DescribeImages", "ecr:BatchGetImage"], Resource = "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/adp-chat-agent" },
    { Effect = "Allow", Action = ["bedrock:GetFoundationModelAvailability"], Resource = "*" },
    { Effect = "Allow", Action = ["bedrock:GetInferenceProfile", "bedrock:InvokeModel"], Resource = [for model in var.runtime_check_model_ids : "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${model}"] },
    { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = [for model in var.runtime_check_model_ids : "arn:aws:bedrock:*::foundation-model/${trimprefix(model, "global.")}"] }
  ] })
}
resource "aws_eks_access_entry" "chat_deployment" {
  count         = var.enable_chat_deployment ? 1 : 0
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.chat_deployment[0].arn
  type          = "STANDARD"
}
resource "aws_eks_access_policy_association" "chat_deployment" {
  count         = var.enable_chat_deployment ? 1 : 0
  cluster_name  = var.cluster_name
  principal_arn = aws_eks_access_entry.chat_deployment[0].principal_arn
  # EKS Admin excludes custom-resource APIs. The wildcard policy is confined
  # by this association's namespace scope, including KEDA release resources.
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type       = "namespace"
    namespaces = ["adp-gateway-agents"]
  }
}
