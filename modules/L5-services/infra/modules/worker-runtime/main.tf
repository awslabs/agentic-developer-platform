resource "aws_security_group" "worker" {
  name   = "${local.name_prefix}-worker"
  vpc_id = var.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "database" {
  security_group_id            = aws_security_group.worker.id
  referenced_security_group_id = var.db_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = var.db_port
  to_port                      = var.db_port
}

resource "aws_vpc_security_group_egress_rule" "endpoints" {
  security_group_id            = aws_security_group.worker.id
  referenced_security_group_id = var.endpoint_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_egress_rule" "gateway_api" {
  security_group_id = aws_security_group.worker.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "Regional API Gateway uses NAT; execute-api IAM limits internal routes"
}

resource "aws_security_group_rule" "database_from_worker" {
  security_group_id        = var.db_security_group_id
  source_security_group_id = aws_security_group.worker.id
  description              = "L5 worker to gateway database"
  type                     = "ingress"
  protocol                 = "tcp"
  from_port                = var.db_port
  to_port                  = var.db_port
}

resource "aws_security_group_rule" "endpoints_from_worker" {
  security_group_id        = var.endpoint_security_group_id
  source_security_group_id = aws_security_group.worker.id
  description              = "L5 worker to private AWS endpoints"
  type                     = "ingress"
  protocol                 = "tcp"
  from_port                = 443
  to_port                  = 443
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${local.name_prefix}-worker"
  retention_in_days = 30
}

resource "aws_lambda_function" "worker" {
  function_name     = "${local.name_prefix}-worker"
  runtime           = "python3.12"
  handler           = "l5_services.workers.execution.lambda_handler"
  role              = aws_iam_role.worker.arn
  timeout           = var.service_id == "security" ? 900 : (var.service_id == "devops" ? 120 : 25)
  memory_size       = var.service_id == "security" ? 1536 : 512
  s3_bucket         = var.worker_artifact_bucket
  s3_key            = var.worker_artifact_key
  s3_object_version = var.worker_artifact_version
  source_code_hash  = var.worker_artifact_sha256

  ephemeral_storage {
    size = var.service_id == "security" ? 4096 : 512
  }

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

  depends_on = [aws_cloudwatch_log_group.worker, aws_iam_role_policy.worker]
}

# A run intentionally sends its next durable poll back to this queue. Lambda's
# default recursion cutoff would stop a healthy assessment after ~16 steps.
# SQL deadlines, poll limits, dispatch limits and fenced claims bound this loop.
resource "aws_lambda_function_recursion_config" "worker" {
  function_name  = aws_lambda_function.worker.function_name
  recursive_loop = "Allow"
}
