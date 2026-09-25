locals {
  active        = var.enabled ? 1 : 0
  route_arn     = "${var.api_execution_arn}/${var.stage_name}/POST/tools/cyber"
  function_name = var.name
}
resource "aws_security_group" "service" {
  count       = var.enabled && var.vpc_id != "" ? 1 : 0
  name        = "${var.name}-lambda"
  description = "Cyber tools Lambda: no ingress; HTTPS to authorized APIs and backends"
  vpc_id      = var.vpc_id
  ingress     = []
  egress {
    description = "HTTPS for platform authority, AWS APIs and configured cyber endpoints"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_dynamodb_table" "operations" {
  count        = local.active
  name         = "${var.name}-operations"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"
  range_key    = "arrived_at"
  attribute {
    name = "event_id"
    type = "S"
  }
  attribute {
    name = "arrived_at"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
  deletion_protection_enabled = true
}
resource "aws_cloudwatch_log_group" "service" {
  count             = local.active
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 30
}
resource "aws_iam_role" "service" {
  count = local.active
  name  = "${var.name}-lambda"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" }
  }] })
}
resource "aws_iam_role_policy" "service" {
  count = local.active
  role  = aws_iam_role.service[0].id
  name  = "cyber-tools-scoped-access"
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.service[0].arn}:*" },
    { Effect = "Allow", Action = ["execute-api:Invoke"], Resource = sort(tolist(var.authority_invoke_arns)) },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem"],
    Resource = aws_dynamodb_table.operations[0].arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"],
    Resource = "${var.sample_bucket_arn}/o/*/t/task-service/u/sp-*/s/*/*/in/*" }
    ],
    length(var.queue_arns) == 0 ? [] : [{ Effect = "Allow", Action = ["sqs:SendMessage"], Resource = sort(tolist(var.queue_arns)) }],
    var.results_table_arn == "" ? [] : [{ Effect = "Allow", Action = ["dynamodb:Query"], Resource = var.results_table_arn }],
    length(var.secret_arns) == 0 ? [] : [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = sort(tolist(var.secret_arns)) }],
    length(var.kms_key_arns) == 0 ? [] : [{ Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = sort(tolist(var.kms_key_arns)) }],
    length(var.subnet_ids) == 0 ? [] : [{ Effect = "Allow", Action = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"], Resource = "*" }]
  ) })
}
resource "aws_lambda_function" "service" {
  count                          = local.active
  function_name                  = local.function_name
  role                           = aws_iam_role.service[0].arn
  package_type                   = "Image"
  image_uri                      = var.image_uri
  architectures                  = ["x86_64"]
  timeout                        = 28
  memory_size                    = 1024
  reserved_concurrent_executions = 10
  environment {
    variables = merge(var.backend_environment, {
      ADP_TASK_CYBER_ENABLED      = tostring(var.capability_enabled)
      CYBER_TOOLS_TABLE           = aws_dynamodb_table.operations[0].name
      CYBER_TOOLS_WORKER_ROLES    = join(",", sort(tolist(var.worker_role_arns)))
      ADP_TASK_AUTHORITY_ENDPOINT = var.authority_endpoint
      CYBER_SAMPLE_BUCKET         = trimprefix(var.sample_bucket_arn, "arn:aws:s3:::")
    })
  }
  dynamic "vpc_config" {
    for_each = length(var.subnet_ids) == 0 ? [] : [1]
    content {
      subnet_ids         = var.subnet_ids
      security_group_ids = concat(tolist(var.security_group_ids), aws_security_group.service[*].id)
    }
  }
  lifecycle {
    precondition {
      condition     = length(var.endpoint_security_group_ids) == 0 || var.vpc_id != ""
      error_message = "Endpoint ingress rules require the service-owned Lambda security group via vpc_id."
    }
    precondition {
      condition     = var.api_execution_arn == "arn:aws:execute-api:${var.aws_region}:${var.aws_account_id}:${var.rest_api_id}" && alltrue([for arn in var.worker_role_arns : startswith(arn, "arn:aws:iam::${var.aws_account_id}:role/")])
      error_message = "The shared API and attached worker roles must belong to the confirmed target account."
    }
    precondition {
      condition = var.authority_invoke_arns == toset([
        "${var.api_execution_arn}/${var.stage_name}/POST/internal/v1/agent/task/tool-authorize",
        "${var.api_execution_arn}/${var.stage_name}/POST/internal/v1/agent/task/artifact"
      ])
      error_message = "Authorize exactly both generic Task endpoints on this API stage."
    }
    precondition {
      condition     = var.image_uri != "" && var.rest_api_id != "" && var.tools_parent_resource_id != "" && var.stage_name != "" && var.api_execution_arn != "" && var.authority_endpoint != "" && length(var.authority_invoke_arns) == 2 && length(var.worker_role_arns) > 0 && var.sample_bucket_arn != ""
      error_message = "Activation requires immutable image, existing API/stage/tools parent, both authority routes, worker roles and sample bucket."
    }
    precondition {
      condition     = (length(var.subnet_ids) == 0) == (length(var.security_group_ids) == 0 && var.vpc_id == "")
      error_message = "Configure subnets with existing security groups or a service-owned group via vpc_id, or neither."
    }
  }
  depends_on = [aws_iam_role_policy.service, aws_cloudwatch_log_group.service]
}
resource "aws_api_gateway_resource" "cyber" {
  count       = local.active
  rest_api_id = var.rest_api_id
  parent_id   = var.tools_parent_resource_id
  path_part   = "cyber"
}
resource "aws_api_gateway_method" "cyber" {
  count         = local.active
  rest_api_id   = var.rest_api_id
  resource_id   = aws_api_gateway_resource.cyber[0].id
  http_method   = "POST"
  authorization = "AWS_IAM"
}
resource "aws_api_gateway_integration" "cyber" {
  count                   = local.active
  rest_api_id             = var.rest_api_id
  resource_id             = aws_api_gateway_resource.cyber[0].id
  http_method             = aws_api_gateway_method.cyber[0].http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.service[0].invoke_arn
  timeout_milliseconds    = 29000
}
resource "aws_lambda_permission" "api" {
  count          = local.active
  statement_id   = "SharedApiCyberRoute"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.service[0].function_name
  principal      = "apigateway.amazonaws.com"
  source_arn     = local.route_arn
  source_account = var.aws_account_id
}
resource "aws_iam_role_policy" "worker" {
  for_each = var.enabled ? var.worker_role_arns : toset([])
  name     = "${var.name}-invoke"
  role     = element(reverse(split("/", each.value)), 0)
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "execute-api:Invoke", Resource = local.route_arn
  }] })
}
resource "aws_vpc_security_group_ingress_rule" "endpoints" {
  for_each                     = var.enabled && var.vpc_id != "" ? var.endpoint_security_group_ids : toset([])
  security_group_id            = each.value
  referenced_security_group_id = aws_security_group.service[0].id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "HTTPS from cyber tools Lambda to private AWS endpoints"
}
