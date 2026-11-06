data "archive_file" "interceptor_placeholder" {
  count = var.enable_interceptor ? 1 : 0
  type  = "zip"
  source {
    content  = file("${path.module}/../../interceptor/bootstrap_placeholder.py")
    filename = "tools_interceptor/handler.py"
  }
  source {
    content  = ""
    filename = "tools_interceptor/__init__.py"
  }
  output_path = "${path.module}/interceptor-placeholder.zip"
}

resource "aws_iam_role" "interceptor" {
  count = var.enable_interceptor ? 1 : 0
  name  = "adp-tools-${var.environment}-interceptor"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "interceptor" {
  count             = var.enable_interceptor ? 1 : 0
  name              = "/aws/lambda/adp-tools-${var.environment}-interceptor"
  retention_in_days = 30
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_iam_role_policy" "interceptor" {
  count = var.enable_interceptor ? 1 : 0
  name  = "adp-tools-interceptor-runtime"
  role  = aws_iam_role.interceptor[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["kms:Verify", "kms:DescribeKey"]
        Resource = aws_kms_key.assertion[0].arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.interceptor[0].arn}:*"
      }
    ]
  })
}

resource "aws_lambda_function" "interceptor" {
  count                          = var.enable_interceptor ? 1 : 0
  function_name                  = "adp-tools-${var.environment}-interceptor"
  role                           = aws_iam_role.interceptor[0].arn
  runtime                        = "python3.12"
  handler                        = "tools_interceptor.handler.handler"
  filename                       = data.archive_file.interceptor_placeholder[0].output_path
  source_code_hash               = data.archive_file.interceptor_placeholder[0].output_base64sha256
  publish                        = false
  timeout                        = 10
  reserved_concurrent_executions = var.interceptor_reserved_concurrent_executions
  environment {
    variables = {
      ADP_TOOLS_FACADE_ROLE_ARN   = var.tools_facade_role_arn
      ADP_TOOLS_ASSERTION_KEY_ARN = aws_kms_key.assertion[0].arn
    }
  }
  depends_on = [aws_iam_role_policy.interceptor, aws_cloudwatch_log_group.interceptor]
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [filename, source_code_hash]
  }
}

locals {
  interceptor_verification_role_arn = var.enable_interceptor ? aws_iam_role.interceptor[0].arn : var.interceptor_execution_role_arn
}
