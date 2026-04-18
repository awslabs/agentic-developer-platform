output "queue_url" {
  description = "URL of the ingestion SQS queue"
  value       = aws_sqs_queue.ingestion.url
}

output "queue_arn" {
  description = "ARN of the ingestion SQS queue"
  value       = aws_sqs_queue.ingestion.arn
}

output "queue_name" {
  description = "Name of the ingestion SQS queue"
  value       = aws_sqs_queue.ingestion.name
}

output "dlq_url" {
  description = "URL of the dead letter queue"
  value       = aws_sqs_queue.ingestion_dlq.url
}

output "dlq_arn" {
  description = "ARN of the dead letter queue"
  value       = aws_sqs_queue.ingestion_dlq.arn
}

output "publish_policy_arn" {
  description = "ARN of the IAM policy for publishing messages"
  value       = aws_iam_policy.sqs_publish.arn
}

output "consume_policy_arn" {
  description = "ARN of the IAM policy for consuming messages"
  value       = aws_iam_policy.sqs_consume.arn
}
