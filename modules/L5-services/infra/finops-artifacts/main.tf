data "aws_partition" "current" {}

locals {
  name         = "adp-${var.environment}-l5-finops-executor"
  executor_arn = "arn:${data.aws_partition.current.partition}:lambda:${var.aws_region}:${var.aws_account_id}:function:${local.name}"
}

resource "aws_ecr_repository" "executor" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_repository_policy" "executor" {
  repository = aws_ecr_repository.executor.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "LambdaPullSelectedFinOpsExecutor"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.aws_account_id }
        ArnEquals    = { "aws:SourceArn" = local.executor_arn }
      }
    }]
  })
}

output "repository_name" { value = aws_ecr_repository.executor.name }
output "repository_arn" { value = aws_ecr_repository.executor.arn }
output "repository_url" { value = aws_ecr_repository.executor.repository_url }
