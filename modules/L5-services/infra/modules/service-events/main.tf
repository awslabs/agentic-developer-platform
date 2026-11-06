terraform {
  required_providers { aws = { source = "hashicorp/aws", version = "~> 6.0" } }
}
variable "name_prefix" { type = string }
variable "functions" {
  type = map(object({ handler = string, schedule = optional(string), event_pattern = optional(string) }))
}
variable "permissions_boundary_arn" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "security_group_id" { type = string }
variable "worker_environment" { type = map(string) }
variable "worker_artifact_bucket" { type = string }
variable "worker_artifact_key" { type = string }
variable "worker_artifact_version" { type = string }
variable "worker_artifact_sha256" { type = string }
variable "gateway_api_arn" { type = string }
variable "rds_connect_arn" { type = string }
variable "work_queue_arn" { type = string }
data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
locals {
  network_actions = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"]
  function_prefix = "arn:${data.aws_partition.current.partition}:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${var.name_prefix}"
}
resource "aws_cloudwatch_log_group" "function" {
  for_each          = var.functions
  name              = "/aws/lambda/${var.name_prefix}-${each.key}"
  retention_in_days = 30
}
resource "aws_iam_role" "function" {
  for_each             = var.functions
  name                 = "${var.name_prefix}-${each.key}"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }] })
}
resource "aws_iam_role_policy" "function" {
  for_each = var.functions
  name     = aws_iam_role.function[each.key].name
  role     = aws_iam_role.function[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.function[each.key].arn}:*" },
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = var.work_queue_arn },
      { Effect = "Allow", Action = ["rds-db:connect"], Resource = var.rds_connect_arn },
      { Effect = "Allow", Action = ["execute-api:Invoke"], Resource = var.gateway_api_arn },
      { Effect = "Allow", Action = local.network_actions, Resource = "*" },
      { Effect = "Deny", Action = local.network_actions, Resource = "*", Condition = { ArnEquals = { "lambda:SourceFunctionArn" = "${local.function_prefix}-${each.key}" } } }
    ]
  })
}
resource "aws_lambda_function" "event" {
  for_each          = var.functions
  function_name     = "${var.name_prefix}-${each.key}"
  role              = aws_iam_role.function[each.key].arn
  runtime           = "python3.12"
  handler           = each.value.handler
  timeout           = 25
  memory_size       = 512
  s3_bucket         = var.worker_artifact_bucket
  s3_key            = var.worker_artifact_key
  s3_object_version = var.worker_artifact_version
  source_code_hash  = var.worker_artifact_sha256
  environment { variables = merge(var.worker_environment, { L5_RDS_CONNECT_ARN = var.rds_connect_arn }) }
  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [var.security_group_id]
  }
  lifecycle { ignore_changes = [s3_bucket, s3_key, s3_object_version, source_code_hash] }
  depends_on = [aws_cloudwatch_log_group.function, aws_iam_role_policy.function]
}
resource "aws_cloudwatch_event_rule" "event" {
  for_each            = var.functions
  name                = "${var.name_prefix}-${each.key}"
  schedule_expression = each.value.schedule
  event_pattern       = each.value.event_pattern
  state               = "DISABLED"
  lifecycle { ignore_changes = [state] }
}
resource "aws_cloudwatch_event_target" "event" {
  for_each  = var.functions
  rule      = aws_cloudwatch_event_rule.event[each.key].name
  target_id = each.key
  arn       = aws_lambda_function.event[each.key].arn
}
resource "aws_lambda_permission" "event" {
  for_each      = var.functions
  statement_id  = "AllowScopedEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.event[each.key].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.event[each.key].arn
}
output "function_names" { value = { for name, fn in aws_lambda_function.event : name => fn.function_name } }

variable "agent_registry_table_name" { type = string }
resource "aws_dynamodb_table_item" "function" {
  for_each   = var.functions
  table_name = var.agent_registry_table_name
  hash_key   = "agent_id"
  item = jsonencode({
    agent_id          = { S = aws_iam_role.function[each.key].name }
    role_arn          = { S = aws_iam_role.function[each.key].arn }
    iam_role_id       = { S = aws_iam_role.function[each.key].unique_id }
    agent_name        = { S = aws_iam_role.function[each.key].name }
    org_id            = { S = "__platform__" }
    team_id           = { S = "__agents__" }
    owner             = { S = "platform" }
    scope             = { S = "internal" }
    credential_scopes = { SS = ["l5:credential-session"] }
    status            = { S = "active" }
  })
}
