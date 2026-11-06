resource "aws_cloudwatch_log_group" "repair" {
  name              = "/aws/lambda/${local.name_prefix}-repair"
  retention_in_days = 30
}

resource "aws_lambda_function" "repair" {
  function_name     = "${local.name_prefix}-repair"
  runtime           = "python3.12"
  handler           = "l5_services.workers.recovery.lambda_handler"
  role              = aws_iam_role.repair.arn
  timeout           = 25
  memory_size       = 512
  s3_bucket         = var.worker_artifact_bucket
  s3_key            = var.worker_artifact_key
  s3_object_version = var.worker_artifact_version
  source_code_hash  = var.worker_artifact_sha256

  environment {
    variables = merge(var.worker_environment, {
      L5_SERVICE_ID      = var.service_id
      L5_WORKER_FACTORY  = var.worker_factory
      L5_WORK_QUEUE_URL  = aws_sqs_queue.work.url
      L5_RDS_CONNECT_ARN = var.rds_connect_arn
    })
  }

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.worker.id]
  }

  lifecycle {
    ignore_changes = [s3_bucket, s3_key, s3_object_version, source_code_hash]
  }

  depends_on = [aws_cloudwatch_log_group.repair, aws_iam_role_policy.repair]
}

resource "aws_cloudwatch_event_rule" "repair" {
  name                = "${local.name_prefix}-repair"
  schedule_expression = "rate(1 minute)"
  state               = "DISABLED"

  lifecycle {
    ignore_changes = [state]
  }
}

resource "aws_cloudwatch_event_target" "repair" {
  rule      = aws_cloudwatch_event_rule.repair.name
  target_id = "l5-repair"
  arn       = aws_lambda_function.repair.arn
}

resource "aws_lambda_permission" "repair_eventbridge" {
  statement_id  = "AllowEventBridgeRepair"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.repair.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.repair.arn
}
