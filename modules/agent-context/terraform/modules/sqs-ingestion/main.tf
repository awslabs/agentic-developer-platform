# SQS Ingestion Queue + Dead Letter Queue
# Used by the parallel ingestion pipeline (KEDA ScaledJob workers)

resource "aws_sqs_queue" "ingestion_dlq" {
  name                      = "${var.cluster_name}-context-ingestion-dlq"
  message_retention_seconds = 1209600 # 14 days
  receive_wait_time_seconds = 0
  sqs_managed_sse_enabled   = true

  tags = merge(var.tags, {
    Component = "ingestion-dlq"
  })
}

resource "aws_sqs_queue" "ingestion" {
  name                       = "${var.cluster_name}-context-ingestion"
  visibility_timeout_seconds = var.visibility_timeout
  message_retention_seconds  = var.message_retention_seconds
  receive_wait_time_seconds  = var.receive_wait_time_seconds
  delay_seconds              = 0
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ingestion_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = merge(var.tags, {
    Component = "ingestion-queue"
  })
}

# IAM policy for publishing and consuming messages
data "aws_caller_identity" "current" {}

resource "aws_iam_policy" "sqs_publish" {
  name        = "${var.cluster_name}-context-ingestion-publish"
  description = "Allow publishing to the ingestion SQS queue"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:GetQueueUrl",
          "sqs:GetQueueAttributes",
        ]
        Resource = aws_sqs_queue.ingestion.arn
      }
    ]
  })
}

resource "aws_iam_policy" "sqs_consume" {
  name        = "${var.cluster_name}-context-ingestion-consume"
  description = "Allow consuming from the ingestion SQS queue"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueUrl",
          "sqs:GetQueueAttributes",
          "sqs:ChangeMessageVisibility",
        ]
        Resource = aws_sqs_queue.ingestion.arn
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:GetQueueUrl",
          "sqs:GetQueueAttributes",
        ]
        Resource = aws_sqs_queue.ingestion_dlq.arn
      }
    ]
  })
}

# Attach policies to the existing IRSA role used by the platform
resource "aws_iam_role_policy_attachment" "sqs_publish" {
  count      = var.irsa_role_name != "" ? 1 : 0
  role       = var.irsa_role_name
  policy_arn = aws_iam_policy.sqs_publish.arn
}

resource "aws_iam_role_policy_attachment" "sqs_consume" {
  count      = var.irsa_role_name != "" ? 1 : 0
  role       = var.irsa_role_name
  policy_arn = aws_iam_policy.sqs_consume.arn
}
