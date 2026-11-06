data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  executor_name = "adp-${var.environment}-l5-finops-executor"
  executor_arn  = "arn:${data.aws_partition.current.partition}:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${local.executor_name}"
}

resource "aws_cloudwatch_log_group" "executor" {
  name              = "/aws/lambda/${local.executor_name}"
  retention_in_days = 30
}

resource "aws_iam_role_policy" "executor" {
  role = module.runtime.worker_role_name
  name = "${local.executor_name}-private"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = local.executor_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:${aws_cloudwatch_log_group.executor.name}:*"
      }
    ]
  })
}

resource "aws_lambda_function" "executor" {
  function_name = local.executor_name
  package_type  = "Image"
  image_uri     = var.executor_image_uri
  role          = module.runtime.worker_role_arn
  timeout       = 90
  memory_size   = 2048

  environment {
    variables = merge(var.worker_environment, {
      L5_SERVICE_ID     = "finops"
      L5_FINOPS_SURFACE = var.executor_surface
    })
  }

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [module.runtime.worker_security_group_id]
  }

  depends_on = [aws_cloudwatch_log_group.executor, aws_iam_role_policy.executor]
}
